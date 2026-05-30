from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent_platform.plugins.freqtrade_plugin import FreqtradePlugin
from agent_platform.registry.tool_registry import ToolRegistry
from agent_platform.storage.db import AgentDB


class FakeFreqtradeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def request(  # noqa: C901 - fixture maps many endpoint names to tiny fake responses.
        self,
        method: str,
        api_path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        authenticate: bool = True,
    ) -> Any:
        self.calls.append((method, api_path, params))
        if api_path == "show_config":
            return {
                "dry_run": True,
                "strategy": "SimpleStrategy",
                "timeframe": "5m",
                "stake_currency": "USDT",
            }
        if api_path == "profit_all":
            return {"all": {"trade_count": 2}, "long": {"trade_count": 2}}
        if api_path in {"performance", "entries", "exits", "mix_tags"}:
            return [{"pair": "BTC/USDT", "profit": 1.2}]
        if api_path in {"daily", "weekly", "monthly"}:
            return {"data": [{"date": "2026-05-28", "profit_abs": 1.0}]}
        if api_path == "trade/7":
            return {"trade_id": 7, "pair": "BTC/USDT"}
        if api_path == "trades/open/custom-data":
            return [{"trade_id": 7, "key": "note"}]
        if api_path == "trades/7/custom-data":
            return [{"trade_id": 7, "key": "note"}]
        if api_path == "locks":
            return {"locks": [{"id": 1, "pair": "BTC/USDT"}]}
        if api_path == "blacklist":
            return {"blacklist": ["BAD/USDT"]}
        if api_path == "version":
            return {"version": "2026.4"}
        if api_path == "plot_config":
            return {"main_plot": {"ema": {}}}
        if api_path == "strategy/SimpleStrategy":
            return {
                "strategy": "SimpleStrategy",
                "timeframe": "5m",
                "params": [{"name": "buy"}],
                "code": "class SimpleStrategy:\n    pass\n",
            }
        if api_path == "markets":
            return {
                "exchange_id": "binance",
                "markets": {
                    "BTC/USDT": {"symbol": "BTC/USDT"},
                    "ETH/USDT": {"symbol": "ETH/USDT"},
                },
            }
        if api_path in {"pair_candles", "pair_history"}:
            return {
                "pair": "BTC/USDT",
                "timeframe": "5m",
                "length": 3,
                "columns": ["date", "close"],
                "data": [[1, 100], [2, 101], [3, 102]],
            }
        if api_path == "background":
            return [{"job_id": "abc", "running": False}]
        if api_path == "background/abc":
            return {"job_id": "abc", "running": False}
        if api_path == "available_pairs":
            return {"length": 1, "pairs": ["BTC/USDT"], "pair_interval": []}
        if api_path == "stats":
            return {"wins": 1, "losses": 1}
        return {"ok": True}


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        freqtrade_api_base_url="http://127.0.0.1:8080",
        freqtrade_api_user="user",
        freqtrade_api_password="pass",
        local_config={
            "dry_run": True,
            "strategy": "SimpleStrategy",
            "stake_currency": "USDT",
        },
    )


def _registry(tmp_path: Path) -> tuple[ToolRegistry, FakeFreqtradeClient]:
    db = AgentDB(tmp_path / "test.sqlite")
    registry = ToolRegistry(db)
    plugin = FreqtradePlugin(_settings(tmp_path))
    fake_client = FakeFreqtradeClient()
    plugin.client = fake_client
    plugin.register(registry)
    return registry, fake_client


def test_extended_l0_tools_are_registered(tmp_path: Path) -> None:
    registry, _client = _registry(tmp_path)
    names = {tool["name"] for tool in registry.list_tools()}
    assert {
        "ft_profit_all",
        "ft_performance",
        "ft_stats",
        "ft_daily",
        "ft_weekly",
        "ft_monthly",
        "ft_entries",
        "ft_exits",
        "ft_mix_tags",
        "ft_trade_detail",
        "ft_locks",
        "ft_strategy_info",
        "ft_pair_candles",
        "ft_pair_history",
        "ft_background_tasks",
    }.issubset(names)


def test_extended_l0_tools_call_readonly_endpoints(tmp_path: Path) -> None:
    registry, client = _registry(tmp_path)
    assert registry.execute("ft_profit_all", {})["success"] is True
    assert registry.execute("ft_daily", {"timescale": 200})["success"] is True
    assert registry.execute("ft_entries", {"pair": "BTC/USDT"})["success"] is True
    assert registry.execute("ft_trade_detail", {"trade_id": 7})["success"] is True
    assert registry.execute("ft_open_trade_custom_data", {"limit": 500})["success"] is True
    assert registry.execute("ft_trade_custom_data", {"trade_id": 7})["success"] is True
    assert registry.execute("ft_locks", {})["success"] is True
    assert registry.execute("ft_pair_candles", {
        "pair": "BTC/USDT", "timeframe": "5m", "limit": 2,
    })["success"] is True
    assert registry.execute("ft_pair_history", {
        "pair": "BTC/USDT", "timeframe": "5m", "timerange": "20250101-20250102",
    })["success"] is True
    assert registry.execute("ft_background_tasks", {"job_id": "abc"})["success"] is True

    assert ("GET", "daily", {"timescale": 90}) in client.calls
    assert ("GET", "trades/open/custom-data", {"limit": 100, "offset": 0}) in client.calls
    assert all(method == "GET" for method, _path, _params in client.calls)


def test_strategy_info_omits_full_code_by_default(tmp_path: Path) -> None:
    registry, _client = _registry(tmp_path)
    result = registry.execute("ft_strategy_info", {})
    data = result["data"]
    assert result["success"] is True
    assert "code" not in data
    assert data["code_line_count"] == 2
    assert "code_excerpt" not in data


def test_markets_defaults_to_stake_currency_and_limits_output(tmp_path: Path) -> None:
    registry, client = _registry(tmp_path)
    result = registry.execute("ft_markets", {"limit": 1})
    assert result["success"] is True
    assert result["data"]["markets_returned"] == 1
    assert ("GET", "markets", {"quote": "USDT"}) in client.calls
