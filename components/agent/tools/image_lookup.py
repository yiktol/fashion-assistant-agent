"""Image-lookup tool: nearest-neighbor query against Amazon S3 Vectors.

Embeds the query (text and/or image) with Nova 2, queries the vector index, and
returns the top hit's catalog ``s3_uri`` read from the vector metadata (no
base64 round-trip through the vector store).

The legacy behavior of echoing the user's own input image back on a miss is
intentionally removed; a zero-hit lookup returns ``not_found`` with ``s3_uri``
of ``None`` so the system prompt can steer the model to ``generate_image``.
"""

from __future__ import annotations

import logging

from botocore.exceptions import ClientError
from strands import tool

from . import embeddings, s3_io
from ._result import err, not_found, ok

logger = logging.getLogger(__name__)


def image_lookup_impl(
    bedrock_embedding, s3vectors, s3, settings, input_image_uri=None, input_query=None
) -> dict:
    """Find the closest catalog image by image and/or text."""
    if not input_image_uri and not input_query:
        return not_found(
            s3_uri=None,
            distance=None,
            message="provide an image or a text description to search for",
        )

    image_b64 = None
    if input_image_uri:
        image_b64 = s3_io.download_b64(s3, input_image_uri)

    try:
        vector = embeddings.embed(
            bedrock_embedding,
            settings,
            text=input_query,
            image_b64=image_b64,
            purpose=embeddings.PURPOSE_RETRIEVAL,
        )
        response = s3vectors.query_vectors(
            vectorBucketName=settings.vector_bucket,
            indexName=settings.vector_index,
            topK=settings.top_k,
            queryVector={"float32": vector},
            returnMetadata=True,
            returnDistance=True,
        )
    except (ClientError, embeddings.EmbeddingError) as exc:
        logger.error("image_lookup failed: %s", exc)
        return err(f"image lookup failed: {exc}")

    hits = response.get("vectors") or []
    if not hits:
        return not_found(
            s3_uri=None, distance=None, message="no matching catalog image found"
        )

    matches = [
        {"s3_uri": m.get("s3_uri"), "name": m.get("name"), "distance": h.get("distance")}
        for h in hits
        for m in [h.get("metadata") or {}]
        if m.get("s3_uri")
    ]
    if not matches:
        return not_found(
            s3_uri=None,
            distance=hits[0].get("distance"),
            message="matching vector has no catalog s3_uri",
        )
    return ok(
        s3_uri=matches[0]["s3_uri"],
        distance=matches[0]["distance"],
        matches=matches,
        message="match found",
    )


def make_image_lookup(bedrock_embedding, s3vectors, s3, settings):
    @tool
    def image_lookup(
        input_image_uri: str | None = None, input_query: str | None = None
    ) -> dict:
        """Find the closest matching catalog image by image and/or text.

        Args:
            input_image_uri: s3:// URI of an uploaded image, or None.
            input_query: Text description of the desired item, or None.

        Returns (json payload):
            {"result": "ok"|"not_found"|"error", "s3_uri": str|None,
             "distance": float|None, "message": str,
             "matches": [{"s3_uri": str, "name": str|None,
                          "distance": float|None}, ...]}
            On "ok", "matches" lists ALL top-K hits that carried a catalog
            s3_uri (newest/best first) and "s3_uri"/"distance" mirror
            matches[0]; "matches" is absent on not_found/error.
        """
        return image_lookup_impl(
            bedrock_embedding, s3vectors, s3, settings, input_image_uri, input_query
        )

    return image_lookup
