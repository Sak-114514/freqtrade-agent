from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from agent_platform.registry.permissions import PermissionAction, PermissionLevel
from agent_platform.runtime_utils import log_event
from agent_platform.storage.db import AgentDB, sanitize_data


ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]
PermissionResolver = Callable[[dict[str, Any]], PermissionAction]
logger = logging.getLogger(__name__)

DEFAULT_CACHEABLE_TOOLS = frozenset(
    {
        "ft_ping",
        "ft_health",
        "ft_status",
        "ft_balance",
        "ft_profit",
        "ft_count",
        "ft_daily",
        "ft_entries",
        "ft_exits",
        "ft_trades_recent",
        "ft_locks",
        "ft_markets",
        "ft_mix_tags",
        "ft_monthly",
        "ft_performance",
        "ft_plot_config",
        "ft_profit_all",
        "ft_stats",
        "ft_show_config_sanitized",
        "ft_strategy_info",
        "ft_trade_detail",
        "ft_weekly",
        "ft_whitelist",
        "ft_sysinfo",
        "market_ticker",
        "market_snapshot",
    }
)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    permission_level: PermissionLevel
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    handler: ToolHandler
    requires_confirmation: bool
    risk_notes: str
    permission_default: PermissionAction = PermissionAction.ALLOW
    permission_resolver: PermissionResolver | None = None

    def public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "permission_level": self.permission_level.value,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "requires_confirmation": self.requires_confirmation,
            "risk_notes": self.risk_notes,
            "permission_default": self.permission_default.value,
            "dynamic_permission": self.permission_resolver is not None,
        }

    def openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


_TRANSIENT_ERROR_SUBSTRINGS = (
    "unavailable",
    "timed out",
    "timeout",
    "connection",
    "could not connect",
)


def _is_transient_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(s in msg for s in _TRANSIENT_ERROR_SUBSTRINGS)


