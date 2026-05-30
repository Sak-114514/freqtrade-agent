from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from agent_platform.registry.permissions import PermissionAction, PermissionLevel
from agent_platform.registry.tool_registry import ToolRegistry, ToolSpec, object_schema
from agent_platform.storage.db import AgentDB


@pytest.fixture
def db(tmp_path: Path) -> AgentDB:
    return AgentDB(tmp_path / "test_registry.sqlite")


@pytest.fixture
def registry(db: AgentDB) -> ToolRegistry:
    return ToolRegistry(db)


def _l0_tool(name: str, handler=None) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"test tool {name}",
        permission_level=PermissionLevel.L0,
        input_schema=object_schema(),
        output_schema=object_schema(),
        handler=handler or (lambda _a: {"success": True, "summary": "ok"}),
        requires_confirmation=False,
        risk_notes="test",
        permission_default=PermissionAction.ALLOW,
    )


def _l1_tool(name: str, handler=None) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"L1 tool {name}",
        permission_level=PermissionLevel.L1,
        input_schema=object_schema(),
        output_schema=object_schema(),
        handler=handler or (lambda _a: {"success": True, "summary": "ok"}),
        requires_confirmation=True,
        risk_notes="L1 control",
        permission_default=PermissionAction.ASK,
    )


class TestToolRegistry:
    def test_register_and_list(self, registry: ToolRegistry) -> None:
        registry.register(_l0_tool("test_a"))
        registry.register(_l0_tool("test_b"))
        names = [t["name"] for t in registry.list_tools()]
        assert "test_a" in names
        assert "test_b" in names

    def test_duplicate_register_raises(self, registry: ToolRegistry) -> None:
        registry.register(_l0_tool("dup"))
        with pytest.raises(ValueError, match="Duplicate"):
            registry.register(_l0_tool("dup"))

    def test_execute_allow(self, registry: ToolRegistry) -> None:
        registry.register(_l0_tool("ft_health", lambda _a: {"success": True, "summary": "healthy"}))
        result = registry.execute("ft_health", {})
        assert result["success"] is True
        assert result["tool_name"] == "ft_health"
        assert "latency_ms" in result

    def test_execute_unknown_tool(self, registry: ToolRegistry) -> None:
        result = registry.execute("nonexistent", {})
        assert result["success"] is False
        assert "Unknown tool" in result["error"]

    def test_execute_ask_creates_permission_request(self, registry: ToolRegistry) -> None:
        registry.register(_l1_tool("ft_pause"))
        result = registry.execute("ft_pause", {})
        assert result.get("permission_required") is True
        assert result["success"] is False
        pr = result.get("permission_request")
        assert pr is not None
        assert pr["status"] == "pending"

    def test_confirm_permission(self, registry: ToolRegistry) -> None:
        registry.register(_l1_tool("ft_pause", lambda _a: {"success": True, "summary": "paused"}))
        result = registry.execute("ft_pause", {})
        pr = result["permission_request"]
        req_id = pr["id"]
        confirm = registry.confirm_permission(req_id)
        assert confirm["success"] is True
        assert confirm["permission_request"]["status"] == "confirmed"

    def test_confirm_already_confirmed(self, registry: ToolRegistry) -> None:
        registry.register(_l1_tool("ft_stop"))
        result = registry.execute("ft_stop", {})
        req_id = result["permission_request"]["id"]
        registry.confirm_permission(req_id)
        second = registry.confirm_permission(req_id)
        assert second["success"] is True
        assert second.get("already_confirmed") is True

    def test_confirm_expired_permission(self, registry: ToolRegistry) -> None:
        registry.register(_l1_tool("web_search"))
        result = registry.execute("web_search", {"query": "btc"})
        req_id = result["permission_request"]["id"]
        registry.db.expire_permission_requests(
            now=datetime.now(UTC) + timedelta(minutes=11)
        )
        confirm = registry.confirm_permission(req_id)
        assert confirm["success"] is False
        assert confirm["permission_request"]["status"] == "expired"
        assert "已过期" in confirm["summary"]

    def test_dry_run_guard_blocks_ask(self, db: AgentDB) -> None:
        def guard() -> bool:
            return False

        reg = ToolRegistry(db, dry_run_guard=guard)
        reg.register(_l1_tool("ft_pause"))
        result = reg.execute("ft_pause", {})
        assert result["denied"] is True

    def test_permission_overrides(self, db: AgentDB) -> None:
        reg = ToolRegistry(db, permission_overrides={"ft_health": PermissionAction.DENY})
        reg.register(_l0_tool("ft_health"))
        result = reg.execute("ft_health", {})
        assert result["denied"] is True

    def test_execute_with_handler_exception(self, registry: ToolRegistry) -> None:
        def bad_handler(_a: dict) -> dict:
            raise RuntimeError("boom")

        registry.register(_l0_tool("bad_tool", bad_handler))
        result = registry.execute("bad_tool", {})
        assert result["success"] is False
        assert "boom" in result["error"]

    def test_retry_on_transient_error(self, registry: ToolRegistry) -> None:
        call_count = 0

        def flaky_handler(_a: dict) -> dict:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("connection timed out")
            return {"success": True, "summary": "recovered"}

        registry.register(_l0_tool("flaky", flaky_handler))
        result = registry.execute("flaky", {})
        assert result["success"] is True
        assert call_count == 2

    def test_no_retry_on_non_transient_error(self, registry: ToolRegistry) -> None:
        def bad_handler(_a: dict) -> dict:
            raise ValueError("invalid argument")

        registry.register(_l0_tool("val_err", bad_handler))
        result = registry.execute("val_err", {})
        assert result["success"] is False

    def test_openai_tools_excludes_denied(self, db: AgentDB) -> None:
        reg = ToolRegistry(db, permission_overrides={"hidden": "deny"})
        reg.register(_l0_tool("visible"))
        reg.register(_l0_tool("hidden"))
        tools = reg.openai_tools()
        names = [t["function"]["name"] for t in tools]
        assert "visible" in names
        assert "hidden" not in names

    def test_cacheable_tool_returns_short_ttl_hit(self, db: AgentDB) -> None:
        calls = 0

        def handler(_a: dict) -> dict:
            nonlocal calls
            calls += 1
            return {"success": True, "summary": f"call {calls}"}

        reg = ToolRegistry(db, cache_ttl_seconds=10, cacheable_tools={"ft_profit"})
        reg.register(_l0_tool("ft_profit", handler))
        first = reg.execute("ft_profit", {})
        second = reg.execute("ft_profit", {})
        assert first["summary"] == "call 1"
        assert second["summary"] == "call 1"
        assert second["cache_hit"] is True
        assert calls == 1

    def test_execute_batch_runs_independent_tools_concurrently(self, db: AgentDB) -> None:
        reg = ToolRegistry(db, cache_ttl_seconds=0)

        def slow(_a: dict) -> dict:
            time.sleep(0.15)
            return {"success": True, "summary": "ok"}

        reg.register(_l0_tool("ft_status", slow))
        reg.register(_l0_tool("ft_profit", slow))
        started = time.perf_counter()
        results = reg.execute_batch(
            [{"name": "ft_status", "args": {}}, {"name": "ft_profit", "args": {}}],
        )
        elapsed = time.perf_counter() - started
        assert [result["tool_name"] for result in results] == ["ft_status", "ft_profit"]
        assert all(result["success"] for result in results)
        assert elapsed < 0.28
