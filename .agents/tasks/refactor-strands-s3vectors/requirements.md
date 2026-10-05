# Requirements: Refactor Fashion Assistant to Strands Agents + S3 Vectors

## Summary

The Fashion Assistant is an AWS CDK sample. Today it deploys a managed Amazon Bedrock
Agent (`CfnAgent` + `CfnAgentAlias`) whose five action-group operations run in a Lambda
function, backs image lookup with OpenSearch Serverless, and uses Amazon Titan models
(Titan Image Generator, Titan Multimodal Embeddings). A Streamlit UI calls the agent
through `bedrock-agent-runtime` and parses an `<generated_s3_uri>` tag out of the
agent's text to find the result image.

This refactor converts the project to a **local app, cloud data-plane** architecture:

1. The agent runs **in-process** inside the Streamlit app using the **Strands Agents
   SDK**. The managed Bedrock Agent, its alias, its execution role, and the
   action-group Lambda are deleted. The five operations become Strands `@tool`
   functions. `prompt.py` instructions become the agent `system_prompt`.
2. Image retrieval moves from OpenSearch Serverless to **Amazon S3 Vectors** (the
   `s3vectors` boto3 service): a vector bucket + vector index, written with
   `PutVectors`, queried with `QueryVectors`. The matching image's S3 URI is stored in
   vector **metadata** (no base64 blobs in the vector store).
3. Models are modernized to verified-live Bedrock models (verified via
   `ListFoundationModels` 2026-10-05). Image generation requires **us-west-2** while all
   other model calls use **us-east-1**; both regions are configurable.
4. CDK provisions **only the data plane** (S3 image bucket, access-log bucket, S3
   Vectors bucket + index, and an IAM policy/role). No ECS / App Runner / Lambda hosting
   for the UI. The app runs locally and reads resource names from
   `variables.json`.

Supporting modernization: `requirements.txt` → `pyproject.toml` with pinned versions and
separated app vs infra dependency groups; typed config/settings and structured logging;
unit tests per Strands tool with boto3 mocked; the ingest notebook rewritten for Nova 2
embeddings + `PutVectors`; README updated.

**Assumptions** (to be confirmed in design — see Open Questions):
- Primary deploy region is `us-east-1`; image-generation region is `us-west-2`. These
  are the user's stated choices and are configurable.
- Default agent brain: `anthropic.claude-sonnet-4-5-20250929-v1:0`.
- Default embeddings: `amazon.nova-2-multimodal-embeddings-v1:0` (replaces Titan
  Multimodal). Its output dimension is **not** assumed to be 1024 — it must be verified
  and the S3 Vectors index dimension set to match.
- Default text-to-image: `stability.stable-image-core-v1:1`, with
  `stability.stable-image-ultra-v1:1` allowed as an alternative. Inpaint:
  `stability.stable-image-inpaint-v1:0`. Outpaint: `stability.stable-outpaint-v1:0`.
- The project stays a Python CDK app (`app.py` + `cdk.json` `"app": "python app.py"`).
- Target Python version is 3.12 (matching the existing Lambda runtime baseline), unless
  Strands SDK requires otherwise — resolve in design.

## Functional Requirements

### FR-1: Strands in-process agent
- FR-1.1 The agent is constructed and invoked **in-process** inside the Streamlit app
  via the Strands Agents SDK. No `bedrock-agent-runtime` calls; no managed agent
  resource.
- FR-1.2 The agent brain is a Bedrock model (default
  `anthropic.claude-sonnet-4-5-20250929-v1:0`) configured through the Strands Bedrock
  model provider pinned to the primary region (`us-east-1`, configurable).
- FR-1.3 The system prompt is sourced from the existing `prompt.py` instructions,
  adapted so it no longer tells the model to emit `<generated_s3_uri>` tags.
