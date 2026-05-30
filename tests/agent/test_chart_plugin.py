from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image

from agent_platform.config import Settings
from agent_platform.plugins.agent_meta_plugin import AgentMetaPlugin
from agent_platform.plugins.chart_plugin import ChartPlugin
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
        local_config={"timeframe": "5m"},
    )


def _register_tool(registry: ToolRegistry, name: str, data: Any) -> None:
    registry.register(
        ToolSpec(
            name=name,
            description=name,
            permission_level=PermissionLevel.L0,
            input_schema=object_schema(),
            output_schema=any_output_schema(),
            handler=lambda _args, payload=data: {
                "success": True,
                "summary": f"{name} ok",
                "data": payload,
            },
            requires_confirmation=False,
            risk_notes="test",
            permission_default=PermissionAction.ALLOW,
        )
    )


def _registry_with_chart(tmp_path: Path) -> tuple[ToolRegistry, ChartPlugin]:
    db = AgentDB(tmp_path / "test.sqlite")
    registry = ToolRegistry(db)
    _register_tool(
        registry,
        "ft_profit",
        {"profit_all_abs": 12.4, "profit_all_percent": 1.8, "trade_count": 6},
    )
    _register_tool(
        registry,
        "ft_daily",
        {
            "data": [
                {"date": "2026-05-24", "profit_abs": -1.0},
                {"date": "2026-05-25", "profit_abs": 2.0},
                {"date": "2026-05-26", "profit_abs": 0.4},
                {"date": "2026-05-27", "profit_abs": 3.2},
            ]
        },
    )
    _register_tool(registry, "ft_stats", {"winrate": 66.7})
    _register_tool(
        registry,
        "ft_trades_recent",
        {
            "trades": [
                {"pair": "BTC/USDT", "profit_abs": 1.2, "open_rate": 100},
                {"pair": "ETH/USDT", "profit_abs": -0.4, "open_rate": 200},
            ]
        },
    )
    _register_tool(
        registry,
        "ft_pair_candles",
        {
            "data": [
                {
                    "date": f"2026-05-27 00:{idx:02d}",
                    "open": 100 + idx,
                    "high": 103 + idx,
                    "low": 98 + idx,
                    "close": 101 + idx + (idx % 3 - 1),
                }
                for idx in range(30)
            ]
        },
    )
    plugin = ChartPlugin(settings=_settings(tmp_path), registry=registry)
    plugin.register(registry)
    return registry, plugin


def test_trade_overview_generates_png(tmp_path: Path) -> None:
    registry, _plugin = _registry_with_chart(tmp_path)

    result = registry.execute("chart_trade_overview_preview", {"days": 14})

    assert result["success"] is True
    chart_path = Path(result["data"]["chart_path"])
    assert chart_path.exists()
    assert chart_path.read_bytes().startswith(b"\x89PNG")
    assert "*" not in result["data"]["caption"]


def test_candles_generates_png(tmp_path: Path) -> None:
    registry, _plugin = _registry_with_chart(tmp_path)

    result = registry.execute(
        "chart_candles_preview",
        {"pair": "BTC/USDT", "timeframe": "5m", "limit": 30},
    )

    assert result["success"] is True
    chart_path = Path(result["data"]["chart_path"])
    assert chart_path.exists()
    with Image.open(chart_path) as image:
        assert image.size == (1200, 800)


def test_missing_candles_returns_clear_error(tmp_path: Path) -> None:
    db = AgentDB(tmp_path / "test.sqlite")
    registry = ToolRegistry(db)
    _register_tool(registry, "ft_pair_candles", {"data": []})
    _register_tool(registry, "ft_trades_recent", {"trades": []})
    ChartPlugin(settings=_settings(tmp_path), registry=registry).register(registry)

    result = registry.execute("chart_candles_preview", {"pair": "BTC/USDT", "timeframe": "5m"})

    assert result["success"] is False
    assert "没有可绘制" in result["summary"]


def test_telegram_chart_send_asks_then_sends(tmp_path: Path) -> None:
    registry, plugin = _registry_with_chart(tmp_path)
    preview = registry.execute("chart_trade_overview_preview", {})
    sent: list[dict[str, Any]] = []
    plugin._send_photo = lambda **kwargs: sent.append(kwargs) or {  # type: ignore[method-assign]
        "ok": True,
        "result": {"message_id": 88},
    }

    ask = registry.execute(
        "telegram_chart_send",
        {"chart_path": preview["data"]["chart_path"], "caption": "plain caption"},
    )
    assert ask["permission_required"] is True
    assert sent == []

    confirmed = registry.confirm_permission(ask["permission_request"]["id"])
    assert confirmed["success"] is True
    assert sent[0]["chat_id"] == "chat-1"
    assert sent[0]["photo_path"] == Path(preview["data"]["chart_path"])


def test_telegram_chart_rejects_outside_chart_dir(tmp_path: Path) -> None:
    registry, _plugin = _registry_with_chart(tmp_path)
    outside = tmp_path / "outside.png"
    Image.new("RGB", (10, 10), "white").save(outside)

    result = registry.execute(
        "telegram_chart_send",
        {"chart_path": str(outside), "caption": "no markdown"},
        force=True,
    )

    assert result["success"] is False
    assert "agent_charts" in result["summary"]


def test_agent_capabilities_groups_chart_tools(tmp_path: Path) -> None:
    registry, _plugin = _registry_with_chart(tmp_path)
    AgentMetaPlugin().register(registry)

    result = registry.execute("agent_capabilities", {})

    groups = result["data"]["groups"]
    chart_names = {item["name"] for item in groups["charts"]}
    assert "chart_trade_overview_preview" in chart_names
    assert "telegram_chart_send" in chart_names
