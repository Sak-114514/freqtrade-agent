from __future__ import annotations

from typing import Any

from agent_platform.registry.tool_registry import ToolRegistry


class MacroPlugin:
    """Placeholder for future macro data providers such as FRED or calendars."""

    def register(self, _registry: ToolRegistry) -> None:
        return None

    def capabilities(self) -> dict[str, Any]:
        return {
            "name": "macro_plugin",
            "status": "placeholder",
            "future_tools": ["fred_series", "economic_calendar", "macro_snapshot"],
        }
