from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from agent_platform.registry.permissions import PermissionAction, PermissionLevel
from agent_platform.registry.tool_registry import (
    ToolRegistry,
    ToolSpec,
    any_output_schema,
    object_schema,
)
from agent_platform.storage.db import sanitize_data


class MarketPlugin:
    """Read-only public market data tools.

    These tools do not use exchange API keys and do not place orders.
    """

    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout

    def register(self, registry: ToolRegistry) -> None:
        registry.register(
            ToolSpec(
                name="market_ticker",
                description="Read a public Binance 24h ticker for one spot pair.",
                permission_level=PermissionLevel.L0,
                input_schema=object_schema(
                    {"pair": {"type": "string", "default": "BTC/USDT"}}
                ),
                output_schema=any_output_schema(),
                handler=self._ticker,
                requires_confirmation=False,
                risk_notes="Read-only public market ticker; no exchange keys or trades.",
                permission_default=PermissionAction.ALLOW,
            )
        )
        registry.register(
            ToolSpec(
                name="market_snapshot",
                description="Read public Binance 24h tickers for multiple spot pairs.",
                permission_level=PermissionLevel.L0,
                input_schema=object_schema(
                    {
                        "pairs": {
                            "type": "array",
                            "items": {"type": "string"},
                            "default": ["BTC/USDT"],
                        }
                    }
                ),
                output_schema=any_output_schema(),
                handler=self._snapshot,
                requires_confirmation=False,
                risk_notes="Read-only public market snapshot; no exchange keys or trades.",
                permission_default=PermissionAction.ALLOW,
            )
        )

    def capabilities(self) -> dict[str, Any]:
        return {
            "name": "market_plugin",
            "status": "active",
            "future_tools": ["ticker", "ohlcv", "multi_pair_snapshot"],
        }

    def _ticker(self, args: dict[str, Any]) -> dict[str, Any]:
        pair = normalize_pair(str(args.get("pair") or "BTC/USDT"))
        data = self._binance_ticker(pair)
        return {
            "success": True,
            "summary": summarize_ticker(pair, data),
            "data": {"pair": pair, "ticker": sanitize_data(data)},
        }

    def _snapshot(self, args: dict[str, Any]) -> dict[str, Any]:
        raw_pairs = args.get("pairs")
        pairs = raw_pairs if isinstance(raw_pairs, list) and raw_pairs else ["BTC/USDT"]
        normalized = [normalize_pair(str(pair)) for pair in pairs[:8]]
        tickers = []
        failures = []
        try:
            batch = self._binance_tickers(normalized)
            tickers.extend(batch)
        except Exception as exc:
            failures.append({"pairs": normalized, "error": str(exc)})
            for pair in normalized:
                try:
                    tickers.append({"pair": pair, "ticker": self._binance_ticker(pair)})
                except Exception as fallback_exc:
                    failures.append({"pair": pair, "error": str(fallback_exc)})
        if not tickers:
            return {
                "success": False,
                "summary": f"market_snapshot 调用失败: {failures}",
                "error": json.dumps(failures, ensure_ascii=False),
            }
        summaries = [summarize_ticker(item["pair"], item["ticker"]) for item in tickers]
        if failures:
            summaries.append(f"失败: {failures}")
        return {
            "success": True,
            "summary": "根据工具结果: " + "; ".join(summaries),
            "data": {"tickers": sanitize_data(tickers), "failures": sanitize_data(failures)},
        }

    def _binance_ticker(self, pair: str) -> dict[str, Any]:
        symbol = pair.replace("/", "").upper()
        url = "https://api.binance.com/api/v3/ticker/24hr?" + urlencode({"symbol": symbol})
        request = Request(  # noqa: S310 - fixed public market-data endpoint.
            url,
            method="GET",
            headers={"Accept": "application/json", "User-Agent": "freqtrade-agent/market"},
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                payload = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Binance ticker HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError) as exc:
            raise RuntimeError(f"Binance ticker unavailable: {exc}") from exc
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Binance ticker returned non-JSON response.") from exc
        if not isinstance(data, dict):
            raise RuntimeError("Binance ticker returned invalid payload.")
        return data

    def _binance_tickers(self, pairs: list[str]) -> list[dict[str, Any]]:
        if len(pairs) == 1:
            return [{"pair": pairs[0], "ticker": self._binance_ticker(pairs[0])}]
        symbols = [pair.replace("/", "").upper() for pair in pairs]
        url = "https://api.binance.com/api/v3/ticker/24hr?" + urlencode(
            {"symbols": json.dumps(symbols, separators=(",", ":"))}
        )
        request = Request(  # noqa: S310 - fixed public market-data endpoint.
            url,
            method="GET",
            headers={"Accept": "application/json", "User-Agent": "freqtrade-agent/market"},
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                payload = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Binance ticker HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError) as exc:
            raise RuntimeError(f"Binance ticker unavailable: {exc}") from exc
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Binance ticker returned non-JSON response.") from exc
        if not isinstance(data, list):
            raise RuntimeError("Binance batch ticker returned invalid payload.")
        by_symbol = {str(item.get("symbol")): item for item in data if isinstance(item, dict)}
        return [
            {"pair": pair, "ticker": by_symbol[pair.replace("/", "").upper()]}
            for pair in pairs
            if pair.replace("/", "").upper() in by_symbol
        ]


def normalize_pair(pair: str) -> str:
    pair = pair.strip().upper().replace("-", "/").replace("_", "/")
    aliases = {
        "BTC": "BTC/USDT",
        "BITCOIN": "BTC/USDT",
        "比特币": "BTC/USDT",
        "ETH": "ETH/USDT",
        "ETHEREUM": "ETH/USDT",
        "以太坊": "ETH/USDT",
    }
    if pair in aliases:
        return aliases[pair]
    if "/" not in pair and pair.endswith("USDT"):
        return pair[:-4] + "/USDT"
    if "/" not in pair:
        return pair + "/USDT"
    return pair


def summarize_ticker(pair: str, data: dict[str, Any]) -> str:
    return (
        "根据工具结果: {pair} ticker last={last} USDT, 24h_change={change}%, "
        "high={high}, low={low}, quote_volume={quote_volume}."
    ).format(
        pair=pair,
        last=data.get("lastPrice", "unknown"),
        change=data.get("priceChangePercent", "unknown"),
        high=data.get("highPrice", "unknown"),
        low=data.get("lowPrice", "unknown"),
        quote_volume=data.get("quoteVolume", "unknown"),
    )