- FR-1.4 The agent exposes exactly these five tools as Strands `@tool` functions, each
  with a clear docstring and type-hinted signature/return:
  - `get_weather` — resolve a location to coordinates and return a short weather
    description (Open-Meteo, unchanged external APIs).
  - `generate_image` — text-to-image via Stability, pinned to the image-generation
    region (`us-west-2`); uploads result to the image bucket; returns the S3 URI.
  - `image_lookup` — embed the input (image S3 URI and/or text) with Nova 2, query S3
    Vectors, return the matching image's S3 URI from metadata.
  - `inpaint` — Stability inpaint on an input image + mask; uploads result; returns S3
    URI.
  - `outpaint` — Stability outpaint on an input image + mask; uploads result; returns S3
    URI.
- FR-1.5 Tools return **structured results containing S3 URIs** (not tag-wrapped text).
  The UI reads the S3 reference directly from the tool/agent result rather than
  regex-parsing `<generated_s3_uri>`.
- FR-1.6 The Streamlit UI retains its current behaviors: image upload to S3, chat
  history, trace display, and rendering/downloading the result image. Trace display is
  adapted to Strands' event/callback model (exact mechanism resolved in design).

### FR-2: S3 Vectors retrieval
- FR-2.1 A CDK construct creates an **S3 vector bucket** and a **vector index** with
  dimension matched to the Nova 2 embedding output and **cosine** distance metric.
- FR-2.2 Ingestion uses `s3vectors` `PutVectors`; lookup uses `QueryVectors` with a
  configurable top-K and returns metadata.
- FR-2.3 Vector metadata stores the matching image's **S3 reference** (URI/bucket+key),
  not a base64 image blob.
- FR-2.4 `image_lookup` returns the S3 URI(s) of the top matches; the UI downloads and
  renders from S3 as it does today.
- FR-2.5 The old OpenSearch stack (`opensearchserverless_stack.py`), the OpenSearch
  Lambda layer (`components/layers/opensearch_layer`), and all `opensearch-py` /
  `aoss` / `AWSV4SignerAuth` usage and the `RETRIEVE_THRESHOLD`/kNN query code are
  removed.

### FR-3: Model modernization
- FR-3.1 Agent brain default `anthropic.claude-sonnet-4-5-20250929-v1:0` (us-east-1,
  configurable).
- FR-3.2 Embeddings default `amazon.nova-2-multimodal-embeddings-v1:0` (us-east-1),
  replacing `amazon.titan-embed-image-v1`. Index dimension = Nova 2 output dimension
  (verified, not assumed).
- FR-3.3 Text-to-image default `stability.stable-image-core-v1:1`, alternative
  `stability.stable-image-ultra-v1:1`, both invoked in **us-west-2** via a Bedrock
  runtime client pinned to that region.
- FR-3.4 Inpaint `stability.stable-image-inpaint-v1:0` (us-east-1); outpaint
  `stability.stable-outpaint-v1:0` (us-east-1).
- FR-3.5 Stability request/response payloads differ from the old Titan
  `taskType`/`maskPrompt` payloads and must be built to the Stability schema (resolved
  in design). The old Titan `TEXT_IMAGE`/`INPAINTING`/`OUTPAINTING` payloads are
  removed.

### FR-4: Data-plane-only CDK
- FR-4.1 CDK provisions: the S3 image bucket + access-log bucket (keep existing config:
  SSL enforced, block public access, access logging, auto-delete), the S3 Vectors bucket
  + index, and an IAM policy/role.
- FR-4.2 The IAM policy grants:
  - `bedrock:InvokeModel` in **both** us-east-1 and us-west-2 (scoped to the configured
    foundation models),
  - `s3:GetObject` / `s3:PutObject` on the image bucket,
  - `s3vectors:PutVectors` / `s3vectors:QueryVectors` plus the describe/get operations
    needed for the vector resources.
- FR-4.3 `CfnAgent`, `CfnAgentAlias`, the agent execution role
  (`AmazonBedrockExecutionRoleForAgents_FashionAgent`), the action-group Lambda
  (`FashionAgentLambda`) and its role/policies, the Pillow/requests Klayers, and the
  schema-based action-group wiring are removed. `FashionAgent_Schema.json` is removed (or
  explicitly retired) since there is no managed agent to consume it.
