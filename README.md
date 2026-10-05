# Fashion Assistant agent

A fashion assistant that runs an in-process [Strands](https://strandsagents.com/) agent with a Claude Sonnet 4.5 brain. It retrieves similar catalog items from an [Amazon S3 Vectors](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors.html) index (embedded with Amazon Nova 2 multimodal embeddings) and generates or edits images with Stability models — all orchestrated locally, with CDK provisioning only the cloud data plane (S3 buckets + the S3 Vectors bucket).

## Features

- **Image-to-Image or Text-to-Image Search**: Find catalog items similar to a style you like, by uploaded image and/or text, via an S3 Vectors nearest-neighbor query.
- **Text-to-Image Generation**: When nothing in the catalog matches, generate a new item from the user's description.
- **Weather API Integration**: Pull current weather for a mentioned location (Open-Meteo) to steer weather-appropriate outfit suggestions.
- **Inpainting**: Modify a specific region of an uploaded image (e.g. recolor or restyle a garment).
- **Outpainting**: Extend an uploaded image outward (e.g. change or widen the background/setting).

## Architecture

The agent is **in-process**: `components/agent/agent.py` builds a Strands `Agent` with a `BedrockModel` brain and five `@tool` functions (`get_weather`, `generate_image`, `image_lookup`, `inpaint`, `outpaint`). There is no managed Bedrock Agent and no action-group Lambda. The Streamlit UI (`frontend/app.py`) calls the agent directly and discovers result images from the tool-result hook (no response-text parsing).

Work is split across three regions by model availability:

- **primary (`us-east-1`)** — the Claude Sonnet 4.5 brain (via a `us.` cross-region inference profile), the Stability inpaint/outpaint `us.` profiles, S3, and S3 Vectors.
- **embeddings (`us-east-1`)** — Amazon Nova 2 multimodal embeddings (on-demand, `us-east-1` today).
- **image generation (`us-west-2`)** — Stability `stable-image-core` / `stable-image-ultra` (on-demand, `us-west-2` today).

Retrieval data plane: catalog images live in the S3 image bucket under `catalog/`; their embeddings live in an S3 Vectors index whose metadata holds the catalog `s3_uri` (no base64 blob is stored in the vector store). The flowchart below is illustrative of the overall request flow.

![Flow Chart](images/flowchart_agent.png)

## Prerequisites

- An active AWS account using the default credential chain (no profile is baked into the project).
- Node.js + npm and the AWS CDK CLI (`npm install -g aws-cdk`).
- Python `>=3.12,<3.14`.
- **Enable model access** in the Amazon Bedrock console for these models, in the listed regions:
  - **Anthropic Claude Sonnet 4.5** via the `us.anthropic.claude-sonnet-4-5-20250929-v1:0` cross-region inference profile (fans out to `us-east-1`, `us-east-2`, `us-west-2`).
  - **Amazon Nova 2 multimodal embeddings** (`amazon.nova-2-multimodal-embeddings-v1:0`) in `us-east-1`.
  - **Stability stable-image-core** (`stability.stable-image-core-v1:1`) and **stable-image-ultra** (`stability.stable-image-ultra-v1:1`) in `us-west-2`.
  - **Stability inpaint/outpaint** profiles (`us.stability.stable-image-inpaint-v1:0`, `us.stability.stable-outpaint-v1:0`) in `us-east-1`.

## Installation

Install the project (app + infra + dev extras) in editable mode:

```bash
pip install -e '.[infra,dev]'
```

This replaces the old `pip install -r requirements.txt` flow; dependencies are pinned in `pyproject.toml`.

## Deploy and run

1. **Deploy the data plane** and capture the stack outputs:

   ```bash
   cdk deploy --outputs-file variables.json
   ```

   This provisions the S3 image bucket and the S3 Vectors **bucket** (the index is created later, in the notebook), plus a `FashionDataPlanePolicy` managed IAM policy with the exact `bedrock:InvokeModel`, `s3`, and `s3vectors` permissions the agent needs.

2. **Attach the data-plane policy** to the IAM identity that will run the notebook and the app. Take the `DataPlanePolicyArn` output from `variables.json` and attach it to your user/role:

   ```bash
   aws iam attach-user-policy --user-name <your-user> --policy-arn <DataPlanePolicyArn>
   # or: aws iam attach-role-policy --role-name <your-role> --policy-arn <DataPlanePolicyArn>
   ```

3. **Run the ingest notebook once** to create and populate the index. The vector *bucket* is created by `cdk deploy`, but the vector *index* is created by the notebook — the app refuses to start until the index exists.

   Open and run `s3vectors_ingest.ipynb` end to end. It measures the Nova 2 embedding dimension, creates the index with that dimension, uploads catalog images to `s3://<bucket>/catalog/`, and writes their embeddings to S3 Vectors.

4. **Start the UI**:

   ```bash
   streamlit run frontend/app.py
   ```

   A browser tab opens with the Fashion Assistant chat. Upload an image (optional) and ask a fashion question.

## Configuration

`config.yml` holds the non-secret runtime configuration:

- `primary_region` / `image_region` / `embedding_region`: the two-region split described above (defaults `us-east-1` / `us-west-2` / `us-east-1`). Override at runtime with `FASHION_PRIMARY_REGION` / `FASHION_IMAGE_REGION` / `FASHION_EMBEDDING_REGION`.
- `models`: the Bedrock model/profile ids for the brain, embeddings, text-to-image (core + ultra), inpaint, and outpaint.
- `inference_profile_fanout_regions`: the regions the Claude `us.` inference profile fans out to. This is a verified snapshot; re-check it with `GetInferenceProfile` if the profile id changes.
- `vector_index`:
  - `bucket_name` / `index_name`: the S3 Vectors bucket/index names.
  - `expected_dimension`: defaults to `null`. When null, the notebook measures and uses the model's native embedding dimension and the app skips the dimension check. Set it to pin an exact dimension.
  - `distance_metric` (default `cosine`) and `top_k` (default `3`).

Data-plane names (`BucketName`, `VectorBucketName`, `VectorIndexName`) are read from `variables.json`, produced by `cdk deploy --outputs-file variables.json`.

## Testing

```bash
python3 -m pytest -q
```

All AWS and HTTP calls are mocked; the suite makes no real AWS or network calls.

## Cleanup

To avoid ongoing costs, tear everything down with:

```bash
cdk destroy
```

The S3 Vectors custom resource best-effort deletes the index and the vector bucket; the image buckets are set to auto-delete on stack destroy.
