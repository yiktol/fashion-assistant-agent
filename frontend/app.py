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

    st.set_page_config(layout="wide", page_title="Fashion Assistant", page_icon="🛍️")

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
        st.session_state.pop("agent", None)
        st.session_state.pop("collector", None)

    def upload_image(file_obj) -> str:
        """Normalize + upload an image to ``uploads/<uuid>.png``; return the key."""
        key = build_upload_key()
        data = normalize_image(file_obj)
        boto3.client("s3", region_name=settings.primary_region).put_object(
            Bucket=image_bucket, Key=key, Body=data, ContentType="image/png"
        )
        return key

    def download_image(s3_uri: str) -> Image.Image:
        s3 = boto3.client("s3", region_name=settings.primary_region)
        return Image.open(io.BytesIO(download_bytes(s3, s3_uri)))

    st.title("Fashion Assistant")

    if "chat_history" not in st.session_state or not st.session_state["chat_history"]:
        st.session_state["chat_history"] = [INIT_MESSAGE]

    with st.sidebar:
        st.button("New Chat", on_click=new_chat, type="primary")
        st.session_state["img"] = st.file_uploader(
            "Upload an image", type=["png", "jpeg"], label_visibility="collapsed"
        )

    if st.session_state["img"] is not None:
        if st.session_state["img"] != st.session_state["previous_img"]:
            key = upload_image(st.session_state["img"])
            st.session_state["previous_img"] = st.session_state["img"]
            st.session_state["user_image"] = st.session_state["img"]
            st.session_state["upload_key"] = key
    else:
        st.session_state["user_image"] = None
        st.session_state["upload_key"] = None

    if st.session_state["user_image"] is not None:
        st.image(st.session_state["user_image"], caption="Uploaded Image", width=200)

    for index, chat in enumerate(st.session_state["chat_history"]):
        with st.chat_message(chat["role"]):
            if chat["role"] == "assistant":
                col1, col2, col3 = st.columns((5, 4, 1))
                col1.markdown(chat["content"])
                if "image" in chat:
                    col1.image(chat["image"], caption="Result Image", width=200)
                    buffer = io.BytesIO()
                    chat["image"].save(buffer, format="PNG")
                    col1.download_button(
                        label="Download Image",
                        data=buffer,
                        file_name="generated_image.png",
                        mime="image/png",
                        key=str(uuid.uuid4()),
                    )
                if chat.get("trace") and col3.checkbox(
                    "Trace", value=False, key=f"trace-{index}"
                ):
                    col2.subheader("Trace")
                    col2.markdown(chat["trace"])
            else:
                st.markdown(chat["content"])

    if prompt := st.chat_input("Start your conversation..."):
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

        with st.chat_message("assistant"):
            col1, col2, col3 = st.columns((5, 4, 1))
            trace = TraceCollector()
            agent.callback_handler = trace
            result = agent(handoff)
            response_text = str(result)

            col1.markdown(response_text)

            chat_entry = {
                "role": "assistant",
                "content": response_text,
                "trace": trace.text,
            }

            result_uri = collector.latest_ok_s3_uri()
            if result_uri:
                try:
                    generated_img = download_image(result_uri)
                except Exception as exc:  # noqa: BLE001 - surfaced to the user
                    col1.warning(f"Could not load result image: {exc}")
                    generated_img = None
                if generated_img is not None:
                    col1.image(generated_img, caption="Result Image", width=200)
                    buffer = io.BytesIO()
                    generated_img.save(buffer, format="PNG")
                    col1.download_button(
                        label="Download Image",
                        data=buffer,
                        file_name="generated_image.png",
                        mime="image/png",
                        key=str(uuid.uuid4()),
                    )
                    chat_entry["image"] = generated_img

            if trace.text and col3.checkbox(
                "Trace", value=True, key=f"trace-live-{len(st.session_state['chat_history'])}"
            ):
                col2.subheader("Trace")
                col2.markdown(trace.text)

            st.session_state["chat_history"].append(chat_entry)
        # Keep `results_before` referenced for clarity on turn boundaries.
        _ = results_before


if __name__ == "__main__":
    run_app()