- FR-4.4 Primary deploy region `us-east-1`.
- FR-4.5 `cdk deploy --outputs-file variables.json` continues to work and emits the
  image bucket name plus the S3 Vectors bucket name and index name that the app and
  ingest notebook read.

### FR-5: Config, logging, packaging, tests, docs
- FR-5.1 Replace `requirements.txt` with `pyproject.toml` using **pinned** versions, and
  separate dependency groups for the app (streamlit, strands-agents,
  strands-agents-tools, boto3, pillow, requests, pyyaml) vs CDK/infra
  (aws-cdk-lib, constructs, cdk-nag; drop `aws-cdk-aws-lambda-python-alpha` and
  `opensearch-py`).
- FR-5.2 Introduce a typed config/settings object carrying the region split (primary +
  image-gen region), model IDs, bucket name, vector bucket name, and index name, sourced
  from `config.yml` and `variables.json`.
- FR-5.3 Add structured logging (replacing bare `print(...)` and ad-hoc logging) across
  the tools and app.
- FR-5.4 Add unit tests for each of the five Strands tools with boto3 (and external HTTP)
  **mocked** — no real AWS calls.
- FR-5.5 Rewrite `opensearch_ingest.ipynb` to use Nova 2 embeddings + `PutVectors` with
  the image's S3 reference in metadata (no OpenSearch, no base64 in the vector store).
  Renaming the notebook to reflect S3 Vectors is at the design's discretion.
- FR-5.6 Update `README.md`: Strands in-process agent, S3 Vectors, the two-region note,
  the exact "enable model access" list, and the deploy + run steps.
- FR-5.7 Update `config.yml` to the new shape (regions, model IDs, vector bucket/index
  names) and remove the `opensearch` and `foundation_model`/`embeddingSize` legacy keys.

## Non-Functional Requirements

