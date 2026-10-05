"""Docker-free S3 Vectors bucket construct.

Provisions a CloudFormation custom resource that owns an S3 Vectors *bucket*
(not its index). The provider Lambda carries an ``s3vectors``-capable boto3 via
a PRE-POPULATED layer asset, so ``cdk synth`` is Docker-free by construction
(LOCKED FIX 1): no ``BundlingOptions`` and no pip at synth time.

LOCKED FIX 2: the provider does NOT create an index and is NOT granted
``s3vectors:CreateIndex``. Index creation + population is the ingest notebook /
app identity's responsibility. The ``indexName`` is passed only so teardown can
remove a leftover index before deleting the bucket.
"""

from pathlib import Path

from aws_cdk import CustomResource, Duration
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import custom_resources as cr
from cdk_nag import NagSuppressions, NagPackSuppression
from constructs import Construct

_STACKS_DIR = Path(__file__).resolve().parent
# Plain directory already containing the handler (no bundling needed).
_PROVIDER_ASSET = str(_STACKS_DIR / "s3vectors_provider")
# Pre-populated layer asset (python/ holds a pinned s3vectors-capable boto3).
_PROVIDER_LAYER_ASSET = str(_STACKS_DIR / "s3vectors_provider_layer")


class S3VectorsConstruct(Construct):
    def __init__(
        self,
        scope: Construct,
        id: str,
        vector_bucket_name: str,
        index_name: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, id, **kwargs)

        self.vector_bucket_name = vector_bucket_name
        self.index_name = index_name

        # Pre-populated layer carrying a pinned, s3vectors-capable boto3.
        # from_asset on a plain directory => no Docker, no synth-time bundling.
        sdk_layer = lambda_.LayerVersion(
            self,
            "S3VectorsSdkLayer",
            code=lambda_.Code.from_asset(_PROVIDER_LAYER_ASSET),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_12],
            description="Pinned s3vectors-capable boto3/botocore (Docker-free asset)",
        )

        on_event_fn = lambda_.Function(
            self,
            "S3VectorsProviderFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.on_event",
            # Plain directory asset (handler only) => Docker-free by construction.
            code=lambda_.Code.from_asset(_PROVIDER_ASSET),
            layers=[sdk_layer],
            timeout=Duration.minutes(5),
        )

        # The provider manages the vector BUCKET lifecycle only (LOCKED FIX 2:
        # no CreateIndex). GetIndex/DeleteIndex are needed solely for teardown.
        # S3 Vectors has no pre-creation resource ARN for the bucket being
        # created, so a wildcard resource is required here.
        on_event_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "s3vectors:CreateVectorBucket",
                    "s3vectors:DeleteVectorBucket",
                    "s3vectors:GetVectorBucket",
                    "s3vectors:DeleteIndex",
                    "s3vectors:GetIndex",
                ],
                resources=["*"],
            )
        )

        provider = cr.Provider(
            self,
            "S3VectorsProvider",
            on_event_handler=on_event_fn,
        )

        resource = CustomResource(
            self,
            "S3VectorBucket",
            service_token=provider.service_token,
            properties={
                "vectorBucketName": vector_bucket_name,
                "indexName": index_name,
            },
        )

        self._resource = resource

        self._add_nag_suppressions(on_event_fn, provider)

    @property
    def vector_bucket_arn(self) -> str:
        from aws_cdk import Stack

        stack = Stack.of(self)
        return (
            f"arn:aws:s3vectors:{stack.region}:{stack.account}:"
            f"bucket/{self.vector_bucket_name}"
        )

    def _add_nag_suppressions(self, on_event_fn, provider):
        NagSuppressions.add_resource_suppressions(
            on_event_fn,
            [
                NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason=(
                        "s3vectors bucket lifecycle (Create/Delete/Get bucket + "
                        "teardown DeleteIndex/GetIndex) targets a bucket that does "
                        "not yet exist at policy-creation time, so S3 Vectors "
                        "offers no resource ARN to scope to; a wildcard resource "
                        "is required. CreateIndex is intentionally NOT granted "
                        "(LOCKED FIX 2)."
                    ),
                ),
                NagPackSuppression(
                    id="AwsSolutions-IAM4",
                    reason=(
                        "Provider Lambda uses the AWS-managed basic execution role "
                        "for CloudWatch Logs; this is the standard custom-resource "
                        "provider pattern."
                    ),
                ),
            ],
            apply_to_children=True,
        )
        NagSuppressions.add_resource_suppressions(
            provider,
            [
                NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason=(
                        "custom_resources.Provider framework role uses a wildcard "
                        "to invoke the on-event handler and write its own log "
                        "streams; this is CDK-generated framework behaviour."
                    ),
                ),
                NagPackSuppression(
                    id="AwsSolutions-IAM4",
                    reason=(
                        "Provider framework Lambda uses the AWS-managed basic "
                        "execution role (CDK-generated)."
                    ),
                ),
                NagPackSuppression(
                    id="AwsSolutions-L1",
                    reason=(
                        "custom_resources.Provider pins its own framework Lambda "
                        "runtime; it is CDK-managed and not user-selectable."
                    ),
                ),
            ],
            apply_to_children=True,
        )
