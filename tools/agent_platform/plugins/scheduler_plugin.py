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


class SchedulerPlugin:
    def __init__(self, db: AgentDB) -> None:
        self.db = db

    def register(self, registry: ToolRegistry) -> None:
        for tool in [
            ToolSpec(
                name="scheduler_list",
                description="List local scheduled information jobs and recent runs.",
                permission_level=PermissionLevel.L0,
                input_schema=object_schema(),
                output_schema=any_output_schema(),
                handler=self._list,
                requires_confirmation=False,
                risk_notes="Lists local scheduler metadata only.",
                permission_default=PermissionAction.ALLOW,
            ),
            ToolSpec(
                name="scheduler_enable",
                description="Enable a local scheduled information job.",
                permission_level=PermissionLevel.L1,
                input_schema=object_schema(
                    {"job_id": {"type": "integer", "minimum": 1}},
                    required=["job_id"],
                ),
                output_schema=any_output_schema(),
                handler=self._enable,
                requires_confirmation=False,
                risk_notes="Enables local information reporting only; no trades are executed.",
                permission_default=PermissionAction.ASK,
            ),
            ToolSpec(
                name="scheduler_disable",
                description="Disable a local scheduled information job.",
                permission_level=PermissionLevel.L1,
                input_schema=object_schema(
                    {"job_id": {"type": "integer", "minimum": 1}},
                    required=["job_id"],
                ),
                output_schema=any_output_schema(),
                handler=self._disable,
                requires_confirmation=False,
                risk_notes="Disables local information reporting only; no trades are executed.",
                permission_default=PermissionAction.ASK,
            ),
            ToolSpec(
                name="scheduler_run_once",
                description="Request one manual run of a local scheduled information job.",
                permission_level=PermissionLevel.L1,
                input_schema=object_schema(
                    {"job_id": {"type": "integer", "minimum": 1}},
                    required=["job_id"],
                ),
                output_schema=any_output_schema(),
                handler=self._run_once_marker,
                requires_confirmation=False,
                risk_notes="Runs a predefined information report only; no trades are executed.",
                permission_default=PermissionAction.ASK,
            ),
        ]:
            registry.register(tool)

    def _list(self, _args: dict[str, Any]) -> dict[str, Any]:
        jobs = self.db.scheduled_jobs()
        runs = self.db.scheduled_job_runs(limit=5)
        return {
            "success": True,
            "summary": f"根据工具结果: 当前有 {len(jobs)} 个 scheduled jobs。",
            "data": {"jobs": jobs, "recent_runs": runs},
        }

    def _enable(self, args: dict[str, Any]) -> dict[str, Any]:
        job = self.db.set_scheduled_job_enabled(int(args.get("job_id") or 0), True)
        if not job:
            return {"success": False, "summary": "scheduler_enable 未找到 job。"}
        return {"success": True, "summary": f"根据工具结果: 已启用 scheduled job #{job['id']}。"}

    def _disable(self, args: dict[str, Any]) -> dict[str, Any]:
        job = self.db.set_scheduled_job_enabled(int(args.get("job_id") or 0), False)
        if not job:
            return {"success": False, "summary": "scheduler_disable 未找到 job。"}
        return {"success": True, "summary": f"根据工具结果: 已禁用 scheduled job #{job['id']}。"}

    def _run_once_marker(self, args: dict[str, Any]) -> dict[str, Any]:
        job = self.db.get_scheduled_job(int(args.get("job_id") or 0))
        if not job:
            return {"success": False, "summary": "scheduler_run_once 未找到 job。"}
        return {
            "success": True,
            "summary": (
                f"根据工具结果: scheduled job #{job['id']} 已通过权限确认, "
                "将由 scheduler service 手动运行。"
            ),
            "data": {"job": job},
        }
