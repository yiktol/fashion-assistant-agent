"""Amazon Nova 2 multimodal embedding helper.

Isolates every ``[DOC]`` field name of the Nova 2 ``invoke_model`` body so a
correction touches this module (and its tests) only. The body is UNVERIFIED
(``invoke_model`` was never called, NFR-5); field names/enums are documentation
sourced and provisional.

LOCKED FIX 3: ``embeddingDimension`` is included in the request body ONLY when
``settings.expected_embedding_dimension`` is not None. It is never hardcoded to
1024; when the expectation is unset the field is omitted entirely and the model
emits its native dimension.
"""

from __future__ import annotations

import base64
import json
import logging

from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# [DOC] embeddingPurpose enum values -- provisional, re-confirm at first invoke.
PURPOSE_INDEX = "GENERIC_INDEX"
PURPOSE_RETRIEVAL = "GENERIC_RETRIEVAL"

# [DOC] Nova 2 single-embedding body task type and image format.
_TASK_TYPE = "SINGLE_EMBEDDING"
_IMAGE_FORMAT = "jpeg"


def _detect_image_format(image_b64: str) -> str:
    """Detect the Nova 2 image ``format`` from the base64-encoded bytes.

    Nova 2 sniffs the real MIME type of the decoded image and rejects the
    request when the declared ``format`` disagrees with the bytes. The frontend
    uploads PNGs while the ingest path encodes JPEGs, so the format must be
    derived from the actual bytes rather than hardcoded.

    Returns one of the Nova 2 image-format enums (``png``, ``jpeg``, ``gif``,
    ``webp``); falls back to :data:`_IMAGE_FORMAT` when the signature is
    unrecognized (the model will surface a clear error if it truly mismatches).
    """
    try:
        header = base64.b64decode(image_b64[:24], validate=False)
    except (ValueError, TypeError):
        return _IMAGE_FORMAT
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if header.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "webp"
    return _IMAGE_FORMAT


class EmbeddingError(Exception):
    """Raised when the embedding model invocation fails."""


def embed(
    bedrock_embedding_client,
    settings,
    text: str | None = None,
    image_b64: str | None = None,
    purpose: str = PURPOSE_RETRIEVAL,
) -> list[float]:
    """Return the Nova 2 embedding vector for ``text`` and/or ``image_b64``.

    Args:
        bedrock_embedding_client: boto3 ``bedrock-runtime`` client pinned to
            ``settings.embedding_region`` (Nova 2 is us-east-1 only).
        settings: resolved :class:`~components.agent.settings.Settings`.
        text: optional text to embed.
        image_b64: optional base64-encoded image to embed.
        purpose: ``PURPOSE_INDEX`` or ``PURPOSE_RETRIEVAL`` ([DOC]).

    Raises:
        ValueError: when neither ``text`` nor ``image_b64`` is provided.
        EmbeddingError: when the model invocation raises ``ClientError``.
    """
    if not text and not image_b64:
        raise ValueError("embed requires at least one of text or image_b64")

    # [DOC] body fields (field names / nesting UNVERIFIED).
    params: dict = {"embeddingPurpose": purpose}
    # LOCKED FIX 3: send the dimension hint only when an expectation is set.
    if settings.expected_embedding_dimension is not None:
        params["embeddingDimension"] = settings.expected_embedding_dimension
    if text:
        params["text"] = {"truncationMode": "END", "value": text}
    if image_b64:
        image_format = _detect_image_format(image_b64)
        params["image"] = {"format": image_format, "source": {"bytes": image_b64}}

    body = json.dumps({"taskType": _TASK_TYPE, "singleEmbeddingParams": params})

    try:
        response = bedrock_embedding_client.invoke_model(
            modelId=settings.embedding_model_id,
            body=body,
            accept="application/json",
            contentType="application/json",
        )
    except ClientError as exc:  # tool-boundary error per design error-handling
        logger.error("Nova 2 embedding invocation failed: %s", exc)
        raise EmbeddingError(f"embedding model invocation failed: {exc}") from exc

    payload = json.loads(response["body"].read())
    # [DOC] response path: embeddings[0].embedding (UNVERIFIED).
    return payload["embeddings"][0]["embedding"]
