"""CloudFormation custom-resource handler that manages an S3 Vectors bucket.

Runs on the provider Lambda. It owns the VECTOR BUCKET lifecycle ONLY:

  Create -> create_vector_bucket(vectorBucketName)
  Update -> if the bucket name changed, return a new PhysicalResourceId so
            CloudFormation performs replace-then-delete semantics; otherwise
            it is a no-op.
  Delete -> best-effort delete_index(vectorBucketName, indexName) then
            delete_vector_bucket(vectorBucketName); both tolerate NotFound so
            rollback/teardown is idempotent.

LOCKED FIX 2: this handler NEVER creates an index and NEVER needs an embedding
dimension. Index creation + population is the ingest notebook's job
(the app/notebook identity holds s3vectors:CreateIndex). The handler reads
only ``vectorBucketName`` and the optional ``indexName`` (used solely so
teardown can delete a leftover index before the bucket).
"""

import boto3


def _client():
    # boto3 resolves the region from the Lambda execution environment.
    return boto3.client("s3vectors")


def _is_not_found(exc) -> bool:
    name = type(exc).__name__
    if name in ("NotFoundException", "ResourceNotFoundException"):
        return True
    resp = getattr(exc, "response", None) or {}
    code = resp.get("Error", {}).get("Code", "")
    return code in ("NotFoundException", "ResourceNotFoundException", "404")


def on_event(event, context):
    request_type = event["RequestType"]
    props = event.get("ResourceProperties", {}) or {}
    bucket_name = props["vectorBucketName"]
    index_name = props.get("indexName")

    if request_type == "Create":
        return _on_create(bucket_name)
    if request_type == "Update":
        return _on_update(event, bucket_name)
    if request_type == "Delete":
        return _on_delete(bucket_name, index_name)
    raise ValueError(f"Unexpected RequestType: {request_type}")


def _on_create(bucket_name):
    client = _client()
    try:
        client.create_vector_bucket(vectorBucketName=bucket_name)
    except Exception as exc:  # noqa: BLE001 - tolerate already-exists on retry
        if not _already_exists(exc):
            raise
    return {"PhysicalResourceId": bucket_name}


def _on_update(event, bucket_name):
    old_props = event.get("OldResourceProperties", {}) or {}
    old_name = old_props.get("vectorBucketName")
    if old_name and old_name != bucket_name:
        # Name change -> replacement. Create the new bucket now; CloudFormation
        # issues Delete for the old PhysicalResourceId afterwards.
        return _on_create(bucket_name)
    # No material change.
    return {"PhysicalResourceId": event.get("PhysicalResourceId", bucket_name)}


def _on_delete(bucket_name, index_name):
    client = _client()
    if index_name:
        try:
            client.delete_index(vectorBucketName=bucket_name, indexName=index_name)
        except Exception as exc:  # noqa: BLE001
            if not _is_not_found(exc):
                raise
    try:
        client.delete_vector_bucket(vectorBucketName=bucket_name)
    except Exception as exc:  # noqa: BLE001
        if not _is_not_found(exc):
            raise
    return {"PhysicalResourceId": bucket_name}


def _already_exists(exc) -> bool:
    name = type(exc).__name__
    if name in ("ConflictException", "BucketAlreadyExists", "AlreadyExistsException"):
        return True
    resp = getattr(exc, "response", None) or {}
    code = resp.get("Error", {}).get("Code", "")
    return code in ("ConflictException", "BucketAlreadyExists", "AlreadyExistsException")
