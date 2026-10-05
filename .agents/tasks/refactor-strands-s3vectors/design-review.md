# Design Review: Refactor Fashion Assistant to Strands Agents + S3 Vectors

Reviewer: design-review subagent (fresh read, no authoring context).
Scope reviewed: `design.md` against `requirements.md`, with load-bearing claims
independently verified against the SDKs installed on this machine and the existing
source in the worktree.

## Verdict

**CHANGES_REQUESTED** — 0 HIGH, 2 MEDIUM, 4 NIT.

This is a mature, twice-revised design. The headline risk the task flagged — a Nova 2
embedding dimension assumed to be 1024 — is **not** present: the design measures the
dimension from a real probe embedding at bootstrap and never feeds a literal to
`CreateIndex`. All claims the design marks "VERIFIED" that I could independently check
held up exactly. The remaining findings are a feasibility gap around CDK asset bundling
(AC-1), an under-specified lazy-bootstrap concurrency/permission path, and four nits.

---

## What I independently verified

I re-ran the design's verification claims against the installed packages
(`strands-agents` 1.23.0, `botocore`/`boto3` 1.43.107, `aws-cdk-lib` 2.150.0, Python
3.13.3) and the existing worktree source. See the Verified Assumptions section for the
full list. Everything the design labels VERIFIED and everything it labels `[DOC]` was
categorized correctly — the design's provenance discipline is accurate.

---

## Findings

### 1. MEDIUM — CDK custom-resource provider Lambda uses Docker/pip asset bundling, but AC-1 requires `cdk synth` to succeed and the design does not guarantee a Docker-free synth path

Where: §S3 Vectors construct ("Chosen: Option 4"), bullet beginning "Defines a Python
`lambda.Function` … bundled via `BundlingOptions` (pip install into the asset)".

