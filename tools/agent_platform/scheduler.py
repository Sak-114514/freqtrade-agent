from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from agent_platform.config import Settings
from agent_platform.storage.db import AgentDB, utc_now_iso


logger = logging.getLogger(__name__)

AskCallback = Callable[..., dict[str, Any]]
AfterChangeCallback = Callable[[str], dict[str, Any] | None]


DEFAULT_SCHEDULED_JOBS: list[dict[str, Any]] = [
    {
        "name": "hourly_bot_health_check",
        "description": "每小时检查 Freqtrade health、open trades 和最近日志。",
        "cron": "interval:60m",
        "interval_minutes": 60,
        "report_path": "agent_reports/hourly_bot_health_check.md",
    },
    {
        "name": "daily_trading_report",
        "description": "每日汇总状态、持仓、收益、近期交易、日志和配置摘要。",
        "cron": "daily@07:00 Asia/Shanghai",
        "interval_minutes": 1440,
        "report_path": "agent_reports/daily_report_example.md",
    },
    {
        "name": "daily_profit_summary",
        "description": "每日生成 profit、drawdown、winrate 摘要。",
        "cron": "daily@07:10 Asia/Shanghai",
        "interval_minutes": 1440,
        "report_path": "agent_reports/daily_profit_summary.md",
    },
    {
        "name": "daily_log_error_scan",
        "description": "每日扫描最近日志错误和 warning。",
        "cron": "daily@07:20 Asia/Shanghai",
        "interval_minutes": 1440,
        "report_path": "agent_reports/daily_log_error_scan.md",
    },
    {
        "name": "daily_market_snapshot",
        "description": "每日生成市场/交易对信息摘要; 外部 web 搜索仍走 ask 权限。",
        "cron": "daily@07:30 Asia/Shanghai",
        "interval_minutes": 1440,
        "report_path": "agent_reports/daily_market_snapshot.md",
    },
    {
        "name": "scheduled_observation_save",
        "description": "定时保存一条运行观察到 SQLite observations。",
        "cron": "daily@07:40 Asia/Shanghai",
        "interval_minutes": 1440,
        "report_path": "agent_reports/scheduled_observation_save.md",
    },
]


