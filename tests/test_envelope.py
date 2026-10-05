"""Tests for the ToolResult envelope builders and the result-collector hook."""

from __future__ import annotations

from components.agent.hooks import ToolResultCollector
from components.agent.tools._result import err, not_found, ok


def test_ok_envelope_shape():
    env = ok(s3_uri="s3://b/k.png", message="done")
    assert env["status"] == "success"
    payload = env["content"][0]["json"]
    assert payload["result"] == "ok"
    assert payload["s3_uri"] == "s3://b/k.png"


def test_not_found_is_success_with_nested_result():
    env = not_found(description="default", temperature_f=None)
    assert env["status"] == "success"  # LOCKED FIX 6
    assert env["content"][0]["json"]["result"] == "not_found"


def test_err_is_error_status():
    env = err("boom")
    assert env["status"] == "error"
    payload = env["content"][0]["json"]
    assert payload["result"] == "error"
    assert payload["s3_uri"] is None
    assert payload["message"] == "boom"


class FakeToolUse(dict):
    pass


class FakeAfterToolCallEvent:
    """Minimal stand-in matching the fields the collector reads."""

    def __init__(self, name, result):
        self.tool_use = {"name": name}
        self.result = result


class FakeRegistry:
    def __init__(self):
        self.callbacks = {}

    def add_callback(self, event_type, cb):
        self.callbacks[event_type] = cb


def test_collector_extracts_latest_ok_s3_uri():
    collector = ToolResultCollector()
    # Simulate two tool calls: a not_found then an ok.
    collector._on_after_tool_call(
        FakeAfterToolCallEvent("image_lookup", not_found(s3_uri=None))
    )
    collector._on_after_tool_call(
        FakeAfterToolCallEvent("generate_image", ok(s3_uri="s3://b/gen.png"))
    )
    assert collector.latest_ok_s3_uri() == "s3://b/gen.png"
    assert [name for name, _ in collector.results] == [
        "image_lookup",
        "generate_image",
    ]


def test_collector_returns_none_when_no_ok():
    collector = ToolResultCollector()
    collector._on_after_tool_call(
        FakeAfterToolCallEvent("image_lookup", not_found(s3_uri=None))
    )
    assert collector.latest_ok_s3_uri() is None


def test_collector_registers_on_after_tool_call():
    from strands.hooks import AfterToolCallEvent

    collector = ToolResultCollector()
    registry = FakeRegistry()
    collector.register_hooks(registry)
    assert AfterToolCallEvent in registry.callbacks
