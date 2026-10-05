"""Strands hook provider for collecting tool results during a turn.

``ToolResultCollector`` registers a callback on ``AfterToolCallEvent`` (the
verified stable event in ``strands.hooks``) and records each
``(tool_name, result)`` pair. The UI scans the collected results newest-first
for the first ``ok`` result carrying a non-null ``s3_uri`` -- this replaces the
deleted legacy URI-tag text-parsing path.

``callback_handler`` (trace text) and this hook (result extraction) never
overlap: the hook is the only result-extraction path.
"""

from __future__ import annotations

import logging

from strands.hooks import AfterToolCallEvent, HookProvider, HookRegistry

logger = logging.getLogger(__name__)


class ToolResultCollector(HookProvider):
    """Collects ``(tool_name, ToolResult)`` pairs from ``AfterToolCallEvent``."""

    def __init__(self) -> None:
        self.results: list[tuple[str, dict]] = []

    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
        registry.add_callback(AfterToolCallEvent, self._on_after_tool_call)

    def _on_after_tool_call(self, event: AfterToolCallEvent) -> None:
        self.results.append((event.tool_use["name"], event.result))

    def latest_ok_s3_uri(self) -> str | None:
        """Return the newest ``ok`` result's non-null ``s3_uri``, else ``None``."""
        for _name, result in reversed(self.results):
            try:
                payload = result["content"][0]["json"]
            except (KeyError, IndexError, TypeError):
                continue
            if payload.get("result") == "ok" and payload.get("s3_uri"):
                return payload["s3_uri"]
        return None
