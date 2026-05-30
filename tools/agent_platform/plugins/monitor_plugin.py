from __future__ import annotations

from typing import Any

from agent_platform.registry.permissions import PermissionAction, PermissionLevel
from agent_platform.registry.tool_registry import (
    ToolRegistry,
    ToolSpec,
    any_output_schema,
    object_schema,
)
from agent_platform.storage.db import AgentDB


class MonitorPlugin:
    def __init__(self, db: AgentDB) -> None:
        self.db = db

    def register(self, registry: ToolRegistry) -> None:
        for tool in [
            ToolSpec(
                name="monitor_list",
                description="List configured proactive monitor rules.",
                permission_level=PermissionLevel.L0,
                input_schema=object_schema(),
                output_schema=any_output_schema(),
                handler=self._list,
                requires_confirmation=False,
                risk_notes="Lists local monitor rules.",
                permission_default=PermissionAction.ASK,
            ),
            ToolSpec(
                name="monitor_set",
                description="Create a proactive Telegram suggestion rule.",
                permission_level=PermissionLevel.L1,
                input_schema=object_schema(
                    {
                        "name": {"type": "string"},
                        "prompt": {"type": "string"},
                        "interval_minutes": {"type": "integer", "minimum": 1, "maximum": 1440},
                        "pair": {"type": "string"},
                        "change_threshold_pct": {"type": "number", "minimum": 0},
                        "enabled": {"type": "boolean"},
                    },
                    required=["name", "prompt"],
                ),
                output_schema=any_output_schema(),
                handler=self._set,
                requires_confirmation=False,
                risk_notes="Creates a scheduled Telegram-only suggestion; no trades are executed.",
                permission_default=PermissionAction.ASK,
            ),
            ToolSpec(
                name="monitor_pause",
                description="Pause a proactive monitor rule.",
                permission_level=PermissionLevel.L1,
                input_schema=object_schema(
                    {"rule_id": {"type": "integer", "minimum": 1}},
                    required=["rule_id"],
                ),
                output_schema=any_output_schema(),
                handler=self._pause,
                requires_confirmation=False,
                risk_notes="Pauses a local monitor rule only.",
                permission_default=PermissionAction.ASK,
            ),
            ToolSpec(
                name="monitor_resume",
                description="Resume a proactive monitor rule.",
                permission_level=PermissionLevel.L1,
                input_schema=object_schema(
                    {"rule_id": {"type": "integer", "minimum": 1}},
                    required=["rule_id"],
                ),
                output_schema=any_output_schema(),
                handler=self._resume,
                requires_confirmation=False,
                risk_notes="Resumes a local monitor rule only.",
                permission_default=PermissionAction.ASK,
            ),
            ToolSpec(
                name="monitor_run_once",
                description="Run a proactive monitor rule once and record a suggestion event.",
                permission_level=PermissionLevel.L1,
                input_schema=object_schema({"rule_id": {"type": "integer", "minimum": 1}}),
                output_schema=any_output_schema(),
                handler=self._run_once,
                requires_confirmation=False,
                risk_notes="Runs a Telegram-only suggestion check; no trades are executed.",
                permission_default=PermissionAction.ASK,
            ),
        ]:
            registry.register(tool)

    def _list(self, _args: dict[str, Any]) -> dict[str, Any]:
        rules = self.db.monitor_rules()
        return {
            "success": True,
            "summary": f"根据工具结果: 当前有 {len(rules)} 条 monitor rules。",
            "data": {"rules": rules},
        }

    def _set(self, args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name") or "").strip()
        prompt = str(args.get("prompt") or "").strip()
        if not name or not prompt:
            return {"success": False, "summary": "monitor_set 需要 name 和 prompt。"}
        interval = int(args.get("interval_minutes") or 30)
        interval = max(1, min(interval, 1440))
        threshold = args.get("change_threshold_pct")
        rule = self.db.upsert_monitor_rule(
            name=name,
            prompt=prompt,
            interval_minutes=interval,
            pair=str(args.get("pair") or "").strip() or None,
            change_threshold_pct=float(threshold) if threshold is not None else None,
            enabled=bool(args.get("enabled", True)),
        )
        return {
            "success": True,
            "summary": f"根据工具结果: 已创建 monitor rule #{rule.get('id')}: {name}。",
            "data": {"rule": rule},
        }

    def _pause(self, args: dict[str, Any]) -> dict[str, Any]:
        rule = self.db.set_monitor_enabled(int(args.get("rule_id") or 0), False)
        if not rule:
            return {"success": False, "summary": "monitor_pause 未找到 rule。"}
        return {"success": True, "summary": f"根据工具结果: 已暂停 monitor #{rule['id']}。"}

    def _resume(self, args: dict[str, Any]) -> dict[str, Any]:
        rule = self.db.set_monitor_enabled(int(args.get("rule_id") or 0), True)
        if not rule:
            return {"success": False, "summary": "monitor_resume 未找到 rule。"}
        return {"success": True, "summary": f"根据工具结果: 已恢复 monitor #{rule['id']}。"}

    def _run_once(self, args: dict[str, Any]) -> dict[str, Any]:
        rule_id = int(args.get("rule_id") or 0)
        rule = self.db.get_monitor_rule(rule_id) if rule_id else None
        if not rule:
            return {"success": False, "summary": "monitor_run_once 未找到 rule。"}
        event_id = self.db.save_monitor_event(
            rule_id=rule_id,
            event_type="manual_request",
            trigger_reason="monitor_run_once permission confirmed",
            answer=None,
            tool_calls=[],
            sent=False,
        )
        return {
            "success": True,
            "summary": f"根据工具结果: 已记录 monitor_run_once event #{event_id}。",
            "data": {"event_id": event_id, "rule": rule},
        }
