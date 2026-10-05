"""Weather tool: Open-Meteo geocode + current-forecast lookup.

Reuses the open-meteo logic and the WMO weather-code table from the old
``lambda_function.py``. The dead legacy embedding "WEATHER" taskType payload is
dropped.

LOCKED FIX 6: a geocode miss or non-200 is a *recoverable* miss returned as
``status="success"`` with a nested ``result="not_found"`` (never a Strands
``error`` status).
"""

from __future__ import annotations

import logging

import requests
from strands import tool

from ._result import not_found, ok

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 10
_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_DEFAULT_DESCRIPTION = "no weather information available; using a mild default"

# WMO weather interpretation codes (from the legacy lambda).
_WEATHER_CODE_DICT = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Drizzle: Light intensity",
    53: "Drizzle: Moderate intensity",
    55: "Drizzle: Dense intensity",
    56: "Freezing Drizzle: Light intensity",
    57: "Freezing Drizzle: Dense intensity",
    61: "Rain: Slight intensity",
    63: "Rain: Moderate intensity",
    65: "Rain: Heavy intensity",
    66: "Freezing Rain: Light intensity",
    67: "Freezing Rain: Heavy intensity",
    71: "Snow fall: Slight intensity",
    73: "Snow fall: Moderate intensity",
    75: "Snow fall: Heavy intensity",
    77: "Snow grains",
    80: "Rain showers: Slight intensity",
    81: "Rain showers: Moderate intensity",
    82: "Rain showers: Violent intensity",
    85: "Snow showers: Slight intensity",
    86: "Snow showers: Heavy intensity",
    95: "Thunderstorm: Slight or moderate",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


def _geocode(location_name: str, http) -> tuple[float | None, float | None]:
    response = http.get(
        _GEOCODE_URL, params={"name": location_name}, timeout=REQUEST_TIMEOUT
    )
    if response.status_code != 200:
        return None, None
    data = response.json()
    results = data.get("results")
    if not results:
        return None, None
    first = results[0]
    return first.get("latitude"), first.get("longitude")


def get_weather_impl(location_name: str, http=requests) -> dict:
    """Resolve a location to current weather, returning the native envelope.

    On a geocode miss or non-200 forecast response, returns a recoverable
    ``not_found`` envelope with a default description (LOCKED FIX 6).
    """
    latitude, longitude = _geocode(location_name, http)
    if latitude is None or longitude is None:
        logger.warning("no coordinates found for location %r", location_name)
        return not_found(description=_DEFAULT_DESCRIPTION, temperature_f=None)

    response = http.get(
        _FORECAST_URL,
        params={
            "latitude": latitude,
            "longitude": longitude,
            "current_weather": True,
            "hourly": "temperature_2m,relativehumidity_2m,windspeed_10m",
        },
        timeout=REQUEST_TIMEOUT,
    )
    if response.status_code != 200:
        logger.warning("forecast lookup failed for %r", location_name)
        return not_found(description=_DEFAULT_DESCRIPTION, temperature_f=None)

    current = response.json().get("current_weather") or {}
    temperature = current.get("temperature")
    weathercode = current.get("weathercode")
    description = _WEATHER_CODE_DICT.get(weathercode, "Unknown conditions")
    return ok(description=description, temperature_f=temperature)


@tool
def get_weather(location_name: str) -> dict:
    """Resolve a location name to coordinates and return current weather.

    Use when the user mentions a place and weather could influence the outfit.

    Args:
        location_name: City or place name from the user's request.

    Returns (json payload):
        {"result": "ok"|"not_found", "description": str, "temperature_f": float|None}
    """
    return get_weather_impl(location_name)
