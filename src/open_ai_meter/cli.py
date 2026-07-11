"""Deprecated compatibility module for :mod:`ai_meter.cli`."""

from __future__ import annotations

import warnings

warnings.warn(
    "open_ai_meter has been renamed to ai_meter and will be removed in a future release.",
    DeprecationWarning,
    stacklevel=2,
)

from ai_meter.cli import app, deprecated_app, run  # noqa: E402,F401