class SchedulerService:
    def __init__(
        self,
        *,
        settings: Settings,
        db: AgentDB,
        ask_callback: AskCallback,
        after_change_callback: AfterChangeCallback | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.ask_callback = ask_callback
        self.after_change_callback = after_change_callback
        self.db.ensure_default_scheduled_jobs(DEFAULT_SCHEDULED_JOBS)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="TradingAgentScheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(60):
            try:
                self.run_due_jobs()
            except Exception:
                logger.exception("SchedulerService run failed")

    def run_due_jobs(self) -> list[dict[str, Any]]:
        results = []
        for job in self.db.scheduled_jobs(enabled_only=True):
            if self._is_due(job):
                results.append(self.run_job(int(job["id"]), trigger_reason="schedule due"))
        return results

    def run_job(self, job_id: int, *, trigger_reason: str = "manual") -> dict[str, Any]:
        job = self.db.get_scheduled_job(job_id)
        if not job:
            return {"success": False, "summary": f"scheduled job #{job_id} 不存在。"}

        started_at = utc_now_iso()
        error: str | None = None
        response: dict[str, Any]
        try:
            response = self.ask_callback(
                question=self._question_for_job(job, trigger_reason),
                source="scheduler",
                user_id="scheduler",
                chat_id=self.settings.telegram_chat_id or "scheduler",
            )
            answer = str(response.get("answer") or "")
            if job["name"] == "scheduled_observation_save":
                self.db.save_observation(
                    text=f"Scheduler observation: {answer[:500]}",
                    tags=["scheduler", str(job["name"])],
                    importance=2,
                )
            report_path = self._write_report(job, answer, response, trigger_reason)
            success = True
        except Exception as exc:
            answer = ""
            response = {"tool_calls": []}
            report_path = None
            success = False
            error = str(exc)

        finished_at = utc_now_iso()
        run_id = self.db.save_scheduled_job_run(
            job_id=int(job["id"]),
            job_name=str(job["name"]),
            result_summary=answer or error or "",
            report_path=report_path,
            success=success,
            error=error,
            started_at=started_at,
            finished_at=finished_at,
            tool_calls=response.get("tool_calls") if isinstance(response, dict) else [],
        )
        self.db.mark_scheduled_job_run(
            int(job["id"]),
            interval_minutes=int(job.get("interval_minutes") or 1440),
        )
        dashboard_refresh = self._refresh_dashboard(
            f"scheduled job #{job['id']} {job['name']} run"
        )
        return {
            "success": success,
            "summary": (
                f"根据工具结果: scheduled job #{job['id']} {job['name']} 已运行, "
                f"run #{run_id}。"
            )
            if success
            else f"scheduled job #{job_id} 运行失败: {error}",
            "data": {
                "job": job,
                "run_id": run_id,
                "report_path": report_path,
                "answer": answer,
                "dashboard_refresh": dashboard_refresh,
            },
        }

    def _refresh_dashboard(self, trigger: str) -> dict[str, Any] | None:
        if not self.after_change_callback:
            return None
        try:
            return self.after_change_callback(trigger)
        except Exception:
            logger.exception("Scheduler dashboard refresh failed")
            return {"success": False, "summary": "dashboard refresh failed"}

    def _is_due(self, job: dict[str, Any]) -> bool:
        next_run_at = job.get("next_run_at")
        if not next_run_at:
            return False
        try:
            due_at = datetime.fromisoformat(str(next_run_at))
        except ValueError:
            return False
        if due_at.tzinfo is None:
            due_at = due_at.replace(tzinfo=UTC)
        return datetime.now(UTC) >= due_at

    def _question_for_job(self, job: dict[str, Any], trigger_reason: str) -> str:
        name = str(job["name"])
        prefix = (
            f"定时任务 {name}; 触发原因: {trigger_reason}。"
            "请只使用白名单只读工具, 输出结论、数据来源工具、关键指标、风险提示。"
            "必须说明未执行任何交易。"
        )
        prompts = {
            "hourly_bot_health_check": "检查 bot 是否健康、当前 open trades、最近有没有报错。",
            "daily_trading_report": (
                "生成今日交易日报: 状态、open trades、profit、recent trades、logs、"
                "config summary。"
            ),
            "daily_profit_summary": "生成每日收益摘要: profit、drawdown、winrate。",
            "daily_log_error_scan": "扫描最近日志中 ERROR/WARNING/异常。",
            "daily_market_snapshot": (
                "生成每日行情摘要。先查看 whitelist 和当前 bot 状态; "
                "如需外部新闻或行情 web_search, 只生成 permission_request, 不要自动联网。"
            ),
            "scheduled_observation_save": "保存观察: 定时记录当前 bot 状态、收益和日志摘要。",
        }
        return f"{prefix}\n{prompts.get(name, str(job.get('description') or ''))}"

    def _write_report(
        self,
        job: dict[str, Any],
        answer: str,
        response: dict[str, Any],
        trigger_reason: str,
    ) -> str | None:
        raw_path = job.get("report_path")
        if not raw_path:
            return None
        report_path = self.settings.user_data_dir / str(raw_path)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        tools = [
            str(item.get("tool_name"))
            for item in response.get("tool_calls", [])
            if isinstance(item, dict)
        ]
        content = (
            f"# {job['name']}\n\n"
            f"- created_at: {utc_now_iso()}\n"
            f"- trigger_reason: {trigger_reason}\n"
            f"- dry_run: true required\n"
            f"- tools: {', '.join(tools) if tools else 'none'}\n"
            "- trading_actions: none\n\n"
            "## Answer\n\n"
            f"{answer}\n"
        )
        report_path.write_text(content, encoding="utf-8")
        return str(report_path)
