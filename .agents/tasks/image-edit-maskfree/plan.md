# Implementation Plan — mask-free image edits (search_replace / search_recolor) + in-UI mask brush

Scope: add two mask-free, text-targeted Stability edit tools (search_replace, search_recolor), wire them into the agent, update the system prompt routing, grant them in the CDK IAM policy, and add an optional in-UI mask brush so the existing `inpaint` tool is usable without a hand-made mask. Code + IAM policy only — NO `cdk deploy`, NO real AWS/Bedrock, NO `streamlit run`. All tests mock boto3.

All paths below are absolute under the worktree `/Users/erictole/demo/fashion-assistant-agent/.worktrees/image-edit-maskfree`. Shortened here as `<WT>`.

## Verified model facts (authoritative — do NOT re-guess)
Both models are INFERENCE-PROFILE only, ACTIVE in us-east-1, invoked on the PRIMARY-region (us-east-1) client exactly like inpaint/outpaint. Shared Stability response envelope `{images:[b64], finish_reasons:[...], seeds:[...]}` → reuse `_decode_image`. `output_format` stays `"png"`.

- search_replace — profile id `us.stability.stable-image-search-replace-v1:0`
  - Accepted request fields ONLY: `image`, `prompt`, `search_prompt`, `grow_mask`, `negative_prompt`, `seed`, `output_format`, `style_preset`.
  - Region selector field: `search_prompt` (describes the region to select). `prompt` describes the replacement. MASK-FREE.
- search_recolor — profile id `us.stability.stable-image-search-recolor-v1:0`
  - Accepted request fields ONLY: `image`, `prompt`, `negative_prompt`, `select_prompt`, `seed`, `grow_mask`, `output_format`, `style_preset`.
  - Region selector field: `select_prompt` (NOT search_prompt). `prompt` describes the target color/style. MASK-FREE.

## LOCKED-FIX constraints to preserve
- LOCKED FIX 4: generate_image body still omits `aspect_ratio` and `seed` (do not touch).
- LOCKED FIX 5: `build_invoke_model_resource_arns()` fans each `us.`-prefixed id out to one inference-profile ARN + one foundation-model ARN per region in `inference_profile_fanout_regions` {us-east-1, us-east-2, us-west-2}; bare ids emit one region-less foundation-model ARN. The two new ids are `us.`-prefixed, so each adds 2 ARNs × 3 regions = 6 ARNs. Keep the de-dup and the unit test that asserts an ARN per fan-out region per `us.` id.
- LOCKED FIX 6 / result envelope: tools return the native ToolResult shape via `ok()`/`err()` from `components/agent/tools/_result.py`. Reuse them; do not invent a new shape.
- Import safety: `python -c "import frontend.app"` must import with no Streamlit/page execution and must NOT hard-fail if `streamlit_drawable_canvas` is absent. Agent modules import cleanly.
- No hardcoded regions/bucket/index/model-ids in logic — read from settings/config.

## Design decisions (made here, grounded in the code read)
1. `search_replace_impl` / `search_recolor_impl` mirror `inpaint_impl` exactly (download source → b64, build body with ONLY verified fields, invoke primary client with the settings profile id, `_decode_image`, upload PNG to `OutputImages/<stem>_<uuid>.png`, return `ok`/`err`). Rationale: the task mandates mirroring inpaint's structure and the response shape is shared.
2. Both impls validate non-empty `prompt` AND non-empty region selector before invoking, returning `err(...)` otherwise — mirrors inpaint's `mask_uri` guard. Rationale: fail fast with a friendly message instead of a Bedrock ValidationException.
3. Optional fields `negative_prompt` and `grow_mask` are added to the body only when provided (same conditional-insert pattern as inpaint). `seed`/`style_preset` are NOT exposed as tool args (kept out of the body) to keep the surface minimal and match inpaint's chosen subset. Rationale: proportional change; `seed` is deliberately omitted elsewhere for variability.
4. Mask-brush dep pinned as `streamlit-drawable-canvas==0.9.3` in `pyproject.toml`. Rationale: long-standing stable release of the component. If the env cannot install it, the lazy import inside `run_app()` degrades to a friendly "mask brush unavailable" note — import-safety and the UI never hard-fail either way. (Installed streamlit is 1.60.0; if 0.9.3 proves incompatible at install time, 0.12.0 — which requires streamlit >= 1.53 — is the fallback pin. The graceful-degradation path is the hard guarantee, not the exact version.)
5. Mask-conversion logic (`strokes_to_mask_png`) is a PURE module-scope helper in `frontend/app.py` (PIL only, no Streamlit, no AWS) with its own PIL-based unit test. All canvas/Streamlit calls stay inside `run_app()`. Rationale: the hard import-safety constraint plus "pure importable helper + unit test" requirement.

