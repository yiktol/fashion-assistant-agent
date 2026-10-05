# Implementation Plan — Fashion Assistant modern UI/UX

Worktree (ALL paths absolute, work here only):
`/Users/erictole/demo/fashion-assistant-agent/.worktrees/frontend-modern-ux`

Baseline verified before planning: `python3 -m pytest -q` → 35 passed; `python -c "import frontend.app"` → import-safe (no page code runs); `streamlit 1.60.0` with both `st.status` and `st.dialog` present (gate anyway per constraint); branch `frontend-modern-ux`, clean tree. Vector metadata confirmed to carry BOTH `s3_uri` and `name` (`s3vectors_ingest.ipynb` writes `{"s3_uri": catalog_uri, "name": name}`).

Design decisions (made here, grounded in code):
- **Result-shape contract.** `image_lookup` keeps its existing top-level keys (`result`, `s3_uri`, `distance`, `message`) for backward-compat with `latest_ok_s3_uri()` and existing tests, and ADDS a `matches` list: `matches: list[{"s3_uri": str, "name": str | None, "distance": float | None}]`. `s3_uri`/`distance` continue to mirror `matches[0]`. Rationale: additive change keeps every existing test and the hook's URI accessor green while exposing all top-K hits.
- **New hook accessor.** Add `latest_image_lookup_matches() -> list[dict]` to `ToolResultCollector`, scanning newest-first for the latest `result=="ok"` payload from the `image_lookup` tool and returning its `matches` (empty list when none). Keyed on tool name `"image_lookup"` (the hook already records `(tool_name, result)`). Rationale: a dedicated accessor keeps `latest_ok_s3_uri()` untouched for generate/inpaint/outpaint images.
- **Streamlit API gating.** Wrap `st.status`/`st.dialog` use behind `hasattr(st, "status")` / `hasattr(st, "dialog")` with `st.spinner`/inline fallbacks, so the module works even on older Streamlit. No new deps, no external fonts.
- **Stable widget keys.** Replace every `key=str(uuid.uuid4())` with deterministic keys derived from message index + card index (e.g. `f"dl-{index}-{card}"`), so Streamlit reruns don't orphan widget state.

HARD CONSTRAINTS honored throughout: pure helpers stay at module scope with no Streamlit side effects; ALL page code stays inside `run_app()` under `if __name__ == "__main__"`; no hardcoded regions/bucket/index (read from `settings`); no real AWS, no `cdk deploy`, no `streamlit run`; verification is deploy-free with mocked boto3.

---

- [ ] 1. Extend `image_lookup` to return ALL top-K matches (additive `matches` list) while preserving the existing top-level keys.
      In `image_lookup_impl`, after `hits = response.get("vectors")`, build `matches` from every hit that has a `metadata.s3_uri`, each as `{"s3_uri": metadata["s3_uri"], "name": metadata.get("name"), "distance": hit.get("distance")}`. Keep the zero-hit `not_found` path and the "top hit has no s3_uri" `not_found` path unchanged. On success call `ok(s3_uri=matches[0]["s3_uri"], distance=matches[0]["distance"], matches=matches, message="match found")`. If NO hit has an `s3_uri`, keep returning `not_found` as today.
      Files: `components/agent/tools/image_lookup.py`
      Verify: `python3 -m pytest -q tests/test_image_lookup.py` — existing tests pass; new multi-match test (item 3) passes.

- [ ] 2. Add the `latest_image_lookup_matches()` accessor to `ToolResultCollector` (keep `latest_ok_s3_uri()` unchanged).
      New method scans `self.results` newest-first; for the first pair whose tool name is `"image_lookup"` and whose payload `result == "ok"`, return `payload.get("matches", [])`; return `[]` when none found. Guard payload extraction with the same try/except (KeyError, IndexError, TypeError) pattern already used in `latest_ok_s3_uri()`.
      Files: `components/agent/hooks.py`
      Verify: `python3 -m pytest -q tests/test_envelope.py` — existing collector tests pass; new accessor tests (item 3) pass.

- [ ] 3. Add/adjust unit tests for the new result shape and accessor.
      In `tests/test_image_lookup.py`: add a test feeding RecordingS3Vectors three hits (two with `metadata.s3_uri` + `name`, mixed distances, and confirm `payload["matches"]` has the expected length, order, `name`, and `distance`, and that `payload["s3_uri"]`/`distance` still equal `matches[0]`). In `tests/test_envelope.py`: add a test that records an `image_lookup` `ok` result carrying `matches=[...]` and asserts `latest_image_lookup_matches()` returns that list, plus a test that it returns `[]` when the latest `image_lookup` is `not_found` and when no `image_lookup` ran.
      Files: `tests/test_image_lookup.py`, `tests/test_envelope.py`
      Verify: `python3 -m pytest -q` — full suite green (≥ 35 + new tests).

