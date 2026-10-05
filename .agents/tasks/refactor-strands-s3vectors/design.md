# Design: Refactor Fashion Assistant to Strands Agents + S3 Vectors

## Overview

This refactor converts the Fashion Assistant from a managed-Bedrock-Agent architecture
into a **local app, cloud data-plane** architecture. The agent is reconstructed
in-process inside the Streamlit app using the **Strands Agents SDK**: the five
action-group operations become Strands `@tool` functions, the `prompt.py` text becomes
the agent `system_prompt`, and the managed `CfnAgent`/`CfnAgentAlias`, its execution
role, the action-group Lambda, and all OpenSearch Serverless resources are deleted. Image
retrieval moves to **Amazon S3 Vectors** (the `s3vectors` boto3 service): a vector bucket
plus a vector index, written with `PutVectors`, queried with `QueryVectors`, with the
matching image's S3 URI stored in vector metadata (no base64 blobs). Models are
modernized to Claude Sonnet 4.5 (agent brain), Amazon Nova 2 multimodal embeddings, and
Stability Stable Image models for generation / inpaint / outpaint. CDK provisions only the
data plane; the UI runs locally and reads resource names from `variables.json`.

The locked technology stack: Python **>=3.12,<3.14** (floor 3.12; import-verified on the
local 3.13 interpreter — see Finding 7), Strands Agents SDK (`strands-agents`,
`strands-agents-tools`), `boto3`/`botocore`, AWS CDK v2 (`aws-cdk-lib` 2.150.0 +
`constructs` + `cdk-nag`), Streamlit, Pillow, requests, PyYAML, pytest. Packaging moves
from `requirements.txt` to `pyproject.toml`.

## Verified API / payload facts

These were verified against the **botocore service models bundled on this machine**
(botocore 1.43.107 / boto3 1.43.107), the installed Strands SDK (strands-agents 1.23.0),
and **live Bedrock control-plane calls** (`ListFoundationModels` / `GetFoundationModel` /
`GetInferenceProfile`) re-run read-only against account `875692608981` during this
revision pass. No `invoke_model` call and no `cdk deploy` were made (NFR-5).

**Verification provenance (what is proven vs. what is `[DOC]`), stated once up front so
the implementer knows exactly which values are load-bearing:**

- **Live-verified (control plane, this pass):** existence, per-region availability, input/
  output modalities, and inference type of all six model ids; the `us.` inference-profile
  fan-out region set; and the `s3vectors` and `bedrock-runtime` **operation** shapes
  (operation names, required/optional params, enums) from the botocore service models.
  These are not assumptions — the exact command output is reproduced below.
- **`[DOC]` / UNVERIFIED (cannot be verified without invoking the model, which NFR-5
  forbids):** the JSON **inside** `invoke_model`'s `body` for Nova 2 and for the three
  Stability models (field names, enum string values), and — critically — **Nova 2's output
  embedding dimension**. botocore models `InvokeModel` only as an opaque `blob`; the
  Bedrock control plane (`GetFoundationModel`, verified below) returns **no** body schema
  and **no** embedding dimension; and no Bedrock model-card MCP server is configured on
  this machine. Every such value is marked `[DOC]` **inline at its point of use** below,
  and the design is structured so that each one is isolated to a single module and is
  either (a) re-confirmed at first integration run or (b) turned into a **checked runtime
  invariant** (the embedding dimension — see Finding 1 resolution). No `[DOC]` value is
  allowed to be load-bearing for a `cdk deploy`-time decision without a runtime check.

### Verified model inventory — live `ListFoundationModels` / `GetInferenceProfile` (re-run this pass)

Every default `modelId` was confirmed present via `ListFoundationModels` in both
us-east-1 and us-west-2, and each model's **supported inference type and modalities** were
read live. The raw results of the re-run are:

```
REGION us-east-1
  anthropic.claude-sonnet-4-5-20250929-v1:0: in=[TEXT,IMAGE] out=[TEXT]       inf=[INFERENCE_PROFILE]
  amazon.nova-2-multimodal-embeddings-v1:0 : in=[TEXT,IMAGE,AUDIO,VIDEO] out=[EMBEDDING] inf=[ON_DEMAND]
  stability.stable-image-core-v1:1         : ABSENT
  stability.stable-image-ultra-v1:1        : ABSENT
  stability.stable-image-inpaint-v1:0      : in=[TEXT,IMAGE] out=[IMAGE]      inf=[INFERENCE_PROFILE]
  stability.stable-outpaint-v1:0           : in=[TEXT,IMAGE] out=[IMAGE]      inf=[INFERENCE_PROFILE]
REGION us-west-2
  anthropic.claude-sonnet-4-5-20250929-v1:0: in=[TEXT,IMAGE] out=[TEXT]       inf=[INFERENCE_PROFILE]
  amazon.nova-2-multimodal-embeddings-v1:0 : ABSENT
  stability.stable-image-core-v1:1         : in=[TEXT] out=[IMAGE]            inf=[ON_DEMAND]
  stability.stable-image-ultra-v1:1        : in=[TEXT] out=[IMAGE]            inf=[ON_DEMAND]
  stability.stable-image-inpaint-v1:0      : in=[TEXT,IMAGE] out=[IMAGE]      inf=[INFERENCE_PROFILE]
  stability.stable-outpaint-v1:0           : in=[TEXT,IMAGE] out=[IMAGE]      inf=[INFERENCE_PROFILE]
```

`GetFoundationModel` for Nova 2 returns `inputModalities=[TEXT,IMAGE,AUDIO,VIDEO]`,
`outputModalities=[EMBEDDING]`, `inferenceTypesSupported=[ON_DEMAND]`, lifecycle ACTIVE
(startOfLife 2025-10-28) — and **no embedding-dimension field and no body schema** (this
is the direct evidence behind the Finding 1 resolution: the control plane cannot tell us
the dimension). This is the resolution of requirements §Summary #3 and review Findings 2
and 6.

| Model id (config key) | Present in | Input→Output | `inferenceTypesSupported` | How it must be invoked |
|---|---|---|---|---|
| `anthropic.claude-sonnet-4-5-20250929-v1:0` (agent) | us-east-1, us-west-2 | TEXT,IMAGE → TEXT | **INFERENCE_PROFILE** (no ON_DEMAND) | via profile **`us.anthropic.claude-sonnet-4-5-20250929-v1:0`** |
| `amazon.nova-2-multimodal-embeddings-v1:0` (embeddings) | **us-east-1 only** (absent in us-west-2) | TEXT,IMAGE,AUDIO,VIDEO → EMBEDDING | **ON_DEMAND** | bare id, region us-east-1 |
| `stability.stable-image-core-v1:1` (text→image) | **us-west-2 only** (absent in us-east-1) | TEXT → IMAGE | **ON_DEMAND** | bare id, region us-west-2 |
| `stability.stable-image-ultra-v1:1` (text→image alt) | **us-west-2 only** | TEXT → IMAGE | **ON_DEMAND** | bare id, region us-west-2 |
| `stability.stable-image-inpaint-v1:0` (inpaint) | us-east-1, us-west-2 | TEXT,IMAGE → IMAGE | **INFERENCE_PROFILE** | via profile **`us.stability.stable-image-inpaint-v1:0`** |
| `stability.stable-outpaint-v1:0` (outpaint) | us-east-1, us-west-2 | TEXT,IMAGE → IMAGE | **INFERENCE_PROFILE** | via profile **`us.stability.stable-outpaint-v1:0`** |

Key verified consequences (these drive several decisions below):

1. **The outpaint id prefix really is `stable-outpaint` (not `stable-image-outpaint`).**
   Confirmed by `ListFoundationModels`. The implementer must **not** "correct" it.
2. **Nova 2 embeddings is us-east-1 only** in this account and is **ON_DEMAND** → the
   embeddings tool pins the `us-east-1` client with the bare id. Good: it co-locates with
   the primary region.
3. **stable-image-core / ultra are us-west-2 only and TEXT-input only** → text-to-image
   must run in `us-west-2` (confirms the two-region split, FR-3.3) and `generate_image`
   must send **text only** (no image input).
4. **Claude Sonnet 4.5, inpaint, and outpaint are INFERENCE_PROFILE-only** — the bare
   `foundation-model` id is **not** directly invocable (ON_DEMAND is unsupported). They
   must be invoked through the **system-defined cross-region inference profile** id
   (the `us.` prefix). `GetInferenceProfile` shows the `us.` profiles fan out to
   **us-east-1, us-east-2, us-west-2**. This is the resolution of Finding 6 and changes
   both the invoked ids and the IAM policy (see §Settings and §IAM).

Verified inference-profile ids (`GetInferenceProfile` re-run this pass returned
`type=SYSTEM_DEFINED` and fan-out region set **{us-east-1, us-east-2, us-west-2}** for all
three): `us.anthropic.claude-sonnet-4-5-20250929-v1:0`,
`us.stability.stable-image-inpaint-v1:0`, `us.stability.stable-outpaint-v1:0`.
(A `global.anthropic.claude-sonnet-4-5-...` profile also exists but is not used; we pin
the regional `us.` profile to keep the fan-out set small and IAM-scopable.)

### S3 Vectors (`s3vectors`, API version `2025-07-15`) — VERIFIED via botocore

- **CreateVectorBucket** — required: `vectorBucketName` (string). Optional:
  `encryptionConfiguration {sseType, kmsKeyArn}`, `tags`. Output: `vectorBucketArn`.
- **CreateIndex** — required: `indexName` (string), `dataType` (enum, only `float32`),
  `dimension` (integer), `distanceMetric` (enum: `euclidean` | `cosine`). Optional:
  `vectorBucketName` **or** `vectorBucketArn`, `metadataConfiguration
  {nonFilterableMetadataKeys: list<string>}`, `encryptionConfiguration`, `tags`. Output:
  `indexArn`. → We use `distanceMetric="cosine"`, `dataType="float32"`.
- **PutVectors** — required: `vectors` (list). Optional: `vectorBucketName` + `indexName`
  (or `indexArn`). Each `vectors[]` item is a structure with required `key` (string) and
  `data` (structure with a single `float32` list member), plus optional `metadata`
  (free-form structure / document). Output: empty.
  Item shape: `{"key": <str>, "data": {"float32": [<floats>]}, "metadata": {...}}`.
