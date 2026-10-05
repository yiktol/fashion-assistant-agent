"""Shared fixtures for the tool/hook unit tests.

Everything is mocked: no real AWS or network calls (NFR-5). The fake settings
object carries only the fields the tools read, so tests do not depend on
``load_settings`` or disk fixtures.
"""

from __future__ import annotations

import base64
import io
import json
from dataclasses import dataclass

import pytest


@dataclass(frozen=True)
class FakeSettings:
    """Minimal settings carrier for tool unit tests."""

    primary_region: str = "us-east-1"
    image_region: str = "us-west-2"
    embedding_region: str = "us-east-1"
    agent_model_id: str = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    embedding_model_id: str = "amazon.nova-2-multimodal-embeddings-v1:0"
    text_to_image_model_id: str = "stability.stable-image-core-v1:1"
    text_to_image_alt_model_id: str = "stability.stable-image-ultra-v1:1"
    inpaint_model_id: str = "us.stability.stable-image-inpaint-v1:0"
    outpaint_model_id: str = "us.stability.stable-outpaint-v1:0"
    search_replace_model_id: str = "us.stability.stable-image-search-replace-v1:0"
    search_recolor_model_id: str = "us.stability.stable-image-search-recolor-v1:0"
    expected_embedding_dimension: int | None = None
    image_bucket: str = "fashion-bucket"
    vector_bucket: str = "fashion-vectors"
    vector_index: str = "images-index"
    top_k: int = 3
    distance_metric: str = "cosine"


def _body(payload: dict):
    """Wrap a dict as an invoke_model-style streaming body."""
    return io.BytesIO(json.dumps(payload).encode("utf-8"))


class RecordingBedrock:
    """Fake bedrock-runtime client recording the last invoke_model call."""

    def __init__(self, region_name: str, response_payload: dict):
        self.meta = type("meta", (), {"region_name": region_name})()
        self._response_payload = response_payload
        self.last_call: dict | None = None

    def invoke_model(self, *, modelId, body, accept=None, contentType=None):
        self.last_call = {
            "modelId": modelId,
            "body": json.loads(body),
            "region_name": self.meta.region_name,
        }
        return {"body": _body(self._response_payload)}


@pytest.fixture()
def settings() -> FakeSettings:
    return FakeSettings()


@pytest.fixture()
def png_b64() -> str:
    # A tiny opaque "image" payload; content is irrelevant to the mocked path.
    return base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode("ascii")


@pytest.fixture()
def image_response(png_b64):
    """A successful Stability-style response: images[0] + null finish_reasons."""
    return {"images": [png_b64], "finish_reasons": [None], "seeds": [0]}


@pytest.fixture()
def embedding_response():
    return {"embeddings": [{"embedding": [0.1, 0.2, 0.3]}]}


class FakeS3:
    """Fake S3 client recording put_object calls and serving canned bytes."""

    def __init__(self, object_bytes: bytes = b"source-image-bytes"):
        self._object_bytes = object_bytes
        self.puts: list[dict] = []

    def get_object(self, *, Bucket, Key):
        return {"Body": io.BytesIO(self._object_bytes)}

    def put_object(self, *, Bucket, Key, Body, ContentType=None):
        self.puts.append(
            {"Bucket": Bucket, "Key": Key, "Body": Body, "ContentType": ContentType}
        )
        return {}


@pytest.fixture()
def fake_s3() -> FakeS3:
    return FakeS3()
