"""Streamlit UI for the in-process Strands fashion agent.

The agent runs in-process (``build_agent`` from ``components.agent.agent``); there
is no managed Bedrock agent runtime call. Result images are discovered via the
``ToolResultCollector`` hook's ``latest_ok_s3_uri`` (the legacy URI-tag
text-parsing path is gone), downloaded from S3, and rendered/offered for
download.

The pure helpers (upload-key building, prose handoff, hook extraction, trace
accumulation) live at module scope so the module imports without executing any
Streamlit page code (``run_app`` is only called from ``if __name__ ==
"__main__"``). This keeps the module import-safe for the G-import gate.
"""

from __future__ import annotations

import io
import uuid

import numpy as np
from PIL import Image

# ----------------------------------------------------------------------------
# Pure, importable logic (no Streamlit side effects).
# ----------------------------------------------------------------------------

INIT_MESSAGE = {
    "role": "assistant",
    "content": "Hi! I'm the AI Stylist chat bot. You can ask me questions about clothes to wear!",
}

# Minimum/maximum pixel bounds for the uploaded image (preserved from the
# legacy UI: embed/generation models prefer images in this range).
_MAX_SIZE = (1024, 1024)
_MIN_SIZE = (256, 256)


def build_upload_key() -> str:
    """Return a fresh ``uploads/<uuid>.png`` S3 key for an uploaded image."""
    return f"uploads/{uuid.uuid4()}.png"


def build_s3_uri(bucket: str, key: str) -> str:
    """Return the ``s3://bucket/key`` URI for an object."""
    return f"s3://{bucket}/{key}"


def build_handoff(prompt: str, s3_uri: str | None) -> str:
    """Build the plain-prose user turn passed to the agent.

    When the user attached an image, append its S3 location in plain prose so
    the model can pass it to the image tools. No XML tag wrapping (the legacy
    ``<input_s3_uri>`` framing is removed).
    """
    if s3_uri:
        return f"{prompt}\n\nThe uploaded image is at {s3_uri}"
    return prompt


def strokes_to_mask_png(canvas_rgba: "np.ndarray", size: tuple[int, int]) -> bytes:
    """Convert a drawable-canvas RGBA stroke layer to a black/white mask PNG.

    Mask contract (matches the Stability inpaint mask): white (255) marks the
    brushed region to REPAINT, black (0) marks the region to KEEP. Any canvas
    pixel with a non-zero alpha channel is treated as brushed.

    Args:
        canvas_rgba: an ``(H, W, 4)`` RGBA array of the stroke layer (e.g.
            ``st_canvas(...).image_data``). A 3-channel array is accepted too,
            in which case any non-black pixel counts as brushed.
        size: the target ``(width, height)`` of the source image; the mask is
            resized to match so inpaint receives matching dimensions.

    Returns:
        PNG bytes of a single-channel ("L") mask resized to ``size``.

    Pure: PIL/numpy only, no Streamlit, no AWS, no network.
    """
    array = np.asarray(canvas_rgba)
    if array.ndim == 3 and array.shape[2] >= 4:
        brushed = array[:, :, 3] > 0
    elif array.ndim == 3:
        brushed = array[:, :, :3].any(axis=2)
    else:
        brushed = array > 0
    mask = np.where(brushed, 255, 0).astype("uint8")
    # A 2-D uint8 array maps to an "L" (8-bit grayscale) image.
    mask_image = Image.fromarray(mask).convert("L")
    # Nearest keeps the mask strictly black/white after the resize.
    mask_image = mask_image.resize(size, resample=Image.NEAREST)
    out = io.BytesIO()
    mask_image.save(out, format="PNG")
    return out.getvalue()


