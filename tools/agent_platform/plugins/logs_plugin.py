from __future__ import annotations

import re
from typing import Any

from agent_platform.plugins.freqtrade_plugin import FreqtradeApiClient
from agent_platform.registry.permissions import PermissionAction, PermissionLevel
from agent_platform.registry.tool_registry import (
    ToolRegistry,
    ToolSpec,
    any_output_schema,
    object_schema,
)
from agent_platform.storage.db import sanitize_data


TOKEN_IN_TEXT_RE = re.compile(
    r"(token=)[A-Za-z0-9._~+/=-]+|"
    r"(Bearer\s+)[A-Za-z0-9._~+/=-]+|"
    r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
)


class LogsPlugin:
    def __init__(self, client: FreqtradeApiClient) -> None:
        self.client = client

    def register(self, registry: ToolRegistry) -> None:
        registry.register(
            ToolSpec(
                name="ft_logs",
                description=(
                    "Read latest Freqtrade logs. Use for errors, warnings and recent events."
                ),
                permission_level=PermissionLevel.L0,
                input_schema=object_schema(
                    {"limit": {"type": "integer", "minimum": 1, "maximum": 200}}
                ),
                output_schema=any_output_schema(),
                handler=self._logs,
                requires_confirmation=False,
                risk_notes="Read-only log retrieval. Output is sanitized before returning.",
                permission_default=PermissionAction.ALLOW,
            )
        )

    def _logs(self, args: dict[str, Any]) -> dict[str, Any]:
        limit = int(args.get("limit") or 50)
        limit = max(1, min(limit, 200))
        data = self.client.request("GET", "logs", params={"limit": limit})
        logs = data.get("logs") if isinstance(data, dict) else []
        entries = logs if isinstance(logs, list) else []
        level_counts = log_level_counts(entries)
        safe_recent = [compact_log_entry(item) for item in entries[-5:]]
        return {
            "success": True,
            "summary": (
                f"根据工具结果: 读取最近 {len(logs or [])} 条 logs, "
                f"level_counts={level_counts}, 末尾样本={safe_recent}。"
            ),
            "data": sanitize_data(data),
        }


def sanitize_log_text(value: Any) -> Any:
    if isinstance(value, list):
        return [sanitize_log_text(item) for item in value]
    if isinstance(value, dict):
        return {key: sanitize_log_text(item) for key, item in value.items()}
    if isinstance(value, str):
        return TOKEN_IN_TEXT_RE.sub(
            lambda match: (match.group(1) or match.group(2) or "") + "[REDACTED]",
            value,
        )
    return value


def log_level_counts(entries: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        if isinstance(entry, list) and len(entry) >= 4:
            level = str(entry[3] or "UNKNOWN")
        elif isinstance(entry, dict):
            level = str(entry.get("level") or entry.get("levelname") or "UNKNOWN")
        else:
            level = "UNKNOWN"
        counts[level] = counts.get(level, 0) + 1
    return counts


def compact_log_entry(entry: Any, max_message_chars: int = 220) -> Any:
    safe = sanitize_log_text(entry)
    if isinstance(safe, list):
        timestamp = safe[0] if len(safe) > 0 else ""
        logger_name = safe[2] if len(safe) > 2 else ""
        level = safe[3] if len(safe) > 3 else ""
        message = safe[4] if len(safe) > 4 else ""
        return {
            "time": timestamp,
            "logger": logger_name,
            "level": level,
            "message": truncate_log_message(str(message), max_message_chars),
        }
    if isinstance(safe, dict):
        message = str(safe.get("message") or safe.get("msg") or "")
        return {
            "time": safe.get("time") or safe.get("timestamp"),
            "logger": safe.get("name") or safe.get("logger"),
            "level": safe.get("level") or safe.get("levelname"),
            "message": truncate_log_message(message, max_message_chars),
        }
    return truncate_log_message(str(safe), max_message_chars)


def truncate_log_message(message: str, max_chars: int) -> str:
    normalized = " ".join(message.split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 3] + "..."
