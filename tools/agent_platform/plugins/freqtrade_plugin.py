from __future__ import annotations

import base64
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

from agent_platform.config import Settings
from agent_platform.registry.permissions import PermissionAction, PermissionLevel
from agent_platform.registry.tool_registry import (
    ToolRegistry,
    ToolSpec,
    any_output_schema,
    object_schema,
)
from agent_platform.storage.db import sanitize_data


class FreqtradeApiError(RuntimeError):
    pass


class FreqtradeApiClient:
    def __init__(self, settings: Settings, timeout: float = 10.0) -> None:
        self.base_url = settings.freqtrade_api_base_url.rstrip("/")
        self.username = settings.freqtrade_api_user
        self.password = settings.freqtrade_api_password
        self.timeout = timeout
        self._access_token: str | None = None
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"}:
            raise FreqtradeApiError("Freqtrade API base URL must use http or https.")

    def login(self) -> None:
        if not self.username or not self.password:
            raise FreqtradeApiError("Missing Freqtrade API username or password.")
        credentials = f"{self.username}:{self.password}".encode()
        basic_token = base64.b64encode(credentials).decode("ascii")
        response = self.request(
            "POST",
            "token/login",
            headers={"Authorization": f"Basic {basic_token}"},
            authenticate=False,
        )
        access_token = response.get("access_token")
        if not access_token:
            raise FreqtradeApiError("Freqtrade login response missed access_token.")
        self._access_token = access_token

    def request(
        self,
        method: str,
        api_path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        authenticate: bool = True,
    ) -> Any:
        if authenticate and not self._access_token:
            self.login()

        request_headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if authenticate and self._access_token:
            request_headers["Authorization"] = f"Bearer {self._access_token}"
        if headers:
            request_headers.update(headers)

        url = urljoin(f"{self.base_url}/api/v1/", api_path.lstrip("/"))
        if params:
            url = f"{url}?{urlencode(params)}"
        request = Request(  # noqa: S310 - base URL scheme is validated in __init__.
            url,
            method=method.upper(),
            headers=request_headers,
        )

        try:
            with urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                payload = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = _read_error_detail(exc)
            if exc.code == 401 and authenticate and self._access_token:
                self._access_token = None
                self.login()
                return self.request(method, api_path, params=params, headers=headers)
            raise FreqtradeApiError(f"HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError) as exc:
            raise FreqtradeApiError(f"Could not connect to Freqtrade API: {exc}") from exc

        if not payload:
            return {}
        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise FreqtradeApiError("Freqtrade API returned non-JSON response.") from exc


def _read_error_detail(exc: HTTPError) -> Any:
    try:
        payload = exc.read().decode("utf-8", errors="replace")
    except Exception:
        return str(exc)
    if not payload:
        return str(exc)
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return payload


def summarize_status(data: Any) -> str:
    trades = data if isinstance(data, list) else []
    if not trades:
        return "根据工具结果: 当前没有 open trades, 持仓为空。"
    lines = [f"根据工具结果: 当前有 {len(trades)} 个 open trades。"]
    for trade in trades[:8]:
        lines.append(
            (
                "- #{id} {pair}: profit={profit}, open_rate={open_rate}, "
                "current_rate={current_rate}"
            ).format(
                id=trade.get("trade_id", "?"),
                pair=trade.get("pair", "unknown"),
                profit=trade.get("profit_pct", trade.get("profit_ratio", "unknown")),
                open_rate=trade.get("open_rate", "unknown"),
                current_rate=trade.get("current_rate", "unknown"),
            )
        )
    return "\n".join(lines)


def summarize_balance(data: dict[str, Any]) -> str:
    return (
        "根据工具结果: balance total={total} {symbol}, total_bot={total_bot} {symbol}, "
        "currencies={count}。"
    ).format(
        total=data.get("total", "unknown"),
        total_bot=data.get("total_bot", "unknown"),
        symbol=data.get("symbol") or data.get("stake") or "",
        count=len(data.get("currencies") or []),
    )


def summarize_profit(data: dict[str, Any]) -> str:
    return (
        "根据工具结果: profit_all={profit_all_coin}, profit_all_percent={profit_all_percent}%, "
        "trades={trade_count}, closed_trades={closed_trade_count}, winrate={winrate}%, "
        "max_drawdown={max_drawdown}%."
    ).format(
        profit_all_coin=data.get("profit_all_coin", "unknown"),
        profit_all_percent=data.get("profit_all_percent", "unknown"),
        trade_count=data.get("trade_count", "unknown"),
        closed_trade_count=data.get("closed_trade_count", "unknown"),
        winrate=data.get("winrate", "unknown"),
        max_drawdown=data.get("max_drawdown", "unknown"),
    )


def summarize_list(label: str, data: Any, item_name: str = "items") -> str:
    count = len(data) if isinstance(data, list) else 0
    return f"根据工具结果: {label} 返回 {count} 条 {item_name}。"


def sanitize_config(config: dict[str, Any]) -> dict[str, Any]:
    safe = sanitize_data(config)
    preferred_keys = [
        "state",
        "runmode",
        "dry_run",
        "trading_mode",
        "strategy",
        "strategy_version",
        "timeframe",
        "stake_currency",
        "stake_amount",
        "max_open_trades",
        "exchange",
        "api_version",
        "bot_name",
        "fiat_display_currency",
    ]
    if any(key in safe for key in preferred_keys):
        return {key: safe.get(key) for key in preferred_keys if key in safe}
    return safe


class FreqtradePlugin:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = FreqtradeApiClient(settings)

    def is_dry_run(self) -> bool:
        try:
            config = self.client.request("GET", "show_config")
            if "dry_run" in config:
                return config.get("dry_run") is True
        except Exception:
            return self.settings.local_config.get("dry_run") is True
        return self.settings.local_config.get("dry_run") is True

    def register(self, registry: ToolRegistry) -> None:
        self._register_l0(registry)
        self._register_l1(registry)

    def _register_l0(self, registry: ToolRegistry) -> None:
        registry.register(
            self._tool(
                "ft_ping",
                "Ping Freqtrade API readiness.",
                self._ping,
            )
        )
        registry.register(
            self._tool(
                "ft_health",
                "Read bot health and process timestamps.",
                self._health,
            )
        )
        registry.register(
            self._tool(
                "ft_status",
                "List current open trades.",
                self._status,
            )
        )
        registry.register(
            self._tool(
                "ft_balance",
                "Show account and bot managed balance per currency.",
                self._balance,
            )
        )
        registry.register(
            self._tool(
                "ft_profit",
                "Show profit, winrate and drawdown summary.",
                self._profit,
            )
        )
        registry.register(
            self._tool(
                "ft_profit_all",
                "Show profit statistics grouped by all/long/short when available.",
                self._profit_all,
            )
        )
        registry.register(
            self._tool(
                "ft_count",
                "Show current and maximum trade count.",
                self._count,
            )
        )
        registry.register(
            self._tool(
                "ft_performance",
                "Show pair performance summary.",
                self._performance,
            )
        )
        registry.register(
            self._tool(
                "ft_stats",
                "Show trading statistics such as wins/losses and exit reasons.",
                self._stats,
            )
        )
        registry.register(
            self._tool(
                "ft_daily",
                "Show daily profit buckets.",
                self._daily,
                input_schema=object_schema(
                    {"timescale": {"type": "integer", "minimum": 1, "maximum": 90}}
                ),
            )
        )
        registry.register(
            self._tool(
                "ft_weekly",
                "Show weekly profit buckets.",
                self._weekly,
                input_schema=object_schema(
                    {"timescale": {"type": "integer", "minimum": 1, "maximum": 52}}
                ),
            )
        )
        registry.register(
            self._tool(
                "ft_monthly",
                "Show monthly profit buckets.",
                self._monthly,
                input_schema=object_schema(
                    {"timescale": {"type": "integer", "minimum": 1, "maximum": 36}}
                ),
            )
        )
        tag_schema = object_schema({"pair": {"type": "string"}})
        registry.register(
            self._tool(
                "ft_entries",
                "Show entry tag performance, optionally filtered by pair.",
                self._entries,
                input_schema=tag_schema,
            )
        )
        registry.register(
            self._tool(
                "ft_exits",
                "Show exit reason performance, optionally filtered by pair.",
                self._exits,
                input_schema=tag_schema,
            )
        )
        registry.register(
            self._tool(
                "ft_mix_tags",
                "Show combined entry/exit tag performance, optionally filtered by pair.",
                self._mix_tags,
                input_schema=tag_schema,
            )
        )
        registry.register(
            self._tool(
                "ft_trades_recent",
                "List recent trades, limited to 500 by Freqtrade API.",
                self._trades_recent,
                input_schema=object_schema(
                    {"limit": {"type": "integer", "minimum": 1, "maximum": 100}}
                ),
            )
        )
        registry.register(
            self._tool(
                "ft_trade_detail",
                "Show one trade by trade_id.",
                self._trade_detail,
                input_schema=object_schema(
                    {"trade_id": {"type": "integer", "minimum": 1}},
                    required=["trade_id"],
                ),
            )
        )
        registry.register(
            self._tool(
                "ft_open_trade_custom_data",
                "Read custom data for open trades.",
                self._open_trade_custom_data,
                input_schema=object_schema(
                    {
                        "key": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                        "offset": {"type": "integer", "minimum": 0, "maximum": 10000},
                    }
                ),
            )
        )
        registry.register(
            self._tool(
                "ft_trade_custom_data",
                "Read custom data for one trade.",
                self._trade_custom_data,
                input_schema=object_schema(
                    {
                        "trade_id": {"type": "integer", "minimum": 1},
                        "key": {"type": "string"},
                    },
                    required=["trade_id"],
                ),
            )
        )
        registry.register(
            self._tool(
                "ft_show_config_sanitized",
                "Show sanitized running configuration without secrets.",
                self._show_config_sanitized,
            )
        )
        registry.register(
            self._tool(
                "ft_whitelist",
                "Show current pair whitelist.",
                self._whitelist,
            )
        )
        registry.register(
            self._tool(
                "ft_blacklist",
                "Show current pair blacklist.",
                self._blacklist,
            )
        )
        registry.register(
            self._tool(
                "ft_locks",
                "Show active pair locks.",
                self._locks,
            )
        )
        registry.register(
            self._tool(
                "ft_version",
                "Show Freqtrade version.",
                self._version,
            )
        )
        registry.register(
            self._tool(
                "ft_sysinfo",
                "Show sanitized Freqtrade system info.",
                self._sysinfo,
            )
        )
        registry.register(
            self._tool(
                "ft_plot_config",
                "Show strategy plot configuration.",
                self._plot_config,
                input_schema=object_schema({"strategy": {"type": "string"}}),
            )
        )
        registry.register(
            self._tool(
                "ft_strategy_info",
                "Show current strategy metadata and parameters; code is omitted by default.",
                self._strategy_info,
                input_schema=object_schema(
                    {
                        "strategy": {"type": "string"},
                        "include_code_excerpt": {"type": "boolean"},
                    }
                ),
            )
        )
        registry.register(
            self._tool(
                "ft_markets",
                "Show exchange markets, optionally filtered by base or quote currency.",
                self._markets,
                input_schema=object_schema(
                    {
                        "base": {"type": "string"},
                        "quote": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                    }
                ),
            )
        )
        registry.register(
            self._tool(
                "ft_pair_candles",
                "Show analyzed candles/indicator dataframe for one pair and timeframe.",
                self._pair_candles,
                input_schema=object_schema(
                    {
                        "pair": {"type": "string"},
                        "timeframe": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                    },
                    required=["pair", "timeframe"],
                ),
            )
        )
        registry.register(
            self._tool(
                "ft_pair_history",
                (
                    "Show historical analyzed candles for one pair/timeframe/timerange. "
                    "This Freqtrade endpoint is available in webserver mode."
                ),
                self._pair_history,
                input_schema=object_schema(
                    {
                        "pair": {"type": "string"},
                        "timeframe": {"type": "string"},
                        "timerange": {"type": "string"},
                        "strategy": {"type": "string"},
                    },
                    required=["pair", "timeframe", "timerange"],
                ),
            )
        )
        registry.register(
            self._tool(
                "ft_background_tasks",
                (
                    "Show Freqtrade API background tasks, optionally one job id. "
                    "This Freqtrade endpoint is available in webserver mode."
                ),
                self._background_tasks,
                input_schema=object_schema({"job_id": {"type": "string"}}),
            )
        )
        registry.register(
            self._tool(
                "ft_available_pairs",
                (
                    "Show locally available downloaded OHLCV pairs. "
                    "This Freqtrade endpoint is available in webserver mode."
                ),
                self._available_pairs,
                input_schema=object_schema(
                    {
                        "timeframe": {"type": "string"},
                        "stake_currency": {"type": "string"},
                    }
                ),
            )
        )

    def _register_l1(self, registry: ToolRegistry) -> None:
        for name, description in [
            ("ft_start", "Prepare a pending action to start the bot."),
            ("ft_pause", "Prepare a pending action to pause new entries."),
            ("ft_stop", "Prepare a pending action to stop the bot."),
            ("ft_reload_config", "Prepare a pending action to reload configuration."),
        ]:
            registry.register(
                ToolSpec(
                    name=name,
                    description=description,
                    permission_level=PermissionLevel.L1,
                    input_schema=object_schema(),
                    output_schema=any_output_schema(),
                    handler=lambda _args: {
                        "success": False,
                        "summary": "L1 control remains pending-only in this build.",
                    },
                    requires_confirmation=True,
                    risk_notes="L1 control is not executed in Phase 1; pending_action only.",
                    permission_default=PermissionAction.ASK,
                )
            )

    def _tool(
        self,
        name: str,
        description: str,
        handler,
        input_schema: dict[str, Any] | None = None,
    ) -> ToolSpec:
        return ToolSpec(
            name=name,
            description=description,
            permission_level=PermissionLevel.L0,
            input_schema=input_schema or object_schema(),
            output_schema=any_output_schema(),
            handler=handler,
            requires_confirmation=False,
            risk_notes="Read-only L0 tool.",
            permission_default=PermissionAction.ALLOW,
        )

    def _summary_result(self, label: str, data: Any) -> dict[str, Any]:
        return {
            "success": True,
            "summary": f"根据工具结果: {label} 返回成功。",
            "data": sanitize_data(data),
        }

    def _data_result(self, data: Any, summarizer) -> dict[str, Any]:
        return {"success": True, "summary": summarizer(data), "data": sanitize_data(data)}

    def _ping(self, _args: dict[str, Any]) -> dict[str, Any]:
        return self._summary_result(
            "ft_ping",
            self.client.request("GET", "ping", authenticate=False),
        )

    def _health(self, _args: dict[str, Any]) -> dict[str, Any]:
        return self._summary_result("ft_health", self.client.request("GET", "health"))

    def _status(self, _args: dict[str, Any]) -> dict[str, Any]:
        return self._data_result(self.client.request("GET", "status"), summarize_status)

    def _balance(self, _args: dict[str, Any]) -> dict[str, Any]:
        return self._data_result(self.client.request("GET", "balance"), summarize_balance)

    def _profit(self, _args: dict[str, Any]) -> dict[str, Any]:
        return self._data_result(self.client.request("GET", "profit"), summarize_profit)

    def _profit_all(self, _args: dict[str, Any]) -> dict[str, Any]:
        data = self.client.request("GET", "profit_all")
        groups = ", ".join(data.keys()) if isinstance(data, dict) else "unknown"
        return {
            "success": True,
            "summary": f"根据工具结果: profit_all 返回分组: {groups}。",
            "data": sanitize_data(data),
        }

    def _count(self, _args: dict[str, Any]) -> dict[str, Any]:
        return self._summary_result("ft_count", self.client.request("GET", "count"))

    def _performance(self, _args: dict[str, Any]) -> dict[str, Any]:
        data = self.client.request("GET", "performance")
        return self._data_result(
            data,
            lambda value: summarize_list("performance", value, "pair performance entries"),
        )

    def _stats(self, _args: dict[str, Any]) -> dict[str, Any]:
        data = self.client.request("GET", "stats")
        return {
            "success": True,
            "summary": "根据工具结果: stats 返回成功, 包含交易胜负和退出原因统计。",
            "data": sanitize_data(data),
        }

    def _daily(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._period_result("daily", args, default=7, max_value=90)

    def _weekly(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._period_result("weekly", args, default=4, max_value=52)

    def _monthly(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._period_result("monthly", args, default=3, max_value=36)

    def _period_result(
        self,
        endpoint: str,
        args: dict[str, Any],
        *,
        default: int,
        max_value: int,
    ) -> dict[str, Any]:
        timescale = self._bounded_int(args.get("timescale"), default, 1, max_value)
        data = self.client.request("GET", endpoint, params={"timescale": timescale})
        rows = data.get("data") if isinstance(data, dict) else None
        row_count = len(rows) if isinstance(rows, list) else "unknown"
        return {
            "success": True,
            "summary": (
                f"根据工具结果: {endpoint} 返回 timescale={timescale}, "
                f"records={row_count}。"
            ),
            "data": sanitize_data(data),
        }

    def _entries(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._tag_result("entries", args)

    def _exits(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._tag_result("exits", args)

    def _mix_tags(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._tag_result("mix_tags", args)

    def _tag_result(self, endpoint: str, args: dict[str, Any]) -> dict[str, Any]:
        params = self._clean_params({"pair": args.get("pair")})
        data = self.client.request("GET", endpoint, params=params or None)
        return self._data_result(
            data,
            lambda value: summarize_list(endpoint, value, "tag performance entries"),
        )

    def _whitelist(self, _args: dict[str, Any]) -> dict[str, Any]:
        return self._summary_result("ft_whitelist", self.client.request("GET", "whitelist"))

    def _blacklist(self, _args: dict[str, Any]) -> dict[str, Any]:
        return self._summary_result("ft_blacklist", self.client.request("GET", "blacklist"))

    def _locks(self, _args: dict[str, Any]) -> dict[str, Any]:
        data = self.client.request("GET", "locks")
        locks = data.get("locks") if isinstance(data, dict) else []
        count = len(locks) if isinstance(locks, list) else "unknown"
        return {
            "success": True,
            "summary": f"根据工具结果: locks 返回 {count} 条 pair locks。",
            "data": sanitize_data(data),
        }

    def _version(self, _args: dict[str, Any]) -> dict[str, Any]:
        data = self.client.request("GET", "version")
        return {
            "success": True,
            "summary": f"根据工具结果: Freqtrade version={data.get('version', 'unknown')}。",
            "data": sanitize_data(data),
        }

    def _sysinfo(self, _args: dict[str, Any]) -> dict[str, Any]:
        return self._summary_result("ft_sysinfo", self.client.request("GET", "sysinfo"))

    def _trades_recent(self, args: dict[str, Any]) -> dict[str, Any]:
        limit = int(args.get("limit") or 10)
        limit = max(1, min(limit, 100))
        data = self.client.request("GET", "trades", params={"limit": limit})
        count = (
            data.get("trades_count", len(data.get("trades") or []))
            if isinstance(data, dict)
            else 0
        )
        return {
            "success": True,
            "summary": f"根据工具结果: 最近 trades 返回 {count} 条记录。",
            "data": sanitize_data(data),
        }

    def _trade_detail(self, args: dict[str, Any]) -> dict[str, Any]:
        trade_id = int(args.get("trade_id") or 0)
        if trade_id <= 0:
            return {"success": False, "summary": "ft_trade_detail 需要 trade_id。"}
        data = self.client.request("GET", f"trade/{trade_id}")
        return {
            "success": True,
            "summary": (
                f"根据工具结果: trade #{trade_id} detail 返回 pair="
                f"{data.get('pair', 'unknown') if isinstance(data, dict) else 'unknown'}。"
            ),
            "data": sanitize_data(data),
        }

    def _open_trade_custom_data(self, args: dict[str, Any]) -> dict[str, Any]:
        params = self._clean_params(
            {
                "key": args.get("key"),
                "limit": self._bounded_int(args.get("limit"), 100, 1, 100),
                "offset": self._bounded_int(args.get("offset"), 0, 0, 10000),
            }
        )
        data = self.client.request("GET", "trades/open/custom-data", params=params)
        return self._data_result(
            data,
            lambda value: summarize_list("open trade custom data", value, "records"),
        )

    def _trade_custom_data(self, args: dict[str, Any]) -> dict[str, Any]:
        trade_id = int(args.get("trade_id") or 0)
        if trade_id <= 0:
            return {"success": False, "summary": "ft_trade_custom_data 需要 trade_id。"}
        params = self._clean_params({"key": args.get("key")})
        data = self.client.request(
            "GET",
            f"trades/{trade_id}/custom-data",
            params=params or None,
        )
        return self._data_result(
            data,
            lambda value: summarize_list(f"trade #{trade_id} custom data", value, "records"),
        )

    def _show_config_sanitized(self, _args: dict[str, Any]) -> dict[str, Any]:
        remote = self.client.request("GET", "show_config")
        config = sanitize_config(remote if isinstance(remote, dict) else {})
        if "dry_run" not in config and "dry_run" in self.settings.local_config:
            config["dry_run"] = self.settings.local_config["dry_run"]
        return {
            "success": True,
            "summary": (
                "根据工具结果: config 已脱敏。dry_run={dry_run}, strategy={strategy}, "
                "timeframe={timeframe}."
            ).format(
                dry_run=config.get("dry_run", "unknown"),
                strategy=config.get("strategy", "unknown"),
                timeframe=config.get("timeframe", "unknown"),
            ),
            "data": config,
        }

    def _plot_config(self, args: dict[str, Any]) -> dict[str, Any]:
        params = self._clean_params({"strategy": args.get("strategy")})
        data = self.client.request("GET", "plot_config", params=params or None)
        return {
            "success": True,
            "summary": "根据工具结果: plot_config 返回成功。",
            "data": sanitize_data(data),
        }

    def _strategy_info(self, args: dict[str, Any]) -> dict[str, Any]:
        strategy = str(args.get("strategy") or "").strip() or self._current_strategy()
        if not strategy:
            return {"success": False, "summary": "ft_strategy_info 未找到当前 strategy。"}
        data = self.client.request("GET", f"strategy/{strategy}")
        safe = sanitize_data(data if isinstance(data, dict) else {})
        if isinstance(safe, dict) and "code" in safe:
            code = str(safe.get("code") or "")
            safe["code_line_count"] = len(code.splitlines())
            if args.get("include_code_excerpt"):
                safe["code_excerpt"] = code[:1200]
            safe.pop("code", None)
        params_count = len(safe.get("params") or []) if isinstance(safe, dict) else "unknown"
        timeframe = safe.get("timeframe", "unknown") if isinstance(safe, dict) else "unknown"
        return {
            "success": True,
            "summary": (
                f"根据工具结果: strategy={strategy}, "
                f"timeframe={timeframe}, "
                f"params={params_count}, code 已默认省略。"
            ),
            "data": safe,
        }

    def _markets(self, args: dict[str, Any]) -> dict[str, Any]:
        quote = str(args.get("quote") or "").strip() or self._stake_currency()
        limit = self._bounded_int(args.get("limit"), 50, 1, 200)
        params = self._clean_params({"base": args.get("base"), "quote": quote})
        data = self.client.request("GET", "markets", params=params or None)
        safe = sanitize_data(data if isinstance(data, dict) else {})
        if isinstance(safe, dict) and isinstance(safe.get("markets"), dict):
            markets = safe["markets"]
            items = list(markets.items())
            safe["markets"] = dict(items[:limit])
            safe["markets_count"] = len(items)
            safe["markets_returned"] = min(len(items), limit)
        return {
            "success": True,
            "summary": (
                "根据工具结果: markets 返回 "
                f"{safe.get('markets_returned', 'unknown')}/"
                f"{safe.get('markets_count', 'unknown')} 个 markets, quote={quote or 'any'}。"
            ),
            "data": safe,
        }

    def _pair_candles(self, args: dict[str, Any]) -> dict[str, Any]:
        pair = str(args.get("pair") or "").strip()
        timeframe = str(args.get("timeframe") or "").strip()
        if not pair or not timeframe:
            return {"success": False, "summary": "ft_pair_candles 需要 pair 和 timeframe。"}
        limit = self._bounded_int(args.get("limit"), 50, 1, 500)
        data = self.client.request(
            "GET",
            "pair_candles",
            params={"pair": pair, "timeframe": timeframe, "limit": limit},
        )
        safe = self._trim_pair_data(data, max_rows=limit)
        length = safe.get("length", "unknown") if isinstance(safe, dict) else "unknown"
        rows_returned = len(safe.get("data") or []) if isinstance(safe, dict) else "unknown"
        return {
            "success": True,
            "summary": (
                f"根据工具结果: pair_candles {pair} {timeframe} 返回 "
                f"length={length}, rows_returned={rows_returned}。"
            ),
            "data": sanitize_data(safe),
        }

    def _pair_history(self, args: dict[str, Any]) -> dict[str, Any]:
        pair = str(args.get("pair") or "").strip()
        timeframe = str(args.get("timeframe") or "").strip()
        timerange = str(args.get("timerange") or "").strip()
        strategy = str(args.get("strategy") or "").strip() or self._current_strategy()
        if not pair or not timeframe or not timerange or not strategy:
            return {
                "success": False,
                "summary": "ft_pair_history 需要 pair、timeframe、timerange 和 strategy。",
            }
        data = self.client.request(
            "GET",
            "pair_history",
            params={
                "pair": pair,
                "timeframe": timeframe,
                "timerange": timerange,
                "strategy": strategy,
            },
        )
        safe = self._trim_pair_data(data, max_rows=200)
        rows_returned = len(safe.get("data") or []) if isinstance(safe, dict) else "unknown"
        return {
            "success": True,
            "summary": (
                f"根据工具结果: pair_history {pair} {timeframe} {timerange} "
                f"返回 rows_returned={rows_returned}。"
            ),
            "data": sanitize_data(safe),
        }

    def _background_tasks(self, args: dict[str, Any]) -> dict[str, Any]:
        job_id = str(args.get("job_id") or "").strip()
        data = self.client.request("GET", f"background/{job_id}" if job_id else "background")
        if isinstance(data, list):
            summary = f"根据工具结果: background 返回 {len(data)} 个后台任务。"
        else:
            summary = f"根据工具结果: background job {job_id or ''} 返回成功。"
        return {"success": True, "summary": summary, "data": sanitize_data(data)}

    def _available_pairs(self, args: dict[str, Any]) -> dict[str, Any]:
        params = self._clean_params(
            {
                "timeframe": args.get("timeframe"),
                "stake_currency": args.get("stake_currency") or self._stake_currency(),
            }
        )
        data = self.client.request("GET", "available_pairs", params=params or None)
        return {
            "success": True,
            "summary": (
                f"根据工具结果: available_pairs 返回 length="
                f"{data.get('length', 'unknown') if isinstance(data, dict) else 'unknown'}。"
            ),
            "data": sanitize_data(data),
        }

    def _current_strategy(self) -> str:
        local = self.settings.local_config
        strategy = str(local.get("strategy") or "").strip()
        if strategy:
            return strategy
        try:
            config = self.client.request("GET", "show_config")
            return str(config.get("strategy") or "").strip() if isinstance(config, dict) else ""
        except Exception:
            return ""

    def _stake_currency(self) -> str:
        local = self.settings.local_config
        stake = str(local.get("stake_currency") or "").strip()
        if stake:
            return stake
        try:
            config = self.client.request("GET", "show_config")
            if isinstance(config, dict):
                return str(config.get("stake_currency") or "").strip()
            return ""
        except Exception:
            return ""

    def _trim_pair_data(self, data: Any, *, max_rows: int) -> Any:
        if not isinstance(data, dict):
            return data
        safe = dict(data)
        rows = safe.get("data")
        if isinstance(rows, list) and len(rows) > max_rows:
            safe["data"] = rows[-max_rows:]
            safe["rows_truncated"] = len(rows) - max_rows
        else:
            safe["rows_truncated"] = 0
        return safe

    def _bounded_int(self, value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(minimum, min(parsed, maximum))

    def _clean_params(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in params.items()
            if value is not None and str(value).strip() != ""
        }