def normalize_image(file_obj) -> bytes:
    """Resize an uploaded image into the model-friendly range and return PNG bytes.

    Images larger than :data:`_MAX_SIZE` are thumbnailed down; images smaller
    than :data:`_MIN_SIZE` are scaled up. The output is always PNG (the pinned
    output format), regardless of the source filename.
    """
    image = Image.open(file_obj)
    original_width, original_height = image.size
    if original_width > _MAX_SIZE[0] or original_height > _MAX_SIZE[1]:
        image.thumbnail(_MAX_SIZE)
    if original_width < _MIN_SIZE[0] or original_height < _MIN_SIZE[1]:
        image = image.resize(_MIN_SIZE)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


class TraceCollector:
    """A Strands ``callback_handler`` that accumulates streamed trace text only.

    Captures streamed reasoning deltas and tool-call announcements for the Trace
    pane. It deliberately does NOT read tool results -- result extraction is the
    ``ToolResultCollector`` hook's job (see ``components.agent.hooks``).
    """

    def __init__(self) -> None:
        self.text = ""

    def __call__(self, **kwargs) -> None:
        data = kwargs.get("data")
        if isinstance(data, str):
            self.text += data
        tool_use = kwargs.get("current_tool_use")
        if tool_use and tool_use.get("name"):
            self.text += f"\n\n[tool call] {tool_use['name']}\n"


# ----------------------------------------------------------------------------
# Streamlit page (only executed when run as a script, never at import time).
# ----------------------------------------------------------------------------


