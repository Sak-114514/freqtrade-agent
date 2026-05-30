from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from agent_platform.config import Settings
from agent_platform.registry.permissions import PermissionAction, PermissionLevel
from agent_platform.registry.tool_registry import (
    ToolRegistry,
    ToolSpec,
    any_output_schema,
    object_schema,
)
from agent_platform.storage.db import AgentDB


class TelegramDashboardPlugin:
    def __init__(
        self,
        *,
        settings: Settings,
        db: AgentDB,
        registry: ToolRegistry,
    ) -> None:
        self.settings = settings
        self.db = db
        self.registry = registry

    def register(self, registry: ToolRegistry) -> None:
        registry.register(
            ToolSpec(
                name="telegram_dashboard_preview",
                description=(
                    "Build a read-only Telegram pinned dashboard preview from config, "
                    "scheduler jobs and monitor rules."
                ),
                permission_level=PermissionLevel.L0,
                input_schema=object_schema(
                    {"chat_id": {"type": "string"}},
                    required=[],
                ),
                output_schema=any_output_schema(),
                handler=self._preview,
                requires_confirmation=False,
                risk_notes="Preview only; does not send or pin Telegram messages.",
                permission_default=PermissionAction.ALLOW,
            )
        )
        registry.register(
            ToolSpec(
                name="telegram_dashboard_pin",
                description=(
                    "Send or update a Telegram pinned dashboard message for current "
                    "strategy, scheduler jobs and monitor rules."
                ),
                permission_level=PermissionLevel.L1,
                input_schema=object_schema(
                    {"chat_id": {"type": "string"}},
                    required=[],
                ),
                output_schema=any_output_schema(),
                handler=self._pin,
                requires_confirmation=False,
                risk_notes=(
                    "Pins or edits one Telegram dashboard message only; no trades, "
                    "strategy changes or config changes are executed."
                ),
                permission_default=PermissionAction.ALLOW,
                permission_resolver=self._pin_permission,
            )
        )

    def _pin_permission(self, args: dict[str, Any]) -> PermissionAction:
        chat_id = self._target_chat_id(args)
        if not chat_id:
            return PermissionAction.ASK
        if self.db.get_telegram_dashboard(chat_id):
            return PermissionAction.ALLOW
        return PermissionAction.ASK

    def _preview(self, args: dict[str, Any]) -> dict[str, Any]:
        chat_id = self._target_chat_id(args)
        text = self._build_dashboard_text(chat_id=chat_id)
        return {
            "success": True,
            "summary": (
                "根据工具结果: 已生成 Telegram dashboard 预览, 未发送消息。"
                + self._dashboard_summary(text)
            ),
            "data": {"chat_id": chat_id, "text": text},
        }

    def _pin(self, args: dict[str, Any]) -> dict[str, Any]:
        chat_id = self._target_chat_id(args)
        if not self.settings.telegram_token:
            return {
                "success": False,
                "summary": "telegram_dashboard_pin 失败: 未配置 Telegram token。",
            }
        if not chat_id:
            return {
                "success": False,
                "summary": "telegram_dashboard_pin 失败: 未配置 Telegram chat_id。",
            }

        text = self._build_dashboard_text(chat_id=chat_id)
        existing = self.db.get_telegram_dashboard(chat_id)
        if existing:
            message_id = int(existing["message_id"])
            edited = self._try_edit_message(chat_id=chat_id, message_id=message_id, text=text)
            if edited:
                dashboard = self.db.upsert_telegram_dashboard(
                    chat_id=chat_id,
                    message_id=message_id,
                    last_text=text,
                )
                return {
                    "success": True,
                    "summary": (
                        f"根据工具结果: 已更新 Telegram pinned dashboard "
                        f"message #{message_id}。{self._dashboard_summary(text)}"
                    ),
                    "data": {"dashboard": dashboard, "text": text, "updated_existing": True},
                }

        message_id = self._send_message(chat_id=chat_id, text=text)
        self._pin_message(chat_id=chat_id, message_id=message_id)
        dashboard = self.db.upsert_telegram_dashboard(
            chat_id=chat_id,
            message_id=message_id,
            last_text=text,
        )
        return {
            "success": True,
            "summary": (
                f"根据工具结果: 已发送并置顶 Telegram dashboard message #{message_id}。"
                f"{self._dashboard_summary(text)}"
            ),
            "data": {"dashboard": dashboard, "text": text, "updated_existing": False},
        }

    def _dashboard_summary(self, text: str) -> str:
        compact_lines = [
            line
            for line in text.splitlines()
            if line.startswith(("Bot:", "dry-run:", "Strategy:", "Exchange/stake:"))
        ]
        job_count = sum(
            1
            for line in text.splitlines()
            if line.startswith("- #") and "next " in line
        )
        rule_count = sum(
            1
            for line in text.splitlines()
            if line.startswith("- #") and "every " in line
        )
        compact_lines.append(f"Scheduler jobs: {job_count}")
        compact_lines.append(f"Monitor rules: {rule_count}")
        return " " + " | ".join(compact_lines)

    def _build_dashboard_text(self, *, chat_id: str) -> str:
        config_result = self.registry.execute("ft_show_config_sanitized", {})
        health_result = self.registry.execute("ft_health", {})
        config = config_result.get("data") if isinstance(config_result.get("data"), dict) else {}
        health = health_result.get("data") if isinstance(health_result.get("data"), dict) else {}
        jobs = self.db.scheduled_jobs()
        rules = self.db.monitor_rules()
        updated_at = datetime.now(UTC).replace(microsecond=0).isoformat()

        lines = [
            "Trading Copilot Dashboard",
            "",
            f"Bot: {self._status_text(health_result, health)}",
            f"dry-run: {config.get('dry_run', 'unknown')}",
            (
                "Strategy: "
                f"{config.get('strategy', 'unknown')} | "
                f"timeframe: {config.get('timeframe', 'unknown')}"
            ),
            (
                "Exchange/stake: "
                f"{config.get('exchange', 'unknown')} | "
                f"{config.get('stake_amount', 'unknown')} "
                f"{config.get('stake_currency', '')}".rstrip()
            ),
            "",
            "Scheduler jobs:",
            *self._format_jobs(jobs),
            "",
            "Monitor rules:",
            *self._format_rules(rules),
            "",
            f"Chat: {chat_id}",
            f"Last update: {updated_at}",
            "提示: 未执行任何交易; 这不是投资建议。",
        ]
        return "\n".join(lines)[:4096]

    def _status_text(self, result: dict[str, Any], data: dict[str, Any]) -> str:
        if not result.get("success"):
            return "unknown"
        for key in ("status", "state"):
            if data.get(key):
                return str(data[key])
        return "reachable"

    def _format_jobs(self, jobs: list[dict[str, Any]]) -> list[str]:
        if not jobs:
            return ["- none"]
        lines: list[str] = []
        for job in jobs[:8]:
            enabled = "enabled" if int(job.get("enabled") or 0) else "disabled"
            next_run = job.get("next_run_at") or "not scheduled"
            lines.append(f"- #{job.get('id')} {job.get('name')}: {enabled}, next {next_run}")
        if len(jobs) > 8:
            lines.append(f"- ... {len(jobs) - 8} more")
        return lines

    def _format_rules(self, rules: list[dict[str, Any]]) -> list[str]:
        if not rules:
            return ["- none"]
        lines: list[str] = []
        for rule in rules[:8]:
            enabled = "enabled" if int(rule.get("enabled") or 0) else "paused"
            pair = rule.get("pair") or "any"
            threshold = rule.get("change_threshold_pct")
            threshold_text = f"{threshold}%" if threshold is not None else "no threshold"
            interval = rule.get("interval_minutes") or "?"
            lines.append(
                f"- #{rule.get('id')} {rule.get('name')}: {enabled}, "
                f"{pair}, every {interval}m, {threshold_text}"
            )
        if len(rules) > 8:
            lines.append(f"- ... {len(rules) - 8} more")
        return lines

    def _target_chat_id(self, args: dict[str, Any]) -> str:
        return str(args.get("chat_id") or self.settings.telegram_chat_id or "").strip()

    def _telegram_request(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"https://api.telegram.org/bot{self.settings.telegram_token}/{method}"
        request = Request(  # noqa: S310 - Telegram API endpoint is fixed.
            url,
            data=urlencode(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urlopen(request, timeout=20) as response:  # noqa: S310
            raw = response.read().decode("utf-8")
        data = json.loads(raw)
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {data}")
        return data

    def _send_message(self, *, chat_id: str, text: str) -> int:
        data = self._telegram_request("sendMessage", {"chat_id": chat_id, "text": text})
        message = data.get("result") if isinstance(data.get("result"), dict) else {}
        message_id = int(message.get("message_id") or 0)
        if message_id <= 0:
            raise RuntimeError(f"Telegram sendMessage returned no message_id: {data}")
        return message_id

    def _pin_message(self, *, chat_id: str, message_id: int) -> None:
        self._telegram_request(
            "pinChatMessage",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "disable_notification": "true",
            },
        )

    def _try_edit_message(self, *, chat_id: str, message_id: int, text: str) -> bool:
        try:
            self._telegram_request(
                "editMessageText",
                {"chat_id": chat_id, "message_id": message_id, "text": text},
            )
            return True
        except Exception:
            return False