- **QueryVectors** — required: `topK` (integer), `queryVector` (structure with a single
  `float32` list member). Optional: `vectorBucketName` + `indexName` (or `indexArn`),
  `filter` (document), `queryMode` (enum `CLASSIC` | `ENHANCED`), `returnMetadata`
  (bool), `returnDistance` (bool), `nextToken`. Output: `vectors` (list of
  `{distance, key, metadata}`), `distanceMetric`, `nextToken`.
  Call shape: `query_vectors(vectorBucketName=.., indexName=.., topK=k,
  queryVector={"float32": [...]}, returnMetadata=True, returnDistance=True)`.
- **GetVectors** / **ListVectors** also exist (keys-based fetch; segmented list) — not
  needed by the app but available to the notebook.

### bedrock-runtime `InvokeModel` — VERIFIED via botocore

- Input required: `modelId`. Relevant optional: `body` (blob), `contentType` (string),
  `accept` (string). Output: `body` (blob, read via `response["body"].read()`),
  `contentType`. The per-model request/response **JSON inside `body` is opaque to
  botocore** — the shapes below are `[DOC]`.

### Amazon Nova 2 multimodal embeddings — `amazon.nova-2-multimodal-embeddings-v1:0`

**Verification status: the `invoke_model` body/response below and the output dimension are
`[DOC]` / UNVERIFIED.** `GetFoundationModel` (re-run this pass) returns no body schema and
no dimension; botocore models the body as an opaque blob; NFR-5 forbids invoking the model
to probe it; no model-card MCP server is configured. Every field value and the dimension
are therefore documentation-sourced and are treated as **provisional** by the design. The
Finding 1 resolution (below) removes the dependency of any deploy-time decision on these
unverified values.

- **Region:** `embedding_region`, a **dedicated setting defaulting to us-east-1** (see
  Finding 2 resolution in §Settings). Nova 2 is us-east-1-only (verified ABSENT in
  us-west-2), so this region is **not** tied to the overridable `primary_region`.
- **Request body** `[DOC]` (`invoke_model` `body`, JSON — field names provisional):
  ```json
  {
    "taskType": "SINGLE_EMBEDDING",                       // [DOC]
    "singleEmbeddingParams": {                            // [DOC]
      "embeddingDimension": 1024,                         // [DOC] value sent, see dimension note
      "embeddingPurpose": "GENERIC_INDEX",                // [DOC] enum string, provisional
      "text":  { "truncationMode": "END", "value": "<text>" },  // [DOC]
      "image": { "format": "jpeg", "source": { "bytes": "<base64>" } }  // [DOC]
    }
  }
  ```
  Provide `text`, `image`, or both. `embeddingPurpose` is **`GENERIC_INDEX`** for ingested
  catalog vectors and **`GENERIC_RETRIEVAL`** for query-time embedding — pinned to a single
  value each (resolves the old "X or Y" ambiguity), but **both string values are `[DOC]`**
  (marked inline above) and a wrong value will not raise a shape error under mocked tests;
  it only surfaces against the live model. They are single module-local constants in
  `embeddings.py`. See the **integration smoke test (Finding 4 resolution)** in
  §Testability that catches a wrong purpose/field by asserting a known catalog image
  round-trips to a sensible nearest neighbor.
- **Response body** `[DOC]` (JSON): the embedding vector is read from
  `embeddings[0].embedding` (list of floats): `json.loads(resp["body"].read())
  ["embeddings"][0]["embedding"]`. The response **path is provisional**; if it differs,
  only `embeddings.py` + the notebook change.