The design selects `aws_lambda.Code.from_asset(..., bundling=...)` to pin an
s3vectors-capable boto3 into the provider Lambda. `from_asset` bundling runs the bundling
command **inside a Docker container at synth time** when the local bundling path is not
available. AC-1 ("`cdk synth` succeeds from the worktree root") and AC-5 ("`python app.py`
under `cdk synth` runs cleanly") are both synth-time gates that CI/the implementer must
pass **without** `cdk deploy`. If Docker is not present in the implementation/verification
environment, `from_asset` bundling fails and `cdk synth` fails — so the chosen primary
path can block the primary acceptance criterion.

The design acknowledges this only as a deferred, implementation-time escape hatch ("If …
the bundling step proves environment-hostile (no Docker), the fallback is a prebuilt
`LayerVersion`"). That leaves the AC-1-critical decision unresolved in the design and
pushes a potentially blocking environment dependency onto the coder with no specified
Docker-free route.

Concrete fix: pin the synth path to one that does not require Docker at synth. Options,
pick one and state it:
- (a) Use `aws_lambda.Code.from_asset` with `bundling` configured to attempt
  **`local` bundling first** via a `local` `ILocalBundling` implementation (pip install
  into the output dir on the host), falling back to Docker only if local fails — and state
  that the local path is the expected one in CI. Document that CI must have `pip`
  available for the target platform.
- (b) Vendor the pinned `boto3`/`botocore` into the asset directory at a committed path
  (a `requirements`-free pre-populated asset) so `from_asset` needs **no** bundling step
  at all, making synth Docker-free by construction.
- (c) Make the prebuilt `LayerVersion` the primary design (not the fallback), carrying the
  pinned botocore, so the provider function asset is plain source with no bundling.

Whichever is chosen, the design should state the exact mechanism and that `cdk synth`
runs without Docker, so AC-1/AC-5 are provably met during this (deploy-free) work.

### 2. MEDIUM — The app's lazy `bootstrap_index()` path is specified only in passing; its concurrency, idempotency, and "index not yet created" runtime behavior are undefined

Where: §Nova 2 Finding-1 resolution ("an equivalent `bootstrap_index()` helper the app
calls lazily if the index is missing"), §Invariants, §config.yml note, and
§Error handling (which covers the *notebook* bootstrap but not the app's lazy call).

The design cleanly specifies the **notebook** bootstrap cell (probe embed → measure dim →
`CreateIndex` → `DimensionMismatchError` on mismatch, `ConflictException`=idempotent). But
it also says the Streamlit app "calls lazily if the index is missing" and shares "the same
helper", and this app-side path has unspecified behavior that materially affects a
first-run user:
- **Trigger & timing:** When does the app detect "index missing" — at `build_agent()`,
  at first `image_lookup`, or elsewhere? A lazy `CreateIndex` on the first user query will
  do a real Nova 2 embed + `CreateIndex` synchronously inside a tool call, which can be
  slow and will surface as a long first lookup.
- **Concurrency:** Streamlit reruns and multiple sessions can call `bootstrap_index()`
  concurrently. The design relies on `ConflictException`=success for idempotency in the
  *notebook*, but does not state the app path catches the same race (two sessions both
  seeing "missing", both calling `CreateIndex`).
- **Empty index semantics:** Even once created, a freshly bootstrapped index has **no
  catalog vectors** (ingest is a separate notebook step). The design's §Ingest note says
  "the notebook must run once before the app can query," which **contradicts** the idea
  that the app can usefully self-bootstrap the index — a lazily-created-but-empty index
  makes every `image_lookup` return `not_found`.

This is an internal conflict (app-can-lazily-bootstrap vs. notebook-must-run-first) plus
undefined concurrency/trigger behavior.

Concrete fix: pick one model and specify it. Recommended: **drop the app's lazy
`bootstrap_index()` entirely** and make the index a prerequisite created only by the
notebook (ingest and index creation co-located, since an index without catalog vectors is
useless). Then: on app start, `load_settings()` or `build_agent()` calls `GetIndex`; if
`NotFound`, raise a clear fatal `ConfigError` ("run `s3vectors_ingest.ipynb` once to
create and populate the index"). If the lazy path is kept instead, specify (a) the exact
trigger point, (b) that it catches `ConflictException` as success under concurrency, and
(c) resolve the "empty index" conflict by stating the app only bootstraps an *empty* index
and that lookups return `not_found` until ingest runs.

### 3. NIT — `expected_dimension: 1024` default in `config.yml` plus the `embeddingDimension` request hint reintroduce the "1024" value the Finding-1 resolution worked to remove

Where: §config.yml (`expected_dimension: 1024`) and §Nova 2 request body
(`"embeddingDimension": 1024, // [DOC]`) and the note "The request still sends
`embeddingDimension` from config as a *best-effort hint*".

The dimension is correctly **measured** at bootstrap and the index is sized to the
measurement, so this does not reach `CreateIndex` and does not violate AC-7. But shipping
`expected_dimension: 1024` as the committed default means the mismatch check will **fire
and fail** (`DimensionMismatchError`) the first time an operator bootstraps if Nova 2's
actual length is not 1024 — turning the first run into a hard stop rather than a
measurement. That is arguably the intended "fail loud" behavior, but defaulting the
expectation to an unverified value is a footgun.

Concrete fix: default `expected_dimension: null` in the committed `config.yml` (accept
whatever the model emits on first bootstrap; log the measured value), and document that an
operator may *then* pin the measured value to lock it. Keep the mismatch check for when it
is explicitly set. Separately, if `embeddingDimension` is sent as a request hint, source
it from `expected_dimension` only when non-null, and omit the field when null rather than
hardcoding 1024.

### 4. NIT — `aspect_ratio: "1:1"` is sent as a `[DOC]` value with no fallback if the enum string is wrong

Where: §Stability text-to-image request body.

`aspect_ratio` is marked `[DOC]` (allowed set unverified). Unlike the mask/outpaint
params, which the design describes as gated by first-integration re-confirmation, a wrong
`aspect_ratio` enum would cause every `generate_image` call to fail validation at the live
model, and the mocked unit tests cannot catch it (they only assert the body is
constructed). This is the same class of risk as the pinned `embeddingPurpose`, but
`generate_image` has **no** equivalent of the Finding-4 nearest-neighbor smoke check to
catch it.

Concrete fix: either (a) omit `aspect_ratio` from the body entirely so the service default
applies (removes an unverified enum from the hot path), or (b) add `generate_image`/
`inpaint`/`outpaint` to a first-integration smoke step analogous to the embedding smoke
check — one real call per image tool asserting a non-filtered `images[0]` comes back — and
reference it from §Testability so the `[DOC]` Stability bodies have a correctness gate, not
just mocked construction tests.

### 5. NIT — IAM policy fan-out ARN set is derived from a `GetInferenceProfile` result that may drift; no synth-time assertion ties the config fan-out list to the invoked profile ids

Where: §Data-plane IAM policy (fan-out set `{us-east-1, us-east-2, us-west-2}`) and
§config.yml (`inference_profile_fanout_regions`).

The fan-out region set is live-verified this pass, but it is encoded as a **static config
list** (`inference_profile_fanout_regions`). If AWS changes a `us.` profile's fan-out set,
the committed list silently diverges and `InvokeModel` can fail with AccessDenied in a
region the policy didn't grant. The design's §Error-handling entry for "Brain / profile
AccessDenied" already anticipates this failure mode, which confirms it is a real edge.

Concrete fix: keep the static list (a deploy-time snapshot is reasonable since IAM is
built at synth), but add a one-line note that the list is a verified snapshot that must be
re-checked with `GetInferenceProfile` if a profile id changes, and have the IAM ARN-builder
unit test assert the policy includes a `foundation-model` ARN for **every** region in
`inference_profile_fanout_regions` for each `us.`-prefixed id (so config drift between the
list and the emitted ARNs is caught). This mostly exists in §Testability; make the
"every fan-out region present" assertion explicit.

### 6. NIT — `get_weather` return envelope in the docstring omits the `status` key the design elsewhere mandates

Where: §Strands tool interfaces, `get_weather` prose ("On geocode miss or non-200:
`{"status": "not_found", "description": ...}`").

The common envelope decision (§Strands verified-facts, §tool interfaces) is that tools
return the native `{"status": "success"|"error", "content": [{"json": {"result": ...}}]}`
shape, where `status` is the Strands flag and `result` is the semantic outcome. The
`get_weather` prose writes `{"status": "not_found", ...}` — mixing the semantic value into
the Strands `status` slot. Read literally, `status="not_found"` is not one of Strands'
`"success"|"error"` values and would be an invalid `ToolResult.status`.

Concrete fix: make the prose consistent with the envelope — a recoverable weather miss is
`status="success"` with nested `{"result": "not_found", "description": "<default>",
"temperature_f": None}`, matching how §tool interfaces documents the other tools and the
`ok()`/`err()` helpers in `tools/_result.py`.

---

## Verified Assumptions

Each confirmed by direct inspection on this machine (commands/paths noted):

1. **Strands tool-result wrapping** — `strands/tools/decorator.py` line 632: a returned
   dict is treated as a ready `ToolResult` only if it has **both** `status` and `content`;
   otherwise it is stringified into `{"status":"success","content":[{"text":str(result)}]}`.
   The design's rationale for returning the native envelope is correct.
2. **`ToolResultContent`** — `strands/types/tools.py` line 68: `TypedDict(total=False)` with
   members `document`, `image`, `json` (`Any`), `text`. A `{"json": {...}}` content block is
   valid. Correct.
3. **`BedrockModel.__init__`** — keyword-only with `boto_session`, `boto_client_config`,
   `region_name`, `endpoint_url`, `**model_config`. Matches the design verbatim.
4. **`Agent.__init__`** — accepts `model`, `tools`, `system_prompt`, `callback_handler`,
   and `hooks: list[HookProvider] | None`. Matches.
5. **Hook events** — `strands/hooks/events.py`: `BeforeToolCallEvent` has `tool_use`;
   `AfterToolCallEvent` (line 151) has `selected_tool`, `tool_use`, and `result: ToolResult`.
   Matches the design's result-extraction mechanism.
6. **Callback handler kwargs** — `strands/handlers/callback_handler.py`: `reasoningText`,
   `data`, `complete`, `current_tool_use`. Confirms trace-only use and that
   `current_tool_use` is tool input, not result. Matches.
7. **`@tool` signature** — `tool(func, description, inputSchema, name, context)`; docstring
   → description, type hints → input schema. Matches (design wrote `context=False`; actual
   is `context: bool | str = False` — immaterial).
8. **strands-agents version** — `pip show` reports 1.23.0, as the design states.
9. **s3vectors botocore model** — API version `2025-07-15`. `CreateVectorBucket` required
   `[vectorBucketName]`; `CreateIndex` required `[indexName, dataType, dimension,
   distanceMetric]`, `dataType` enum `[float32]`, `distanceMetric` enum `[euclidean,
   cosine]`, optional `metadataConfiguration{nonFilterableMetadataKeys}`; `PutVectors`
   required `[vectors]`, item required `[key, data]` + optional `metadata` (structure),
   `data` member `float32`; `QueryVectors` required `[topK, queryVector]`, optional
   `returnMetadata`/`returnDistance`/`queryMode`, output `vectors[{distance,key,metadata}]`.
   Every shape matches the design exactly.
10. **`bedrock-runtime` `InvokeModel`** — required `[modelId]`; `body` is a `blob`
    (opaque). Confirms the design's honest `[DOC]` labeling of all `invoke_model` body JSON.
11. **`aws-cdk-lib` version & modules** — 2.150.0; `aws_cdk.aws_s3vectors` **absent**
    (find_spec → None), so Option 1 is correctly rejected; `custom_resources.AwsCustomResource`
    and `custom_resources.Provider` both present, consistent with the Option-3-vs-4 analysis.
12. **Python** — local interpreter is 3.13.3, matching the Finding-7 rationale for widening
    the pin to `>=3.12,<3.14`.
13. **Existing Lambda (`components/lambda/agent/lambda_function.py`)** — confirms the facts
    the design relies on for removal: image-echo-on-miss (`response["body"] = input_image`),
    Titan `taskType` payloads `TEXT_IMAGE`/`INPAINTING`/`OUTPAINTING` with `maskPrompt`,
    `seed: 0` in text-to-image, `amazon.titan-embed-image-v1` embeddings with
    `embeddingConfig.outputEmbeddingLength`, OpenSearch/`AWSV4SignerAuth`/`RETRIEVE_THRESHOLD`.
    All accurately described.
14. **Existing `config.yml`** — has `schema_name`, `foundation_model`, `agent_name`,
    `embeddingSize`, and the `opensearch:` block the design removes. Accurate.
15. **Existing `prompt.py`** — step 5 emits `<generated_s3_uri>`; `<answer>`/`<thinking>`
    framing present. Matches the design's system-prompt edits.
16. **Existing `frontend/app.py`** — `folder = "blogpost/"` (line 94), malformed
    `<input_s3_uri>{...}<input_s3_uri>` (both open tags, line 162), `<generated_s3_uri>`
    regex parse (line 171). The design's "called out as changes" descriptions are accurate.
17. **Existing stack CfnOutput pattern** — `CfnOutput(self, "BucketName", ...)` present;
    confirms the construct-id→`variables.json`-key contract the design pins.
18. **cdk-nag discipline** — `app.py` uses `AwsSolutionsChecks` + `NagSuppressions`; `cdk.json`
    carries `"profile": "sandbox"` (the design removes it). Accurate.

## Unverified / Wrong Assumptions

No assumption in the design was found to be **wrong**. The following remain **unverified**,
but the design labels each `[DOC]` and isolates/gates it (so they are correctly handled,
not silent):

1. **Nova 2 `invoke_model` body & response field names** (`taskType`,
   `singleEmbeddingParams`, `embeddingPurpose` values, `embeddings[0].embedding` path) —
   unverifiable without invoking the model (NFR-5) and absent from the control plane.
   Labeled `[DOC]`, isolated to `embeddings.py`, gated by the Finding-4 nearest-neighbor
   smoke check. Correctly handled.
2. **Nova 2 output embedding dimension** — unverified, but **not assumed to be 1024** for
   any load-bearing decision: measured at bootstrap and the index is sized to the
   measurement (AC-7 satisfied by measurement). The task's HARD-flag condition does **not**
   apply. See NIT-3 for the residual `expected_dimension: 1024` default.
3. **Stability request bodies & response fields** (`prompt`/`image`/`output_format`,
   `images[0]`, `finish_reasons`; `mask_source: "MASK_IMAGE_WHITE"`; outpaint
   `left/right/up/down`; `aspect_ratio`) — unverifiable (blob body, NFR-5). Labeled
   `[DOC]` inline, isolated to `image_gen.py`. The mask-image-vs-maskPrompt divergence from
   Titan is a documented design choice. See NIT-4 for the missing generate-side smoke gate.
4. **`AwsCustomResource` JS-SDK vintage** (the Option-3 rejection rationale) — I confirmed
   `AwsCustomResource` and `install_latest_aws_sdk` exist in 2.150.0 and that
   `aws_s3vectors` is absent, but the specific claim that the Lambda-runtime-bundled v3 SDK
   predates `@aws-sdk/client-s3vectors` is a documentation/runtime-vintage argument I could
   not verify without a deploy. The design's conservative choice (Option 4) does not depend
   on this being exactly right — it avoids the risk either way — so this is a reasonable,
   clearly-labeled judgment, not a load-bearing unverified fact.
5. **Inference-profile fan-out region set** `{us-east-1, us-east-2, us-west-2}` — live-verified
   this pass per the design, but a snapshot that can drift (see NIT-5). Not independently
   re-verified by me (no account access from this review), accepted as the design's live
   result.

---

## Scope / requirements cross-check (confirmed in-scope, no creep)

- **Data-plane-only (no UI hosting infra):** confirmed — §CDK stack provisions only the two
  S3 buckets + S3 Vectors bucket + managed policy; no ECS/App Runner/Lambda-for-UI. The
  only Lambda is the custom-resource provider (infra plumbing, not UI hosting). Matches
  FR-4 and Out-of-Scope.
- **Two-region split:** confirmed and strengthened — primary (us-east-1), image
  (us-west-2), and a dedicated embedding region (us-east-1), each validated against a model
  availability map. Matches FR-3/NFR-2.
- **Managed-agent resource removal:** confirmed — `CfnAgent`, `CfnAgentAlias`, agent exec
  role, action-group Lambda + layers, `FashionAgent_Schema.json` all removed/deleted.
  Matches FR-4.3 and AC-2.
- No scope creep beyond requirements was found; the extra `embedding_region` and the
  measured-dimension bootstrap are justified responses to verified constraints, not
  gold-plating.
