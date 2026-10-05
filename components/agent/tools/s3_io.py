"""Shared S3 I/O helpers for the fashion agent tools.

Download/encode source images and upload generated PNGs. Output keys are always
``.png`` (the pinned ``output_format`` rule in design.md); the extension is never
derived from the source filename.
"""

from __future__ import annotations

import base64


def parse_s3_uri(uri: str) -> tuple[str, str]:
    """Parse ``s3://bucket/key`` into ``(bucket, key)`` with strict validation.

    Raises:
        ValueError: when the URI is not a well-formed ``s3://bucket/key``.
    """
    if not isinstance(uri, str) or not uri.startswith("s3://"):
        raise ValueError(f"not an s3:// URI: {uri!r}")
    remainder = uri[len("s3://") :]
    bucket, sep, key = remainder.partition("/")
    if not bucket or not sep or not key:
        raise ValueError(f"malformed s3:// URI (need bucket and key): {uri!r}")
    return bucket, key


def download_bytes(s3, uri: str) -> bytes:
    """Download an S3 object and return its raw bytes."""
    bucket, key = parse_s3_uri(uri)
    response = s3.get_object(Bucket=bucket, Key=key)
    return response["Body"].read()


def download_b64(s3, uri: str) -> str:
    """Download an S3 object and return its base64-encoded (ascii) string."""
    return base64.b64encode(download_bytes(s3, uri)).decode("ascii")


def upload_png(s3, bucket: str, key: str, data_bytes: bytes) -> str:
    """Upload PNG bytes to ``s3://bucket/key`` with ``image/png`` content type.

    Returns the resulting ``s3://`` URI.
    """
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=data_bytes,
        ContentType="image/png",
    )
    return f"s3://{bucket}/{key}"
