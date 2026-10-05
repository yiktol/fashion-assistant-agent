# Implementation Plan: Refactor Fashion Assistant → Strands Agents + S3 Vectors

All paths are absolute under the worktree
`/Users/erictole/demo/fashion-assistant-agent/.worktrees/refactor-strands-s3vectors`
(abbreviated `<WT>` below). All git ops use
`git -C <WT>`. Implement to `design.md`; where the design's older wording
conflicts, the **six locked fixes** (restated inline per item) win.

## Verified environment facts (grounds the plan)

- Python 3.13.3 local; `strands-agents` 1.23.0, `strands-agents-tools` 0.2.19,
  `botocore`/`boto3` 1.43.107 (the `s3vectors` client constructs successfully),
  `aws-cdk-lib` 2.150.0, `cdk` CLI 2.1126.0, `pytest` 8.3.2 all present.
- `aws_cdk.aws_s3vectors` does **not** exist at 2.150.0 → vector bucket is created by a
  **custom-resource provider Lambda** (design Option 4), not a native/L1 construct.
- **Docker is NOT running** on this machine. `cdk synth` MUST be Docker-free
  (Locked Fix 1): the provider Lambda's s3vectors-capable boto3 is carried by a
  **prebuilt `aws_lambda.LayerVersion`** or a **pre-populated no-bundling asset
  directory** — never `BundlingOptions`/pip-at-synth.
- `cdk.json` uses `"app": "python app.py"`. The non-interactive synth subprocess runs
  under `/bin/sh`, where `python` may be absent (only `python3` exists system-wide).
  **Verification note:** run `cdk synth` inside an activated venv (which puts `python`
  on PATH) OR confirm a `python`→`python3` shim is available; do not change the committed
  `cdk.json` app command unless synth cannot otherwise resolve `python`.

## Verification gates (deploy-free; used throughout)

- **G-synth (Docker-free):** `cd <WT> && cdk synth --quiet` succeeds with **no Docker
  daemon running**, and the template is inspectable at `<WT>/cdk.out/*.template.json`.
- **G-pytest:** `cd <WT> && python3 -m pytest -q` passes; every boto3/HTTP call mocked
  (NFR-5, zero real AWS/network).
- **G-import:** `cd <WT> && python3 -c "import ..."` for each new module (and the
  Streamlit UI logic module) imports cleanly.
- **G-grep (final):** no residual `bedrock-agent-runtime`, `opensearch`, `aoss`,
  `AWSV4SignerAuth`, `titan`, `<generated_s3_uri>`, `CfnAgent`,
  `AmazonBedrockExecutionRoleForAgents` in active source (notebook + README included).

---

- [ ] 1. **Packaging + config + settings + logging foundation.**
      Create `pyproject.toml` (PEP 621, `requires-python = ">=3.12,<3.14"`, hatchling
      backend) with pinned app deps (`streamlit`, `strands-agents`,
      `strands-agents-tools`, `boto3`, `pillow`, `requests`, `pyyaml`) and optional
      groups `infra` (`aws-cdk-lib==2.150.0`, `constructs`, `cdk-nag`) and `dev`
      (`pytest`, `pytest-mock`); pin versions from the resolved environment. Rewrite
      `config.yml` to the new shape (regions incl. `embedding_region`, `models` block
      with `us.`-profile ids for agent/inpaint/outpaint and bare ids for
      embeddings/text_to_image(+alt), `inference_profile_fanout_regions`
      `[us-east-1,us-east-2,us-west-2]`, `vector_index` with
      **`expected_dimension: null`** per Locked Fix 3, `distance_metric: cosine`,
      `top_k: 3`); remove `schema_name`/`foundation_model`/`agent_name`/`embeddingSize`/
      `opensearch` keys. Create `components/agent/__init__.py`,
      `components/agent/settings.py` (frozen `Settings` dataclass + `load_settings()`
      merging `config.yml`+`variables.json`, env overrides
      `FASHION_PRIMARY_REGION`/`FASHION_IMAGE_REGION`/`FASHION_EMBEDDING_REGION`,
      `ConfigError`, region-availability validation; `expected_embedding_dimension: int
      | None`), and `components/agent/logging_config.py` (`configure_logging`). Delete
      `requirements.txt` and `components/layers/opensearch_layer/requirements.txt`.
      Files: `<WT>/pyproject.toml`, `<WT>/config.yml`,
      `<WT>/components/agent/__init__.py`, `<WT>/components/agent/settings.py`,
      `<WT>/components/agent/logging_config.py`; delete `<WT>/requirements.txt`,
      `<WT>/components/layers/opensearch_layer/requirements.txt`.
      Verify: `cd <WT> && pip install -e ".[infra,dev]"` resolves; `python3 -c "import
      yaml,components.agent.settings as s; print('ok')"` imports (G-import). A
      `test_settings.py` stub may be added here or in item 4.

