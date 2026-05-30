from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from agent_platform.config import Settings
from agent_platform.storage.db import AgentDB


logger = logging.getLogger(__name__)

AskCallback = Callable[..., dict[str, Any]]


class MonitorService:
    def __init__(self, *, settings: Settings, db: AgentDB, ask_callback: AskCallback) -> None:
        self.settings = settings
        self.db = db
        self.ask_callback = ask_callback
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="TradingAgentMonitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.settings.monitor_interval_seconds):
            try:
                self.run_due_rules()
            except Exception:
                logger.exception("MonitorService run failed")

    def run_due_rules(self) -> None:
        for rule in self.db.monitor_rules(enabled_only=True):
            if not self._is_due(rule):
                continue
            self.run_rule(rule, trigger_reason="interval due")

    def run_rule(self, rule: dict[str, Any], *, trigger_reason: str) -> dict[str, Any]:
        rule_id = int(rule["id"])
        question = self._monitor_question(rule, trigger_reason)
        response = self.ask_callback(
            question=question,
            source="monitor",
            user_id="monitor",
            chat_id=self.settings.telegram_chat_id or "monitor",
        )
        answer = str(response.get("answer") or "")
        sent = False
        error = None
        try:
            self._send_telegram(self._format_telegram_message(rule, answer, response))
            sent = True
        except Exception as exc:
            error = str(exc)
            logger.warning("Failed to send monitor suggestion: %s", exc)
        raw_tool_calls = response.get("tool_calls")
        tool_calls = raw_tool_calls if isinstance(raw_tool_calls, list) else []
        self.db.save_monitor_event(
            rule_id=rule_id,
            event_type="suggestion",
            trigger_reason=trigger_reason,
            answer=answer,
            tool_calls=tool_calls,
            sent=sent,
            error=error,
        )
        self.db.mark_monitor_run(rule_id)
        return response

    def _is_due(self, rule: dict[str, Any]) -> bool:
        last_run_at = rule.get("last_run_at")
        if not last_run_at:
            return True
        try:
            last = datetime.fromisoformat(str(last_run_at))
        except ValueError:
            return True
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        elapsed = datetime.now(UTC) - last
        return elapsed.total_seconds() >= int(rule.get("interval_minutes") or 30) * 60

    def _monitor_question(self, rule: dict[str, Any], trigger_reason: str) -> str:
        pair = rule.get("pair") or "未指定"
        threshold = rule.get("change_threshold_pct")
        return (
            "主动 monitor 检查。"
            f"规则名称: {rule.get('name')}; "
            f"触发原因: {trigger_reason}; "
            f"关注交易对: {pair}; "
            f"波动阈值: {threshold if threshold is not None else '未指定'}; "
            f"用户规则: {rule.get('prompt')}\n"
            "请只使用已注册的安全工具形成 Telegram 建议。"
            "必须写明事实依据、调用过的工具、非投资建议、未执行任何交易。"
        )

    def _format_telegram_message(
        self,
        rule: dict[str, Any],
        answer: str,
        response: dict[str, Any],
    ) -> str:
        tools = ", ".join(
            str(item.get("tool_name"))
            for item in response.get("tool_calls", [])
            if isinstance(item, dict)
        )
        return (
            f"Agent 主动建议 - {rule.get('name')}\n"
            f"Rule #{rule.get('id')}\n"
            f"Tools: {tools or 'none'}\n\n"
            f"{answer}\n\n"
            "提示: 这不是投资建议; Agent 未执行任何交易。"
        )[:4096]

    def _send_telegram(self, text: str) -> None:
        if not self.settings.telegram_token or not self.settings.telegram_chat_id:
            raise RuntimeError("Telegram token/chat_id not configured for monitor suggestions.")
        query = urlencode(
            {
                "chat_id": self.settings.telegram_chat_id,
                "text": text,
            }
        ).encode("utf-8")
        url = f"https://api.telegram.org/bot{self.settings.telegram_token}/sendMessage"
        request = Request(  # noqa: S310 - Telegram API endpoint is fixed.
            url,
            data=query,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urlopen(request, timeout=20) as response:  # noqa: S310
            payload = response.read().decode("utf-8")
        data = json.loads(payload)
        if not data.get("ok"):
            raise RuntimeError(f"Telegram sendMessage failed: {data}")
