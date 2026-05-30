from __future__ import annotations

from typing import Any

from agent_platform.registry.permissions import PermissionAction, PermissionLevel
from agent_platform.registry.tool_registry import (
    ToolRegistry,
    ToolSpec,
    any_output_schema,
    object_schema,
)


class AgentMetaPlugin:
    def register(self, registry: ToolRegistry) -> None:
        registry.register(
            ToolSpec(
                name="agent_capabilities",
                description=(
                    "Summarize available agent tools, permission groups and denied actions."
                ),
                permission_level=PermissionLevel.L0,
                input_schema=object_schema(),
                output_schema=any_output_schema(),
                handler=lambda _args: self._capabilities(registry),
                requires_confirmation=False,
                risk_notes="Read-only agent metadata; does not query Freqtrade or execute trades.",
                permission_default=PermissionAction.ALLOW,
            )
        )

    def _capabilities(self, registry: ToolRegistry) -> dict[str, Any]:  # noqa: C901
        tools = registry.list_tools()
        grouped: dict[str, list[dict[str, Any]]] = {
            "freqtrade_readonly": [],
            "freqtrade_l1_control": [],
            "market": [],
            "web": [],
            "memory": [],
            "monitor": [],
            "scheduler": [],
            "charts": [],
            "telegram_dashboard": [],
            "agent_meta": [],
            "other": [],
        }
        for tool in tools:
            name = str(tool.get("name") or "")
            item = {
                "name": name,
                "permission": tool.get("permission_default"),
                "level": tool.get("permission_level"),
                "requires_confirmation": tool.get("requires_confirmation"),
                "description": tool.get("description"),
            }
            if name in {"ft_start", "ft_pause", "ft_stop", "ft_reload_config"}:
                grouped["freqtrade_l1_control"].append(item)
            elif name.startswith("ft_"):
                grouped["freqtrade_readonly"].append(item)
            elif name.startswith("market_"):
                grouped["market"].append(item)
            elif name.startswith("web_"):
                grouped["web"].append(item)
            elif name.startswith("memory_"):
                grouped["memory"].append(item)
            elif name.startswith("monitor_"):
                grouped["monitor"].append(item)
            elif name.startswith("scheduler_"):
                grouped["scheduler"].append(item)
            elif name.startswith("chart_") or name.startswith("telegram_chart_"):
                grouped["charts"].append(item)
            elif name.startswith("telegram_dashboard_"):
                grouped["telegram_dashboard"].append(item)
            elif name.startswith("agent_"):
                grouped["agent_meta"].append(item)
            else:
                grouped["other"].append(item)

        return {
            "success": True,
            "summary": "根据工具结果: agent_capabilities 返回当前工具和权限分组。",
            "data": {
                "groups": {key: value for key, value in grouped.items() if value},
                "permission_model": {
                    "allow": "可直接执行。",
                    "ask": "需要 Telegram/API 确认后, 在当前 run 内续跑。",
                    "deny": "不暴露或拒绝执行。",
                },
                "denied_actions": [
                    "forceenter/forceexit",
                    "关闭 dry_run",
                    "实盘下单",
                    "修改策略或 config.json",
                    "shell/docker",
                    "修改交易所凭据",
                ],
            },
        }
