from __future__ import annotations

from pathlib import Path

from agent_platform.config import Settings
from agent_platform.plugins.telegram_dashboard_plugin import TelegramDashboardPlugin
from agent_platform.registry.permissions import PermissionAction, PermissionLevel
from agent_platform.registry.tool_registry import (
    ToolRegistry,
    ToolSpec,
    any_output_schema,
    object_schema,
)
from agent_platform.storage.db import AgentDB


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        agent_host="127.0.0.1",
        agent_port=8090,
        agent_public_url="http://127.0.0.1:8090",
        freqtrade_api_base_url="http://127.0.0.1:8080",
        freqtrade_api_user="user",
        freqtrade_api_password="pass",
        config_path=tmp_path / "config.json",
        user_data_dir=tmp_path,
        memory_db_path=tmp_path / "agent_memory.sqlite",
        llm_base_url="http://127.0.0.1:1234/v1",
        llm_api_key="",
        llm_model="test-model",
        llm_timeout_seconds=30.0,
        llm_env_file_path=tmp_path / "agent_llm.env",
        tavily_api_key="",
        tavily_base_url="https://api.tavily.com",
        tavily_max_results=5,
        agent_max_steps=12,
        permission_overrides={},
        monitor_interval_seconds=60,
        telegram_token="token",
        telegram_chat_id="chat-1",
        trading_agent_doc_path=tmp_path / "TRADING_AGENT.md",
        local_config={},
    )


def _register_fact_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="ft_show_config_sanitized",
            description="config",
            permission_level=PermissionLevel.L0,
            input_schema=object_schema(),
            output_schema=any_output_schema(),
            handler=lambda _args: {
                "success": True,
                "data": {
                    "dry_run": True,
                    "strategy": "SimpleStrategy",
                    "timeframe": "5m",
                    "exchange": "binance",
                    "stake_amount": 100,
                    "stake_currency": "USDT",
                },
                "summary": "config",
            },
            requires_confirmation=False,
            risk_notes="test",
            permission_default=PermissionAction.ALLOW,
        )
    )
    registry.register(
        ToolSpec(
            name="ft_health",
            description="health",
            permission_level=PermissionLevel.L0,
            input_schema=object_schema(),
            output_schema=any_output_schema(),
            handler=lambda _args: {
                "success": True,
                "data": {"status": "running"},
                "summary": "health",
            },
            requires_confirmation=False,
            risk_notes="test",
            permission_default=PermissionAction.ALLOW,
        )
    )


def test_dashboard_preview_does_not_send(tmp_path: Path) -> None:
    db = AgentDB(tmp_path / "test.sqlite")
    db.ensure_default_scheduled_jobs(
        [{"name": "daily", "description": "Daily", "cron": "daily", "interval_minutes": 1440}]
    )
    db.upsert_monitor_rule(
        name="btc_vol",
        prompt="check BTC",
        interval_minutes=30,
        pair="BTC/USDT",
        change_threshold_pct=1.5,
    )
    registry = ToolRegistry(db)
    _register_fact_tools(registry)
    plugin = TelegramDashboardPlugin(settings=_settings(tmp_path), db=db, registry=registry)
    plugin.register(registry)

    result = registry.execute("telegram_dashboard_preview", {})

    assert result["success"] is True
    text = result["data"]["text"]
    assert "Trading Copilot Dashboard" in text
    assert "SimpleStrategy" in text
    assert "daily" in text
    assert "btc_vol" in text
    assert db.get_telegram_dashboard("chat-1") is None


def test_dashboard_pin_first_asks_then_updates_existing(tmp_path: Path) -> None:
    db = AgentDB(tmp_path / "test.sqlite")
    registry = ToolRegistry(db)
    _register_fact_tools(registry)
    plugin = TelegramDashboardPlugin(settings=_settings(tmp_path), db=db, registry=registry)
    sent: list[int] = []
    pinned: list[int] = []
    edited: list[int] = []
    plugin._send_message = lambda **_kwargs: sent.append(101) or 101  # type: ignore[method-assign]
    plugin._pin_message = lambda **kwargs: pinned.append(kwargs["message_id"])  # type: ignore[method-assign]
    plugin._try_edit_message = (  # type: ignore[method-assign]
        lambda **kwargs: edited.append(kwargs["message_id"]) or True
    )
    plugin.register(registry)

    first = registry.execute("telegram_dashboard_pin", {})
    assert first["permission_required"] is True
    confirmed = registry.confirm_permission(first["permission_request"]["id"])
    assert confirmed["success"] is True
    assert sent == [101]
    assert pinned == [101]

    second = registry.execute("telegram_dashboard_pin", {})
    assert second["success"] is True
    assert second["data"]["updated_existing"] is True
    assert sent == [101]
    assert edited == [101]
