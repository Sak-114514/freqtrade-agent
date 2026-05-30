from __future__ import annotations

import json
import logging

from agent_platform.runtime_utils import SlidingWindowRateLimiter, log_event


def test_rate_limiter_allows_then_blocks() -> None:
    limiter = SlidingWindowRateLimiter(limit=2, window_seconds=10)
    assert limiter.allow("u", now=100.0)[0] is True
    assert limiter.allow("u", now=101.0)[0] is True
    allowed, retry_after = limiter.allow("u", now=102.0)
    assert allowed is False
    assert retry_after > 0
    assert limiter.allow("u", now=111.0)[0] is True


def test_log_event_sanitizes_sensitive_fields(caplog) -> None:
    logger = logging.getLogger("test.agent.runtime")
    with caplog.at_level(logging.INFO, logger=logger.name):
        log_event(logger, "tool_call", api_token="secret", profit_all_percent=1.2)
    payload = json.loads(caplog.records[0].message)
    assert payload["event"] == "tool_call"
    assert payload["api_token"] == "***REDACTED***"
    assert payload["profit_all_percent"] == 1.2
