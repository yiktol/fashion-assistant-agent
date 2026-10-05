"""Stability Stable Image tools: generate_image, inpaint, outpaint.

Isolates every ``[DOC]`` Stability field name/enum of the ``invoke_model`` body
(the JSON inside ``body`` is UNVERIFIED; NFR-5 forbids invoking the model). A
correction touches this module and its test fixtures only.

Pinned rules:
- ``output_format`` is always ``"png"`` -> every uploaded key ends ``.png`` and
  the key is never derived from the source filename extension.
- LOCKED FIX 4: the text-to-image body OMITS ``aspect_ratio`` (service default
  applies) and omits ``seed`` (so same-prompt generations vary).
- generate_image invokes the image-region client with the bare core model id;
  inpaint/outpaint invoke the primary-region client with the ``us.``-prefixed
  inference-profile ids (Finding 3b).
"""

from __future__ import annotations

import base64
import json
import logging
import uuid

from botocore.exceptions import ClientError
from strands import tool

from . import s3_io
from ._result import err, ok

logger = logging.getLogger(__name__)

# Pinned output format across all three image tools.
_OUTPUT_FORMAT = "png"
_OUTPUT_PREFIX = "OutputImages"


def _decode_image(response) -> bytes:
    """Decode ``images[0]`` from a Stability response, raising on content filter."""
    payload = json.loads(response["body"].read())
    finish_reasons = payload.get("finish_reasons") or [None]
    # A non-null finish_reasons[0] signals filtered/failed content.
    if finish_reasons[0] is not None:
        raise ValueError(f"content filtered: {finish_reasons[0]}")
    base64_image = payload["images"][0]
    return base64.b64decode(base64_image)


def _source_stem(image_uri: str) -> str:
    _bucket, key = s3_io.parse_s3_uri(image_uri)
    basename = key.rsplit("/", 1)[-1]
    stem = basename.rsplit(".", 1)[0]
    return stem or "image"


# --------------------------------------------------------------------------- #
# generate_image
# --------------------------------------------------------------------------- #
def generate_image_impl(bedrock_image, s3, settings, prompt, weather=None) -> dict:
    """Text-to-image generation (text input only, us-west-2, bare core id)."""
    if not prompt or not prompt.strip():
        return err("a non-empty prompt is required")

    full_prompt = prompt
    if weather:
        full_prompt = f"{prompt}. Make the clothing suitable for {weather} conditions."

    # [DOC] body: prompt + output_format only. LOCKED FIX 4: no aspect_ratio, no seed.
    body = json.dumps({"prompt": full_prompt, "output_format": _OUTPUT_FORMAT})

    try:
        response = bedrock_image.invoke_model(
            modelId=settings.text_to_image_model_id,
            body=body,
            accept="application/json",
            contentType="application/json",
        )
        image_bytes = _decode_image(response)
    except (ClientError, ValueError) as exc:
        logger.error("generate_image failed: %s", exc)
        return err(f"image could not be generated: {exc}")

    key = f"{_OUTPUT_PREFIX}/gen_{uuid.uuid4().hex}.png"
    s3_uri = s3_io.upload_png(s3, settings.image_bucket, key, image_bytes)
    return ok(s3_uri=s3_uri, message="generated")


# --------------------------------------------------------------------------- #
# inpaint
# --------------------------------------------------------------------------- #
def inpaint_impl(
    bedrock_primary,
    s3,
    settings,
    image_uri,
    prompt,
    mask_uri=None,
    negative_prompt=None,
    grow_mask=None,
) -> dict:
    """Inpaint a masked region (us-east-1, us.-prefixed profile id).

    The Stability inpaint model's accepted fields (verified against the live
    endpoint) are: image, prompt, mask, grow_mask, negative_prompt, seed,
    output_format, style_preset. The mask is supplied only as a black/white
    MASK IMAGE -- there is no text-driven region selection on this model.
    """
    if not prompt or not prompt.strip():
        return err("a non-empty prompt is required")
    if not mask_uri:
        return err("inpaint requires a mask image (mask_uri)")

    try:
        source_b64 = s3_io.download_b64(s3, image_uri)
        # Verified Stability inpaint body: image + prompt + mask image.
        body_dict = {
            "prompt": prompt,
            "image": source_b64,
            "mask": s3_io.download_b64(s3, mask_uri),
            "output_format": _OUTPUT_FORMAT,
        }
        if negative_prompt:
            body_dict["negative_prompt"] = negative_prompt
        if grow_mask is not None:
            body_dict["grow_mask"] = grow_mask

        response = bedrock_primary.invoke_model(
            modelId=settings.inpaint_model_id,
            body=json.dumps(body_dict),
            accept="application/json",
            contentType="application/json",
        )
        image_bytes = _decode_image(response)
    except (ClientError, ValueError) as exc:
        logger.error("inpaint failed: %s", exc)
        return err(f"image could not be inpainted: {exc}")

    key = f"{_OUTPUT_PREFIX}/{_source_stem(image_uri)}_{uuid.uuid4().hex}.png"
    s3_uri = s3_io.upload_png(s3, settings.image_bucket, key, image_bytes)
    return ok(s3_uri=s3_uri, message="inpainted")