- [ ] 2. **CDK data-plane stack + Docker-free S3 Vectors construct + IAM managed policy.**
      Rewrite `components/stacks/fashion_agent_stack.py` to keep the two S3 buckets
      (SSL/BPA/access-logging/auto-delete, NFR-6) and REMOVE all `CfnAgent`/
      `CfnAgentAlias`/`AgentRole`(`AmazonBedrockExecutionRoleForAgents_FashionAgent`)/
      `FashionAgentLambda`+role/Klayers/`PythonLayerVersion`/schema-load/OpenSearch
      wiring. Add `components/stacks/s3_vectors_construct.py` creating the **vector
      bucket only** via `custom_resources.Provider` + `CfnCustomResource` whose handler
      lives in `components/stacks/s3vectors_provider/index.py` and calls
      `boto3.client("s3vectors")` (create/delete vector bucket, idempotent on NotFound;
      best-effort delete_index on teardown). **Locked Fix 1:** carry the s3vectors-capable
      boto3 via a **prebuilt `aws_lambda.LayerVersion` (or pre-populated no-bundling
      asset dir)** — NO Docker/pip bundling at synth. **Locked Fix 2:** the construct does
      NOT create or know the index dimension (no `dimension` prop). Add an
      `iam.ManagedPolicy` (`FashionDataPlanePolicy`, no principal — Locked Fix 5 context)
      granting `bedrock:InvokeModel`(+WithResponseStream) via a **config→ARN builder** in
      a new `components/stacks/iam_arns.py` that, for each `us.`-prefixed id, emits the
      `inference-profile/*` ARN PLUS `foundation-model` ARNs for every region in
      `inference_profile_fanout_regions` (Locked Fix 5), and for bare ids a single-region
      `foundation-model` ARN; plus `s3:GetObject/PutObject` on the image bucket and
      `s3vectors:PutVectors/QueryVectors/GetVectors/GetIndex/GetVectorBucket/CreateIndex`
      on the vector bucket+`index/*`. Add a code comment on
      `inference_profile_fanout_regions` that it must be re-checked via
      `GetInferenceProfile` if a profile id changes (Locked Fix 5). Emit `CfnOutput`s with
      ids EXACTLY `VectorBucketName`, `VectorIndexName`, `VectorBucketArn`,
      `DataPlanePolicyArn` (keep `BucketName`). Update `app.py` (drop schema/profile
      coupling, keep `AwsSolutionsChecks` + the provider-Lambda CloudWatch IAM5
      suppression) and `cdk.json` (remove `"profile": "sandbox"`). Delete
      `components/stacks/opensearchserverless_stack.py`, `components/layers/` (whole tree),
      `components/bedrock_agent/FashionAgent_Schema.json`, `components/lambda/` (whole
      tree). Add `tests/test_iam_arns.py` asserting the ARN builder emits a
      foundation-model ARN for EVERY fan-out region for each `us.` id (Locked Fix 5).
      Files: `<WT>/components/stacks/fashion_agent_stack.py`,
      `<WT>/components/stacks/s3_vectors_construct.py`,
      `<WT>/components/stacks/s3vectors_provider/index.py`,
      `<WT>/components/stacks/iam_arns.py`, `<WT>/app.py`, `<WT>/cdk.json`,
      `<WT>/tests/test_iam_arns.py`; delete the four paths above.
      Verify: **G-synth (Docker-free)** — stop Docker, `cdk synth --quiet` succeeds; grep
      the template for `AWS::S3::Bucket`(x2), the custom resource, the managed policy with
      InvokeModel ARNs across us-east-1/us-east-2/us-west-2, and the four CfnOutputs; and
      assert NO `AWS::Bedrock::Agent*` / OpenSearch resources (AC-1,2,3,10). `python3 -m
      pytest -q tests/test_iam_arns.py` passes.

