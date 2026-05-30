from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict, deque
from typing import Any

from agent_platform.storage.db import sanitize_data


def log_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    payload = {"event": event, **sanitize_data(fields)}
    logger.info(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))


class SlidingWindowRateLimiter:
    def __init__(self, *, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> tuple[bool, float]:
        current = time.monotonic() if now is None else now
        cutoff = current - self.window_seconds
        with self._lock:
            hits = self._hits[key]
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.limit:
                retry_after = max(1.0, self.window_seconds - (current - hits[0]))
                return False, retry_after
            hits.append(current)
            return True, 0.0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
