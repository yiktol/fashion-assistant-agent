"""Unit tests for image_lookup + embeddings dimension-hint behavior."""

from __future__ import annotations

import dataclasses

from tests.conftest import RecordingBedrock

from components.agent.tools import embeddings
from components.agent.tools.image_lookup import image_lookup_impl


class RecordingS3Vectors:
    def __init__(self, vectors):
        self._vectors = vectors
        self.last_query: dict | None = None

    def query_vectors(
        self,
        *,
        vectorBucketName,
        indexName,
        topK,
        queryVector,
        returnMetadata=None,
        returnDistance=None,
    ):
        self.last_query = {
            "vectorBucketName": vectorBucketName,
            "indexName": indexName,
            "topK": topK,
            "queryVector": queryVector,
            "returnMetadata": returnMetadata,
            "returnDistance": returnDistance,
        }
        return {"vectors": self._vectors}


def test_lookup_returns_top_hit_s3_uri(settings, fake_s3, embedding_response):
    bedrock = RecordingBedrock("us-east-1", embedding_response)
    s3vectors = RecordingS3Vectors(
        [
            {
                "key": "cat-1",
                "distance": 0.12,
                "metadata": {"s3_uri": "s3://fashion-bucket/catalog/dress.png"},
            }
        ]
    )
    result = image_lookup_impl(
        bedrock, s3vectors, fake_s3, settings, input_query="a floral dress"
    )
    assert result["status"] == "success"
    payload = result["content"][0]["json"]
    assert payload["result"] == "ok"
    assert payload["s3_uri"] == "s3://fashion-bucket/catalog/dress.png"
    assert payload["distance"] == 0.12
    # queryVector uses the float32 envelope; topK is settings.top_k.
    assert s3vectors.last_query["queryVector"] == {"float32": [0.1, 0.2, 0.3]}
    assert s3vectors.last_query["topK"] == settings.top_k
    assert s3vectors.last_query["returnMetadata"] is True


def test_lookup_zero_hits_is_not_found_without_echo(settings, fake_s3, embedding_response):
    bedrock = RecordingBedrock("us-east-1", embedding_response)
    s3vectors = RecordingS3Vectors([])
    result = image_lookup_impl(
        bedrock,
        s3vectors,
        fake_s3,
        settings,
        input_image_uri="s3://fashion-bucket/uploads/mine.png",
    )
    assert result["status"] == "success"
    payload = result["content"][0]["json"]
    assert payload["result"] == "not_found"
    assert payload["s3_uri"] is None
    # Legacy echo of the input image is removed.
    assert payload["s3_uri"] != "s3://fashion-bucket/uploads/mine.png"


def test_lookup_requires_an_input(settings, fake_s3, embedding_response):
    bedrock = RecordingBedrock("us-east-1", embedding_response)
    s3vectors = RecordingS3Vectors([])
    result = image_lookup_impl(bedrock, s3vectors, fake_s3, settings)
    assert result["status"] == "success"
    assert result["content"][0]["json"]["result"] == "not_found"


# --------------------------------------------------------------------------- #
# embeddings dimension-hint behavior (LOCKED FIX 3)
# --------------------------------------------------------------------------- #
def test_embeddings_omit_dimension_when_expectation_none(settings, embedding_response):
    bedrock = RecordingBedrock("us-east-1", embedding_response)
    assert settings.expected_embedding_dimension is None
    vector = embeddings.embed(bedrock, settings, text="hello")
    assert vector == [0.1, 0.2, 0.3]
    params = bedrock.last_call["body"]["singleEmbeddingParams"]
    assert "embeddingDimension" not in params
    # us-east-1 + bare Nova id.
    assert bedrock.last_call["region_name"] == "us-east-1"
    assert bedrock.last_call["modelId"] == "amazon.nova-2-multimodal-embeddings-v1:0"


def test_embeddings_include_dimension_when_set(settings, embedding_response):
    settings = dataclasses.replace(settings, expected_embedding_dimension=1024)
    bedrock = RecordingBedrock("us-east-1", embedding_response)
    embeddings.embed(bedrock, settings, text="hello")
    params = bedrock.last_call["body"]["singleEmbeddingParams"]
    assert params["embeddingDimension"] == 1024