- [ ] 3. **Strands agent runtime: tools, result envelope, hooks, prompt + unit tests.**
      Create `components/agent/tools/__init__.py`,
      `components/agent/tools/_result.py` (`ok(**fields)`/`err(message)` building the
      native `{"status","content":[{"json":{...}}]}` ToolResult envelope),
      `components/agent/tools/s3_io.py` (download/upload/base64, pinned `.png` keys +
      `ContentType="image/png"`), `components/agent/tools/embeddings.py` (Nova 2 helper on
      the `embedding_region` client; **Locked Fix 3:** send `embeddingDimension` ONLY when
      `expected_embedding_dimension` is non-null, never hardcode 1024; `[DOC]` fields
      isolated here), `components/agent/tools/weather.py` (`get_weather` — **Locked Fix 6:**
      Strands `status` only `success|error`; a miss is
      `status="success"`+nested `result="not_found"`), `components/agent/tools/image_gen.py`
      (`generate_image` on `image_region` bare core id, text-only; `inpaint`/`outpaint` on
      `primary_region` via `us.` profile ids; **Locked Fix 4:** OMIT `aspect_ratio` from
      the text-to-image body; pinned `output_format="png"`; pass explicit `modelId` per
      Finding 3b), `components/agent/tools/image_lookup.py` (Nova 2 embed →
      `s3vectors.query_vectors(topK, queryVector={"float32":vec}, returnMetadata=True)`;
      return metadata `s3_uri`; no input-image echo; miss → `result="not_found"`),
      `components/agent/hooks.py` (`ToolResultCollector` HookProvider on
      `AfterToolCallEvent`), `components/agent/agent.py` (`build_agent(settings)`:
      `BedrockModel(region_name=primary_region, model_id=agent_model_id[us.-profile])` +
      `Agent(model, tools, system_prompt, callback_handler, hooks)`; cached per-region
      client factory). Move+adapt the system prompt to
      `components/agent/prompt.py` (remove step 5 `<generated_s3_uri>` and `<answer>`/
      `<thinking>` framing; rename ops to the five tool names). **Locked Fix 2:** NO lazy
      `bootstrap_index()` anywhere — `build_agent()`/settings load calls
      `s3vectors.get_index`; on NotFound raise a fatal `ConfigError` telling the operator
      to run the ingest notebook first. Delete `components/bedrock_agent/prompt.py` after
      moving. Add `tests/conftest.py` (fake clients/settings fixtures) and
      `tests/test_weather.py`, `tests/test_image_gen.py` (generate+inpaint+outpaint,
      asserting body, `region_name`, and exact `modelId`), `tests/test_image_lookup.py`,
      and envelope/hook tests; extend `tests/test_settings.py`.
      Files: all under `<WT>/components/agent/` listed above, `<WT>/components/agent/prompt.py`,
      and `<WT>/tests/{conftest,test_weather,test_image_gen,test_image_lookup,test_settings}.py`;
      delete `<WT>/components/bedrock_agent/prompt.py`.
      Verify: **G-pytest** — `python3 -m pytest -q` passes (per-tool request/`region_name`/
      `modelId` assertions, `not_found` envelope for weather/lookup, no input-image echo,
      hook extracts `s3_uri`, settings env-override + ConfigError + `us.`-prefix checks).
      **G-import** for `components.agent.agent` and each tool module.

- [ ] 4. **Streamlit UI, S3 Vectors ingest notebook, README/flowchart.**
      Rewrite `frontend/app.py` to build the agent in-process via `build_agent(settings)`
      (cached in `st.session_state`), read `BucketName`/`VectorBucketName`/
      `VectorIndexName` through `load_settings()`, upload to `uploads/<uuid>.png`, hand
      the URI to the agent in **plain prose** (`f"{prompt}\n\nThe uploaded image is at
      {s3_uri}"`), discover the result image via the `ToolResultCollector` hook
      (newest-first first `result=="ok"` + non-null `s3_uri`), and render streamed
      reasoning/tool-calls in the Trace pane via `callback_handler`. Delete
      `frontend/bedrock_agent.py`. Rewrite `opensearch_ingest.ipynb` → **rename**
      `s3vectors_ingest.ipynb`: drop `opensearch-py`/`AWSV4SignerAuth`/
      `profile_name="alexhrn-Admin"`; read outputs/config; **bootstrap cell** embeds one
      Nova 2 probe, measures `dim = len(vec)`, raises `DimensionMismatchError` if
      `expected_dimension` set and differs (Locked Fix 3), creates the index at the
      measured dim if `GetIndex`→NotFound (**the only `CreateIndex` call site — Locked Fix
      2**); then per image: upload to `catalog/<name>`, embed, `PutVectors` with
      `metadata={"s3_uri":...}` (no base64 blob). Update `README.md` (Strands in-process
      agent, S3 Vectors, two-region note incl. `embedding_region`, model-access list,
      local deploy+run, attach `DataPlanePolicyArn`, run notebook once before the app) and
      the flowchart reference.
      Files: `<WT>/frontend/app.py`, `<WT>/s3vectors_ingest.ipynb` (rename from
      `opensearch_ingest.ipynb`), `<WT>/README.md`; delete `<WT>/frontend/bedrock_agent.py`,
      `<WT>/opensearch_ingest.ipynb`.
      Verify: **G-import** on the `frontend/app.py` importable logic (guard Streamlit
      top-level calls so import works headless, or extract a logic module); `jupyter
      nbconvert --to script --stdout <WT>/s3vectors_ingest.ipynb >/dev/null` parses;
      **G-grep (final)** passes across all active source; re-run **G-synth** and
      **G-pytest** to confirm nothing regressed.

## Notes / assumptions

- Locked Fix 2 removes the lazy `bootstrap_index()` path the design text still references
  in a few places; the notebook is the sole index creator/populator, and the app fails
  fast via `GetIndex` NotFound → `ConfigError`. The managed policy still carries
  `s3vectors:CreateIndex` (notebook identity needs it).
- The `[DOC]`/unverified `invoke_model` bodies (Nova 2, Stability) stay isolated to
  `embeddings.py`/`image_gen.py`; unit tests assert construction, not live correctness
  (live correctness is a first-run integration concern, out of scope under NFR-5).
- If `cdk synth` cannot resolve `python`, run it from an activated venv; do not alter the
  committed `cdk.json` app command otherwise.
