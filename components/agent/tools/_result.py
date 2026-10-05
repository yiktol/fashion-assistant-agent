"""Native Strands ``ToolResult`` envelope builders.

Every tool returns the native ToolResult shape so its structured payload
survives as JSON rather than being stringified by Strands (a bare dict without
a ``content`` key is wrapped as a ``text`` block). See design.md
"Strands tool interfaces".

The Strands ``status`` slot is ONLY ``"success"`` or ``"error"`` (LOCKED FIX 6).
A recoverable semantic miss (e.g. a weather geocode miss, a zero-hit lookup) is
``status="success"`` with a nested ``result="not_found"`` in the json block.
"""

from __future__ import annotations

from typing import Any


def ok(**fields: Any) -> dict[str, Any]:
    """Build a success envelope with ``result="ok"`` plus arbitrary fields."""
    payload: dict[str, Any] = {"result": "ok"}
    payload.update(fields)
    return {"status": "success", "content": [{"json": payload}]}


def not_found(**fields: Any) -> dict[str, Any]:
    """Build a *recoverable* miss envelope.

    The Strands ``status`` stays ``"success"`` (the event loop is healthy) while
    the nested ``result`` is ``"not_found"`` so the model/UI see the semantic
    miss (LOCKED FIX 6).
    """
    payload: dict[str, Any] = {"result": "not_found"}
    payload.update(fields)
    return {"status": "success", "content": [{"json": payload}]}


def err(message: str, result: str = "error") -> dict[str, Any]:
    """Build an error envelope with ``status="error"``."""
    return {
        "status": "error",
        "content": [{"json": {"result": result, "s3_uri": None, "message": message}}],
    }
