from __future__ import annotations

from typing import Any

from agent_platform.registry.tool_registry import ToolRegistry


class ReportAgentPlugin:
    """Placeholder for future daily, weekly and strategy review report agents."""

    def register(self, _registry: ToolRegistry) -> None:
        return None

    def capabilities(self) -> dict[str, Any]:
        return {
            "name": "report_agent",
            "status": "placeholder",
            "future_tools": ["daily_report", "weekly_report", "strategy_review"],
        }
