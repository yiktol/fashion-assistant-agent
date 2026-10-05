"""Unit tests for the weather tool (open-meteo mocked)."""

from __future__ import annotations

import json

from components.agent.tools.weather import get_weather_impl


class FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class FakeHttp:
    """Serves canned geocode then forecast responses in call order."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        return self._responses.pop(0)


def test_weather_ok():
    http = FakeHttp(
        [
            FakeResponse(200, {"results": [{"latitude": 51.5, "longitude": -0.12}]}),
            FakeResponse(
                200, {"current_weather": {"temperature": 60.0, "weathercode": 2}}
            ),
        ]
    )
    result = get_weather_impl("London", http=http)
    assert result["status"] == "success"
    payload = result["content"][0]["json"]
    assert payload["result"] == "ok"
    assert payload["description"] == "Partly cloudy"
    assert payload["temperature_f"] == 60.0
    # 10s timeout reused from the legacy lambda.
    assert http.calls[0]["timeout"] == 10


def test_weather_geocode_miss_is_recoverable_not_found():
    # LOCKED FIX 6: a miss stays status='success' with nested result='not_found'.
    http = FakeHttp([FakeResponse(200, {"results": []})])
    result = get_weather_impl("Nowheresville", http=http)
    assert result["status"] == "success"
    payload = result["content"][0]["json"]
    assert payload["result"] == "not_found"
    assert payload["temperature_f"] is None
    assert isinstance(payload["description"], str) and payload["description"]


def test_weather_forecast_non_200_is_not_found():
    http = FakeHttp(
        [
            FakeResponse(200, {"results": [{"latitude": 1.0, "longitude": 2.0}]}),
            FakeResponse(500, {}),
        ]
    )
    result = get_weather_impl("Paris", http=http)
    assert result["status"] == "success"
    assert result["content"][0]["json"]["result"] == "not_found"