- [ ] 4. Add the Streamlit theme file (premium-neutral, one accent, readable type; no external assets).
      Create `.streamlit/config.toml` with a `[theme]` block: neutral base, one tasteful accent `primaryColor`, light `backgroundColor`/`secondaryBackgroundColor`, dark readable `textColor`, and a system/Google-font-stack `font` fallback (no webfont download). Values are static config, read by Streamlit at launch.
      Files: `.streamlit/config.toml`
      Verify: `python3 - <<'PY'` loads the file with `tomllib` and asserts a `[theme]` table with `primaryColor` exists — confirms valid TOML. (No `streamlit run`.)

- [ ] 5. Rewrite the page body in `run_app()`: brand header + restrained CSS, full-width conversation, trace-as-expander. (Pure helpers and the `__main__` guard are untouched.)
      Inject restrained CSS via one `st.markdown(..., unsafe_allow_html=True)` call at the top of `run_app` (rounded image corners, soft card shadows, pill buttons, tighter chat spacing; no `@import` of remote fonts). Replace `st.title("Fashion Assistant")` with a styled wordmark header + tagline "Your AI stylist — search the catalog, restyle your photos, dress for the weather". Remove the `st.columns((5,4,1))` hack and the Trace checkbox everywhere; render each assistant message full width with its reasoning trace inside a collapsed `st.expander("How I did this")`. Keep `st.set_page_config`, `load_settings()` fail-fast (`ConfigError` → styled `st.error` + `st.stop()`), `ensure_agent`, `new_chat`, `upload_image`, `download_image` behavior intact.
      Files: `frontend/app.py`
      Verify: `python -c "import frontend.app"` — still import-safe (page code does NOT execute). `python3 -m pytest -q` — unaffected, green.

- [ ] 6. Render `image_lookup` results as a responsive top-K grid; render single-image tool results large with before/after for edits.
      Using `latest_image_lookup_matches()`, when matches exist render a responsive grid via `st.columns` (e.g. up to 3 per row) of cards: image downloaded via `download_image`/`download_bytes` from each match `s3_uri` (sized by `use_container_width`, larger than the old 200px), the `name` (fallback to a generic caption when `name` is `None`), an optional similarity hint from `distance`, and a per-card download button with a STABLE key `f"dl-{index}-{card}"`. Optionally offer a larger view gated on `hasattr(st, "dialog")`. For `generate_image`/`inpaint`/`outpaint` single-image results (via `latest_ok_s3_uri()`), render the result large; for inpaint/outpaint show uploaded SOURCE and EDITED result side by side (before/after) using `st.columns(2)` and meaningful captions/alt text. Persist rendered results into `chat_entry` so history re-renders deterministically with the same stable keys.
      Files: `frontend/app.py`
      Verify: `python -c "import frontend.app"` import-safe; `python3 -m pytest -q` green.

- [ ] 7. Guided empty state, live tool progress, richer upload panel, and friendly error cards.
      Empty state: when `chat_history` holds only the INIT greeting, render 4–6 clickable pill suggestion buttons (stable keys) mapped to real tools ("Find me a floral summer dress", "What should I wear in London today?", "Change my photo background to a beach", "Recolor this jacket to navy", "Generate a minimalist autumn outfit"); a click submits that text as the user's turn (same path as `st.chat_input`). Live progress: wrap the `agent(handoff)` call in `st.status(...)` when `hasattr(st, "status")` else `st.spinner(...)`, updating the label from `TraceCollector`/hook tool-call announcements ("Searching the catalog…", "Generating a look…", "Checking the weather…"); keep final response rendering intact. Upload panel: keep the uploader in the sidebar but as a styled card with a visible thumbnail, filename, and a "Remove image" button that clears `img`/`previous_img`/`user_image`/`upload_key`; show a subtle "image attached" indicator near the chat input. PRESERVE `normalize_image` + upload-to-`uploads/<uuid>.png` and `build_handoff` plain prose (no `<input_s3_uri>` tags). Error cards: `ConfigError` already fail-fast as a clean message; for image-tool error envelopes (`status=="error"`), surface the human-readable `message` from the envelope as a friendly card (e.g. "Image generation isn't enabled yet — ask an admin to enable the Stability models") instead of a raw trace. Replace any remaining `uuid.uuid4()` widget keys with deterministic ones.
      Files: `frontend/app.py`
      Verify: `python -c "import frontend.app"` import-safe; `python3 -m pytest -q` full suite green.

- [ ] 8. Final integration gate (deploy-free).
      Run the full import-safety + unit-test gates together and confirm no regression and no `uuid.uuid4()` left on widget keys.
      Files: (none — verification only)
      Verify: `python -c "import frontend.app" && python3 -m pytest -q` — import-safe and all tests pass; `grep -n "uuid.uuid4()" frontend/app.py` returns only non-widget-key uses (upload key building via `build_upload_key`), none on `key=`.

## Notes / assumptions
- The task brief said Streamlit 1.65.0; the worktree actually has 1.60.0. Both target APIs (`st.status`, `st.dialog`) exist there, but every use is gated on `hasattr` with a graceful fallback per the hard constraint, so either version is safe.
- `name` may be absent on some vectors (older ingests); the UI treats `name is None` as a generic caption rather than erroring.
- No new dependencies; no external/network fonts; no real AWS, no `cdk deploy`, no `streamlit run`.