# --------------------------------------------------------------------------- #
# outpaint
# --------------------------------------------------------------------------- #
def outpaint_impl(
    bedrock_primary, s3, settings, image_uri, prompt, left=0, right=0, up=0, down=0
) -> dict:
    """Outpaint directional extents (us-east-1, us.-prefixed profile id)."""
    if not prompt or not prompt.strip():
        return err("a non-empty prompt is required")
    extents = {"left": left, "right": right, "up": up, "down": down}
    if not any(value > 0 for value in extents.values()):
        return err("outpaint requires at least one extent > 0")

    try:
        source_b64 = s3_io.download_b64(s3, image_uri)
        # [DOC] body: image + prompt + directional pixel extents. Omit zero extents.
        body_dict = {
            "prompt": prompt,
            "image": source_b64,
            "output_format": _OUTPUT_FORMAT,
        }
        for name, value in extents.items():
            if value > 0:
                body_dict[name] = value

        response = bedrock_primary.invoke_model(
            modelId=settings.outpaint_model_id,
            body=json.dumps(body_dict),
            accept="application/json",
            contentType="application/json",
        )
        image_bytes = _decode_image(response)
    except (ClientError, ValueError) as exc:
        logger.error("outpaint failed: %s", exc)
        return err(f"image could not be outpainted: {exc}")

    key = f"{_OUTPUT_PREFIX}/{_source_stem(image_uri)}_{uuid.uuid4().hex}.png"
    s3_uri = s3_io.upload_png(s3, settings.image_bucket, key, image_bytes)
    return ok(s3_uri=s3_uri, message="outpainted")


# --------------------------------------------------------------------------- #
# @tool factories (bind injected clients/settings at agent-build time)
# --------------------------------------------------------------------------- #
def make_generate_image(bedrock_image, s3, settings):
    @tool
    def generate_image(prompt: str, weather: str | None = None) -> dict:
        """Generate a new fashion image from a text description (text-to-image).

        Args:
            prompt: Description of the garment/outfit to generate.
            weather: Optional weather phrase to make the outfit weather-appropriate.

        Returns (json payload):
            {"result": "ok"|"error", "s3_uri": str|None, "message": str}
        """
        return generate_image_impl(bedrock_image, s3, settings, prompt, weather)

    return generate_image


def make_inpaint(bedrock_primary, s3, settings):
    @tool
    def inpaint(
        image_uri: str,
        prompt: str,
        mask_uri: str,
        negative_prompt: str | None = None,
        grow_mask: int | None = None,
    ) -> dict:
        """Edit a masked region of an existing image to a new style (inpaint).

        Requires a black/white mask image: white marks the region to repaint,
        black is kept. There is no text-driven region selection on this model.

        Args:
            image_uri: s3:// URI of the source image.
            prompt: Description of the desired result in the masked region.
            mask_uri: s3:// URI of a black/white mask image (required).
            negative_prompt: Optional text describing what to avoid.
            grow_mask: Optional pixels to grow the mask edge (softens seams).

        Returns (json payload):
            {"result": "ok"|"error", "s3_uri": str|None, "message": str}
        """
        return inpaint_impl(
            bedrock_primary,
            s3,
            settings,
            image_uri,
            prompt,
            mask_uri,
            negative_prompt,
            grow_mask,
        )

    return inpaint


def make_outpaint(bedrock_primary, s3, settings):
    @tool
    def outpaint(
        image_uri: str,
        prompt: str,
        left: int = 0,
        right: int = 0,
        up: int = 0,
        down: int = 0,
    ) -> dict:
        """Extend an image outward in one or more directions (outpaint).

        Args:
            image_uri: s3:// URI of the source image.
            prompt: Description of the surrounding scene to generate.
            left: Pixels to extend left (>= 0).
            right: Pixels to extend right (>= 0).
            up: Pixels to extend up (>= 0).
            down: Pixels to extend down (>= 0).

        Returns (json payload):
            {"result": "ok"|"error", "s3_uri": str|None, "message": str}
        """
        return outpaint_impl(
            bedrock_primary, s3, settings, image_uri, prompt, left, right, up, down
        )

    return outpaint
