# Mask-free Stability edits (search_replace / search_recolor) + in-UI mask brush

The change adds two mask-free, text-targeted Stability edit tools (search_replace, search_recolor) that mirror the existing inpaint tool, wires them into the agent, updates prompt routing, grants them in the config-driven IAM policy, and adds an optional in-UI brush that turns strokes into an inpaint mask. It directly answers the original user frustration ("why is it always asking for a mask image") by making the common replace/recolor edits mask-free and demoting inpaint to the precise, mask-based path. The implementation is code + config + tests only; no AWS, cdk, or streamlit was invoked.

Watch for: nothing blocking. The verification evidence (56 passed, two import gates, static field check) is complete and matches the code I read. **Verdict**: APPROVED

## High-level view

The two new `*_impl` functions are faithful copies of `inpaint_impl`: they invoke the PRIMARY-region (us-east-1) client with the `us.`-prefixed profile ids resolved from `Settings`, build a request body carrying only the verified fields, decode via the shared `_decode_image`, and upload a PNG to `OutputImages/<stem>_<uuid>.png`. The one field that distinguishes the two models is handled correctly: search_replace sends `search_prompt`, search_recolor sends `select_prompt` (never `search_prompt`), and neither sends a `mask`.

The IAM grant needs no logic change. `build_invoke_model_resource_arns()` iterates `config["models"].values()`, so adding the two `us.`-prefixed ids to `config.yml` fans each out to 2 ARNs × 3 regions = 6 new ARNs, lifting the total from 21 to 33. The LOCKED FIX 5 fan-out and its per-region/per-id assertions are untouched; the test's count assertion was updated to 33.

The frontend keeps the mask-free path as the default and adds the brush as an optional, collapsed expander. The canvas dependency is imported lazily inside `run_app` so a missing package degrades to a friendly note rather than crashing, and the module stays import-safe. The stroke-to-mask conversion lives in a pure module-scope helper with a PIL-only unit test, and the widget uses a stable key.

<details>
<summary>Issues (0)</summary>

No blocking or non-blocking findings. All PART A/B/C requirements and HARD CONSTRAINTS are satisfied.

</details>

<details>
<summary>Details</summary>

### PART A — backend tools mirror inpaint

`search_replace_impl` and `search_recolor_impl` (components/agent/tools/image_gen.py) follow `inpaint_impl` step for step: validate non-empty `prompt` and non-empty selector returning `err(...)`, `s3_io.download_b64` the source, build the body, invoke `bedrock_primary.invoke_model` with `settings.search_replace_model_id` / `settings.search_recolor_model_id`, decode with `_decode_image`, and upload via `s3_io.upload_png` to `f"{_OUTPUT_PREFIX}/{_source_stem(image_uri)}_{uuid.uuid4().hex}.png"`. Invoke+decode is wrapped in `except (ClientError, ValueError)` returning `err(...)`.

Request bodies contain only verified fields. search_replace: `prompt`, `image`, `search_prompt`, `output_format="png"`, plus `negative_prompt`/`grow_mask` inserted only when provided. search_recolor: `prompt`, `image`, `select_prompt`, `output_format="png"`, same optional inserts. The selector field name is correct per model — search_recolor uses `select_prompt`, not `search_prompt`. This is the exact field-name bug the user hit (the live error "Invalid field 'search_prompt' in request … Available fields for inpaint are: image, prompt, mask …"), so getting `select_prompt` right here is the core correctness point, and the unit tests assert `"search_prompt" not in body` for recolor.

`make_search_replace` and `make_search_recolor` are `@tool` factories mirroring `make_inpaint`, with typed signatures and docstrings stating the mask-free semantics; both bind `clients.bedrock_primary` and are appended to the `tools` list in `build_agent`. `Settings` carries both frozen fields, `_REQUIRED_MODEL_KEYS` includes `search_replace`/`search_recolor`, and `load_settings` resolves them from `models[...]`. `config.yml` carries both ids. `prompt.py` routing now steers "change/replace this item" → search_replace and "recolor this item to X" → search_recolor as the preferred everyday edits, keeping inpaint as the precise mask-based path and preserving the plain-prose/no-XML closing rule.

The one cosmetic deviation from the plan: `search_replace_impl`'s error string is `"image could not be edited"` rather than a "replaced"-themed phrase. This is still an `err(...)` with a friendly message, so it satisfies the requirement; not a finding.

Backend tests in test_image_gen.py cover each required assertion: region `us-east-1`, correct `modelId`, selector field present with the right name, no stray `mask`/`select_prompt`/`search_prompt`/`mask_uri`, optional fields only when passed and omitted when absent, empty-prompt and empty-selector → `status=="error"`, content-filter → error, and the `.png` output key with `ContentType=="image/png"`.

### PART B — IAM grant via config

