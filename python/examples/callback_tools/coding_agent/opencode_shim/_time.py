"""Time utilities (extracted from agentpool.utils.time_utils)."""

from __future__ import annotations

import time


def now_ms() -> int:
    """Return current time in milliseconds as integer."""
    return int(time.time() * 1000)