- NFR-1 **No hardcoded credentials.** Use the default boto3 credential chain and
  per-service/per-region clients. No profile names baked into committed code (the ingest
  notebook's `profile_name="alexhrn-Admin"` must be removed/parameterized).
- NFR-2 **Two-region split is configurable**, not hardcoded. Image generation defaults to
  us-west-2, everything else to us-east-1; both overridable via config.
- NFR-3 **Least-privilege IAM**, preserving the existing cdk-nag suppression discipline
  (wildcards only where a service has no resource ARN, with documented suppressions).
- NFR-4 **Do-not-touch boundary:** all work stays within the worktree
  `/Users/erictole/demo/fashion-assistant-agent/.worktrees/refactor-strands-s3vectors`.
  Nothing outside `/Users/erictole/demo/fashion-assistant-agent` is modified.
- NFR-5 **No real AWS calls and no `cdk deploy`** during implementation/verification.
  Tests mock all network and AWS SDK calls.
- NFR-6 Preserve existing S3 image bucket security posture (SSL enforced, public access
  blocked, server access logging, auto-delete for the sample).

## Acceptance Criteria

1. `cdk synth` succeeds from the worktree root and the synthesized template contains the
   S3 image bucket, access-log bucket, S3 Vectors bucket + index, and the data-plane IAM
   role/policy.
2. The synthesized template contains **no** `AWS::Bedrock::Agent`,
   `AWS::Bedrock::AgentAlias`, agent execution role, or action-group Lambda resources,
   and **no** OpenSearch Serverless resources.
3. The IAM policy in the synthesized template grants `bedrock:InvokeModel` for both
   `us-east-1` and `us-west-2`, `s3:GetObject`/`s3:PutObject` on the image bucket, and
   `s3vectors:PutVectors`/`QueryVectors` (plus required describe/get) on the vector
   resources.
4. `pytest` passes with a unit test per tool (`get_weather`, `generate_image`,
   `image_lookup`, `inpaint`, `outpaint`), each asserting correct request construction
   and structured-S3-URI return, with boto3 and HTTP fully mocked — zero real AWS/network
   calls.
5. Import/smoke checks pass: the Streamlit app module, the Strands agent module, the
   tools module, and the CDK app import without error (SDKs installed from
   `pyproject.toml`). `python app.py` under `cdk synth` runs cleanly.
6. `grep` finds no remaining references to `bedrock-agent-runtime`, `opensearch`,
   `aoss`, `AWSV4SignerAuth`, `titan`, `<generated_s3_uri>`, `CfnAgent`, or
   `AmazonBedrockExecutionRoleForAgents` in the active source (notebook/README updated
   too).
7. The S3 Vectors index dimension in the CDK construct equals the verified Nova 2
   embedding output dimension (a magic `1024` is not left in place unverified).
8. `generate_image` uses a Bedrock runtime client pinned to `us-west-2`; all other model
   invocations use the `us-east-1` client. Verified by the unit tests asserting the
   `region_name` used per client.
9. `pyproject.toml` exists with pinned versions and separate app vs infra groups; the old
   `requirements.txt` and OpenSearch layer requirements are removed.
10. `variables.json` outputs (produced by `cdk deploy --outputs-file`, verified at synth
    time via `CfnOutput` presence) include image bucket name, vector bucket name, and
    index name; the app and notebook read these keys.
11. No hardcoded credentials/profiles remain in any committed file.
12. README reflects the Strands in-process agent, S3 Vectors, two-region requirement, the
    model-access enablement list, and local deploy + run steps.

## Open Questions (must be resolved in design)

1. **Nova 2 multimodal embeddings invoke shape + dimension.** Exact
   `invoke_model` request body for `amazon.nova-2-multimodal-embeddings-v1:0` (field
   names for image bytes/text, any config block), the response field holding the vector,
   and the **output dimension** (which sets the S3 Vectors index dimension). Must be
   verified via the Bedrock MCP server / model docs, not assumed to be 1024.
2. **Stability invoke bodies + response fields.** Exact `invoke_model` request bodies for
   `stable-image-core-v1:1` / `stable-image-ultra-v1:1` (text-to-image),
   `stable-image-inpaint-v1:0` (inpaint), and `stable-outpaint-v1:0` (outpaint) — these
   differ from the old Titan `taskType`/`maskPrompt`/`maskImage` payloads — and the
   base64 image field(s) in each response. Clarify how masks are supplied (mask image vs
   prompt) for inpaint/outpaint under Stability.
3. **S3 Vectors operation shapes.** Exact `s3vectors` API shapes and parameter names for
   `CreateVectorBucket`, `CreateIndex` (dimension, distance metric = cosine, metadata
   config), `PutVectors` (vector + metadata payload), and `QueryVectors` (top-K + metadata
   return), and whether the CDK path uses an L1 CFN resource, a custom resource, or
   another mechanism for the vector bucket/index.
4. **Strands usage pattern.** Confirm the correct `strands-agents` /
   `strands-agents-tools` APIs: `Agent` construction with `system_prompt` and `tools`,
   the `@tool` decorator contract (how structured results/S3 URIs are returned and read),
   and the Bedrock model provider class and its `region_name` parameter (so the brain
   runs in us-east-1 while `generate_image` uses a separately pinned us-west-2 Bedrock
   client). Also: how to surface agent reasoning/trace to the Streamlit UI.
5. **Target Python version / SDK compatibility.** Confirm the Python version required by
   `strands-agents` and keep it consistent across `pyproject.toml` and any remaining
   runtime assumptions.
6. **Schema file disposition.** Confirm whether `FashionAgent_Schema.json` is deleted
   outright or kept as reference documentation, given no managed agent consumes it.

## Out of Scope

- Any UI hosting infrastructure (ECS, App Runner, Lambda, container builds). The UI runs
  locally.
- Running `cdk deploy` or making real AWS/model calls during this work.
- Changes to the external weather APIs (Open-Meteo geocoding/forecast) beyond moving the
  logic into the `get_weather` tool.
- The sample dataset source and any image-dataset licensing changes.
- Multi-account / cross-account access patterns.
- Any files or directories outside the worktree.