---

# Part A — new mask-free backend tools

- [ ] 1. Add `search_replace_impl` and `search_recolor_impl` to `components/agent/tools/image_gen.py`, mirroring `inpaint_impl`.
      Each: validate non-empty `prompt` and non-empty selector (`err(...)` on failure); `source_b64 = s3_io.download_b64(s3, image_uri)`; build `body_dict` with ONLY verified fields; add `negative_prompt`/`grow_mask` only when provided; invoke `bedrock_primary.invoke_model(modelId=settings.<id>, body=json.dumps(body_dict), accept="application/json", contentType="application/json")`; decode via `_decode_image`; upload to `f"{_OUTPUT_PREFIX}/{_source_stem(image_uri)}_{uuid.uuid4().hex}.png"` via `s3_io.upload_png`; return `ok(s3_uri=..., message="replaced"|"recolored")`; wrap invoke+decode in `try/except (ClientError, ValueError)` returning `err(f"image could not be ...: {exc}")`.
      - search_replace body keys: `prompt`, `image`, `search_prompt`, `output_format="png"` (+ optional `negative_prompt`, `grow_mask`). Signature: `search_replace_impl(bedrock_primary, s3, settings, image_uri, prompt, search_prompt, negative_prompt=None, grow_mask=None)`. Uses `settings.search_replace_model_id`.
      - search_recolor body keys: `prompt`, `image`, `select_prompt`, `output_format="png"` (+ optional `negative_prompt`, `grow_mask`). Signature: `search_recolor_impl(bedrock_primary, s3, settings, image_uri, prompt, select_prompt, negative_prompt=None, grow_mask=None)`. Uses `settings.search_recolor_model_id`.
      Files: `<WT>/components/agent/tools/image_gen.py`
      Verify: `cd <WT> && python -c "import components.agent.tools.image_gen"` imports clean (full test run is item 9).

- [ ] 2. Add `make_search_replace` and `make_search_recolor` `@tool` factories in the same file, mirroring `make_inpaint`.
      Clear docstrings stating MASK-FREE text-targeted editing and the selector semantics; typed signatures returning the `{"result","s3_uri","message"}` payload. search_replace tool args: `image_uri: str, prompt: str, search_prompt: str, negative_prompt: str | None = None, grow_mask: int | None = None`. search_recolor tool args: `image_uri: str, prompt: str, select_prompt: str, negative_prompt: str | None = None, grow_mask: int | None = None`. Each delegates to its `*_impl` with `bedrock_primary`.
      Files: `<WT>/components/agent/tools/image_gen.py`
      Verify: `cd <WT> && python -c "import components.agent.tools.image_gen as m; assert hasattr(m,'make_search_replace') and hasattr(m,'make_search_recolor')"`.

- [ ] 3. Add `search_replace_model_id` and `search_recolor_model_id` to `Settings` and resolve them in `load_settings`.
      Add both fields to the frozen `Settings` dataclass; add `"search_replace"` and `"search_recolor"` to `_REQUIRED_MODEL_KEYS`; set `search_replace_model_id=models["search_replace"]`, `search_recolor_model_id=models["search_recolor"]` in the `Settings(...)` construction.
      Files: `<WT>/components/agent/settings.py`
      Verify: covered by item 8's settings test; `cd <WT> && python -c "import components.agent.settings"` imports clean.

- [ ] 4. Add the two model ids to `config.yml` under `models:`.
      Add `search_replace: "us.stability.stable-image-search-replace-v1:0"` and `search_recolor: "us.stability.stable-image-search-recolor-v1:0"` after `outpaint:`.
      Files: `<WT>/config.yml`
      Verify: `cd <WT> && python -c "import yaml;m=yaml.safe_load(open('config.yml'))['models'];assert m['search_replace']=='us.stability.stable-image-search-replace-v1:0' and m['search_recolor']=='us.stability.stable-image-search-recolor-v1:0'"`.

- [ ] 5. Wire both new tools into `build_agent`.
      Import `make_search_replace, make_search_recolor` from `.tools.image_gen`; append `make_search_replace(clients.bedrock_primary, clients.s3, settings)` and `make_search_recolor(clients.bedrock_primary, clients.s3, settings)` to the `tools` list (primary-region client, same as inpaint/outpaint).
      Files: `<WT>/components/agent/agent.py`
      Verify: `cd <WT> && python -c "import components.agent.agent"` imports clean. (build_agent itself hits GetIndex at runtime and is not invoked in tests.)

