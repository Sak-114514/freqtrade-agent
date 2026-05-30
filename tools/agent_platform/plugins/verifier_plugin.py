from __future__ import annotations

from typing import Any

from agent_platform.registry.tool_registry import ToolRegistry


class VerifierPlugin:
    """Placeholder for future verifier modules beyond the current rule verifier."""

    def register(self, _registry: ToolRegistry) -> None:
        return None

    def capabilities(self) -> dict[str, Any]:
        return {
            "name": "verifier_plugin",
            "status": "placeholder",
            "future_tools": ["answer_grounding_check", "risk_policy_check"],
        }