No logic change in iam_arns.py or the stack, which is correct: the ARN builder is config-driven and the stack passes `config["models"]`, so adding the two ids to config.yml grants them. The test was extended properly — both ids added to `MODELS` and `US_PREFIXED`, and `test_expected_total_arn_count` updated from 21 to 33 (5 `us.`-prefixed × 3 regions × 2 + 3 bare = 33). The LOCKED FIX 5 fan-out behavior and its per-region/per-id assertions are preserved. verification.md states `cdk deploy` is required before the tools work live, with the admin/SSO caveat.

### PART C — in-UI mask brush

`streamlit-drawable-canvas==0.9.3` is pinned in pyproject.toml. The canvas import is gated inside `run_app` in a try/except ImportError that shows a friendly `st.info` and sets `st_canvas = None`, so a missing package degrades gracefully with no crash and the module stays import-safe. The brush lives in a collapsed `st.expander` ("Precise edit — brush a region (optional)") shown only when an image is attached, so the mask-free path stays the default. A helper `st.caption` explains the two paths. The widget key is the stable `"mask-canvas"` (no uuid4 keys). The premium theme CSS and the before/after rendering are preserved.

How strokes become the mask (the part that can't be screenshotted deploy-free): the canvas is `st_canvas(..., background_image=source_image, drawing_mode="freedraw", stroke_color=white, key="mask-canvas")`, rendered at the normalized source image's width/height. After a draw, `canvas_result.image_data` is an `(H, W, 4)` RGBA numpy array of the stroke layer. `run_app` checks `np.asarray(strokes)[:, :, 3].any()` to detect any brushed pixel, then calls the pure helper `strokes_to_mask_png(strokes, (source_width, source_height))`. The helper treats any pixel with alpha > 0 as brushed, maps brushed → 255 (white = repaint) and the rest → 0 (black = keep), converts to an "L" image, and resizes to the source dims with `Image.NEAREST` (keeping it strictly black/white). The PNG bytes are uploaded to `uploads/<uuid>_mask.png` with `ContentType="image/png"`, and the resulting `s3://` URI is appended to the plain-prose handoff ("A black/white mask for a precise inpaint is at … (white = repaint, black = keep).") so the agent passes it to inpaint as `mask_uri`.

`strokes_to_mask_png` is a pure module-scope helper (numpy/PIL only, no Streamlit, no AWS, no network) and handles both RGBA (alpha channel) and 3-channel (any non-black pixel) inputs. Its unit test (test_frontend_mask.py) builds a brushed RGBA rectangle, asserts the output mode is "L"/"1", size equals the requested size, brushed pixels are 255 and untouched are 0, values are strictly {0,255}, and that an upscaled resize stays black/white. This is a genuine PIL-based test with no Streamlit/AWS dependency.

### HARD CONSTRAINTS

`frontend.app` imports with numpy at module scope and the canvas import deferred into `run_app`, so it is import-safe and tolerant of the missing drawable-canvas package (verification evidence confirms the package is not installed in the env and the graceful path is the one exercised). Agent modules import cleanly. No hardcoded regions/bucket/index/model-ids appear in logic — the impls read ids from `settings`, the mask upload reads `settings.primary_region` and `image_bucket`. No real AWS/cdk/streamlit is invoked anywhere in the change or its tests.

### Verification evidence

verification.md and the commit message record `python -m pytest -q` → 56 passed (baseline 42), both import gates passing, and the static field-name check confirming `search_prompt` for replace / `select_prompt` for recolor and no `mask` key in either new body. I read the source behind each of these claims and they hold. The evidence is complete and self-consistent, so per the review instructions I did not re-run the suites.

</details>

<details>
<summary>File map</summary>

- components/agent/tools/image_gen.py — added `search_replace_impl`, `search_recolor_impl`, and their `@tool` factories mirroring inpaint.
- components/agent/agent.py — import and wire both tools into `build_agent` on the primary client.
- components/agent/settings.py — two new frozen `Settings` fields + required keys + resolution.
- components/agent/prompt.py — routing to prefer search_replace/recolor; inpaint demoted to precise/mask-based.
- config.yml — two new model ids under `models:`.
- pyproject.toml — pinned `streamlit-drawable-canvas==0.9.3`.
- frontend/app.py — pure `strokes_to_mask_png` helper; optional/collapsible lazy-imported mask brush; two-path hint.
- tests/conftest.py, tests/test_settings.py — FakeSettings + settings test updated for the two ids.
- tests/test_iam_arns.py — two ids added; ARN count 21 → 33.
- tests/test_image_gen.py — full search_replace/search_recolor coverage.
- tests/test_frontend_mask.py — PIL-based test for the mask helper.

Full diff: `git -C /Users/erictole/demo/fashion-assistant-agent/.worktrees/image-edit-maskfree diff main`

</details>