- [ ] 6. Update the system prompt routing in `components/agent/prompt.py`.
      In the edit step, add routing: "change/replace this item" → `search_replace` (text region via `search_prompt`, mask-free); "recolor this item to X" → `search_recolor` (text region via `select_prompt`, mask-free); state both are preferred for everyday edits. Keep `inpaint` as the precise mask-based tool (requires `mask_uri`) and keep `outpaint` for extending outward. Keep the plain-prose / no-XML-tags closing rule unchanged.
      Files: `<WT>/components/agent/prompt.py`
      Verify: `cd <WT> && python -c "from components.agent.prompt import system_prompt as s; assert 'search_replace' in s and 'search_recolor' in s and 'mask_uri' in s"`.

# Part B — IAM grant

- [ ] 7. Ensure the new profile ids flow into `build_invoke_model_resource_arns()` and extend its test.
      No logic change is needed in `components/stacks/iam_arns.py` — it iterates `models.values()`, and the stack passes `config["models"]`, so adding the two ids to `config.yml` (item 4) already grants them. Verify this holds, then extend `tests/test_iam_arns.py`: add the two ids to the `MODELS` map and to `US_PREFIXED`, and update `test_expected_total_arn_count` from 21 to 33 (5 us.-prefixed × 3 regions × 2 = 30, + 3 bare × 1 = 33). The existing per-region/per-id assertions then cover the new ids automatically.
      Files: `<WT>/tests/test_iam_arns.py` (and confirm no change needed in `<WT>/components/stacks/iam_arns.py` / `<WT>/components/stacks/fashion_agent_stack.py`)
      Verify: `cd <WT> && python -m pytest -q tests/test_iam_arns.py` — all pass, count assertion = 33.

# Config + backend tests

- [ ] 8. Update `tests/conftest.py` `FakeSettings` and `tests/test_settings.py` for the two new ids.
      Add `search_replace_model_id="us.stability.stable-image-search-replace-v1:0"` and `search_recolor_model_id="us.stability.stable-image-search-recolor-v1:0"` to `FakeSettings` (so backend tool tests can read them). In `tests/test_settings.py`: add the two keys to the inline `_CONFIG.models` block and assert `cfg.search_replace_model_id` / `cfg.search_recolor_model_id` resolve and start with `us.` in `test_model_id_prefixes` (or a new assertion).
      Files: `<WT>/tests/conftest.py`, `<WT>/tests/test_settings.py`
      Verify: `cd <WT> && python -m pytest -q tests/test_settings.py` — all pass.

- [ ] 9. Add backend unit tests for the two new tools in `tests/test_image_gen.py` (mirror the inpaint tests, use `RecordingBedrock`).
      For search_replace assert: `region_name == "us-east-1"`, `modelId == "us.stability.stable-image-search-replace-v1:0"`, body has `search_prompt` and `image` and `prompt`, body has NO `mask`, NO `select_prompt`, NO `mask_uri`; success envelope `s3_uri` under `OutputImages/<stem>_` ending `.png`; `fake_s3.puts[0]["ContentType"]=="image/png"`. For search_recolor assert the mirror with `modelId == "us.stability.stable-image-search-recolor-v1:0"`, body has `select_prompt` (NOT `search_prompt`), `image`, `prompt`. For both: optional `negative_prompt`/`grow_mask` land in the body only when passed; empty `prompt` → `status=="error"`; empty selector → `status=="error"`; a `finish_reasons:["FILTER"]` response → `status=="error"`.
      Files: `<WT>/tests/test_image_gen.py`
      Verify: `cd <WT> && python -m pytest -q tests/test_image_gen.py` — all pass.

# Part C — in-UI mask brush (frontend)

- [ ] 10. Add `streamlit-drawable-canvas==0.9.3` as a pinned dependency.
      Add it to the `[project].dependencies` list in `pyproject.toml`. (If install fails in the chosen env, keep the pin but rely on the lazy-import graceful degradation from item 12; record the outcome in verification.md.)
      Files: `<WT>/pyproject.toml`
      Verify: `cd <WT> && python -c "import tomllib;d=tomllib.load(open('pyproject.toml','rb'));assert any('streamlit-drawable-canvas' in x for x in d['project']['dependencies'])"`.

- [ ] 11. Add a PURE module-scope mask helper `strokes_to_mask_png` to `frontend/app.py`.
      Signature e.g. `strokes_to_mask_png(canvas_rgba: "np.ndarray | bytes", size: tuple[int,int]) -> bytes`. Convert the canvas RGBA stroke layer to a 1-channel black/white PNG matching the source image dimensions (white = brushed region to repaint, black = keep): any pixel with alpha > 0 → 255 else 0, resized to `size`, saved as PNG via PIL. Pure, importable, no Streamlit/AWS/network side effects. Document the white=repaint / black=keep contract in the docstring. Keep `numpy` usage import-safe (numpy ships with Pillow/streamlit; import at module scope is fine, or accept raw bytes — choose numpy at module scope since it is already an indirect dep).
      Files: `<WT>/frontend/app.py`
      Verify: `cd <WT> && python -c "import frontend.app"` still imports clean (unit test is item 14).

