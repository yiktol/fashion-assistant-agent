"""Automated S3 Vectors ingest — mirrors s3vectors_ingest.ipynb cell-for-cell.

Creates the vector index (measuring the Nova 2 embedding dimension from a real
probe) and populates it from the demo western-dress dataset. Credentials come
from the default boto3 chain. Honors LOCKED FIXES 2 (notebook-only index
creation) and 3 (dimension hint only when expected_dimension is set).

Usage:
    python scripts/run_ingest.py            # default: ingest up to LIMIT images
    LIMIT=0 python scripts/run_ingest.py    # ingest the entire dataset
"""
import base64
import io
import json
import os
from pathlib import Path

import boto3
import yaml
from botocore.exceptions import ClientError
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
LIMIT = int(os.environ.get("LIMIT", "100"))  # 0 = all

# --- Cell 7: load config + CDK outputs ---------------------------------------
with open(ROOT / "config.yml") as fh:
    config = yaml.safe_load(fh)
with open(ROOT / "variables.json") as fh:
    raw_outputs = json.load(fh)


def flatten_outputs(raw):
    if any(k in raw for k in ("BucketName", "VectorBucketName", "VectorIndexName")):
        return raw
    flat = {}
    for value in raw.values():
        if isinstance(value, dict):
            flat.update(value)
    return flat


outputs = flatten_outputs(raw_outputs)
IMAGE_BUCKET = outputs["BucketName"]
VECTOR_BUCKET = outputs["VectorBucketName"]
VECTOR_INDEX = outputs["VectorIndexName"]
EMBEDDING_REGION = config["embedding_region"]
PRIMARY_REGION = config["primary_region"]
EMBEDDING_MODEL_ID = config["models"]["embeddings"]
EXPECTED_DIMENSION = config["vector_index"].get("expected_dimension")
DISTANCE_METRIC = config["vector_index"].get("distance_metric", "cosine")

print(f"image bucket:    {IMAGE_BUCKET}")
print(f"vector bucket:   {VECTOR_BUCKET}")
print(f"vector index:    {VECTOR_INDEX}")
print(f"embedding model: {EMBEDDING_MODEL_ID} @ {EMBEDDING_REGION}")
print(f"expected_dimension (config): {EXPECTED_DIMENSION}")

# --- Cell 8: clients ---------------------------------------------------------
session = boto3.Session()
bedrock = session.client("bedrock-runtime", region_name=EMBEDDING_REGION)
s3 = session.client("s3", region_name=PRIMARY_REGION)
s3vectors = session.client("s3vectors", region_name=PRIMARY_REGION)

# --- Cell 10: Nova 2 embedding helper ----------------------------------------
PURPOSE_INDEX = "GENERIC_INDEX"
TASK_TYPE = "SINGLE_EMBEDDING"
IMAGE_FORMAT = "jpeg"


def embed(text=None, image_b64=None, purpose=PURPOSE_INDEX):
    if not text and not image_b64:
        raise ValueError("embed requires text and/or image_b64")
    params = {"embeddingPurpose": purpose}
    if EXPECTED_DIMENSION is not None:
        params["embeddingDimension"] = int(EXPECTED_DIMENSION)
    if text:
        params["text"] = {"truncationMode": "END", "value": text}
    if image_b64:
        params["image"] = {"format": IMAGE_FORMAT, "source": {"bytes": image_b64}}
    body = json.dumps({"taskType": TASK_TYPE, "singleEmbeddingParams": params})
    response = bedrock.invoke_model(
        modelId=EMBEDDING_MODEL_ID,
        body=body,
        accept="application/json",
        contentType="application/json",
    )
    payload = json.loads(response["body"].read())
    return payload["embeddings"][0]["embedding"]


def encode_image(image_path, max_size=(1024, 1024)):
    with Image.open(image_path) as image:
        image = image.convert("RGB")
        if image.size[0] * image.size[1] > max_size[0] * max_size[1]:
            image.thumbnail(max_size)
        buf = io.BytesIO()
        image.save(buf, format="JPEG")
    return base64.b64encode(buf.getvalue()).decode("utf8")


# --- Cell 12: bootstrap the index (only create_index call site) --------------
class DimensionMismatchError(Exception):
    pass


print("\nProbing Nova 2 to measure embedding dimension...")
probe_vector = embed(text="a fashion outfit", purpose=PURPOSE_INDEX)
measured_dim = len(probe_vector)
print(f"measured Nova 2 embedding dimension: {measured_dim}")
if EXPECTED_DIMENSION is not None and int(EXPECTED_DIMENSION) != measured_dim:
    raise DimensionMismatchError(
        f"config expected_dimension={EXPECTED_DIMENSION} but model emitted {measured_dim}"
    )

NOT_FOUND_CODES = {"NotFoundException", "ResourceNotFoundException", "NotFound"}
try:
    s3vectors.get_index(vectorBucketName=VECTOR_BUCKET, indexName=VECTOR_INDEX)
    print(f"index '{VECTOR_INDEX}' already exists; reusing it")
except ClientError as exc:
    code = exc.response.get("Error", {}).get("Code", "")
    if code not in NOT_FOUND_CODES:
        raise
    print(f"index '{VECTOR_INDEX}' not found; creating with dimension {measured_dim}")
    s3vectors.create_index(
        vectorBucketName=VECTOR_BUCKET,
        indexName=VECTOR_INDEX,
        dataType="float32",
        dimension=measured_dim,
        distanceMetric=DISTANCE_METRIC,
    )
    print("index created")

# --- Cell 14: ingest the catalog ---------------------------------------------
dataset_path = ROOT / "Fashion-Dataset-Images-Western-Dress" / "WesternDress_Images"
image_paths = sorted(
    p for p in dataset_path.iterdir() if p.is_file() and not p.name.startswith(".")
)
if LIMIT > 0:
    image_paths = image_paths[:LIMIT]

print(f"\nIngesting {len(image_paths)} images (LIMIT={LIMIT or 'all'})...")
failed = []
for i, image_path in enumerate(image_paths, 1):
    try:
        name = image_path.name
        catalog_key = f"catalog/{name}"
        with open(image_path, "rb") as fh:
            s3.put_object(Bucket=IMAGE_BUCKET, Key=catalog_key, Body=fh.read())
        catalog_uri = f"s3://{IMAGE_BUCKET}/{catalog_key}"
        vector = embed(image_b64=encode_image(image_path), purpose=PURPOSE_INDEX)
        vector_id = image_path.stem
        s3vectors.put_vectors(
            vectorBucketName=VECTOR_BUCKET,
            indexName=VECTOR_INDEX,
            vectors=[
                {
                    "key": vector_id,
                    "data": {"float32": vector},
                    "metadata": {"s3_uri": catalog_uri, "name": name},
                }
            ],
        )
        if i % 20 == 0 or i == len(image_paths):
            print(f"  {i}/{len(image_paths)} ingested")
    except Exception as exc:  # noqa: BLE001
        print(f"  failed on {image_path.name}: {exc}")
        failed.append(image_path.name)

print(f"\nIngestion complete. Succeeded: {len(image_paths) - len(failed)}, Failed: {len(failed)}")
if failed:
    print(f"Failed items: {failed}")
