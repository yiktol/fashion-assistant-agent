# S3 Vectors provider Lambda layer (pre-populated, Docker-free)

LOCKED FIX 1: `cdk synth` must run with the Docker daemon stopped. This layer
asset is therefore **pre-populated** — the `python/` directory already contains
a pinned, `s3vectors`-capable boto3/botocore installed with:

```
pip install --target python boto3==1.43.107 botocore==1.43.108
```

The Lambda runtime's built-in boto3 is older than the first release that knows
the `s3vectors` service, so the provider function layers this directory on top.

`S3VectorsConstruct` consumes `python/` via `aws_lambda.Code.from_asset(...)`
with **no** `BundlingOptions` and runs **no** pip at synth time, so synthesis
never touches Docker. To refresh the pinned SDK, re-run the pip command above
from an activated virtualenv (never via a synth-time bundler).
