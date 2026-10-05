"""Structured logging setup for the fashion assistant agent.

Modules obtain their logger with ``logging.getLogger(__name__)``. Call
:func:`configure_logging` once at process start (CLI, Streamlit, notebook).
Never log secrets, credentials, or raw model payloads containing user data.
"""

from __future__ import annotations

import logging

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


def configure_logging(level: str = "INFO") -> None:
    """Configure stdlib logging with a structured, single-line formatter.

    Args:
        level: Root log level name (e.g. ``"INFO"``, ``"DEBUG"``).

    Idempotent: re-applies the formatter to the root handler(s) on repeat calls
    rather than stacking duplicate handlers.
    """
    formatter = logging.Formatter(fmt=_LOG_FORMAT, datefmt=_DATE_FORMAT)
    root = logging.getLogger()
    root.setLevel(level)

    if root.handlers:
        for handler in root.handlers:
            handler.setFormatter(formatter)
    else:
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        root.addHandler(handler)
