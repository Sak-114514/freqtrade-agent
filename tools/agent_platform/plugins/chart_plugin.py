from __future__ import annotations

import json
import math
import re
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib import error, request

from PIL import Image, ImageDraw, ImageFont

from agent_platform.config import Settings
from agent_platform.registry.permissions import PermissionAction, PermissionLevel
from agent_platform.registry.tool_registry import (
    ToolRegistry,
    ToolSpec,
    any_output_schema,
    object_schema,
)


PNG_LIMIT_BYTES = 10 * 1024 * 1024
CANVAS_SIZE = (1200, 800)
BG = "#0f172a"
PANEL = "#111827"
GRID = "#334155"
TEXT = "#e5e7eb"
MUTED = "#94a3b8"
GREEN = "#22c55e"
RED = "#ef4444"
BLUE = "#38bdf8"
AMBER = "#f59e0b"


class ChartPlugin:
    """Generate local PNG trading charts and optionally send them to Telegram."""

    def __init__(self, *, settings: Settings, registry: ToolRegistry) -> None:
        self.settings = settings
        self.registry = registry
        self.chart_dir = settings.user_data_dir / "agent_charts"

    def register(self, registry: ToolRegistry) -> None:
        registry.register(
            ToolSpec(
                name="chart_trade_overview_preview",
                description=(
                    "Generate a local PNG trading overview chart from Freqtrade "
                    "profit, daily stats and recent trades. Does not send Telegram."
                ),
                permission_level=PermissionLevel.L0,
                input_schema=object_schema(
                    {
                        "days": {
                            "type": "integer",
                            "description": "Lookback days, default 14, max 90.",
                            "default": 14,
                        },
                        "include_trades": {
                            "type": "boolean",
                            "description": "Include recent trade markers/summary if available.",
                            "default": True,
                        },
                    }
                ),
                output_schema=any_output_schema(),
                handler=self._trade_overview_preview,
                requires_confirmation=False,
                risk_notes="Read-only chart generation. Does not send Telegram or trade.",
                permission_default=PermissionAction.ALLOW,
            )
        )
        registry.register(
            ToolSpec(
                name="chart_candles_preview",
                description=(
                    "Generate a local PNG candlestick chart for a pair/timeframe using "
                    "Freqtrade pair_candles. Does not send Telegram."
                ),
                permission_level=PermissionLevel.L0,
                input_schema=object_schema(
                    {
                        "pair": {
                            "type": "string",
                            "description": "Trading pair, default BTC/USDT.",
                            "default": "BTC/USDT",
                        },
                        "timeframe": {
                            "type": "string",
                            "description": "Timeframe, default current config timeframe.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Candle count, default 100, max 500.",
                            "default": 100,
                        },
                        "include_indicators": {
                            "type": "boolean",
                            "description": "Overlay simple moving average when possible.",
                            "default": True,
                        },
                    }
                ),
                output_schema=any_output_schema(),
                handler=self._candles_preview,
                requires_confirmation=False,
                risk_notes="Read-only chart generation. Does not send Telegram or trade.",
                permission_default=PermissionAction.ALLOW,
            )
        )
        registry.register(
            ToolSpec(
                name="telegram_chart_send",
                description=(
                    "Send a previously generated local chart PNG to Telegram via sendPhoto. "
                    "Requires confirmation."
                ),
                permission_level=PermissionLevel.L1,
                input_schema=object_schema(
                    {
                        "chart_path": {
                            "type": "string",
                            "description": "Path returned by a chart preview tool.",
                        },
                        "caption": {
                            "type": "string",
                            "description": "Plain text Telegram caption.",
                        },
                        "chat_id": {
                            "type": "string",
                            "description": "Optional Telegram chat_id, defaults to config.",
                        },
                    },
                    required=["chart_path"],
                ),
                output_schema=any_output_schema(),
                handler=self._send_chart,
                requires_confirmation=True,
                risk_notes=(
                    "Sends a Telegram photo only. No trading or config changes. "
                    "The image must be generated under user_data/agent_charts."
                ),
                permission_default=PermissionAction.ASK,
            )
        )

    def capabilities(self) -> dict[str, Any]:
        return {
            "name": "chart_plugin",
            "status": "active",
            "tools": [
                "chart_trade_overview_preview",
                "chart_candles_preview",
                "telegram_chart_send",
            ],
        }

    def _trade_overview_preview(self, args: dict[str, Any]) -> dict[str, Any]:
        days = _bounded_int(args.get("days"), 14, 1, 90)
        include_trades = bool(args.get("include_trades", True))
        calls = [
            ("ft_profit", {}),
            ("ft_daily", {"timescale": days}),
            ("ft_stats", {}),
        ]
        if include_trades:
            calls.append(("ft_trades_recent", {"limit": 20}))

        results = {name: self.registry.execute(name, call_args) for name, call_args in calls}
        failures = [
            f"{name}: {result.get('summary') or result.get('error')}"
            for name, result in results.items()
            if not result.get("success")
        ]
        daily_points = _daily_points(_result_data(results.get("ft_daily")))
        trade_points = _trade_profit_points(_result_data(results.get("ft_trades_recent")))
        values = daily_points or trade_points
        if not values:
            return {
                "success": False,
                "summary": "图表生成失败: ft_daily/ft_trades_recent 没有可绘制的收益数据。",
                "data": {"data_sources": list(results), "warnings": failures},
            }

        chart_path = self._chart_path("trade_overview")
        warnings = failures[:]
        if not daily_points:
            warnings.append("ft_daily 无日收益序列, 已使用 recent trades profit 作为替代。")

        profit_data = _result_data(results.get("ft_profit"))
        stats_data = _result_data(results.get("ft_stats"))
        self._draw_trade_overview(
            chart_path=chart_path,
            values=values,
            profit_data=profit_data,
            stats_data=stats_data,
            trades_data=_result_data(results.get("ft_trades_recent")),
            days=days,
            warnings=warnings,
        )
        caption = _plain_caption(
            f"Trading overview chart, last {days} days. "
            "Data: ft_profit, ft_daily, ft_stats"
            + (", ft_trades_recent." if include_trades else ".")
            + " Dry-run only; no trade executed."
        )
        return {
            "success": True,
            "summary": f"图表已生成: {chart_path}",
            "data": {
                "chart_path": str(chart_path),
                "caption": caption,
                "data_sources": list(results),
                "warnings": warnings,
            },
        }

    def _candles_preview(self, args: dict[str, Any]) -> dict[str, Any]:
        pair = str(args.get("pair") or "BTC/USDT").strip() or "BTC/USDT"
        timeframe = str(args.get("timeframe") or self._default_timeframe()).strip()
        limit = _bounded_int(args.get("limit"), 100, 1, 500)
        include_indicators = bool(args.get("include_indicators", True))

        candles_result = self.registry.execute(
            "ft_pair_candles",
            {"pair": pair, "timeframe": timeframe, "limit": limit},
        )
        if not candles_result.get("success"):
            return {
                "success": False,
                "summary": "图表生成失败: ft_pair_candles 调用失败。",
                "data": {
                    "data_sources": ["ft_pair_candles"],
                    "warnings": [str(candles_result.get("summary") or candles_result.get("error"))],
                },
            }

        candles = _candles(_result_data(candles_result))
        if not candles:
            return {
                "success": False,
                "summary": f"图表生成失败: {pair} {timeframe} 没有可绘制 K 线数据。",
                "data": {"data_sources": ["ft_pair_candles"], "warnings": []},
            }

        trades_result = self.registry.execute("ft_trades_recent", {"limit": 50})
        trades = _trades_for_pair(_result_data(trades_result), pair)
        warnings: list[str] = []
        if not trades:
            warnings.append("无交易标记数据。")
        if not trades_result.get("success"):
            warnings.append(str(trades_result.get("summary") or trades_result.get("error")))

        chart_path = self._chart_path(f"candles_{pair}_{timeframe}")
        self._draw_candles(
            chart_path=chart_path,
            candles=candles,
            pair=pair,
            timeframe=timeframe,
            include_indicators=include_indicators,
            trades=trades,
            warnings=warnings,
        )
        caption = _plain_caption(
            f"{pair} {timeframe} candlestick chart, {len(candles)} candles. "
            "Data: ft_pair_candles"
            + (", ft_trades_recent." if trades else ".")
            + " Dry-run only; no trade executed."
        )
        return {
            "success": True,
            "summary": f"K线图已生成: {chart_path}",
            "data": {
                "chart_path": str(chart_path),
                "caption": caption,
                "data_sources": ["ft_pair_candles", "ft_trades_recent"],
                "warnings": warnings,
            },
        }

    def _send_chart(self, args: dict[str, Any]) -> dict[str, Any]:
        token = self.settings.telegram_token
        chat_id = str(args.get("chat_id") or self.settings.telegram_chat_id or "").strip()
        if not token or not chat_id:
            return {
                "success": False,
                "summary": "telegram_chart_send 失败: 缺少 Telegram token 或 chat_id。",
            }

        chart_path = Path(str(args.get("chart_path") or "")).expanduser()
        try:
            safe_path = self._resolve_chart_path(chart_path)
        except ValueError as exc:
            return {"success": False, "summary": f"telegram_chart_send 失败: {exc}"}
        if safe_path.stat().st_size > PNG_LIMIT_BYTES:
            return {"success": False, "summary": "telegram_chart_send 失败: PNG 超过 10 MB。"}

        caption = _plain_caption(str(args.get("caption") or "Trading chart. Dry-run only."))
        response = self._send_photo(
            token=token,
            chat_id=chat_id,
            photo_path=safe_path,
            caption=caption,
        )
        message_id = (
            response.get("result", {}).get("message_id")
            if isinstance(response.get("result"), dict)
            else None
        )
        return {
            "success": True,
            "summary": f"图表已发送到 Telegram: message_id={message_id or 'unknown'}。",
            "data": {
                "message_id": message_id,
                "chart_path": str(safe_path),
                "caption": caption,
            },
        }

    def _send_photo(
        self,
        *,
        token: str,
        chat_id: str,
        photo_path: Path,
        caption: str,
    ) -> dict[str, Any]:
        boundary = f"----freqtrade-agent-{uuid.uuid4().hex}"
        parts: list[bytes] = []

        def add_field(name: str, value: str) -> None:
            parts.append(f"--{boundary}\r\n".encode())
            parts.append(
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
            )

        def add_file(name: str, file_path: Path) -> None:
            data = file_path.read_bytes()
            parts.append(f"--{boundary}\r\n".encode())
            parts.append(
                (
                    f'Content-Disposition: form-data; name="{name}"; '
                    f'filename="{file_path.name}"\r\n'
                    "Content-Type: image/png\r\n\r\n"
                ).encode()
            )
            parts.append(data)
            parts.append(b"\r\n")

        add_field("chat_id", chat_id)
        add_field("caption", caption[:1024])
        add_file("photo", photo_path)
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        req = request.Request(
            f"https://api.telegram.org/bot{token}/sendPhoto",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=20) as resp:  # noqa: S310 - Telegram Bot API.
                payload = resp.read().decode("utf-8")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"Telegram sendPhoto HTTP {exc.code}: {detail}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"Telegram sendPhoto network error: {exc}") from exc
        parsed = json.loads(payload)
        if not parsed.get("ok"):
            raise RuntimeError(f"Telegram sendPhoto failed: {parsed}")
        return parsed

    def _draw_trade_overview(
        self,
        *,
        chart_path: Path,
        values: list[tuple[str, float]],
        profit_data: dict[str, Any],
        stats_data: dict[str, Any],
        trades_data: dict[str, Any],
        days: int,
        warnings: list[str],
    ) -> None:
        image, draw = _base_canvas("Trading Overview", f"Last {days} days | dry-run only")
        _draw_panel(draw, (48, 116, 1152, 548))
        _draw_line_chart(draw, values, (86, 160, 1108, 500), BLUE)

        total_profit = _first_number(
            profit_data,
            ["profit_all_abs", "profit_closed_coin", "profit_all_coin", "profit_total_abs"],
        )
        total_pct = _first_number(
            profit_data,
            ["profit_all_percent", "profit_closed_percent", "profit_total_percent"],
        )
        trade_count = _first_number(profit_data, ["trade_count", "total_trades"])
        win_rate = _first_number(stats_data, ["winrate", "win_rate", "winning_rate"])
        cards = [
            ("Profit", _format_money(total_profit), BLUE),
            ("Profit %", _format_pct(total_pct), GREEN if (total_pct or 0) >= 0 else RED),
            ("Trades", str(int(trade_count)) if trade_count is not None else "n/a", AMBER),
            ("Win rate", _format_pct(win_rate), GREEN),
        ]
        x = 48
        for label, value, color in cards:
            _metric_card(draw, (x, 582, x + 252, 704), label, value, color)
            x += 284

        recent_count = len(_trades_for_pair(trades_data, "")) if trades_data else 0
        footer = f"Data sources: ft_profit, ft_daily, ft_stats, ft_trades_recent ({recent_count} recent trades)."
        if warnings:
            footer += " Warning: " + "; ".join(warnings[:2])
        _text(draw, (48, 742), footer[:180], MUTED, size=15)
        image.save(chart_path, "PNG")

    def _draw_candles(
        self,
        *,
        chart_path: Path,
        candles: list[dict[str, Any]],
        pair: str,
        timeframe: str,
        include_indicators: bool,
        trades: list[dict[str, Any]],
        warnings: list[str],
    ) -> None:
        image, draw = _base_canvas(f"{pair} Candles", f"{timeframe} | {len(candles)} candles | dry-run only")
        _draw_panel(draw, (48, 116, 1152, 650))
        bounds = (82, 150, 1118, 612)
        lows = [c["low"] for c in candles]
        highs = [c["high"] for c in candles]
        min_price = min(lows)
        max_price = max(highs)
        if math.isclose(min_price, max_price):
            min_price *= 0.99
            max_price *= 1.01

        left, top, right, bottom = bounds
        for i in range(6):
            y = top + (bottom - top) * i / 5
            draw.line((left, y, right, y), fill=GRID)
            price = max_price - (max_price - min_price) * i / 5
            _text(draw, (right + 12, y - 8), f"{price:.4g}", MUTED, size=13)

        width = max(2, (right - left) / max(1, len(candles)))
        body_w = max(2, min(10, int(width * 0.65)))

        def y_for(price: float) -> float:
            return bottom - (price - min_price) / (max_price - min_price) * (bottom - top)

        closes: list[float] = []
        for idx, candle in enumerate(candles):
            x = left + idx * width + width / 2
            open_y = y_for(candle["open"])
            close_y = y_for(candle["close"])
            high_y = y_for(candle["high"])
            low_y = y_for(candle["low"])
            color = GREEN if candle["close"] >= candle["open"] else RED
            draw.line((x, high_y, x, low_y), fill=color, width=1)
            y1, y2 = sorted((open_y, close_y))
            draw.rectangle((x - body_w / 2, y1, x + body_w / 2, max(y1 + 1, y2)), fill=color)
            closes.append(candle["close"])

        if include_indicators and len(closes) >= 10:
            ma = _moving_average(closes, 10)
            points = [
                (left + idx * width + width / 2, y_for(value))
                for idx, value in enumerate(ma)
                if value is not None
            ]
            if len(points) >= 2:
                draw.line(points, fill=AMBER, width=2)
                _text(draw, (88, 620), "MA10", AMBER, size=14)

        for trade in trades[:20]:
            marker_price = _first_number(trade, ["open_rate", "close_rate", "price", "rate"])
            if marker_price is None:
                continue
            idx = min(len(candles) - 1, max(0, len(candles) - 1))
            x = left + idx * width + width / 2
            y = y_for(marker_price)
            draw.ellipse((x - 5, y - 5, x + 5, y + 5), fill=AMBER)

        footer = "Data sources: ft_pair_candles, ft_trades_recent."
        if warnings:
            footer += " " + "; ".join(warnings[:2])
        _text(draw, (48, 704), footer[:180], MUTED, size=15)
        _text(draw, (48, 734), "Chart is informational only. No trading action was executed.", MUTED, size=15)
        image.save(chart_path, "PNG")

    def _chart_path(self, chart_type: str) -> Path:
        self.chart_dir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", chart_type).strip("_") or "chart"
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return self.chart_dir / f"{slug}_{stamp}_{uuid.uuid4().hex[:8]}.png"

    def _resolve_chart_path(self, chart_path: Path) -> Path:
        if not chart_path.is_absolute():
            chart_path = self.chart_dir / chart_path
        resolved = chart_path.resolve()
        chart_root = self.chart_dir.resolve()
        try:
            resolved.relative_to(chart_root)
        except ValueError as exc:
            raise ValueError("chart_path 必须位于 user_data/agent_charts 目录内。") from exc
        if not resolved.exists() or resolved.suffix.lower() != ".png":
            raise ValueError("chart_path 不存在或不是 PNG 文件。")
        return resolved

    def _default_timeframe(self) -> str:
        local = self.settings.local_config
        return str(local.get("timeframe") or "5m")


