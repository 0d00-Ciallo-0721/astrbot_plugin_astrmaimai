from __future__ import annotations

import time
from datetime import datetime, timezone


def now_timestamp() -> float:
    return time.time()


def utc_timestamp() -> float:
    return datetime.now(timezone.utc).timestamp()


__all__ = ["now_timestamp", "utc_timestamp"]
