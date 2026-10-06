# Verification — mask-free image edits (search_replace / search_recolor) + in-UI mask brush

Deploy-free verification run from the worktree
`/Users/erictole/demo/fashion-assistant-agent/.worktrees/image-edit-maskfree`.
No real AWS/Bedrock calls, no `cdk deploy`, no `streamlit run`. All boto3 mocked.

Env: Python 3.13.3, streamlit 1.60.0, numpy 2.4.2, boto3 1.43.107, Pillow present.

## Gate outcomes

### 1. `python -m pytest -q`  → PASS
`56 passed` (baseline was 42). New/changed tests:
- `tests/test_image_gen.py`: added search_replace + search_recolor cases
  (correct profile id + primary region us-east-1; body carries the right
  selector field and NO stray/invalid fields; optional
  negative_prompt/grow_mask only when passed; empty-prompt and empty-selector
  validation → error; content-filter → error; `.png` output key; ContentType
  `image/png`).
- `tests/test_iam_arns.py`: added the two new `us.`-prefixed ids to `MODELS`
  and `US_PREFIXED`; `test_expected_total_arn_count` updated 21 → 33.
- `tests/test_settings.py`: added the two ids to the inline config and assert
  both resolve and start with `us.`.
- `tests/test_frontend_mask.py` (new): PIL-based test of the pure
  `strokes_to_mask_png` helper — mode "L"/"1", output size == requested size,
  brushed pixels 255 (white = repaint), untouched 0 (black = keep), strictly
  black/white after a nearest-neighbor resize.

### 2. Import gate — `python -c "import frontend.app"`  → PASS
Imports with NO Streamlit page execution and does NOT hard-fail even though
`streamlit_drawable_canvas` is absent in this env. The canvas import is lazy
inside `run_app()` (try/except ImportError → friendly "mask brush unavailable"
`st.info`, no crash). `strokes_to_mask_png` is a pure module-scope helper
(numpy at module scope is import-safe; already an indirect dep).

### 3. Import gate — `python -c "import components.agent.agent"`  → PASS
Agent module imports cleanly; `build_agent` wires `make_search_replace` and
`make_search_recolor` on the primary-region (us-east-1) client alongside
inpaint/outpaint. (`build_agent` itself hits GetIndex at runtime and is not
invoked in tests.)

### 4. Static field-name check (no live calls)  → PASS
Inspected the two new `*_impl` body dicts directly:
- search_replace body sets `"search_prompt": search_prompt`; references NO
  `select_prompt`, NO `"mask"`, NO `mask_source`.
- search_recolor body sets `"select_prompt": select_prompt`; carries NO
  `"search_prompt"`, NO `"mask"`, NO `mask_source`.
The body-assertion unit tests in `tests/test_image_gen.py` are the
authoritative verification; this static check supplements them.

## Verified Stability request field lists (authoritative)

Both models are INFERENCE-PROFILE only, ACTIVE in us-east-1, invoked on the
PRIMARY-region (us-east-1) client exactly like inpaint/outpaint. Shared
response envelope `{images:[b64], finish_reasons:[...], seeds:[...]}` decoded
via `_decode_image` (non-null `finish_reasons[0]` → filtered → error).
`output_format` stays `"png"`.

- **search_replace** — profile id `us.stability.stable-image-search-replace-v1:0`.
  Accepted fields: `image`, `prompt`, `search_prompt`, `grow_mask`,
  `negative_prompt`, `seed`, `output_format`, `style_preset`.
  Region selector = `search_prompt`. MASK-FREE.
  Body emitted by `search_replace_impl`: `prompt`, `image`, `search_prompt`,
  `output_format="png"` (+ `negative_prompt`/`grow_mask` only when provided).
- **search_recolor** — profile id `us.stability.stable-image-search-recolor-v1:0`.
  Accepted fields: `image`, `prompt`, `negative_prompt`, `select_prompt`,
  `seed`, `grow_mask`, `output_format`, `style_preset`.
  Region selector = `select_prompt` (NOT `search_prompt`). MASK-FREE.
  Body emitted by `search_recolor_impl`: `prompt`, `image`, `select_prompt`,
  `output_format="png"` (+ `negative_prompt`/`grow_mask` only when provided).

`seed` and `style_preset` are intentionally not exposed as tool arguments,
matching the minimal surface chosen for inpaint/outpaint.

## IAM / LOCKED FIX 5

No logic change in `components/stacks/iam_arns.py` or
`components/stacks/fashion_agent_stack.py` — the ARN builder is config-driven
and already fans each `us.`-prefixed id out to one inference-profile ARN + one
foundation-model ARN per fan-out region {us-east-1, us-east-2, us-west-2}.
Adding the two ids to `config.yml` grants them automatically.

ARN count change: **21 → 33** (5 `us.`-prefixed × 3 regions × 2 ARNs = 30, plus
3 bare × 1 = 33). The per-region/per-id assertions in `test_iam_arns.py` cover
the new ids.

**The USER must re-run `cdk deploy` to apply the new IAM grants before the two
tools work live.** An admin/SSO principal with broad Bedrock access can already
invoke the two new models without a redeploy.

## streamlit-drawable-canvas install outcome

Pinned `streamlit-drawable-canvas==0.9.3` in `pyproject.toml`.
`pip install --dry-run streamlit-drawable-canvas==0.9.3` resolved cleanly
against the installed streamlit 1.60.0 ("Would install
streamlit-drawable-canvas-0.9.3"). The package is NOT installed in this
verification env, so the graceful-degradation path is the one exercised: the
import gate passes and the UI would show the friendly "mask brush unavailable"
note instead of crashing. The hard guarantee is the graceful degradation, not
the exact pin; if 0.9.3 proves incompatible at install time, 0.12.0 (requires
streamlit >= 1.53) is the fallback.

## Frontend mask-brush UX summary

- Lazy canvas import inside `run_app()`; collapsed `st.expander`
  ("Precise edit — brush a region (optional)") shown only when a user image is
  attached. Canvas draws over the normalized source image; freedraw, white
  stroke; stable widget key `"mask-canvas"` (no uuid4 widget keys).
- On strokes present, `strokes_to_mask_png` builds a black/white mask sized to
  the normalized source dims; uploaded to `uploads/<uuid>_mask.png` via the
  existing boto3 put_object pattern (ContentType `image/png`); its `s3://` URI
  is appended to the plain-prose handoff (no XML) so the agent passes it as
  `mask_uri` to inpaint.
- A short helper hint explains the two paths; the mask step is optional/
  collapsible so the mask-free search_replace/recolor path stays the easy
  default. Premium theme/CSS and before/after rendering unchanged.