def _base_canvas(title: str, subtitle: str) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGB", CANVAS_SIZE, BG)
    draw = ImageDraw.Draw(image)
    _text(draw, (48, 42), title, TEXT, size=34)
    _text(draw, (48, 82), subtitle, MUTED, size=16)
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    _text(draw, (906, 48), f"Updated {now}", MUTED, size=15)
    return image, draw


def _draw_panel(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int]) -> None:
    draw.rounded_rectangle(box, radius=18, fill=PANEL, outline=GRID, width=1)


def _draw_line_chart(
    draw: ImageDraw.ImageDraw,
    values: list[tuple[str, float]],
    box: tuple[int, int, int, int],
    color: str,
) -> None:
    left, top, right, bottom = box
    nums = [value for _, value in values]
    cumulative: list[float] = []
    total = 0.0
    for value in nums:
        total += value
        cumulative.append(total)
    min_v = min(cumulative + [0.0])
    max_v = max(cumulative + [0.0])
    if math.isclose(min_v, max_v):
        min_v -= 1.0
        max_v += 1.0
    for i in range(6):
        y = top + (bottom - top) * i / 5
        draw.line((left, y, right, y), fill=GRID)
        label = max_v - (max_v - min_v) * i / 5
        _text(draw, (right + 12, y - 8), f"{label:.3g}", MUTED, size=13)
    points = []
    for idx, value in enumerate(cumulative):
        x = left + (right - left) * idx / max(1, len(cumulative) - 1)
        y = bottom - (value - min_v) / (max_v - min_v) * (bottom - top)
        points.append((x, y))
    if len(points) >= 2:
        draw.line(points, fill=color, width=3)
    for x, y in points[-20:]:
        draw.ellipse((x - 3, y - 3, x + 3, y + 3), fill=color)
    _text(draw, (left, bottom + 16), values[0][0], MUTED, size=13)
    _text(draw, (right - 90, bottom + 16), values[-1][0], MUTED, size=13)


