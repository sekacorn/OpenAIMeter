"""Deprecated compatibility import for AIMeter."""

from __future__ import annotations

import warnings

warnings.warn(
    "open_ai_meter has been renamed to ai_meter and will be removed in a future release.",
    DeprecationWarning,
    stacklevel=2,
)

from ai_meter import *  # noqa: F403,E402
from ai_meter import __all__ as _AI_METER_ALL  # noqa: E402
from ai_meter import __version__ as __version__  # noqa: E402
from ai_meter.core import AIMeterError as OpenAIMeterError  # noqa: E402

__all__ = [*_AI_METER_ALL, "__version__", "OpenAIMeterError"]
