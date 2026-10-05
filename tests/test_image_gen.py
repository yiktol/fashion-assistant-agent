"""Unit tests for generate_image / inpaint / outpaint (boto3 mocked)."""

from __future__ import annotations

from tests.conftest import RecordingBedrock

from components.agent.tools.image_gen import (
    generate_image_impl,
    inpaint_impl,
    outpaint_impl,
)


# --------------------------------------------------------------------------- #
# generate_image
# --------------------------------------------------------------------------- #
def test_generate_image_omits_aspect_ratio_and_seed(settings, fake_s3, image_response):
    bedrock = RecordingBedrock("us-west-2", image_response)
    result = generate_image_impl(bedrock, fake_s3, settings, "a red dress")

    assert result["status"] == "success"
    payload = result["content"][0]["json"]
    assert payload["result"] == "ok"
    assert payload["s3_uri"].startswith("s3://fashion-bucket/OutputImages/gen_")
    assert payload["s3_uri"].endswith(".png")

    body = bedrock.last_call["body"]
    # LOCKED FIX 4: no aspect_ratio; also no seed.
    assert "aspect_ratio" not in body
    assert "seed" not in body
    assert body["output_format"] == "png"
    assert body["prompt"] == "a red dress"
    # us-west-2 + bare core modelId.
    assert bedrock.last_call["region_name"] == "us-west-2"
    assert bedrock.last_call["modelId"] == "stability.stable-image-core-v1:1"
    # Upload sets image/png content type and a .png key.
    assert fake_s3.puts[0]["ContentType"] == "image/png"
    assert fake_s3.puts[0]["Key"].endswith(".png")


def test_generate_image_appends_weather(settings, fake_s3, image_response):
    bedrock = RecordingBedrock("us-west-2", image_response)
    generate_image_impl(bedrock, fake_s3, settings, "a coat", weather="snowy")
    assert "snowy" in bedrock.last_call["body"]["prompt"]


def test_generate_image_rejects_empty_prompt(settings, fake_s3, image_response):
    bedrock = RecordingBedrock("us-west-2", image_response)
    result = generate_image_impl(bedrock, fake_s3, settings, "   ")
    assert result["status"] == "error"


def test_generate_image_content_filter_is_error(settings, fake_s3):
    bedrock = RecordingBedrock("us-west-2", {"images": [], "finish_reasons": ["FILTER"]})
    result = generate_image_impl(bedrock, fake_s3, settings, "x")
    assert result["status"] == "error"


# --------------------------------------------------------------------------- #
# inpaint
# --------------------------------------------------------------------------- #
def test_inpaint_uses_profile_id_in_primary_region(settings, fake_s3, image_response):
    bedrock = RecordingBedrock("us-east-1", image_response)
    result = inpaint_impl(
        bedrock,
        fake_s3,
        settings,
        "s3://fashion-bucket/uploads/photo.jpg",
        "make it blue",
        mask_uri="s3://fashion-bucket/uploads/mask.png",
    )
    assert result["status"] == "success"
    assert bedrock.last_call["region_name"] == "us-east-1"
    assert bedrock.last_call["modelId"] == "us.stability.stable-image-inpaint-v1:0"
    body = bedrock.last_call["body"]
    assert body["output_format"] == "png"
    # Verified Stability inpaint contract: a mask IMAGE, never search_prompt.
    assert "mask" in body
    assert "search_prompt" not in body
    assert "mask_source" not in body
    assert "image" in body
    # source stem reused, extension forced to .png (never derived from .jpg).
    key = fake_s3.puts[0]["Key"]
    assert key.startswith("OutputImages/photo_") and key.endswith(".png")


def test_inpaint_passes_optional_mask_controls(settings, fake_s3, image_response):
    bedrock = RecordingBedrock("us-east-1", image_response)
    inpaint_impl(
        bedrock,
        fake_s3,
        settings,
        "s3://fashion-bucket/uploads/photo.png",
        "swap sky",
        mask_uri="s3://fashion-bucket/uploads/mask.png",
        negative_prompt="blurry",
        grow_mask=4,
    )
    body = bedrock.last_call["body"]
    assert "mask" in body
    assert body["negative_prompt"] == "blurry"
    assert body["grow_mask"] == 4


def test_inpaint_requires_mask(settings, fake_s3, image_response):
    bedrock = RecordingBedrock("us-east-1", image_response)
    result = inpaint_impl(
        bedrock, fake_s3, settings, "s3://fashion-bucket/uploads/a.png", "x"
    )
    assert result["status"] == "error"


# --------------------------------------------------------------------------- #
# outpaint
# --------------------------------------------------------------------------- #
def test_outpaint_uses_profile_id_and_omits_zero_extents(
    settings, fake_s3, image_response
):
    bedrock = RecordingBedrock("us-east-1", image_response)
    result = outpaint_impl(
        bedrock,
        fake_s3,
        settings,
        "s3://fashion-bucket/uploads/scene.png",
        "extend the beach",
        right=512,
    )
    assert result["status"] == "success"
    assert bedrock.last_call["region_name"] == "us-east-1"
    assert bedrock.last_call["modelId"] == "us.stability.stable-outpaint-v1:0"
    body = bedrock.last_call["body"]
    assert body["right"] == 512
    # Zero extents are omitted.
    assert "left" not in body and "up" not in body and "down" not in body


def test_outpaint_requires_at_least_one_extent(settings, fake_s3, image_response):
    bedrock = RecordingBedrock("us-east-1", image_response)
    result = outpaint_impl(
        bedrock, fake_s3, settings, "s3://fashion-bucket/uploads/a.png", "x"
    )
    assert result["status"] == "error"