class ToolRegistry:
    def __init__(
        self,
        db: AgentDB,
        dry_run_guard: Callable[[], bool] | None = None,
        permission_overrides: dict[str, PermissionAction] | None = None,
        cache_ttl_seconds: float = 3.0,
        cacheable_tools: Iterable[str] | None = None,
    ) -> None:
        self.db = db
        self.dry_run_guard = dry_run_guard
        self.permission_overrides = permission_overrides or {}
        self._tools: dict[str, ToolSpec] = {}
        self.cache_ttl_seconds = max(0.0, cache_ttl_seconds)
        self.cacheable_tools = frozenset(cacheable_tools or DEFAULT_CACHEABLE_TOOLS)
        self._cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
        self._cache_lock = threading.Lock()

    def register(self, tool: ToolSpec) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Duplicate tool registered: {tool.name}")
        self._tools[tool.name] = tool

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            tool.public_dict()
            for tool in sorted(self._tools.values(), key=lambda item: item.name)
        ]

    def openai_tools(self) -> list[dict[str, Any]]:
        return [
            tool.openai_tool()
            for tool in sorted(self._tools.values(), key=lambda item: item.name)
            if self.permission_for(tool) != PermissionAction.DENY
        ]

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def permission_for(
        self,
        tool: ToolSpec,
        args: dict[str, Any] | None = None,
    ) -> PermissionAction:
        if tool.name in self.permission_overrides:
            return self.permission_overrides[tool.name]
        if tool.permission_level == PermissionLevel.L2:
            return PermissionAction.DENY
        if tool.permission_resolver is not None:
            return tool.permission_resolver(args or {})
        if tool.requires_confirmation:
            return PermissionAction.ASK
        return tool.permission_default

    def execute(
        self,
        name: str,
        args: dict[str, Any] | None = None,
        *,
        run_id: int | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        args = args or {}
        tool = self.get(name)
        if tool is None:
            latency_ms = self._latency_ms(started)
            return {
                "success": False,
                "tool_name": name,
                "error": f"Unknown tool: {name}",
                "summary": "未知工具, 已停止执行。",
                "latency_ms": latency_ms,
            }

        permission = self.permission_for(tool, args)
        if permission == PermissionAction.DENY and not force:
            result = {
                "success": False,
                "tool_name": name,
                "permission_level": tool.permission_level.value,
                "denied": True,
                "permission": permission.value,
                "summary": f"{name} 当前权限为 deny, 已拒绝执行。",
                "risk_notes": tool.risk_notes,
            }
            result["latency_ms"] = self._latency_ms(started)
            self._save_call(tool.name, args, result)
            return result

        if permission == PermissionAction.ASK and not force:
            if self.dry_run_guard and not self.dry_run_guard():
                result = {
                    "success": False,
                    "tool_name": tool.name,
                    "permission_level": tool.permission_level.value,
                    "permission": permission.value,
                    "denied": True,
                    "summary": (
                        "show_config 显示 dry_run 不是 true, Agent 已进入只读模式, "
                        "拒绝生成权限请求。"
                    ),
                }
                result["latency_ms"] = self._latency_ms(started)
                self._save_call(tool.name, args, result)
                return result
            confirmation = (
                f"工具 {tool.name} 当前权限为 ask。已生成 permission_request, "
                "确认前不会执行。"
            )
            permission_request = self.db.create_permission_request(
                run_id=run_id,
                tool_name=tool.name,
                args=args,
                confirmation_text=confirmation,
                risk_notes=tool.risk_notes,
            )
            log_event(
                logger,
                "permission_created",
                run_id=run_id,
                tool_name=tool.name,
                permission_request_id=permission_request.get("id"),
                expires_at=permission_request.get("expires_at"),
            )
            result = {
                "success": False,
                "tool_name": tool.name,
                "permission_level": tool.permission_level.value,
                "permission": permission.value,
                "permission_required": True,
                "permission_request": permission_request,
                "pending_action": permission_request,
                "summary": confirmation,
            }
            result["latency_ms"] = self._latency_ms(started)
            self._save_call(tool.name, args, result)
            return result

        cached = self._cached_result(tool.name, args, started)
        if cached is not None:
            self._save_call(tool.name, args, cached)
            return cached

        max_retries = 1
        for attempt in range(max_retries + 1):
            try:
                result = tool.handler(args)
                if "success" not in result:
                    result["success"] = True
                result["tool_name"] = tool.name
                result["permission_level"] = tool.permission_level.value
                result["permission"] = PermissionAction.ALLOW.value if force else permission.value
                result["latency_ms"] = self._latency_ms(started)
                result = sanitize_data(result)
                self._store_cache(tool.name, args, result)
                self._save_call(tool.name, args, result)
                return result
            except Exception as exc:
                if attempt < max_retries and _is_transient_error(exc):
                    time.sleep(0.5 * (attempt + 1))
                    continue
                result = {
                    "success": False,
                    "tool_name": tool.name,
                    "permission_level": tool.permission_level.value,
                    "permission": permission.value,
                    "error": str(exc),
                    "summary": f"{tool.name} 调用失败: {exc}",
                }
                result["latency_ms"] = self._latency_ms(started)
                self._save_call(tool.name, args, result)
                return result

    def execute_batch(
        self,
        calls: list[dict[str, Any]],
        *,
        run_id: int | None = None,
        force_tools: set[str] | None = None,
        max_workers: int = 6,
    ) -> list[dict[str, Any]]:
        if not calls:
            return []
        workers = max(1, min(max_workers, len(calls)))
        results: list[dict[str, Any] | None] = [None] * len(calls)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    self.execute,
                    str(call.get("name") or ""),
                    call.get("args") if isinstance(call.get("args"), dict) else {},
                    run_id=run_id,
                    force=str(call.get("name") or "") in (force_tools or set()),
                ): index
                for index, call in enumerate(calls)
            }
            for future in as_completed(futures):
                results[futures[future]] = future.result()
        return [
            result or {"success": False, "summary": "tool execution missing"}
            for result in results
        ]

    def confirm_permission(self, request_id: int) -> dict[str, Any]:
        expired_count = self.db.expire_permission_requests()
        if expired_count:
            log_event(logger, "permissions_expired", count=expired_count)
        request = self.db.get_permission_request(request_id)
        if not request:
            return {
                "success": False,
                "summary": f"permission_request #{request_id} 不存在。",
            }
        if request.get("status") != "pending":
            if request.get("status") == "confirmed" and request.get("executed"):
                return {
                    "success": True,
                    "summary": (
                        request.get("result_summary")
                        or f"permission_request #{request_id} 已确认并执行。"
                    ),
                    "permission_request": request,
                    "already_confirmed": True,
                    "result": {
                        "success": True,
                        "tool_name": request.get("tool_name"),
                        "summary": request.get("result_summary") or "",
                    },
                }
            return {
                "success": False,
                "summary": (
                    f"permission_request #{request_id} 已过期, 请重新发起请求。"
                    if request.get("status") == "expired"
                    else f"permission_request #{request_id} 状态不是 pending。"
                ),
                "permission_request": request,
            }
        args_raw = request.get("args_json_sanitized") or "{}"
        try:
            args = json.loads(args_raw)
        except json.JSONDecodeError:
            args = {}
        result = self.execute(
            str(request["tool_name"]),
            args if isinstance(args, dict) else {},
            run_id=request.get("run_id"),
            force=True,
        )
        updated = self.db.update_permission_request(
            request_id,
            status="confirmed",
            executed=bool(result.get("success")),
            result_summary=str(result.get("summary") or result)[:1000],
        )
        log_event(
            logger,
            "permission_confirmed",
            run_id=request.get("run_id"),
            tool_name=request.get("tool_name"),
            permission_request_id=request_id,
            success=bool(result.get("success")),
        )
        return {
            "success": bool(result.get("success")),
            "summary": str(result.get("summary") or ""),
            "permission_request": updated,
            "result": result,
        }

    def _save_call(self, tool_name: str, args: dict[str, Any], result: dict[str, Any]) -> None:
        self.db.save_tool_call(
            tool_name=tool_name,
            args=args,
            result_summary=str(result.get("summary") or result)[:1000],
            success=bool(result.get("success")),
            error=result.get("error"),
            latency_ms=result.get("latency_ms"),
        )
        log_event(
            logger,
            "tool_call",
            tool_name=tool_name,
            success=bool(result.get("success")),
            latency_ms=result.get("latency_ms"),
            cache_hit=result.get("cache_hit"),
            error=result.get("error"),
        )

    def _latency_ms(self, started: float) -> float:
        return round((time.perf_counter() - started) * 1000, 3)

    def _cache_key(self, tool_name: str, args: dict[str, Any]) -> tuple[str, str]:
        return (
            tool_name,
            json.dumps(sanitize_data(args), ensure_ascii=False, sort_keys=True, default=str),
        )

    def _cached_result(
        self,
        tool_name: str,
        args: dict[str, Any],
        started: float,
    ) -> dict[str, Any] | None:
        if self.cache_ttl_seconds <= 0 or tool_name not in self.cacheable_tools:
            return None
        key = self._cache_key(tool_name, args)
        now = time.monotonic()
        with self._cache_lock:
            cached = self._cache.get(key)
            if not cached:
                return None
            cached_at, result = cached
            if now - cached_at > self.cache_ttl_seconds:
                self._cache.pop(key, None)
                return None
            fresh = deepcopy(result)
        fresh["cache_hit"] = True
        fresh["latency_ms"] = self._latency_ms(started)
        return fresh

    def _store_cache(
        self,
        tool_name: str,
        args: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        if (
            self.cache_ttl_seconds <= 0
            or tool_name not in self.cacheable_tools
            or not result.get("success")
            or result.get("permission_required")
        ):
            return
        key = self._cache_key(tool_name, args)
        with self._cache_lock:
            self._cache[key] = (time.monotonic(), deepcopy(result))


def object_schema(properties: dict[str, Any] | None = None, required: list[str] | None = None):
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


def any_output_schema() -> dict[str, Any]:
    return object_schema(
        {
            "success": {"type": "boolean"},
            "summary": {"type": "string"},
        }
    )
