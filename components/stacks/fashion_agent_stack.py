"""Data-plane-only CDK stack for the Fashion Agent.

The managed Bedrock agent, its action-group Lambda, the managed search-cluster
collection, and every associated role/policy are GONE. The agent now runs
in-process (Strands) against this cloud data plane, which provisions only:

  * the two S3 buckets (image bucket + access-log bucket), preserved exactly
    as before (NFR-6);
  * an S3 Vectors bucket via a Docker-free custom resource (LOCKED FIX 1);
  * a principal-less ``iam.ManagedPolicy`` the operator attaches to the local
    runtime identity, granting Bedrock InvokeModel across the inference-profile
    fan-out regions (LOCKED FIX 5), S3 object access, and S3 Vectors data-plane
    operations (including CreateIndex for the ingest notebook).
"""

from typing import Any, Dict

import aws_cdk as cdk
from aws_cdk import CfnOutput, RemovalPolicy
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from cdk_nag import NagSuppressions, NagPackSuppression
from constructs import Construct

from .iam_arns import build_invoke_model_resource_arns
from .s3_vectors_construct import S3VectorsConstruct


class FashionAgentStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        stack_name: str,
        config: Dict[str, Any],
        **kwargs,
    ) -> None:
        super().__init__(scope, stack_name, **kwargs)
        self.nag_suppressed_resources = []

        # --- S3 buckets (preserved exactly, NFR-6) ---------------------------
        access_log_bucket = s3.Bucket(
            self,
            "AccessLogBucket",
            bucket_name=f"fashion-agent-access-logs-{self.account}-{self.region}",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
        )

        if not config["bucket_name"]:
            bucket_name = f"fashion-agent-{self.account}-{self.region}"
        else:
            bucket_name = config["bucket_name"]

        bucket = s3.Bucket(
            self,
            "FashionAgentBucket",
            bucket_name=bucket_name,
            removal_policy=RemovalPolicy.DESTROY,
            enforce_ssl=True,
            auto_delete_objects=True,
            server_access_logs_bucket=access_log_bucket,
            server_access_logs_prefix="fashion-agent-logs/",
        )

        CfnOutput(
            self,
            "BucketName",
            value=bucket.bucket_name,
        )

        # --- S3 Vectors data plane (Docker-free custom resource) -------------
        vector_cfg = config["vector_index"]
        vector_bucket_name = (
            f"{vector_cfg['bucket_name']}-{self.account}-{self.region}"
        )
        index_name = vector_cfg["index_name"]

        vectors = S3VectorsConstruct(
            self,
            "S3Vectors",
            vector_bucket_name=vector_bucket_name,
            index_name=index_name,
        )

        # --- Data-plane managed policy (no principal) ------------------------
        invoke_model_arns = build_invoke_model_resource_arns(
            config["models"],
            config["inference_profile_fanout_regions"],
            self.account,
        )

        data_plane_policy = iam.ManagedPolicy(
            self,
            "FashionDataPlanePolicy",
            document=iam.PolicyDocument(
                statements=[
                    iam.PolicyStatement(
                        sid="BedrockInvokeModel",
                        effect=iam.Effect.ALLOW,
                        actions=[
                            "bedrock:InvokeModel",
                            "bedrock:InvokeModelWithResponseStream",
                        ],
                        resources=invoke_model_arns,
                    ),
                    iam.PolicyStatement(
                        sid="S3ImageObjectAccess",
                        effect=iam.Effect.ALLOW,
                        actions=["s3:GetObject", "s3:PutObject"],
                        resources=[bucket.bucket_arn, f"{bucket.bucket_arn}/*"],
                    ),
                    iam.PolicyStatement(
                        sid="S3VectorsDataPlane",
                        effect=iam.Effect.ALLOW,
                        actions=[
                            "s3vectors:PutVectors",
                            "s3vectors:QueryVectors",
                            "s3vectors:GetVectors",
                            "s3vectors:GetIndex",
                            "s3vectors:GetVectorBucket",
                            "s3vectors:CreateIndex",
                        ],
                        resources=[
                            vectors.vector_bucket_arn,
                            f"{vectors.vector_bucket_arn}/index/*",
                        ],
                    ),
                ]
            ),
        )
        self.nag_suppressed_resources.append(data_plane_policy)

        # --- Outputs ---------------------------------------------------------
        CfnOutput(self, "VectorBucketName", value=vectors.vector_bucket_name)
        CfnOutput(self, "VectorIndexName", value=vectors.index_name)
        CfnOutput(self, "VectorBucketArn", value=vectors.vector_bucket_arn)
        CfnOutput(self, "DataPlanePolicyArn", value=data_plane_policy.managed_policy_arn)

        self.add_nag_suppressions()

    def add_nag_suppressions(self):
        NagSuppressions.add_resource_suppressions(
            self.nag_suppressed_resources,
            [
                NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason=(
                        "Least-privilege data-plane policy. Wildcards appear only "
                        "where a service exposes no sub-resource ARN: the S3 "
                        "object ARN suffix '/*', the S3 Vectors per-index path "
                        "'/index/*', and the Bedrock InvokeModel resource set is "
                        "pinned to explicit inference-profile and foundation-model "
                        "ARNs per fan-out region (LOCKED FIX 5)."
                    ),
                )
            ],
            apply_to_children=True,
        )
