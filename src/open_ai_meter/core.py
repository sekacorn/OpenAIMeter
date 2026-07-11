"""Deprecated compatibility module for :mod:`ai_meter.core`."""

from __future__ import annotations

import warnings

warnings.warn(
    "open_ai_meter has been renamed to ai_meter and will be removed in a future release.",
    DeprecationWarning,
    stacklevel=2,
)

from ai_meter.core import *  # noqa: F403,E402