- [ ] 12. Add the optional/collapsible mask-brush UI inside `run_app()`.
      Lazily `import streamlit_drawable_canvas` INSIDE `run_app` wrapped in try/except ImportError; on ImportError show a friendly `st.info`/`st.caption` "mask brush unavailable — install streamlit-drawable-canvas" and skip the canvas (no crash). When a user image is attached, render a collapsed `st.expander` ("Precise edit — brush a region (optional)") containing the canvas drawn over the uploaded image (background image = the attached PIL image, drawing_mode="freedraw", stroke color white). On submit, if strokes exist: call `strokes_to_mask_png(...)` sized to the normalized source dims, upload to `uploads/<uuid>_mask.png` via the existing `boto3 ... put_object` pattern (ContentType "image/png"), build its `s3://` URI, and include it in the handoff so the agent can pass `mask_uri` to inpaint. Keep the existing `normalize_image`+`upload_image` flow and the plain-prose `build_handoff` (no XML). Use stable widget keys (e.g. `key="mask-canvas"`) — NO uuid4 keys for widgets. Keep the premium theme/CSS and before/after rendering unchanged.
      Files: `<WT>/frontend/app.py`
      Verify: `cd <WT> && python -c "import frontend.app"` — imports with NO Streamlit execution and NO hard failure even though `streamlit_drawable_canvas` is absent.

- [ ] 13. Add a short helper hint in the UI explaining the two paths.
      Near the chat input or the mask expander, add a concise `st.caption`: describe the change in words for an automatic edit (search_replace / recolor), or brush a region for a precise inpaint. Keep the mask step optional so the text-only mask-free path stays the easy default.
      Files: `<WT>/frontend/app.py`
      Verify: `cd <WT> && python -c "import frontend.app"` still imports clean.

- [ ] 14. Add a PIL-based unit test for `strokes_to_mask_png` (no Streamlit, no AWS).
      New `tests/test_frontend_mask.py`: build a small RGBA array/image with a brushed (alpha>0) rectangle, call `strokes_to_mask_png` with a target size, open the returned PNG bytes with PIL, assert mode is suitable for a mask ("L" or "1"), output size equals the requested size, brushed pixels are 255 (white) and untouched pixels are 0 (black).
      Files: `<WT>/tests/test_frontend_mask.py`
      Verify: `cd <WT> && python -m pytest -q tests/test_frontend_mask.py` — all pass.

# Final verification (deploy-free, all gates green)

- [ ] 15. Run the full gate suite and record results in `verification.md`.
      Run, from `<WT>`:
      - `python -m pytest -q` → all tests pass (previously 42; now includes the new search_replace/search_recolor, iam count=33, settings, and mask-helper tests).
      - Import gate 1: `python -c "import frontend.app"` → exit 0, no Streamlit page execution, no hard failure when `streamlit_drawable_canvas` is absent.
      - Import gate 2: `python -c "import components.agent.agent"` → exit 0 (agent module imports cleanly).
      - Static field-name check (no live calls): grep-confirm that `image_gen.py` uses `search_prompt` for search_replace and `select_prompt` for search_recolor, and that neither new body includes a `mask`/`mask_uri` key — e.g. `python -c "import components.agent.tools.image_gen,inspect;src=inspect.getsource(__import__('components.agent.tools.image_gen',fromlist=['x']));assert 'search_prompt' in src and 'select_prompt' in src"`. This is a supplement to the body-assertion unit tests in item 9, which are the authoritative verification.
      Write `<WT>/.agents/tasks/image-edit-maskfree/verification.md` (NOTE: write under `.agents/tasks/...`, not inside the worktree tree that gets committed) stating: the gate outcomes; the LOCKED FIX 5 ARN count change (21 → 33); that the USER must re-run `cdk deploy` to apply the new IAM grants before the two tools work live, and that an admin/SSO principal with broad Bedrock access can use them without redeploy; and the streamlit-drawable-canvas install outcome (installed vs. graceful-degradation fallback active).
      Files: `<WT>/.agents/tasks/image-edit-maskfree/verification.md`
      Verify: all three gates above pass and `verification.md` records them.

# Notes / assumptions
- `verification.md` and this plan live under `.agents/tasks/image-edit-maskfree/` so they are never swept into a commit of the worktree.
- No change is required in `iam_arns.py` or `fashion_agent_stack.py` logic — the ARN builder is config-driven and already fans out `us.`-prefixed ids; adding the ids to `config.yml` is sufficient (item 7 confirms and tests this).
- `seed` and `style_preset` are intentionally not exposed as tool arguments, matching the minimal surface chosen for inpaint/outpaint; they can be added later if needed.