- **Output dimension — NOT assumed, and NOT trusted from config at deploy time
  (Finding 1 resolution).** The prior design claimed Nova 2 is a Matryoshka model whose
  length is request-selectable from {3072,1024,384,256} and that a `config.yml` literal
  (1024) could therefore be fed to `CreateIndex` safely. **That premise is `[DOC]` and
  could not be verified** — if the model emits a fixed length, or if `embeddingDimension`
  is not honored, creating the S3 Vectors index at a config-literal 1024 would build an
  index of the wrong size at `cdk deploy`, and AC-7 ("index dimension equals the
  **verified** Nova 2 output dimension") would be unmet. We therefore **do not create the
  index from a config literal.** Instead the dimension becomes a **checked runtime
  invariant** derived from a real embedding, via a two-step bootstrap:

  1. **The S3 Vectors index is created by the ingest/bootstrap step, not by `cdk deploy`.**
     The CDK custom resource (see §S3 Vectors construct) creates the **vector bucket
     only**; it does **not** create the index. The first cell of the ingest notebook
     (and an equivalent `bootstrap_index()` helper the app calls lazily if the index is
     missing) performs **one real Nova 2 embedding** of a probe input, reads
     `dim = len(vector)`, and calls `CreateIndex(dimension=dim, distanceMetric="cosine",
     dataType="float32")`. The index dimension is thus **measured from the model**, never
     assumed.
  2. **`config.vector_index.dimension` becomes an optional *expectation*, not the source
     of truth.** If set, the bootstrap compares `dim` against it and **fails loudly**
     (`DimensionMismatchError`) if they disagree, so an operator who intends 1024 is told
     when the model actually emits something else, rather than silently getting a wrong
     index. If `embeddingDimension` turns out to be honored, `dim` equals the requested
     value and the config expectation passes; if it is ignored/fixed, `dim` reflects
     reality and the index is still correct.

  This converts the unverified assumption into an enforced invariant: the index dimension
  **equals the measured Nova 2 output length by construction** (AC-7 satisfied by
  measurement, not by a literal), and there is no `1024` magic constant feeding
  `CreateIndex`. The request still sends `embeddingDimension` from config as a *best-effort
  hint*; whether or not the model honors it, the index is sized to the actual returned
  vector. Switching the intended dimension is a config change **plus** a re-bootstrap
  (new index) — the mismatch check guarantees the two stay consistent.

### Stability Stable Image — Bedrock `invoke_model` `[DOC]`

All Stability Stable Image models on Bedrock share a common JSON body contract and return
base64 under an `images` array (plus a `finish_reasons`/`seeds` array). Output format is
selected with `output_format`; we **pin `output_format` to `"png"` across all three image
tools** (`generate_image`, `inpaint`, `outpaint`) — it is a single module constant in
`image_gen.py`, not a per-call knob (output_format↔key-extension consistency). Response:
`json.loads(resp["body"].read())["images"][0]` is a base64 PNG string; a non-empty
`finish_reasons[0]` other than `null` indicates filtered/failed content.

**Output-key extension rule (one rule, stated once):** because `output_format` is always
`png`, every uploaded result object is written with a `.png` key, and the key is **never**
derived from the source filename's extension. The three tools write:
- `generate_image` → `OutputImages/gen_<uuid>.png`
- `inpaint` → `OutputImages/<source-stem>_<uuid>.png`
- `outpaint` → `OutputImages/<source-stem>_<uuid>.png`

where `<source-stem>` is the source key's basename without its extension. This removes the
old `.jpg`-key-with-PNG-bytes mismatch (the source could be `foo.jpg` but the stored
result is always PNG bytes under a `.png` key). The upload also sets
`ContentType="image/png"` so the stored object's MIME matches its bytes.

**Verification status (Finding 3a resolution): every Stability field *value* below is
`[DOC]` / UNVERIFIED and marked inline.** The model ids, regions, and inference types are
live-verified; the JSON *inside* the body is not (same reason as Nova 2). Where the design
names a specific enum (`mask_source: "MASK_IMAGE_WHITE"`) or parameter model (outpaint
`left/right/up/down`), that choice is **provisional, not settled** — the implementer must
treat it as a placeholder gated behind first-integration re-confirmation, not as a fact.

- **Text-to-image** — `stability.stable-image-core-v1:1` (default, ON_DEMAND) /
  `stability.stable-image-ultra-v1:1` (alternative, ON_DEMAND), **region us-west-2**
  (`image_region`, configurable). Invoked with the **bare** model id (ON_DEMAND, verified).
  Request body `[DOC]`:
  ```json
  { "prompt": "<text>", "aspect_ratio": "1:1", "output_format": "png" }
  //          [DOC]     [DOC] value         [DOC] value
  ```
  **`seed` is omitted** so the service randomizes each generation (resolves NIT-6: a fixed
  `seed:0` made every same-prompt generation identical, which is wrong for a "generate me
  an outfit" UX). `aspect_ratio` defaults to `"1:1"` **`[DOC]`** (allowed set unverified);
  `negative_prompt` optional; `mode` defaults to text-to-image when no `image` is supplied.
- **Inpaint** — `stability.stable-image-inpaint-v1:0`, INFERENCE_PROFILE-only (verified),
  invoked via the **`us.` profile id** `us.stability.stable-image-inpaint-v1:0` through the
  **us-east-1** client (`primary_region`).
  Request body `[DOC]`:
  ```json
  {
    "prompt": "<text>",                         // [DOC]
    "image": "<base64 source>",                 // [DOC]
    "mask_source": "MASK_IMAGE_WHITE",          // [DOC] enum value provisional
    "mask": "<base64 mask>",                    // [DOC]
    "output_format": "png"                      // [DOC] value
  }
  ```
  Masks are supplied as a **mask image** (base64), not a text prompt — the key divergence
  from the old Titan `maskPrompt`. The `mask_source` enum value `MASK_IMAGE_WHITE` is
  `[DOC]` and must be re-confirmed. When no mask image is given, inpaint also accepts
  `search_prompt` (text describing the region to replace); the tool exposes both and sends
  whichever is provided (`mask` image wins). The old Titan `inPaintingParams.maskPrompt`
  payload is removed.
- **Outpaint** — `stability.stable-outpaint-v1:0`, INFERENCE_PROFILE-only (verified),
  invoked via the **`us.` profile id** `us.stability.stable-outpaint-v1:0` through the
  **us-east-1** client (`primary_region`). (The `stable-outpaint` prefix — not
  `stable-image-outpaint` — is live-verified; do not "correct" it.)
  Request body `[DOC]` (directional pixel extents rather than a mask):
  ```json
  {
    "prompt": "<text>",                         // [DOC]
    "image": "<base64 source>",                 // [DOC]
    "left": 0, "right": 512, "up": 0, "down": 0,// [DOC] parameter model provisional
    "output_format": "png"                      // [DOC] value
  }
  ```
  The directional-extent model (`left/right/up/down`, pixels) is `[DOC]`/provisional — it
  may instead use a `creativity` knob or a different extent shape; re-confirm at first run.
  The tool accepts the four extents with sensible defaults and omits zeros. The old Titan
  `taskType="OUTPAINTING"` payload is removed.

All Stability `[DOC]` field values are isolated in `tools/image_gen.py`; a correction
touches one module and its unit-test fixtures only. The unit tests assert the body is
*constructed* with these strings (and the correct `modelId`/`region_name`, see Finding 3b),
but cannot prove the strings are *right* — correctness is a first-integration concern.

### Strands Agents SDK — VERIFIED against installed package

- `from strands import Agent, tool`.
- `from strands.models import BedrockModel` — constructor is keyword-only:
  `BedrockModel(*, boto_session=None, boto_client_config=None, region_name=None,
  endpoint_url=None, **model_config)` where `model_config` keys include `model_id`,
  `temperature`, `max_tokens`, `top_p`, `streaming`, `stop_sequences`. → the brain is
  `BedrockModel(region_name=primary_region, model_id=agent_model_id, temperature=...)`.
- `Agent(model=<BedrockModel|str>, tools=[...], system_prompt=<str>, callback_handler=...)`.
- `@tool` decorator: `tool(func=None, description=None, inputSchema=None, name=None,
  context=False)` — used bare as `@tool` over a function whose **docstring becomes the
  tool description** and whose **type hints become the input schema**.
- **Tool return-value wrapping — VERIFIED from source**
  (`strands/tools/decorator.py::DecoratedFunctionTool._wrap_tool_result`): if a tool
  returns a `dict` that **already has both `status` and `content` keys**, Strands treats
  it as a ready `ToolResult` and only injects `toolUseId`. **Any other return value**
  (including a plain `{"status": "ok", "s3_uri": ...}` dict without a `content` key) is
  wrapped as `{"status": "success", "content": [{"text": str(result)}]}` — i.e. the dict
  is **stringified** into a `text` block, which is *not* machine-readable. This is the
  root cause behind Finding 1's secondary path being unreliable.
  **Decision:** our tools return the **native `ToolResult` shape** so the structured
  payload survives as JSON:
  ```python
  {"status": "success" | "error",
   "content": [{"json": {"result": "ok"|"not_found"|"error", "s3_uri": <str|None>, ...}}]}
  ```
  `ToolResultContent` (VERIFIED, `strands/types/tools.py`) is a `TypedDict(total=False)`
  with members `document`, `image`, `json` (`Any`), `text` — so a `{"json": {...}}`
  content block is valid and carries our structured dict losslessly. Readers (the hook
  below, and unit tests) pull `result["content"][0]["json"]`.
- Invocation: `result = agent(prompt)` returns an **`AgentResult`** dataclass with
  `stop_reason`, `message` (the final assistant `Message`), `metrics`, `state`,
  `interrupts`, `structured_output`. `str(result)` yields the concatenated text.
- **Hooks — VERIFIED from source** (`strands.hooks`, the stable non-experimental module in
  1.23.0): `Agent.__init__` accepts a `hooks=[...]` list (confirmed in the signature) of
  `HookProvider`s. A `HookProvider` implements `register_hooks(self, registry)` and calls
  `registry.add_callback(AfterToolCallEvent, cb)`. **`AfterToolCallEvent`** (VERIFIED
  dataclass) carries `selected_tool`, `tool_use` (`ToolUse` with `name`/`input`/
  `toolUseId`), `invocation_state`, **`result`** (a `ToolResult` TypedDict:
  `{content: list[ToolResultContent], status: "success"|"error", toolUseId: str}`), and
  `exception`. This is the supported, correct way to read **tool results** during a turn.
  `BeforeToolCallEvent` carries the tool name + input (for the trace).
- Streaming / trace: `Agent.stream_async(prompt)` is an async iterator of events; a
  synchronous **`callback_handler`** callable receives **streaming text / reasoning /
  `current_tool_use`** events (VERIFIED from `PrintingCallbackHandler.__call__`: kwargs are
  `reasoningText`, `data`, `complete`, `current_tool_use`). Crucially, `current_tool_use`
  holds the tool **name and input**, **not** the tool **result** — so `callback_handler`
  is used **only** for the trace pane's streamed reasoning + tool-call text, never for
  result extraction (details in §Streamlit UI).

## Technology stack (locked)

Python `>=3.12,<3.14` (import-verified on the local 3.13 interpreter — Finding 7);
`strands-agents` + `strands-agents-tools`; `boto3`/`botocore`;
`aws-cdk-lib` 2.150.0, `constructs`, `cdk-nag`; `streamlit`; `pillow`; `requests`;
`PyYAML`; `pytest` + `pytest-mock`. Dropped: `opensearch-py`, `requests_aws4auth`,
`aws-cdk-aws-lambda-python-alpha`, `tqdm` (notebook uses a simple counter).

## Target file layout

```
fashion-assistant-agent/
  app.py                         # CDK entry (unchanged role; stack wiring updated)
  cdk.json                       # "app": "python app.py" (profile line removed)
  config.yml                     # new shape (regions, model ids, vector bucket/index)
  pyproject.toml                 # replaces requirements.txt (app + infra groups)
  variables.json                 # produced by cdk deploy --outputs-file (gitignored)
  README.md                      # updated
  s3vectors_ingest.ipynb         # renamed from opensearch_ingest.ipynb
  components/
    stacks/
      fashion_agent_stack.py     # data-plane only
      s3_vectors_construct.py    # NEW: vector bucket + index via custom-resource provider Lambda
      s3vectors_provider/        # NEW: bundled asset for the provider Lambda
        index.py                 #   on_event handler calling boto3.client("s3vectors")
        requirements.txt         #   pinned boto3/botocore (s3vectors-capable floor)
    agent/                       # NEW (replaces components/bedrock_agent + components/lambda)
      __init__.py
      prompt.py                  # system_prompt (no <generated_s3_uri>)
      settings.py                # typed settings
      logging_config.py          # structured logging setup
      agent.py                   # build_agent(): BedrockModel + Agent + tools
      hooks.py                   # NEW: ToolResultCollector (HookProvider on AfterToolCallEvent)
      tools/
        __init__.py
        _result.py               # NEW: ok()/err() ToolResult-envelope builders
        weather.py               # get_weather
        image_gen.py             # generate_image, inpaint, outpaint (Stability)
        embeddings.py            # Nova 2 embedding helper
        image_lookup.py          # image_lookup (S3 Vectors query)
        s3_io.py                 # shared S3 upload/download/base64 helpers
  frontend/
    app.py                       # Streamlit UI (Strands in-process; reads structured URIs)
  tests/
    test_weather.py
    test_image_gen.py            # generate_image + inpaint + outpaint
    test_image_lookup.py
    test_settings.py
    conftest.py                  # fixtures: fake boto3 clients, settings
```

Removed: `components/bedrock_agent/FashionAgent_Schema.json` (deleted — see §Schema
disposition), `components/bedrock_agent/prompt.py` (moved to `components/agent/prompt.py`),
`components/lambda/` (whole tree), `components/layers/` (whole tree),
`components/stacks/opensearchserverless_stack.py`, `requirements.txt`,
`frontend/bedrock_agent.py`.

## Settings module (`components/agent/settings.py`)

A frozen `@dataclass` `Settings` is the single typed carrier of configuration, built by a
`load_settings()` factory that merges `config.yml` (static defaults) with `variables.json`
(CDK outputs) and allows env-var override of the two regions. Fields:

```python
@dataclass(frozen=True)
class Settings:
    primary_region: str          # default "us-east-1" (brain + inpaint/outpaint profiles, s3, s3vectors)
    image_region: str            # default "us-west-2" (text-to-image; Nova 2/stable-core are region-locked)
    embedding_region: str        # default "us-east-1" (Nova 2 is us-east-1-ONLY; see Finding 2)
    # Invocation ids below reflect the VERIFIED inference-type of each model:
    #   INFERENCE_PROFILE-only models carry the "us." cross-region profile id,
    #   ON_DEMAND models carry the bare foundation-model id.
    agent_model_id: str          # "us.anthropic.claude-sonnet-4-5-20250929-v1:0" (PROFILE)
    embedding_model_id: str      # "amazon.nova-2-multimodal-embeddings-v1:0" (ON_DEMAND, us-east-1)
    text_to_image_model_id: str  # "stability.stable-image-core-v1:1" (ON_DEMAND, us-west-2)
    inpaint_model_id: str        # "us.stability.stable-image-inpaint-v1:0" (PROFILE)
    outpaint_model_id: str       # "us.stability.stable-outpaint-v1:0" (PROFILE)
    expected_embedding_dimension: int | None  # OPTIONAL expectation, NOT the index source of truth
    image_bucket: str            # from variables.json BucketName
    vector_bucket: str           # from variables.json VectorBucketName
    vector_index: str            # from variables.json VectorIndexName
    top_k: int                   # default 3
```

Note the dimension field is renamed `expected_embedding_dimension` and is **optional**:
per the Finding 1 resolution it is a *check value* compared against the measured probe
embedding at bootstrap, not the value fed to `CreateIndex`. If `None`, the bootstrap
accepts whatever the model emits; if set, a mismatch raises `DimensionMismatchError`.

**Region reconciliation (Finding 2 resolution).** Embeddings are pinned to a **dedicated
`embedding_region`** (default us-east-1), decoupled from the overridable `primary_region`,
exactly mirroring how `image_region` is split out for text-to-image. This resolves the
contradiction where overriding `FASHION_PRIMARY_REGION` would have moved the embeddings
call to a region where Nova 2 does not exist. `load_settings()` additionally validates
regions against the known-availability map derived from the live inventory and raises
`ConfigError` if `embedding_region` is set to anything other than a region where Nova 2 is
present (today: us-east-1) or if `image_region` is set where stable-image-core/ultra are
absent — turning the "configurable" claim (NFR-2) into a *safely* configurable one that
fails fast on an impossible region rather than at first invoke.

Validation: `load_settings()` raises `ConfigError` (a module-local exception) with a clear
message if `config.yml` is missing a required model id, if `variables.json` lacks
`BucketName`/`VectorBucketName`/`VectorIndexName` (fatal — the app cannot run without
resources), or if a region is set where the pinned model is unavailable (above). Regions
default from `config.yml` and may be overridden by env vars `FASHION_PRIMARY_REGION` /
`FASHION_IMAGE_REGION` / `FASHION_EMBEDDING_REGION` (NFR-2). No profile names anywhere; the
default boto3 credential chain is used (NFR-1).

### Two-region client split

A small `clients.py` concern (kept inside `agent.py`/tools via a cached factory) builds:
- `bedrock_primary = boto3.client("bedrock-runtime", region_name=settings.primary_region)`
  — used by **inpaint/outpaint** via their profile ids. The Strands `BedrockModel` brain
  is built as `BedrockModel(region_name=settings.primary_region,
  model_id=settings.agent_model_id)` where `agent_model_id` is the **inference-profile id**
  `us.anthropic.claude-sonnet-4-5-20250929-v1:0` (Claude Sonnet 4.5 is
  INFERENCE_PROFILE-only; passing the bare `anthropic.*` id would fail at invoke).
- `bedrock_embedding = boto3.client("bedrock-runtime", region_name=settings.embedding_region)`
  — used **only** by the Nova 2 embedding helper with the bare `embedding_model_id`
  (ON_DEMAND, us-east-1). Decoupled from `primary_region` per Finding 2.
- `bedrock_image = boto3.client("bedrock-runtime", region_name=settings.image_region)`
  — used **only** by `generate_image` with the bare `text_to_image_model_id` (ON_DEMAND).
- `s3 = boto3.client("s3", region_name=settings.primary_region)`.
- `s3vectors = boto3.client("s3vectors", region_name=settings.primary_region)`.

**Explicit `modelId` passed to `invoke_model` (Finding 3b resolution).** Each image tool
calls `invoke_model(modelId=<id>, body=...)` with the id named below — stated here so the
implementer cannot regress to the bare id for the profile-only models:
- `generate_image` → `invoke_model(modelId=settings.text_to_image_model_id, ...)` on
  `bedrock_image` (bare `stability.stable-image-core-v1:1`, ON_DEMAND).
- `inpaint` → `invoke_model(modelId=settings.inpaint_model_id, ...)` on `bedrock_primary`,
  where `inpaint_model_id` is the **`us.`-prefixed profile id**
  `us.stability.stable-image-inpaint-v1:0`. Passing the bare `stability.*` id here would
  fail at invoke (ON_DEMAND unsupported).
- `outpaint` → `invoke_model(modelId=settings.outpaint_model_id, ...)` on
  `bedrock_primary`, with the **`us.`-prefixed** `us.stability.stable-outpaint-v1:0`.
- embedding helper → `invoke_model(modelId=settings.embedding_model_id, ...)` on
  `bedrock_embedding` (bare Nova 2 id).

Each tool receives its clients and `settings` by closure/partial at agent-build time (the
`@tool` functions are thin wrappers that call module functions taking explicit
`settings`/`clients` args, so they are directly unit-testable with injected fakes). The
unit tests assert **both** `region_name` **and** `modelId` per invocation (AC-8 extended):
`generate_image` uses `region_name="us-west-2"` + the bare core id; the embedding helper
uses `region_name="us-east-1"` + the bare Nova id; `inpaint`/`outpaint` use
`region_name="us-east-1"` + the **`us.`-prefixed** profile ids (so a regression to a bare
profile id is caught in CI, not only at invoke).

## Strands tool interfaces

All tools return the **native Strands `ToolResult` shape** carrying a structured `json`
content block (see the Strands verified-facts section for why a bare dict would be
stringified). Each S3 reference is a plain `s3://bucket/key` URI under the `s3_uri` key.
The common envelope is:

```python
{"status": "success" | "error",
 "content": [{"json": {"result": <"ok"|"not_found"|"error">, ...tool-specific fields...}}]}
```

`status` is the Strands-level success/error flag the event loop reads; the nested
`result` field inside the `json` block is the semantic outcome the UI hook and the model
consume (so a recoverable "no match" is `status="success"` + `result="not_found"`, keeping
the event loop healthy while telling the model to try `generate_image`). A tiny helper
`ok(**fields)` / `err(message)` in `tools/_result.py` builds these envelopes so every tool
is consistent and unit tests assert one shape. The docstrings below show the **nested
`json` payload** (what the model/UI see); the `@tool` wrapper functions return the full
envelope.

```python
@tool
def get_weather(location_name: str) -> dict:
    """Resolve a location name to coordinates and return current weather.
    Use when the user mentions a place and weather could influence the outfit.
    Args:
        location_name: City or place name from the user's request.
    Returns (json payload):
        {"result": "ok"|"not_found", "description": str, "temperature_f": float|None}
    """
```
Geocoding + forecast via Open-Meteo (unchanged external APIs), `requests` with a 10s
timeout. On geocode miss or non-200: `{"status": "not_found", "description": "<default>",
"temperature_f": None}` (recoverable; the agent proceeds with a default). No weather
"taskType" payload (that dead Titan code is dropped).

```python
@tool
def generate_image(prompt: str, weather: str | None = None) -> dict:
    """Generate a new fashion image from a text description (text-to-image).
    Args:
        prompt: Description of the garment/outfit to generate.
        weather: Optional weather phrase to make the outfit weather-appropriate.
    Returns (json payload):
        {"result": "ok"|"error", "s3_uri": str|None, "message": str}
    """
```
Builds the Stability text-to-image body (**text input only** — stable-image-core/ultra
accept TEXT only, verified), invokes `bedrock_image` (**us-west-2**, with the bare
`text_to_image_model_id` — ON_DEMAND, verified), decodes `images[0]`, uploads to
`s3://{image_bucket}/OutputImages/gen_<uuid>.png` with `ContentType="image/png"`, returns
the URI. If `weather` is set, appends "Make the clothing suitable for {weather}
conditions."

```python
@tool
def image_lookup(input_image_uri: str | None = None, input_query: str | None = None) -> dict:
    """Find the closest matching catalog image by image and/or text.
    Args:
        input_image_uri: s3:// URI of an uploaded image, or None.
        input_query: Text description of the desired item, or None.
    Returns (json payload):
        {"result": "ok"|"not_found"|"error", "s3_uri": str|None,
         "distance": float|None, "message": str}
    """
```
Requires at least one of the two inputs (else `not_found` with a message asking for an
image or description — recoverable). Embeds via Nova 2 (`embeddings.py`, on the
`bedrock_embedding` client pinned to `embedding_region`=us-east-1,
`embeddingPurpose="GENERIC_RETRIEVAL"` `[DOC]`), calls
`s3vectors.query_vectors(..., topK=settings.top_k, queryVector={"float32": vec},
returnMetadata=True, returnDistance=True)`, takes the top hit, reads the catalog image's
`s3_uri` **from the returned metadata**, and returns it directly (the UI downloads and
renders from S3). No base64 round-trip through the vector store (FR-2.3). No
`RETRIEVE_THRESHOLD`.

**Lookup-miss behavior (one explicit miss policy):** the legacy Lambda echoed the user's own
input image back on a lookup miss (`response["body"] = input_image`). That echo is
**intentionally removed**. On zero hits this tool returns `result="not_found"` with a
`s3_uri` of `None`; the **system prompt** (see §System prompt) steers the model to call
`generate_image` as the fallback, or to ask the user for more detail — the tool does not
silently return the input image. This keeps FR-2.4 ("returns the S3 URI(s) of the top
matches") honest (a miss has no match to return) and gives one, explicit miss policy.

```python
@tool
def inpaint(image_uri: str, prompt: str,
            mask_uri: str | None = None, search_prompt: str | None = None) -> dict:
    """Edit a masked region of an existing image to a new style (inpaint).
    Args:
        image_uri: s3:// URI of the source image.
        prompt: Description of the desired result in the masked region.
        mask_uri: Optional s3:// URI of a black/white mask image.
        search_prompt: Optional text describing the region to replace, used when no mask image is given.
    Returns (json payload):
        {"result": "ok"|"error", "s3_uri": str|None, "message": str}
    """

@tool
def outpaint(image_uri: str, prompt: str,
             left: int = 0, right: int = 0, up: int = 0, down: int = 0) -> dict:
    """Extend an image outward in one or more directions (outpaint).
    Args:
        image_uri: s3:// URI of the source image.
        prompt: Description of the surrounding scene to generate.
        left/right/up/down: Pixels to extend in each direction (>= 0).
    Returns (json payload):
        {"result": "ok"|"error", "s3_uri": str|None, "message": str}
    """
```
`inpaint`/`outpaint` download the source from S3, base64-encode, build the Stability
body, invoke `bedrock_primary` (**us-east-1**) through the verified **inference-profile
ids** `us.stability.stable-image-inpaint-v1:0` / `us.stability.stable-outpaint-v1:0`
(both are INFERENCE_PROFILE-only — ON_DEMAND is unsupported, verified), decode
`images[0]`, upload to `OutputImages/<source-stem>_<uuid>.png` (the pinned-png rule
above) with `ContentType="image/png"`, and return the URI. `inpaint` requires at least
one of `mask_uri`/`search_prompt` (else `error` with a message). `outpaint` requires at
least one extent > 0.

## System prompt (`components/agent/prompt.py`)

Sourced from the existing `prompt.py` instructions, adapted:
- Keep the fashion-only gate and the step guidance (weather → generate / lookup →
  inpaint/outpaint).
- **Remove step 5** entirely (the `<generated_s3_uri>` emission rule) and remove the
  `<answer>`/`<thinking>` tag framing, since tools now return structured URIs that the UI
  reads programmatically.
- Rename the referenced operations to the new tool names (`get_weather`,
  `generate_image`, `image_lookup`, `inpaint`, `outpaint`).
- Instruct the model to call `image_lookup` for "find similar", fall back to
  `generate_image` when lookup returns `not_found`, and to state the result plainly in
  prose (the UI finds the image via the tool result, not via text parsing).

## Streamlit UI (`frontend/app.py`)

- Replaces `frontend/bedrock_agent.py` (deleted). Builds the agent once per session via
  `build_agent(settings)` and stores it in `st.session_state`. New-chat resets the
  message history and rebuilds the agent (fresh conversation).
- Reads `BucketName`, `VectorBucketName`, `VectorIndexName` from `variables.json` through
  `load_settings()`.
- **Image upload (changed from today — called out explicitly).** The resize + `upload_fileobj`
  logic is kept, but two things change from the current `frontend/app.py` and are called
  out as changes, not "unchanged":
  - **Upload key prefix** is pinned to a deterministic **`uploads/<uuid>.png`** (the
    current code uses `blogpost/<uuid>.png`; `uploads/` is clearer and the prefix is now
    fixed so tests can assert it).
  - **Handoff to the agent** changes from the current malformed
    `<input_s3_uri>...<input_s3_uri>` tag wrap (both tags are open tags in the original)
    to **plain prose**: the user turn text becomes
    `f"{prompt}\n\nThe uploaded image is at {s3_uri}"` where
    `s3_uri = f"s3://{image_bucket}/uploads/<uuid>.png"`. The model reads the URI from
    prose and passes it to tools; no tag parsing anywhere.
- **Result image discovery (replaces `<generated_s3_uri>` regex) — hook-based extraction.**
  The UI registers a small `HookProvider` (`ToolResultCollector`) on the agent via
  `Agent(..., hooks=[collector])`. `ToolResultCollector.register_hooks` adds a callback on
  **`AfterToolCallEvent`** (verified stable event in `strands.hooks`, 1.23.0). Each
  callback appends `(event.tool_use["name"], event.result)` to a per-turn list, where
  `event.result` is the `ToolResult` TypedDict. After `result = agent(prompt)` returns,
  the UI scans the collected results **newest-first** for the first whose
  `result["content"][0]["json"]` has `result == "ok"` and a non-null `s3_uri`, then
  downloads/renders that image. Because our tools return the native
  `{"status","content":[{"json":{...}}]}` envelope (see Strands verified-facts), the
  `s3_uri` arrives as structured JSON, not stringified text. There is **no** reliance on
  `callback_handler` for results and **no** reliance on the model echoing the URI in prose
  (the old `response_parser`/`<generated_s3_uri>` path is deleted — AC-6). A unit/smoke
  test feeds a representative `AfterToolCallEvent`-style `ToolResult` through the collector
  and asserts the `s3_uri` is extracted.
- **Trace pane:** a `callback_handler` passed to the agent accumulates **streamed
  reasoning text** (`reasoningText`/`data` kwargs) and **tool-call announcements**
  (`current_tool_use` → tool name + input) into a markdown string shown in the existing
  Trace column; a `BeforeToolCallEvent` hook can additionally record the ordered tool-call
  list. This adapts the old `orchestrationTrace` display to Strands' event model (FR-1.6).
  The split is explicit: **`callback_handler` = trace text only; the `AfterToolCallEvent`
  hook = result extraction** — they never overlap.

## CDK data-plane stack (`components/stacks/fashion_agent_stack.py`)

Keeps the two S3 buckets exactly as today (SSL enforced, block-public-access, access
logging to the log bucket, `RemovalPolicy.DESTROY` + `auto_delete_objects` for the
sample — NFR-6). Adds the S3 Vectors construct and a single data-plane IAM **managed
policy** (no principal — see Finding 5 resolution).
Removes: `CfnAgent`, `CfnAgentAlias`, `AgentRole`
(`AmazonBedrockExecutionRoleForAgents_FashionAgent`), the action-group Lambda
(`FashionAgentLambda`) + its role/policies, the Pillow/requests Klayers, the
`PythonLayerVersion` OpenSearch layer, the schema load, and all OpenSearch wiring.

### S3 Vectors construct (`components/stacks/s3_vectors_construct.py`)

**Decision — how to create the vector bucket + index in CDK.** Three options were
considered:
1. **L2/L1 native construct** (`aws_cdk.aws_s3vectors`) — *not viable*: verified that
   `aws-cdk-lib` **2.150.0 does not ship an `aws_s3vectors` module**, and
   `AWS::S3Vectors::*` CloudFormation types are not available at this pinned version.
2. **Generic L1 `CfnResource`** of type `AWS::S3Vectors::VectorBucket` /
   `AWS::S3Vectors::Index` via the escape hatch — depends on the CloudFormation registry
   having those types in the target account/region; unverifiable here without a deploy
   and would make `cdk synth` emit types the pinned toolkit can't validate.
3. **`AwsCustomResource`** (from `aws_cdk.custom_resources`) that calls the `s3vectors`
   control-plane APIs on create/delete. Attractive because it synthesizes cleanly and
   needs no new CFN type — **but it has a verified, fatal SDK-vintage dependency**
   (below).
4. **A dedicated custom-resource provider Lambda packaging a pinned boto3/botocore that
   includes `s3vectors`.** The handler calls `boto3.client("s3vectors")` directly, so it
   runs against the *same verified SDK* (botocore ≥ 1.43.107, API version 2025-07-15) we
   confirmed locally — no dependency on the provider runtime's bundled SDK.

**Why Option 3 is rejected (SDK-vintage mismatch).** Verified against the installed toolkit:
`aws-cdk-lib` **2.150.0**'s `custom_resources.AwsCustomResource` executes against the
**AWS SDK for JavaScript v3** baked into the provider Lambda runtime (confirmed from the
bundled `custom_resources/__init__.py` docs: "SDK for JavaScript v3"). `s3vectors` is a
**2025-07-15** service and its v3 client (`@aws-sdk/client-s3vectors`) only exists in
recent v3 releases; the Lambda-runtime-bundled v3 vintage at this CDK version predates it,
so the `S3Vectors` call would fail **at deploy time** — and NFR-5 forbids `cdk deploy`
during this work, so `cdk synth` passing (AC-1) would *not* catch it. The
`install_latest_aws_sdk` prop (which *does* exist on `AwsCustomResource` in 2.150.0,
verified) installs the latest **v2** `aws-sdk` (per its own docstring), and v2 — now EOL —
never shipped an `s3vectors` client. So neither the default nor `install_latest_aws_sdk`
makes Option 3 safe. Option 3 is out.

**Chosen: Option 4 — custom provider Lambda with a pinned, s3vectors-capable boto3.** The
`S3VectorsConstruct`:
- Defines a Python `lambda.Function` (`runtime=python3.12`) whose code is a small
  `on_event` handler for the CloudFormation custom-resource lifecycle (Create/Update/
  Delete). Its dependencies pin **`boto3==<impl-time>` / `botocore>=1.43.107`** (the
  verified s3vectors-capable floor) bundled via `BundlingOptions` (pip install into the
  asset) **or** a `lambda.LayerVersion` carrying the pinned sdk — **choose the bundled
  asset** (`aws_lambda.Code.from_asset(..., bundling=...)`) so the handler and its SDK
  travel together and there is no second artifact to keep in sync. (We already dropped
  `aws-cdk-aws-lambda-python-alpha`; the plain `aws_lambda.Code.from_asset` + Docker/`pip`
  bundling path is used, matching the committed CDK being pure `aws-cdk-lib`.)
- Wraps the function in a `custom_resources.Provider` (`provider = Provider(self,
  "S3VectorsProvider", on_event_handler=fn)`) and one `CfnCustomResource` managing the
  **vector bucket only** (the index is created at bootstrap, not by CDK — see the Finding
  1 resolution in §Nova 2 and the note below):
  - **Create:** `s3vectors.create_vector_bucket(vectorBucketName=<name>)`.
  - **Delete:** best-effort `delete_index(vectorBucketName=<name>, indexName=<name>)`
    tolerating `NotFound` (in case bootstrap created one), then
    `delete_vector_bucket(vectorBucketName=<name>)`, tolerating `NotFound` so rollback is
    idempotent.
  - **Update:** a name change replaces the bucket (new physical id → create-new +
    delete-old per CloudFormation replacement semantics).
- **Why the index is not created by CDK (Finding 1).** The S3 Vectors index requires a
  concrete `dimension` at `CreateIndex` time. Because the Nova 2 output dimension is
  `[DOC]`/unverified and MUST NOT be baked in as a config literal at deploy time, the index
  is created by the **bootstrap step** (ingest notebook / app `bootstrap_index()`), which
  measures the dimension from a real probe embedding and creates the index at that size,
  failing loudly on a mismatch with `expected_dimension`. CDK therefore owns the **bucket**
  (dimension-free) and the bootstrap owns the **index** (dimension-measured). This keeps
  the "no magic constant feeds `CreateIndex`" invariant honest and removes the
  "deploy-an-index-at-the-wrong-size" failure mode. There is **no `dimension` prop on the
  custom resource** — nothing at synth/deploy time knows or needs the dimension.
- The handler reads only `vectorBucketName` from `ResourceProperties`.
- The provider Lambda's execution role is granted `s3vectors:CreateVectorBucket`,
  `DeleteVectorBucket`, `GetVectorBucket`, and `DeleteIndex`/`GetIndex` (for idempotent
  teardown) on resource `*` (the control-plane create precedes ARN existence) with a
  **documented cdk-nag `AwsSolutions-IAM5` suppression** explaining the pre-creation
  wildcard (NFR-3), plus the standard Lambda CloudWatch-logs permissions. It is **not**
  granted `CreateIndex` (that is the app/notebook identity's job via the managed policy).
- `CfnOutput`s: construct ids **`VectorBucketName`**, **`VectorIndexName`** (the intended
  index name, created at bootstrap), **`VectorBucketArn`** (ids chosen to equal the
  `variables.json` keys verbatim — see §variables.json outputs and the Settings note).

Trade-off noted: Option 4 adds a bundled Lambda asset and a Docker/pip bundling step at
synth, versus Option 3's zero-asset convenience. We accept it because it is the only
option whose SDK is the one we actually verified; "synthesizes on 2.150.0" is necessary
but not sufficient for a 2025-era service, and a deploy-time failure after all code is
written is the worse outcome. If, at implementation time, the bundling step proves
environment-hostile (no Docker), the fallback is a prebuilt `LayerVersion` pinning the
same botocore — recorded here so the implementer does not silently revert to Option 3.

### Data-plane IAM policy (Finding 5 resolution)

**Decision: emit an `iam.ManagedPolicy` (no principal), not an unassumable `iam.Role`.**
The prior design created an `iam.Role` that "exists to document" but was never assumed —
which both (a) fails to synth (an `iam.Role` requires an `assumed_by` principal) and (b)
enforces zero least-privilege, since the local app and notebook run under the developer's
own credentials (NFR-1) and are not governed by an unassumed role. Two options were
weighed:

- **(A) Reference role with a concrete `assumed_by`** (e.g. `iam.AccountRootPrincipal`)
  plus a README note that production attaches the policy to the real execution identity.
  Synthesizes, but invents a principal that nothing uses and invites "assume this role"
  confusion for a local-run sample.
- **(B) `iam.ManagedPolicy`** carrying the exact data-plane permission set, with **no
  principal**. A developer (or a production execution identity) **attaches** it to their
  own IAM user/role. This is synthesizable without a fabricated principal, satisfies AC-3
  (the policy and its statements are present and inspectable in the template), and is the
  honest artifact for a "run locally under your own creds" sample — the policy *documents
  and bounds* the needed permissions and can be attached to grant exactly them.

**Chosen: (B).** The stack emits `iam.ManagedPolicy(self, "FashionDataPlanePolicy", ...)`
and a `CfnOutput` with its ARN (`DataPlanePolicyArn`) so the README can tell the operator
to run `aws iam attach-user-policy --policy-arn <arn> ...` (or attach it to a CI/exec
role). No role, no fabricated principal. The README (AC-12) states explicitly that the
local app uses the developer's own credentials and that this managed policy is the
least-privilege set to attach to that identity (or to a production execution role). The
policy grants (FR-4.2):
- `bedrock:InvokeModel` (and `bedrock:InvokeModelWithResponseStream` for the streaming
  brain), scoped to reflect the **verified inference types** (profile + fan-out ARNs):
  - **Inference-profile models** (Claude Sonnet 4.5, inpaint, outpaint) require the grant
    on **both** the `inference-profile/*` ARN **and** the underlying `foundation-model`
    ARNs in **every fan-out region** of the `us.` profile. `GetInferenceProfile` verified
    the fan-out set is **{us-east-1, us-east-2, us-west-2}**. So the policy lists:
    - `arn:aws:bedrock:us-east-1:<account>:inference-profile/us.anthropic.claude-sonnet-4-5-20250929-v1:0`,
      and the same for `us.stability.stable-image-inpaint-v1:0`,
      `us.stability.stable-outpaint-v1:0`;
    - the backing `arn:aws:bedrock:{us-east-1,us-east-2,us-west-2}::foundation-model/anthropic.claude-sonnet-4-5-20250929-v1:0`,
      `.../stability.stable-image-inpaint-v1:0`, `.../stability.stable-outpaint-v1:0`.
  - **ON_DEMAND models** need only the plain `foundation-model` ARN in their one region:
    `arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-2-multimodal-embeddings-v1:0`
    (embeddings) and
    `arn:aws:bedrock:us-west-2::foundation-model/stability.stable-image-core-v1:1` +
    `.../stability.stable-image-ultra-v1:1` (text-to-image; both listed so the alternative
    works).
  The ARN list is **generated from `config.yml`** (the same model ids + the fan-out region
  set) so it stays in lock-step with the invoked ids; a `us.`-prefixed id in config emits
  both the profile ARN and the three-region foundation-model ARNs, a bare id emits a single
  foundation-model ARN. This makes AC-3 ("grants InvokeModel for both regions") not just
  nominally but functionally satisfied for the current-gen models.
- `s3:GetObject` / `s3:PutObject` on `image_bucket_arn` and `image_bucket_arn/*`.
- `s3vectors:PutVectors`, `s3vectors:QueryVectors`, `s3vectors:GetVectors`,
  `s3vectors:GetIndex`, `s3vectors:GetVectorBucket` on the vector bucket ARN and
  `.../index/*`, **plus `s3vectors:CreateIndex`** on the vector bucket ARN. `CreateIndex`
  is needed because — per the Finding 1 resolution — the **index is created by the
  bootstrap step (ingest notebook or the app's lazy `bootstrap_index()`), not by `cdk
  deploy`**, so the identity that runs the app/notebook must be able to create the index
  sized to the measured probe embedding. (Data-plane ops are scoped to the vector bucket
  ARN; where s3vectors exposes no resource-level ARN for an op, a wildcard carries a
  documented cdk-nag IAM5 suppression — NFR-3.)

This managed policy therefore covers both the steady-state data plane (put/query vectors,
s3 get/put, bedrock invoke) **and** the one-time index bootstrap, so the single attached
policy is sufficient for a developer to both bootstrap and run.

cdk-nag `AwsSolutionsChecks` stays enabled in `app.py`; the per-statement IAM5
suppressions on the managed policy follow the existing documented-wildcard discipline. The
stack-level CloudWatch-logs IAM5 suppression in `app.py` is kept for the custom-resource
provider Lambda's log streams (it needs it), otherwise narrowed.

### variables.json outputs

`cdk deploy --outputs-file variables.json` emits (under the stack name key):
`BucketName`, `VectorBucketName`, `VectorIndexName`, `VectorBucketArn`, and
**`DataPlanePolicyArn`** (the managed policy ARN an operator attaches to their identity —
Finding 5). The app and the ingest notebook read `BucketName`/`VectorBucketName`/
`VectorIndexName`; the README references `DataPlanePolicyArn`. The old
`AgentId`/`AgentAliasId`/OpenSearch endpoint outputs are gone (AC-10, AC-2).

Note the **index name** is emitted as an output even though the index itself is created at
bootstrap, not by CDK: the output carries the *intended* index name (from `config.yml`)
so the app and notebook agree on the name to create/query. The stack still owns the name;
the bootstrap step owns the index's existence and dimension.

**CfnOutput id ↔ JSON key contract.** A `CfnOutput`'s **construct id** is
what becomes the JSON key under the stack name in `variables.json` (the existing stack
proves this with `CfnOutput(self, "BucketName", ...)` → key `BucketName`). Therefore the
new outputs **must** be created with construct ids **exactly** `VectorBucketName`,
`VectorIndexName`, `VectorBucketArn`, `DataPlanePolicyArn` (no export-name drift), and
`settings.load_settings()` reads those same literal keys. The design pins this as a single source: a module-level
tuple of output-key names shared (by value) between the stack's `CfnOutput` ids and
`settings.py`'s lookups, so a rename cannot desync the two sides. The existing
`BucketName` id is kept unchanged.

## config.yml (new shape)

```yaml
stack_name: "FashionAgentStack"
primary_region: "us-east-1"
image_region: "us-west-2"
embedding_region: "us-east-1"   # Nova 2 is us-east-1 ONLY (verified); decoupled from primary
bucket_name: ""            # defaults to fashion-agent-{account}-{region}
models:
  # INFERENCE_PROFILE-only models carry the "us." cross-region profile id (verified).
  agent: "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
  embeddings: "amazon.nova-2-multimodal-embeddings-v1:0"      # ON_DEMAND, us-east-1 only
  text_to_image: "stability.stable-image-core-v1:1"           # ON_DEMAND, us-west-2 only
  text_to_image_alt: "stability.stable-image-ultra-v1:1"      # ON_DEMAND, us-west-2 only
  inpaint: "us.stability.stable-image-inpaint-v1:0"           # INFERENCE_PROFILE
  outpaint: "us.stability.stable-outpaint-v1:0"               # INFERENCE_PROFILE
# Fan-out regions for the "us." profiles (verified via GetInferenceProfile), used to
# build the IAM foundation-model ARNs:
inference_profile_fanout_regions: ["us-east-1", "us-east-2", "us-west-2"]
vector_index:
  bucket_name: "fashion-agent-vectors"   # suffixed with account/region at synth
  index_name: "images-index"
  # OPTIONAL expectation only (Finding 1): the index dimension is MEASURED from a real
  # Nova 2 probe embedding at bootstrap and the index is created at that measured size.
  # If set, bootstrap raises DimensionMismatchError when the measured length differs.
  # Also sent as the Nova request `embeddingDimension` hint. Leave null to accept whatever
  # the model emits.
  expected_dimension: 1024
  distance_metric: "cosine"
  top_k: 3
```
Removed legacy keys: `schema_name`, `foundation_model`, `agent_name`, `embeddingSize`,
and the whole `opensearch:` block (FR-5.7).

## Ingest notebook (`s3vectors_ingest.ipynb`, renamed)

Rewritten end to end:
- Drop `opensearch-py`, `AWSV4SignerAuth`, the OpenSearch client, and the
  `profile_name="alexhrn-Admin"` session (NFR-1 — use `boto3.Session(region_name=...)`
  from config, default credential chain).
- Read `VectorBucketName` / `VectorIndexName` from `variables.json`; read model id and
  `expected_dimension` from `config.yml`.
- **Bootstrap cell (Finding 1) — create the index at the measured dimension.** Embed one
  **probe** input with Nova 2 (`embeddingPurpose="GENERIC_INDEX"`), read `dim =
  len(vector)`. If `config.vector_index.expected_dimension` is set and differs from `dim`,
  raise `DimensionMismatchError` (loud failure). Then, if the index does not already exist
  (`GetIndex` → `NotFound`), call `s3vectors.create_index(vectorBucketName=..,
  indexName=.., dataType="float32", dimension=dim, distanceMetric="cosine")`. The index is
  thus sized to the **measured** Nova 2 output length, never a literal. This is the single
  place `CreateIndex` is called; the app's lazy `bootstrap_index()` shares the same helper.
- For each dataset image: upload the image to `s3://{image_bucket}/catalog/<name>`;
  embed it with Nova 2 (`embeddingPurpose="GENERIC_INDEX"`); call
  `s3vectors.put_vectors(vectorBucketName=.., indexName=.., vectors=[{"key": <image id>,
  "data": {"float32": vec}, "metadata": {"s3_uri": "s3://.../catalog/<name>", "name":
  <name>}}])`. **No base64 image blob is stored in the vector metadata** (FR-2.3) — only
  the S3 reference.
- A short note explains that the **vector bucket** is created by `cdk deploy` (the custom
  resource) but the **index** is created here (dimension measured from the model), so the
  notebook must run once before the app can query.

## pyproject.toml

PEP 621 project with pinned versions and dependency groups (FR-5.1):
- `[project] requires-python = ">=3.12,<3.14"` (Finding 7: strands-agents 1.23.0 was
  import-verified here on **Python 3.13** — the only interpreter available on this machine
  — so the pin is widened to admit 3.13 rather than claiming a 3.12-only verification;
  3.12 remains the supported floor matching the former Lambda baseline).
- `[project.dependencies]` (app): `streamlit`, `strands-agents`, `strands-agents-tools`,
  `boto3`, `pillow`, `requests`, `pyyaml` — all pinned (`==`), versions chosen at
  implementation time from the resolved environment.
- `[project.optional-dependencies] infra`: `aws-cdk-lib==2.150.0`, `constructs`,
  `cdk-nag`; `dev`: `pytest`, `pytest-mock`.
- Build backend: `hatchling` (simple, no package to build — app is run, not installed; a
  `[tool.hatch]`-less flat config or `setuptools` is equally fine; **choose hatchling**
  for a minimal, standard config). `requirements.txt` and the OpenSearch-layer
  `requirements.txt` are deleted (AC-9).

## Logging (`components/agent/logging_config.py`)

A `configure_logging(level)` sets up the stdlib `logging` with a structured formatter
(timestamp, level, logger name, message; JSON formatter optional via env). Modules use
`logger = logging.getLogger(__name__)`. All bare `print(...)` in tools, the frontend, and
the notebook helpers are replaced with logger calls (FR-5.3). Secrets are never logged;
S3 URIs and model ids are safe to log.

## Error handling (per fallible operation)

- **Open-Meteo geocode/forecast** (`get_weather`): network error, non-200, or empty
  results → caught, **recoverable**, returns `{"status":"not_found", ...}` with a default
  description; logged at WARNING. The agent continues.
- **Nova 2 `invoke_model`** (`embeddings`/`image_lookup`): `ClientError`
  (AccessDenied/Throttling/Validation) → caught, **recoverable** at the tool boundary,
  `image_lookup` returns `{"status":"error", "s3_uri":None, "message": "<safe text>"}`;
  logged at ERROR with the botocore error code (not the raw request). Malformed response
  (missing `embeddings[0].embedding`) → same error return, logged ERROR.
- **S3 Vectors `query_vectors`**: `ClientError` → `image_lookup` returns `error`
  (recoverable), logged ERROR. Zero hits → `not_found` (recoverable), logged INFO.
- **Stability `invoke_model`** (`generate_image`/`inpaint`/`outpaint`): `ClientError`
  → tool returns `{"status":"error", ...}` (recoverable), logged ERROR. A non-null
  `finish_reasons[0]` (content filter) → `error` with a user-safe "content was filtered"
  message, logged WARNING.
- **S3 get/put** (`s3_io`): `ClientError` on download (missing key) → the calling tool
  returns `error`; on upload failure → `error`; both recoverable at the tool boundary,
  logged ERROR.
- **Config load** (`load_settings`): missing model id / missing required
  `variables.json` key / region set where the pinned model is unavailable (Finding 2) →
  raises `ConfigError`, **fatal** (app/CDK cannot proceed); logged CRITICAL by the caller.
  The CDK custom resource surfaces vector-bucket create failures as a CloudFormation
  rollback (standard behavior).
- **Index bootstrap** (`bootstrap_index`, Finding 1): the probe Nova 2 embed failing →
  surfaced as the Nova error above, **fatal for bootstrap** (no index can be created).
  Measured length disagreeing with `expected_dimension` → raises `DimensionMismatchError`,
  **fatal** (refuse to create a wrong-sized index), logged ERROR with both lengths.
  `CreateIndex` `ConflictException` (index already exists) → treated as success
  (idempotent), logged INFO.
- **Brain / profile `AccessDenied`** (Strands `BedrockModel` invoke): because the brain,
  inpaint, and outpaint use **inference-profile** ids, an `AccessDeniedException` here most
  often means the account lacks model access for the profile or the IAM policy is missing
  the profile/foundation-model ARNs in a fan-out region. Caught at the UI (brain) or tool
  (inpaint/outpaint) boundary, surfaced as an actionable error ("enable model access for
  <id> / check InvokeModel on inference-profile + foundation-model ARNs"), logged ERROR
  with the botocore error code. **Fatal for that turn, recoverable for the session.**
- **Agent invocation** (Streamlit): any uncaught exception from `agent(prompt)` → caught
  in the UI, shown as an error chat bubble, logged ERROR; the session stays usable.

The tool contract is deliberately **total**: tools catch their own failures and return a
structured `{"status": ..., "message": ...}` so the model can recover (e.g. lookup →
generate fallback) rather than the event loop aborting.

## Input validation (per external input)

- `location_name` (str, required): non-empty; trimmed. Empty → `not_found`.
- `prompt` (str, required for generate/inpaint/outpaint): non-empty; if empty, tool
  returns `error` asking for a description.
- `input_image_uri`/`input_query` (`image_lookup`, both optional): at least one must be
  non-None/non-empty, else `not_found` with a prompt for input. `s3://` URIs are parsed
  with a strict `s3://bucket/key` split; a malformed URI → `error`.
- `mask_uri`/`search_prompt` (`inpaint`): at least one required; `mask_uri` wins if both.
- `left/right/up/down` (`outpaint`): integers ≥ 0; at least one > 0, else `error`;
  oversized extents are passed through (Stability enforces its own limits and returns a
  filtered/failed `finish_reasons`, handled above).
- `expected_dimension` (config, optional): if set, must be a positive integer; the
  **bootstrap** step (not `load_settings`) compares it against the measured probe-embedding
  length and raises `DimensionMismatchError` on disagreement (Finding 1). It is NOT fed to
  `CreateIndex` — the measured length is. If `None`, bootstrap accepts whatever the model
  emits. This replaces the prior "validate against {3072,1024,384,256}" rule, which rested
  on the unverified Matryoshka claim.
- Uploaded image (Streamlit): type restricted to png/jpeg by the uploader; resized to the
  256–1024 box before upload (unchanged behavior).

## Invariants and ownership

- **Embedding length == index dimension.** Owned by the **bootstrap step** (Finding 1):
  the index is created at `dim = len(probe_embedding)` measured from a real Nova 2 call,
  and `config.vector_index.expected_dimension` (if set) is only a check value — a mismatch
  raises `DimensionMismatchError`. Enforcement is at the bootstrap layer (runtime, against
  the live model), not at the config layer, precisely because the model's output length is
  unverified. Downstream code (query, put) reads the dimension from the created index /
  measured vector, never a literal; CDK does not know the dimension at all.
- **Region split.** Owned by the client factory: `generate_image` uses the `image_region`
  client, everything else the `primary_region` client. Enforced at client construction,
  asserted by unit tests (AC-8).
- **No secrets / no profiles in committed code.** Owned by `load_settings`/client factory
  (default credential chain only). Enforced by code review + a grep check (AC-11).
- **Vector metadata holds an S3 reference, not image bytes.** Owned by the ingest
  notebook (write side) and `image_lookup` (read side).

## Testability

Unit-testable (boto3 + HTTP mocked, no real calls — NFR-5, AC-4):
- `get_weather`: mock `requests.get` for geocode + forecast; assert the parsed
  description and the `not_found` fallback on non-200.
- `generate_image`: mock the `bedrock-runtime` client; assert the request `body` is the
  Stability text-to-image JSON, assert the client was built with
  `region_name="us-west-2"`, assert the returned `s3_uri` and that `s3.put_object`/
  `upload_fileobj` was called.
- `inpaint`/`outpaint`: mock bedrock + s3; assert the Stability inpaint/outpaint body
  (mask image vs search_prompt; directional extents), `region_name="us-east-1"`, and the
  returned URI; assert validation errors on missing mask/extents.
- `generate_image`/`inpaint`/`outpaint` also assert the exact `modelId` passed to
  `invoke_model` (Finding 3b): `generate_image` → bare `stability.stable-image-core-v1:1`;
  `inpaint` → `us.stability.stable-image-inpaint-v1:0`; `outpaint` →
  `us.stability.stable-outpaint-v1:0`. A regression to a bare profile id fails the test,
  not only the live invoke.
- `image_lookup`: mock bedrock (embedding) + `s3vectors.query_vectors`; assert the query
  uses `queryVector={"float32": vec}` and `topK=settings.top_k`, that the returned
  `s3_uri` comes from the hit **metadata**, embedding client `region_name="us-east-1"`,
  and `result="not_found"` when zero hits / no inputs (and that the input image is **not**
  echoed — Finding 5).
- `settings`: assert env-var region override (`FASHION_PRIMARY_REGION` /
  `FASHION_IMAGE_REGION` / `FASHION_EMBEDDING_REGION`), `ConfigError` on missing keys,
  `ConfigError` when `embedding_region`/`image_region` is set where the pinned model is
  unavailable (Finding 2), and that the agent/inpaint/outpaint ids carry the `us.` profile
  prefix while embeddings/text-to-image carry bare ids.
- **bootstrap dimension (Finding 1):** mock the Nova 2 embed to return a vector of a known
  length N and mock `s3vectors`; assert `bootstrap_index()` calls `create_index` with
  `dimension=N` (measured, not a literal), and that it raises `DimensionMismatchError` when
  `expected_dimension` is set to something other than N. This is the test that enforces
  AC-7 at the unit level without a live model.
- **`_result` + tools envelope:** assert every tool returns the native
  `{"status","content":[{"json":{...}}]}` shape (so Strands keeps it structured, not
  stringified) and that `result="ok"` carries a parseable `s3_uri`.
- **`ToolResultCollector` hook (Finding 1):** construct a representative `ToolResult`
  (`{"status":"success","content":[{"json":{"result":"ok","s3_uri":"s3://b/k.png"}}]}`),
  pass it through the collector's `AfterToolCallEvent` callback, and assert the UI
  extraction helper returns `s3://b/k.png`. This is the explicit "assert the chosen field
  is populated" check the review asked for.
- **IAM ARN builder:** a pure unit test over the config→ARN function asserts that a `us.`
  profile id emits an `inference-profile/*` ARN **plus** `foundation-model` ARNs for all
  three fan-out regions, and a bare id emits a single-region `foundation-model` ARN.

Integration-tested (manual / not in CI): `cdk synth` (AC-1,2,3,10), `cdk deploy`,
the ingest notebook against real S3 Vectors, the Streamlit app end to end, and the live
Nova 2 / Stability payloads (the `[DOC]` shapes). The CDK custom-resource behavior and the
Strands event loop are integration concerns, not unit tests.

**First-run embedding smoke check (Finding 4) — catches wrong `embeddingPurpose`/field
names that mocked tests cannot.** After bootstrap, an integration script embeds one known
catalog image, `PutVectors` it, then embeds the *same* image with
`embeddingPurpose="GENERIC_RETRIEVAL"` and `QueryVectors(topK=1)`; it asserts the top hit
is that same catalog key with a small cosine distance. A wrong `embeddingPurpose` string,
a wrong request/response field name, or a wrong distance metric surfaces here as a failing
smoke test (bad or no nearest neighbor) rather than as silent degraded recall. This is the
explicit correctness gate for the `[DOC]` Nova 2 body; it runs once against the live model
before the sample is considered working. The same script doubles as the live confirmation
of the Nova 2 request/response shape. A design note: tools are
written as plain functions taking injected `settings`/clients and wrapped by thin `@tool`
adapters precisely so the fallible logic is unit-testable without the Strands runtime.

Smoke checks (AC-5): import `frontend/app.py` logic module, `components/agent/agent.py`,
`components/agent/tools/*`, and `app.py` under `cdk synth`.

## Schema disposition

`FashionAgent_Schema.json` is **deleted** (not kept as reference): no managed agent
consumes it, the tool contracts are now expressed as typed `@tool` signatures +
docstrings (the authoritative interface), and keeping a stale OpenAPI schema would invite
drift. The operation descriptions it carried are migrated into the tool docstrings.
(Resolves Open Question 6.)

## Open-question resolutions

0. **Model ids + inference types** — **VERIFIED live** via `ListFoundationModels` /
   `GetFoundationModel` / `GetInferenceProfile` (see §Verified model inventory). All
   six ids exist; Claude Sonnet 4.5 + inpaint + outpaint are INFERENCE_PROFILE-only and
   use `us.` profile ids; Nova 2 (us-east-1) and stable-image-core/ultra (us-west-2) are
   ON_DEMAND with bare ids. The `stable-outpaint` prefix is confirmed real.
1. **Nova 2 shape + dimension** — request/response are `[DOC]`/UNVERIFIED (marked inline;
   cannot be verified without invoking the model, which NFR-5 forbids, and the control
   plane exposes no body schema or dimension). The dimension is **measured at bootstrap**
   from a real probe embedding and the index is created at that measured size;
   `config.vector_index.expected_dimension` is only an optional check (mismatch →
   `DimensionMismatchError`). No literal feeds `CreateIndex`; CDK never knows the
   dimension. Query-time `embeddingPurpose` pinned to `GENERIC_RETRIEVAL`, index-time
   `GENERIC_INDEX` (both `[DOC]`, caught by the first-run smoke check). All `[DOC]` field
   names/values are isolated to `embeddings.py` + the notebook bootstrap helper.
2. **Stability bodies** — recorded above `[DOC]`: shared `prompt`/`image`/`output_format`
   contract, `images[0]` base64 response; inpaint uses a **mask image** (`mask` +
   `mask_source`) or `search_prompt`; outpaint uses **directional extents**
   (`left/right/up/down`). Isolated to `image_gen.py`.
3. **S3 Vectors shapes** — fully VERIFIED via botocore (above). CDK path =
   **custom-resource provider Lambda packaging a pinned s3vectors-capable boto3**
   (Option 4), chosen over `AwsCustomResource` (Option 3) because the latter's provider
   runtime SDK predates the 2025-07-15 `s3vectors` service and would fail at deploy.
4. **Strands usage** — VERIFIED against installed source: `Agent(model, tools,
   system_prompt, callback_handler, hooks)`, `@tool` with docstring+hints,
   `BedrockModel(region_name=..., model_id=<profile id for the brain>)`, `AgentResult`
   return. **Result extraction uses a `HookProvider` on `AfterToolCallEvent`** reading
   `event.result["content"][0]["json"]`; `callback_handler` is trace-text only (the
   `current_tool_use` kwarg is tool *input*, not result). Tools return the native
   `ToolResult` envelope so dicts aren't stringified. The brain runs in `primary_region`;
   `generate_image` uses a separately pinned `image_region` boto3 client.
5. **Python version** — **`>=3.12,<3.14`** (Finding 7). strands-agents 1.23.0 was
   import-verified on the local **3.13** interpreter (the only one installed here); 3.12 is
   the supported floor. The pin admits both so the stated verification matches reality.
6. **Schema file** — **deleted** (above).

## Acceptance-criteria traceability

AC-1,2,10 → data-plane stack + CfnOutputs (incl. `DataPlanePolicyArn`); AC-3 → IAM
**managed policy** section (Finding 5); AC-4 → Testability; AC-5 → smoke checks; AC-6 →
system prompt + Streamlit result-discovery (no `<generated_s3_uri>`/`opensearch`/`titan`);
AC-7 → **measured-at-bootstrap** dimension + the bootstrap unit test (Finding 1); AC-8 →
region-split client factory + per-invocation `region_name`+`modelId` tests; AC-9 → pyproject.toml; AC-11 → settings/client
factory (no profiles); AC-12 → README update (Strands, S3 Vectors, two-region, model-access
list, local deploy+run).

## Responses to the design review (revision pass 2)

This section responds to the **current** review (`design-review.json` /
`design-review.md`, verdict CHANGES_REQUESTED: **1 HIGH + 4 MEDIUM + 2 NIT**, findings
1–7). It supersedes the earlier revision-1 response block. Each finding is addressed with
facts re-verified this pass against the installed SDKs (strands-agents 1.23.0, botocore
1.43.107, aws-cdk-lib 2.150.0) and **live read-only Bedrock control-plane calls**
(`ListFoundationModels`, `GetFoundationModel`, `GetInferenceProfile` against account
`875692608981`); no `invoke_model` and no `cdk deploy` were made (NFR-5).

- **Finding 1 (HIGH) — Nova 2 contract/dimension unverified; config-literal dimension
  rests on it. RESOLVED by taking the review's option (b): derive-and-check.** I confirmed
  the premise cannot be verified here — `GetFoundationModel` for Nova 2 returns no body
  schema and no dimension (output reproduced in §Verified model inventory), botocore models
  the `invoke_model` body as an opaque blob, and NFR-5 forbids invoking the model to probe
  it. So the design no longer feeds a config literal to `CreateIndex`. The **index is
  created at bootstrap**, sized to `len(probe_embedding)` measured from one real Nova 2
  call; `config.vector_index.expected_dimension` is only an optional check that raises
  `DimensionMismatchError` on disagreement. CDK creates the **bucket only** and never knows
  the dimension. AC-7 ("index dimension equals the *verified* Nova 2 output dimension") is
  now satisfied by **measurement**, with a unit test asserting `create_index` is called
  with the measured length and raises on an `expected_dimension` mismatch. All Nova 2 body
  fields are marked `[DOC]` inline.

- **Finding 2 (MEDIUM) — Nova 2 is us-east-1-only but primary_region is configurable.
  RESOLVED.** Added a dedicated `embedding_region` setting (default us-east-1), decoupled
  from `primary_region`, mirroring `image_region`. `load_settings()` validates each region
  against the live availability map and raises `ConfigError` if `embedding_region`/
  `image_region` is set where the pinned model is absent — so "configurable" (NFR-2) now
  means *safely* configurable with fail-fast, not a silent invoke-time failure. Reflected
  in §Settings, §config.yml, the client factory, and a settings unit test.

- **Finding 3 (MEDIUM) — Stability bodies unverified + profile modelId unstated/untested.
  RESOLVED.** (a) Every Stability field *value* is now marked `[DOC]` **inline** in the
  bodies (not only in a trailing caveat), so `MASK_IMAGE_WHITE` and the outpaint
  directional-extent model read as provisional placeholders gated behind first-integration
  re-confirmation. (b) The design now states explicitly that
  `invoke_model(modelId=settings.inpaint_model_id/outpaint_model_id)` receives the
  **`us.`-prefixed profile id**, and the inpaint/outpaint unit tests assert `modelId` (not
  just `region_name`), so a regression to a bare id is caught in CI.

- **Finding 4 (MEDIUM) — embeddingPurpose values unverified, not caught by any test.
  RESOLVED.** The two values stay pinned (`GENERIC_INDEX` / `GENERIC_RETRIEVAL`) but are
  marked `[DOC]` inline, and a **first-run integration smoke check** (added to §Testability)
  embeds a known catalog image, `PutVectors` it, then re-embeds+`QueryVectors(topK=1)` and
  asserts the same key returns with a small cosine distance — so a wrong purpose/field name
  surfaces as a failing smoke test rather than silent bad recall.

- **Finding 5 (MEDIUM) — data-plane IAM role has no principal / governs nothing.
  RESOLVED.** Replaced the unassumable `iam.Role` with an `iam.ManagedPolicy` (no
  principal) carrying the exact least-privilege permission set, plus a `DataPlanePolicyArn`
  output and a README instruction to attach it to the developer's own identity (or a
  production execution role). This synthesizes without a fabricated principal, satisfies
  AC-3 (statements present and inspectable), and is the honest artifact for a local-run
  sample. The managed policy also now includes `s3vectors:CreateIndex` because the index is
  bootstrapped by the app/notebook identity (Finding 1).

- **Finding 6 (NIT) — text-to-image seed/aspect_ratio defaults unflagged. RESOLVED.**
  `seed` is **removed** from the text-to-image body so the service randomizes each
  generation (a fixed `seed:0` would make same-prompt outputs identical — wrong for a
  generate-outfit UX); `aspect_ratio` is marked `[DOC]` inline.

- **Finding 7 (NIT) — Python 3.12 pin stated verified but only 3.13 is checkable here.
  RESOLVED.** Widened the pin to `requires-python = ">=3.12,<3.14"` and stated plainly that
  strands-agents 1.23.0 was import-verified on the local **3.13** interpreter (3.12 remains
  the supported floor). The stated verification now matches reality.

Residual `[DOC]`-only risks (honestly labeled): the opaque `invoke_model` JSON bodies for
Nova 2 and Stability (field names / enum values) remain documentation-sourced because
`InvokeModel` bodies are blobs botocore cannot model and NFR-5 forbids invoking them to
probe. They are isolated to `embeddings.py` / `image_gen.py` + the notebook bootstrap
helper, each marked `[DOC]` inline, and are gated by the first-run smoke check (Finding 4)
and the measured-dimension bootstrap (Finding 1). Everything the control plane and the
botocore service models can confirm — model existence, per-region availability, inference
types, profile fan-out, all `s3vectors`/`bedrock-runtime` operation shapes, and all Strands
APIs — is live-verified this pass, with the raw command outputs recorded above.