def _metric_card(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    label: str,
    value: str,
    color: str,
) -> None:
    _draw_panel(draw, box)
    x1, y1, _, _ = box
    _text(draw, (x1 + 20, y1 + 22), label, MUTED, size=16)
    _text(draw, (x1 + 20, y1 + 58), value, color, size=26)


def _text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    fill: str,
    *,
    size: int = 16,
) -> None:
    draw.text(xy, text, fill=fill, font=_font(size))


def _font(size: int) -> ImageFont.ImageFont:
    # DejaVu is available in most Debian/Python images. Fall back to PIL's bitmap font.
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _result_data(result: dict[str, Any] | None) -> dict[str, Any]:
    data = (result or {}).get("data")
    return data if isinstance(data, dict) else {}


def _daily_points(data: dict[str, Any]) -> list[tuple[str, float]]:
    rows = _first_list(data, ("data", "daily", "results", "items"))
    points: list[tuple[str, float]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        value = _first_number(
            row,
            ["profit_abs", "profit", "profit_total_abs", "abs_profit", "profit_closed_abs"],
        )
        if value is None:
            value = _first_number(row, ["profit_percent", "profit_pct", "profit_ratio"])
        if value is None:
            continue
        label = str(
            row.get("date")
            or row.get("day")
            or row.get("time")
            or row.get("timestamp")
            or f"#{index + 1}"
        )[:10]
        points.append((label, float(value)))
    return points


def _trade_profit_points(data: dict[str, Any]) -> list[tuple[str, float]]:
    rows = _first_list(data, ("trades", "data", "items", "results"))
    points: list[tuple[str, float]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        value = _first_number(
            row,
            ["profit_abs", "close_profit_abs", "realized_profit", "profit_ratio", "profit_pct"],
        )
        if value is None:
            continue
        label = str(row.get("close_date") or row.get("open_date") or f"#{index + 1}")[:10]
        points.append((label, float(value)))
    return points


def _candles(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows = _first_list(data, ("data", "candles", "ohlcv", "results"))
    columns = data.get("columns") if isinstance(data.get("columns"), list) else []
    out: list[dict[str, Any]] = []
    for row in rows:
        item = _row_to_dict(row, columns)
        if not item:
            continue
        open_v = _first_number(item, ["open", "o"])
        high_v = _first_number(item, ["high", "h"])
        low_v = _first_number(item, ["low", "l"])
        close_v = _first_number(item, ["close", "c"])
        if None in {open_v, high_v, low_v, close_v}:
            continue
        out.append(
            {
                "date": item.get("date") or item.get("time") or item.get("timestamp") or "",
                "open": float(open_v),
                "high": float(high_v),
                "low": float(low_v),
                "close": float(close_v),
            }
        )
    return out


def _trades_for_pair(data: dict[str, Any], pair: str) -> list[dict[str, Any]]:
    rows = _first_list(data, ("trades", "data", "items", "results"))
    if not pair:
        return [row for row in rows if isinstance(row, dict)]
    return [
        row
        for row in rows
        if isinstance(row, dict)
        and str(row.get("pair") or "").replace(":", "/").startswith(pair)
    ]


def _first_list(data: dict[str, Any], keys: Iterable[str]) -> list[Any]:
    for key in keys:
        value = data.get(key)
        if isinstance(value, list):
            return value
    for value in data.values():
        if isinstance(value, dict):
            nested = _first_list(value, keys)
            if nested:
                return nested
    return []


def _row_to_dict(row: Any, columns: list[Any]) -> dict[str, Any]:
    if isinstance(row, dict):
        return row
    if isinstance(row, (list, tuple)) and columns:
        return {str(columns[idx]): value for idx, value in enumerate(row[: len(columns)])}
    return {}


def _first_number(data: dict[str, Any], keys: Iterable[str]) -> float | None:
    lowered = {str(key).lower(): value for key, value in data.items()}
    for key in keys:
        value = lowered.get(key.lower())
        number = _as_float(value)
        if number is not None:
            return number
    for key, value in lowered.items():
        if any(candidate.lower() in key for candidate in keys):
            number = _as_float(value)
            if number is not None:
                return number
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().rstrip("%")
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _moving_average(values: list[float], window: int) -> list[float | None]:
    out: list[float | None] = []
    for idx in range(len(values)):
        if idx + 1 < window:
            out.append(None)
            continue
        segment = values[idx + 1 - window : idx + 1]
        out.append(sum(segment) / window)
    return out


def _format_money(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.4g}"


def _format_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.2f}%"


def _plain_caption(text: str) -> str:
    cleaned = re.sub(r"[*`~>#\[\]{}|]", "", text)
    cleaned = re.sub(r"https?://\S+", lambda m: m.group(0).rstrip(").,，。"), cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:1024]


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))