def run_app() -> None:  # pragma: no cover - exercised only via `streamlit run`
    import boto3
    import streamlit as st

    from components.agent.agent import build_agent
    from components.agent.hooks import ToolResultCollector
    from components.agent.settings import ConfigError, load_settings
    from components.agent.tools.s3_io import download_bytes

    # Guided empty-state suggestions, each mapped to a real tool path. Stable
    # keys are derived from the enumerate index below.
    _SUGGESTIONS = [
        "Find me a floral summer dress",
        "What should I wear in London today?",
        "Change my photo background to a beach",
        "Recolor this jacket to navy",
        "Generate a minimalist autumn outfit",
    ]

    # Map a recognized tool name to a friendly live-progress label.
    _PROGRESS_LABELS = {
        "image_lookup": "Searching the catalog…",
        "generate_image": "Generating a look…",
        "inpaint": "Restyling your photo…",
        "outpaint": "Extending your photo…",
        "search_replace": "Replacing the item…",
        "search_recolor": "Recoloring the item…",
        "get_weather": "Checking the weather…",
        "weather": "Checking the weather…",
    }

    st.set_page_config(layout="wide", page_title="Fashion Assistant", page_icon="🛍️")

    # --- Item 1: restrained brand CSS (ONE markdown call, no remote fonts). ---
    st.markdown(
        """
        <style>
          /* Rounded image corners + soft card feel. */
          [data-testid="stImage"] img { border-radius: 14px; }
          .fa-wordmark { font-size: 2.1rem; font-weight: 700; letter-spacing: -0.02em;
                         margin: 0 0 0.1rem 0; line-height: 1.1; }
          .fa-wordmark .fa-dot { color: var(--primary-color, #6C5CE7); }
          .fa-tagline { color: #6b7280; font-size: 0.98rem; margin: 0 0 0.6rem 0; }
          /* Soft card shadow for catalog + upload panels. */
          .fa-card { border: 1px solid #ece9e3; border-radius: 16px; padding: 0.6rem;
                     box-shadow: 0 2px 10px rgba(31,36,48,0.06); background: #fff; }
          /* Pill suggestion buttons: full-width, rounded, subtle. */
          .fa-pills [data-testid="stButton"] > button {
              border-radius: 999px; border: 1px solid #e3dfd7; background: #fff;
              font-weight: 500; padding: 0.4rem 0.9rem; }
          .fa-pills [data-testid="stButton"] > button:hover {
              border-color: var(--primary-color, #6C5CE7); }
          /* Tighter chat spacing + full-width conversation. */
          [data-testid="stChatMessage"] { padding-top: 0.4rem; padding-bottom: 0.4rem; }
          .fa-attach { color: #6b7280; font-size: 0.85rem; margin-top: -0.4rem; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    try:
        settings = load_settings()
    except ConfigError as exc:
        st.error(
            "Configuration error: "
            f"{exc}\n\nRun `cdk deploy --outputs-file variables.json` and the "
            "`s3vectors_ingest.ipynb` notebook once before starting the app."
        )
        st.stop()

    image_bucket = settings.image_bucket

    st.session_state.setdefault("img", None)
    st.session_state.setdefault("previous_img", None)
    st.session_state.setdefault("user_image", None)
    st.session_state.setdefault("upload_key", None)
    st.session_state.setdefault("normalized_png", None)
    # A queued suggestion from a pill click, consumed on the next run.
    st.session_state.setdefault("pending_prompt", None)

    def ensure_agent():
        """Build the agent + hook once per session (New Chat rebuilds)."""
        if "agent" not in st.session_state:
            collector = ToolResultCollector()
            try:
                agent = build_agent(settings, hooks=[collector])
            except ConfigError as exc:
                st.error(
                    f"{exc}\n\nRun the `s3vectors_ingest.ipynb` notebook once to "
                    "create and populate the index."
                )
                st.stop()
            st.session_state["agent"] = agent
            st.session_state["collector"] = collector
        return st.session_state["agent"], st.session_state["collector"]

    def new_chat() -> None:
        """Reset history and rebuild the agent + hook."""
        st.session_state["chat_history"] = [INIT_MESSAGE]
        st.session_state["img"] = None
        st.session_state["previous_img"] = None
        st.session_state["user_image"] = None
        st.session_state["upload_key"] = None
        st.session_state["pending_prompt"] = None
        st.session_state["normalized_png"] = None
        st.session_state.pop("agent", None)
        st.session_state.pop("collector", None)

    def remove_image() -> None:
        """Clear the attached image state (sidebar Remove button)."""
        st.session_state["img"] = None
        st.session_state["previous_img"] = None
        st.session_state["user_image"] = None
        st.session_state["upload_key"] = None
        st.session_state["normalized_png"] = None

    def queue_suggestion(text: str) -> None:
        """Queue a pill suggestion to submit as the next user turn."""
        st.session_state["pending_prompt"] = text

    def upload_image(file_obj) -> str:
        """Normalize + upload an image to ``uploads/<uuid>.png``; return the key.

        Also stashes the normalized PNG bytes in session state so the mask-brush
        canvas can render over (and size its mask to) the exact uploaded image.
        """
        key = build_upload_key()
        data = normalize_image(file_obj)
        st.session_state["normalized_png"] = data
        boto3.client("s3", region_name=settings.primary_region).put_object(
            Bucket=image_bucket, Key=key, Body=data, ContentType="image/png"
        )
        return key

    def upload_mask(mask_bytes: bytes) -> str:
        """Upload a black/white mask PNG to ``uploads/<uuid>_mask.png``.

        Returns the resulting ``s3://`` URI so the agent can pass it to inpaint.
        """
        mask_key = f"uploads/{uuid.uuid4()}_mask.png"
        boto3.client("s3", region_name=settings.primary_region).put_object(
            Bucket=image_bucket, Key=mask_key, Body=mask_bytes, ContentType="image/png"
        )
        return build_s3_uri(image_bucket, mask_key)

    def download_image(s3_uri: str) -> Image.Image:
        s3 = boto3.client("s3", region_name=settings.primary_region)
        return Image.open(io.BytesIO(download_bytes(s3, s3_uri)))

    def render_match_grid(matches: list[dict], msg_key: str) -> None:
        """Render top-K catalog matches as a responsive grid of cards."""
        cols_per_row = 3
        for row_start in range(0, len(matches), cols_per_row):
            row = matches[row_start : row_start + cols_per_row]
            columns = st.columns(cols_per_row)
            for offset, match in enumerate(row):
                card_index = row_start + offset
                column = columns[offset]
                with column:
                    uri = match.get("s3_uri")
                    name = match.get("name") or "Catalog match"
                    distance = match.get("distance")
                    try:
                        card_img = download_image(uri)
                    except Exception as exc:  # noqa: BLE001 - surfaced to the user
                        st.warning(f"Could not load {name}: {exc}")
                        continue
                    st.image(card_img, caption=name, use_container_width=True)
                    if distance is not None:
                        st.caption(f"Similarity hint · distance {distance:.3f}")
                    buffer = io.BytesIO()
                    card_img.save(buffer, format="PNG")
                    st.download_button(
                        label="Download",
                        data=buffer,
                        file_name=f"{name.replace(' ', '_')}.png",
                        mime="image/png",
                        key=f"dl-{msg_key}-{card_index}",
                    )

    def render_assistant_message(chat: dict, msg_key: str) -> None:
        """Render one stored assistant turn full-width (history re-render)."""
        st.markdown(chat["content"])
        if chat.get("matches"):
            render_match_grid(chat["matches"], msg_key)
        if "image" in chat:
            edit = chat.get("edit_source")
            if edit is not None:
                before, after = st.columns(2)
                before.image(
                    edit, caption="Before · your photo", use_container_width=True
                )
                after.image(
                    chat["image"], caption="After · edited", use_container_width=True
                )
            else:
                st.image(
                    chat["image"], caption="Generated look", use_container_width=True
                )
            buffer = io.BytesIO()
            chat["image"].save(buffer, format="PNG")
            st.download_button(
                label="Download image",
                data=buffer,
                file_name="generated_image.png",
                mime="image/png",
                key=f"dl-img-{msg_key}",
            )
        if chat.get("error_message"):
            st.error(chat["error_message"])
        if chat.get("trace"):
            with st.expander("How I did this", expanded=False):
                st.markdown(chat["trace"])

    def latest_error_message(collector, start_index: int) -> str | None:
        """Return a friendly message for the newest error envelope this turn."""
        for _name, result in reversed(collector.results[start_index:]):
            if not isinstance(result, dict) or result.get("status") != "error":
                continue
            try:
                payload = result["content"][0]["json"]
            except (KeyError, IndexError, TypeError):
                continue
            message = payload.get("message")
            if message:
                return (
                    f"That didn't work: {message}. "
                    "If this keeps happening, an admin may need to enable the "
                    "image models in Bedrock."
                )
        return None

    def latest_tool_name(collector, start_index: int) -> str | None:
        """Return the newest tool name recorded this turn (for progress label)."""
        slice_ = collector.results[start_index:]
        if not slice_:
            return None
        return slice_[-1][0]

    # --- Item 1: styled wordmark header + tagline. ---
    st.markdown(
        '<p class="fa-wordmark">Fashion Assistant<span class="fa-dot">.</span></p>'
        '<p class="fa-tagline">Your AI stylist — search the catalog, restyle your '
        "photos, dress for the weather</p>",
        unsafe_allow_html=True,
    )

    if "chat_history" not in st.session_state or not st.session_state["chat_history"]:
        st.session_state["chat_history"] = [INIT_MESSAGE]

    # --- Item 7: styled sidebar upload card. ---
    with st.sidebar:
        st.button("New Chat", on_click=new_chat, type="primary")
        st.markdown("#### Your photo")
        st.session_state["img"] = st.file_uploader(
            "Upload an image", type=["png", "jpeg"], label_visibility="collapsed"
        )

    if st.session_state["img"] is not None:
        if st.session_state["img"] != st.session_state["previous_img"]:
            key = upload_image(st.session_state["img"])
            st.session_state["previous_img"] = st.session_state["img"]
            st.session_state["user_image"] = st.session_state["img"]
            st.session_state["upload_key"] = key

    with st.sidebar:
        if st.session_state["user_image"] is not None:
            st.image(
                st.session_state["user_image"],
                caption=getattr(st.session_state["user_image"], "name", "Attached"),
                use_container_width=True,
            )
            st.button("Remove image", on_click=remove_image, key="remove-image")
        else:
            st.caption("Attach a photo to restyle, recolor, or extend it.")

    for index, chat in enumerate(st.session_state["chat_history"]):
        with st.chat_message(chat["role"]):
            if chat["role"] == "assistant":
                render_assistant_message(chat, msg_key=f"hist-{index}")
            else:
                st.markdown(chat["content"])

    # --- Item 4: guided empty state (only the INIT greeting present). ---
    history = st.session_state["chat_history"]
    if len(history) == 1 and history[0] is INIT_MESSAGE:
        st.markdown("##### Try one of these")
        st.markdown('<div class="fa-pills">', unsafe_allow_html=True)
        pill_cols = st.columns(2)
        for pill_index, suggestion in enumerate(_SUGGESTIONS):
            column = pill_cols[pill_index % 2]
            column.button(
                suggestion,
                key=f"pill-{pill_index}",
                on_click=queue_suggestion,
                args=(suggestion,),
                use_container_width=True,
            )
        st.markdown("</div>", unsafe_allow_html=True)

    if st.session_state["user_image"] is not None:
        st.markdown('<p class="fa-attach">📎 Image attached</p>', unsafe_allow_html=True)

    # --- Mask-free vs. precise edit helper hint. ---
    st.caption(
        "Describe the change in words for an automatic edit (replace or recolor "
        "an item — no mask needed), or brush a region below for a precise inpaint."
    )

    # --- Optional in-UI mask brush (precise inpaint without a hand-made mask). ---
    # Builds a black/white mask from brush strokes over the uploaded image and
    # uploads it; the agent then passes its s3:// URI to inpaint. Collapsed by
    # default so the mask-free search_replace/recolor path stays the easy choice.
    mask_uri = None
    if (
        st.session_state["user_image"] is not None
        and st.session_state.get("normalized_png")
    ):
        with st.expander("Precise edit — brush a region (optional)", expanded=False):
            try:
                # Compat shim: streamlit-drawable-canvas 0.9.3 imports
                # `image_to_url` from `streamlit.elements.image`, but Streamlit
                # >=1.40 moved it to `streamlit.elements.lib.image_utils`.
                # Restore the symbol in its old location before importing the
                # canvas so the import does not raise AttributeError.
                import streamlit.elements.image as _st_image_mod

                if not hasattr(_st_image_mod, "image_to_url"):
                    from streamlit.elements.lib.image_utils import (
                        image_to_url as _image_to_url,
                    )

                    _st_image_mod.image_to_url = _image_to_url

                from streamlit_drawable_canvas import st_canvas
            except ImportError:
                st.info(
                    "Mask brush unavailable — install `streamlit-drawable-canvas` "
                    "to brush a region. You can still describe the change in words "
                    "for an automatic edit."
                )
                st_canvas = None
            except Exception as exc:  # noqa: BLE001 - canvas is optional
                st.info(
                    "Mask brush unavailable "
                    f"({exc}). You can still describe the change in words "
                    "for an automatic edit."
                )
                st_canvas = None

            if st_canvas is not None:
                source_image = Image.open(
                    io.BytesIO(st.session_state["normalized_png"])
                ).convert("RGB")
                source_width, source_height = source_image.size
                st.caption(
                    "Brush over the area to repaint (white = repaint, "
                    "untouched = keep)."
                )
                canvas_result = st_canvas(
                    fill_color="rgba(255, 255, 255, 1.0)",
                    stroke_width=30,
                    stroke_color="rgba(255, 255, 255, 1.0)",
                    background_image=source_image,
                    update_streamlit=True,
                    height=source_height,
                    width=source_width,
                    drawing_mode="freedraw",
                    key="mask-canvas",
                )
                strokes = getattr(canvas_result, "image_data", None)
                if strokes is not None and np.asarray(strokes)[:, :, 3].any():
                    mask_bytes = strokes_to_mask_png(
                        strokes, (source_width, source_height)
                    )
                    mask_uri = upload_mask(mask_bytes)
                    st.caption("Mask ready — your next message will use it to inpaint.")

    typed = st.chat_input("Start your conversation...")
    prompt = st.session_state.pop("pending_prompt", None) or typed

    if prompt:
        st.session_state["chat_history"].append({"role": "human", "content": prompt})
        with st.chat_message("human"):
            st.markdown(prompt)

        agent, collector = ensure_agent()
        # Snapshot how many results already exist so we only consider this turn.
        results_before = len(collector.results)

        s3_uri = None
        if st.session_state["user_image"] is not None and st.session_state["upload_key"]:
            s3_uri = build_s3_uri(image_bucket, st.session_state["upload_key"])
        handoff = build_handoff(prompt, s3_uri)
        if mask_uri:
            handoff = (
                f"{handoff}\n\nA black/white mask for a precise inpaint is at "
                f"{mask_uri} (white = repaint, black = keep)."
            )

        with st.chat_message("assistant"):
            trace = TraceCollector()
            agent.callback_handler = trace

            # --- Item 5: live progress via st.status (hasattr-gated). ---
            if hasattr(st, "status"):
                with st.status("Thinking…", expanded=False) as status:
                    result = agent(handoff)
                    tool_name = latest_tool_name(collector, results_before)
                    status.update(
                        label=_PROGRESS_LABELS.get(tool_name, "Done"),
                        state="complete",
                    )
            else:
                with st.spinner("Working on it…"):
                    result = agent(handoff)
            response_text = str(result)

            st.markdown(response_text)

            msg_key = f"live-{len(st.session_state['chat_history'])}"
            chat_entry = {
                "role": "assistant",
                "content": response_text,
                "trace": trace.text,
            }

            # --- Item 2: top-K catalog grid from FEAT-001's accessor. ---
            matches = collector.latest_image_lookup_matches()
            # Only treat matches as fresh if the lookup ran this turn.
            lookup_this_turn = any(
                name == "image_lookup"
                for name, _ in collector.results[results_before:]
            )
            if matches and lookup_this_turn:
                render_match_grid(matches, msg_key)
                chat_entry["matches"] = matches

            # --- Item 3: single large image + before/after for edits. ---
            result_uri = collector.latest_ok_s3_uri()
            if result_uri:
                try:
                    generated_img = download_image(result_uri)
                except Exception as exc:  # noqa: BLE001 - surfaced to the user
                    st.warning(f"Could not load result image: {exc}")
                    generated_img = None
                if generated_img is not None:
                    edit_source = None
                    if st.session_state["user_image"] is not None:
                        try:
                            edit_source = Image.open(st.session_state["user_image"])
                        except Exception:  # noqa: BLE001 - best-effort before/after
                            edit_source = None
                    if edit_source is not None:
                        before, after = st.columns(2)
                        before.image(
                            edit_source,
                            caption="Before · your photo",
                            use_container_width=True,
                        )
                        after.image(
                            generated_img,
                            caption="After · edited",
                            use_container_width=True,
                        )
                        chat_entry["edit_source"] = edit_source
                    else:
                        st.image(
                            generated_img,
                            caption="Generated look",
                            use_container_width=True,
                        )
                    buffer = io.BytesIO()
                    generated_img.save(buffer, format="PNG")
                    st.download_button(
                        label="Download image",
                        data=buffer,
                        file_name="generated_image.png",
                        mime="image/png",
                        key=f"dl-img-{msg_key}",
                    )
                    chat_entry["image"] = generated_img

            # --- Correctness: surface tool error envelopes as friendly cards. ---
            error_message = latest_error_message(collector, results_before)
            if error_message:
                st.error(error_message)
                chat_entry["error_message"] = error_message

            # --- Item 6: trace lives in a collapsed expander, full width. ---
            if trace.text:
                with st.expander("How I did this", expanded=False):
                    st.markdown(trace.text)

            st.session_state["chat_history"].append(chat_entry)


if __name__ == "__main__":
    run_app()
