#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Any


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse

from agent_platform.agents.trading_copilot import TradingCopilot
from agent_platform.agents.verifier import RuleVerifier
from agent_platform.config import Settings, load_settings
from agent_platform.llm_client import OpenAICompatibleClient
from agent_platform.monitor import MonitorService
from agent_platform.plugins.agent_meta_plugin import AgentMetaPlugin
from agent_platform.plugins.chart_plugin import ChartPlugin
from agent_platform.plugins.freqtrade_plugin import FreqtradePlugin
from agent_platform.plugins.logs_plugin import LogsPlugin
from agent_platform.plugins.market_plugin import MarketPlugin
from agent_platform.plugins.memory_plugin import MemoryPlugin
from agent_platform.plugins.monitor_plugin import MonitorPlugin
from agent_platform.plugins.scheduler_plugin import SchedulerPlugin
from agent_platform.plugins.telegram_dashboard_plugin import TelegramDashboardPlugin
from agent_platform.plugins.web_plugin import WebPlugin
from agent_platform.registry.permissions import PermissionAction
from agent_platform.registry.tool_registry import ToolRegistry
from agent_platform.runtime_utils import SlidingWindowRateLimiter, log_event
from agent_platform.scheduler import SchedulerService
from agent_platform.schemas.tool_outputs import AskRequest, AskResumeRequest
from agent_platform.storage.db import AgentDB


logger = logging.getLogger(__name__)
ASK_RATE_LIMIT = 12
ASK_RATE_WINDOW_SECONDS = 60.0
OPTIONAL_JSON_BODY = Body(default=None)
SEARCH_MEMORY_QUERY = Query("", description="Search query")
SEARCH_MEMORY_TYPES = Query(default=None)
SEARCH_MEMORY_LIMIT = Query(8, ge=1, le=20)
BEHAVIOR_MEMORY_QUERY = Query("", description="Search query")
BEHAVIOR_MEMORY_LIMIT = Query(20, ge=1, le=100)
FORGET_MEMORY_TYPE = Query("composite")
rate_limiter = SlidingWindowRateLimiter(
    limit=ASK_RATE_LIMIT,
    window_seconds=ASK_RATE_WINDOW_SECONDS,
)


class AppState:
    def __init__(self) -> None:
        self.settings: Settings = load_settings()
        self.db = AgentDB(self.settings.memory_db_path)
        self.freqtrade_plugin = FreqtradePlugin(self.settings)
        permission_overrides = {
            name: PermissionAction(action)
            for name, action in self.settings.permission_overrides.items()
        }
        self.registry = ToolRegistry(
            self.db,
            dry_run_guard=self.freqtrade_plugin.is_dry_run,
            permission_overrides=permission_overrides,
        )
        self.freqtrade_plugin.register(self.registry)
        LogsPlugin(self.freqtrade_plugin.client).register(self.registry)
        MarketPlugin().register(self.registry)
        MemoryPlugin(self.db).register(self.registry)
        WebPlugin(self.settings).register(self.registry)
        MonitorPlugin(self.db).register(self.registry)
        SchedulerPlugin(self.db).register(self.registry)
        ChartPlugin(settings=self.settings, registry=self.registry).register(self.registry)
        TelegramDashboardPlugin(
            settings=self.settings,
            db=self.db,
            registry=self.registry,
        ).register(self.registry)
        AgentMetaPlugin().register(self.registry)
        self.llm = OpenAICompatibleClient(self.settings)
        self.copilot = TradingCopilot(
            settings=self.settings,
            registry=self.registry,
            db=self.db,
            llm=self.llm,
            verifier=RuleVerifier(),
        )
        self.monitor = MonitorService(
            settings=self.settings,
            db=self.db,
            ask_callback=self.copilot.ask,
        )
        self.scheduler = SchedulerService(
            settings=self.settings,
            db=self.db,
            ask_callback=self.copilot.ask,
            after_change_callback=self.refresh_telegram_dashboard,
        )

    def start_background(self) -> None:
        expired = self.db.expire_permission_requests()
        if expired:
            log_event(logger, "permissions_expired", count=expired, source="startup")
        self.monitor.start()
        self.scheduler.start()
        logger.info("background services started (monitor + scheduler)")

    def stop_background(self) -> None:
        self.monitor.stop()
        self.scheduler.stop()
        logger.info("background services stopped")

    def refresh_telegram_dashboard(self, trigger: str) -> dict[str, Any] | None:
        chat_id = self.settings.telegram_chat_id
        if not chat_id or not self.db.get_telegram_dashboard(chat_id):
            return None
        result = self.registry.execute(
            "telegram_dashboard_pin",
            {"chat_id": chat_id},
            force=True,
        )
        log_event(
            logger,
            "telegram_dashboard_auto_refresh",
            trigger=trigger,
            success=bool(result.get("success")),
            tool_name="telegram_dashboard_pin",
            latency_ms=result.get("latency_ms"),
            error=result.get("error"),
        )
        return result


state: AppState | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global state
    state = AppState()
    state.start_background()
    yield
    state.stop_background()
    state = None


app = FastAPI(
    title="Local Freqtrade Trading Copilot Agent",
    description="Controlled local Trading Copilot with Tool Registry and verifier.",
    version="0.3.0",
    lifespan=lifespan,
)


def _require_state() -> AppState:
    if state is None:
        raise RuntimeError("AppState not initialized")
    return state


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"


def _rate_limit_key(request: AskRequest) -> str:
    return f"{request.source}:{request.user_id}:{request.chat_id}"


def _enforce_ask_rate_limit(request: AskRequest) -> None:
    allowed, retry_after = rate_limiter.allow(_rate_limit_key(request))
    if allowed:
        return
    log_event(
        logger,
        "rate_limited",
        source=request.source,
        user_id=request.user_id,
        chat_id=request.chat_id,
        retry_after_seconds=round(retry_after, 1),
    )
    raise HTTPException(
        status_code=429,
        detail={
            "error": "rate_limited",
            "message": "请求太频繁, 请稍后再试。",
            "retry_after_seconds": round(retry_after, 1),
        },
    )


@app.get("/health")
def health() -> dict[str, Any]:
    s = _require_state()
    ping = s.registry.execute("ft_ping", {})
    dry_run = s.freqtrade_plugin.is_dry_run()
    return {
        "status": "ok" if ping.get("success") else "degraded",
        "agent": "ok",
        "bind": f"{s.settings.agent_host}:{s.settings.agent_port}",
        "public_url": s.settings.agent_public_url,
        "dry_run": dry_run,
        "freqtrade": {
            "ok": bool(ping.get("success")),
            "base_url": s.settings.freqtrade_api_base_url,
            "ping": ping.get("data") or ping.get("summary"),
        },
        "llm": {
            "base_url": s.settings.llm_base_url,
            "model": s.settings.llm_model,
            "api_key_configured": bool(s.settings.llm_api_key),
            "env_file": str(s.settings.llm_env_file_path),
            "env_file_exists": s.settings.llm_env_file_path.exists(),
            "max_steps": s.settings.agent_max_steps,
        },
        "web": {
            "tavily_configured": bool(s.settings.tavily_api_key),
            "tavily_base_url": s.settings.tavily_base_url,
            "tavily_max_results": s.settings.tavily_max_results,
        },
        "permissions": {
            "overrides": s.settings.permission_overrides,
        },
        "scheduler": {
            "jobs": len(s.db.scheduled_jobs()),
            "recent_runs": len(s.db.scheduled_job_runs(limit=100)),
        },
        "memory_db_path": str(s.settings.memory_db_path),
    }


@app.get("/agent/tools")
def list_tools() -> dict[str, Any]:
    return {"tools": _require_state().registry.list_tools()}


@app.get("/agent/memory/recent")
def recent_memory(
    source: str | None = None,
    user_id: str | None = None,
    chat_id: str | None = None,
) -> dict[str, Any]:
    return _require_state().db.recent_memory(
        source=source,
        user_id=user_id,
        chat_id=chat_id,
    )


@app.get("/agent/memory/search")
def search_memory(
    q: str = SEARCH_MEMORY_QUERY,
    memory_type: list[str] | None = SEARCH_MEMORY_TYPES,
    limit: int = SEARCH_MEMORY_LIMIT,
) -> dict[str, Any]:
    hits = _require_state().db.search_memory(q, memory_types=memory_type, limit=limit)
    return {"success": True, "memories": hits}


@app.get("/agent/memory/behavior")
def behavior_memory(
    q: str = BEHAVIOR_MEMORY_QUERY,
    limit: int = BEHAVIOR_MEMORY_LIMIT,
) -> dict[str, Any]:
    records = _require_state().db.behavior_records(q, limit=limit)
    return {"success": True, "behavior_records": records}


@app.post("/agent/memory/compact")
def compact_memory(payload: dict[str, Any] | None = OPTIONAL_JSON_BODY) -> dict[str, Any]:
    s = _require_state()
    args = payload or {}
    return s.registry.execute("memory_compact_now", args)


@app.post("/agent/memory/forget/{memory_id}")
def forget_memory(
    memory_id: int,
    memory_type: str = FORGET_MEMORY_TYPE,
) -> dict[str, Any]:
    s = _require_state()
    return s.registry.execute(
        "memory_forget",
        {"memory_id": memory_id, "memory_type": memory_type},
    )


@app.get("/agent/runs/{run_id}")
def get_run(run_id: int) -> dict[str, Any]:
    result = _require_state().db.get_run(run_id)
    if not result:
        return {"success": False, "error": f"run #{run_id} not found"}
    return {"success": True, **result}


@app.get("/agent/permissions/pending")
def pending_permissions() -> dict[str, Any]:
    s = _require_state()
    expired = s.db.expire_permission_requests()
    if expired:
        log_event(logger, "permissions_expired", count=expired, source="pending_endpoint")
    return {"permission_requests": s.db.pending_permission_requests()}


@app.post("/agent/permissions/{request_id}/confirm")
def confirm_permission(request_id: int) -> dict[str, Any]:
    s = _require_state()
    result = s.registry.confirm_permission(request_id)
    request = result.get("permission_request")
    _attach_dashboard_refresh(
        s,
        result,
        request,
        trigger=f"permission_request #{request_id} confirmed",
    )
    if (
        isinstance(request, dict)
        and request.get("tool_name") == "monitor_run_once"
        and result.get("success")
    ):
        args = request.get("args_json_sanitized")
        try:
            import json

            parsed = json.loads(str(args or "{}"))
        except json.JSONDecodeError:
            parsed = {}
        rule = s.db.get_monitor_rule(int(parsed.get("rule_id") or 0))
        if rule:
            monitor_response = s.monitor.run_rule(
                rule,
                trigger_reason=f"permission_request #{request_id} confirmed",
            )
            result["monitor_response"] = monitor_response
            result["permission_request"] = _update_permission_resume_summary(
                s,
                request_id,
                result["permission_request"],
                monitor_response,
            )
            _attach_dashboard_refresh(
                s,
                result,
                request,
                trigger=f"monitor_run_once permission_request #{request_id}",
            )
    if (
        isinstance(request, dict)
        and request.get("tool_name") == "scheduler_run_once"
        and result.get("success")
    ):
        args = request.get("args_json_sanitized")
        try:
            import json

            parsed = json.loads(str(args or "{}")) if isinstance(args, str) else args or {}
        except json.JSONDecodeError:
            parsed = {}
        job_id = int(parsed.get("job_id") or 0) if isinstance(parsed, dict) else 0
        if job_id:
            scheduler_response = s.scheduler.run_job(
                job_id,
                trigger_reason=f"permission_request #{request_id} confirmed",
            )
            result["scheduler_response"] = scheduler_response
            result["permission_request"] = _update_permission_resume_summary(
                s,
                request_id,
                result["permission_request"],
                scheduler_response,
            )
    return result


def _attach_dashboard_refresh(
    s: AppState,
    result: dict[str, Any],
    request: Any,
    *,
    trigger: str,
) -> None:
    if not result.get("success") or not isinstance(request, dict):
        return
    tool_name = str(request.get("tool_name") or "")
    if tool_name not in {
        "monitor_set",
        "monitor_pause",
        "monitor_resume",
        "scheduler_enable",
        "scheduler_disable",
    }:
        return
    refresh = s.refresh_telegram_dashboard(trigger)
    if refresh:
        result["dashboard_refresh"] = refresh


def _update_permission_resume_summary(
    s: AppState,
    request_id: int,
    permission_request: Any,
    response: dict[str, Any],
) -> dict[str, Any] | Any:
    if not isinstance(permission_request, dict):
        return permission_request
    answer = ""
    data = response.get("data")
    if isinstance(data, dict):
        answer = str(data.get("answer") or "")
    summary = "\n".join(
        item
        for item in [
            str(response.get("summary") or ""),
            answer[:1200],
        ]
        if item
    )
    if not summary:
        return permission_request
    return s.db.update_permission_request(
        request_id,
        status="confirmed",
        executed=bool(response.get("success")),
        result_summary=summary[:1600],
    ) or permission_request


@app.get("/agent/monitors")
def monitors() -> dict[str, Any]:
    return {"rules": _require_state().db.monitor_rules()}


@app.get("/agent/scheduler/jobs")
def scheduler_jobs() -> dict[str, Any]:
    return {
        "jobs": _require_state().db.scheduled_jobs(),
        "recent_runs": _require_state().db.scheduled_job_runs(limit=10),
    }


@app.post("/agent/scheduler/jobs/{job_id}/run")
def scheduler_run(job_id: int) -> dict[str, Any]:
    return _require_state().scheduler.run_job(job_id, trigger_reason="api manual trigger")


@app.post("/agent/scheduler/jobs/{job_id}/enable")
def scheduler_enable(job_id: int) -> dict[str, Any]:
    s = _require_state()
    job = s.db.set_scheduled_job_enabled(job_id, True)
    result: dict[str, Any] = {"success": bool(job), "job": job}
    refresh = s.refresh_telegram_dashboard(f"api scheduler_enable #{job_id}") if job else None
    if refresh:
        result["dashboard_refresh"] = refresh
    return result


@app.post("/agent/scheduler/jobs/{job_id}/disable")
def scheduler_disable(job_id: int) -> dict[str, Any]:
    s = _require_state()
    job = s.db.set_scheduled_job_enabled(job_id, False)
    result: dict[str, Any] = {"success": bool(job), "job": job}
    refresh = s.refresh_telegram_dashboard(f"api scheduler_disable #{job_id}") if job else None
    if refresh:
        result["dashboard_refresh"] = refresh
    return result


@app.post("/agent/ask")
async def ask(request: AskRequest) -> dict[str, Any]:
    s = _require_state()
    _enforce_ask_rate_limit(request)
    started = time.perf_counter()
    log_event(
        logger,
        "agent_ask_start",
        source=request.source,
        user_id=request.user_id,
        chat_id=request.chat_id,
    )
    loop = asyncio.get_running_loop()
    fn = partial(
        s.copilot.ask,
        question=request.question,
        source=request.source,
        user_id=request.user_id,
        chat_id=request.chat_id,
    )
    try:
        result = await loop.run_in_executor(None, fn)
    except Exception as exc:
        log_event(
            logger,
            "agent_ask_error",
            source=request.source,
            user_id=request.user_id,
            chat_id=request.chat_id,
            error=str(exc),
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
        )
        raise
    log_event(
        logger,
        "agent_ask_complete",
        run_id=result.get("run_id"),
        source=request.source,
        user_id=request.user_id,
        chat_id=request.chat_id,
        used_llm=result.get("used_llm"),
        fallback_used=result.get("fallback_used"),
        llm_error=result.get("llm_error"),
        latency_ms=round((time.perf_counter() - started) * 1000, 3),
    )
    return result


@app.post("/agent/ask/stream")
def ask_stream(request: AskRequest) -> StreamingResponse:
    s = _require_state()
    _enforce_ask_rate_limit(request)

    def events():
        started = time.perf_counter()
        run_id = None
        log_event(
            logger,
            "agent_stream_start",
            source=request.source,
            user_id=request.user_id,
            chat_id=request.chat_id,
        )
        try:
            for event in s.copilot.ask_stream(
                question=request.question,
                source=request.source,
                user_id=request.user_id,
                chat_id=request.chat_id,
            ):
                if event.get("run_id"):
                    run_id = event.get("run_id")
                if event.get("type") == "complete":
                    data = event.get("data")
                    log_event(
                        logger,
                        "agent_stream_complete",
                        run_id=run_id,
                        source=request.source,
                        user_id=request.user_id,
                        chat_id=request.chat_id,
                        used_llm=data.get("used_llm") if isinstance(data, dict) else None,
                        fallback_used=(
                            data.get("fallback_used") if isinstance(data, dict) else None
                        ),
                        llm_error=data.get("llm_error") if isinstance(data, dict) else None,
                        latency_ms=round((time.perf_counter() - started) * 1000, 3),
                    )
                yield _sse(event)
        except Exception as exc:
            log_event(
                logger,
                "agent_stream_error",
                run_id=run_id,
                source=request.source,
                user_id=request.user_id,
                chat_id=request.chat_id,
                error=str(exc),
                latency_ms=round((time.perf_counter() - started) * 1000, 3),
            )
            logger.exception("agent stream failed")
            yield _sse({"type": "error", "error": str(exc)})

    return StreamingResponse(events(), media_type="text/event-stream")


@app.post("/agent/ask/resume")
def ask_resume(request: AskResumeRequest) -> StreamingResponse:
    s = _require_state()
    run = s.db.get_run(request.run_id)
    run_meta = run.get("run") if isinstance(run, dict) else None

    def events():
        started = time.perf_counter()
        log_event(
            logger,
            "agent_resume_start",
            run_id=request.run_id,
            source=request.source,
            user_id=request.user_id,
            chat_id=request.chat_id,
        )
        if not isinstance(run_meta, dict):
            yield _sse(
                {
                    "type": "error",
                    "run_id": request.run_id,
                    "error": f"run #{request.run_id} not found",
                }
            )
            return
        question = request.question or str(run_meta.get("question") or "")
        source = request.source or str(run_meta.get("source") or "api")
        user_id = request.user_id or str(run_meta.get("user_id") or "local")
        chat_id = request.chat_id or str(run_meta.get("chat_id") or "local")
        try:
            for event in s.copilot.resume(
                run_id=request.run_id,
                question=question,
                source=source,
                user_id=user_id,
                chat_id=chat_id,
            ):
                if event.get("type") == "complete":
                    data = event.get("data")
                    log_event(
                        logger,
                        "agent_resume_complete",
                        run_id=request.run_id,
                        source=source,
                        user_id=user_id,
                        chat_id=chat_id,
                        used_llm=data.get("used_llm") if isinstance(data, dict) else None,
                        fallback_used=(
                            data.get("fallback_used") if isinstance(data, dict) else None
                        ),
                        llm_error=data.get("llm_error") if isinstance(data, dict) else None,
                        latency_ms=round((time.perf_counter() - started) * 1000, 3),
                    )
                yield _sse(event)
        except Exception as exc:
            log_event(
                logger,
                "agent_resume_error",
                run_id=request.run_id,
                source=source,
                user_id=user_id,
                chat_id=chat_id,
                error=str(exc),
                latency_ms=round((time.perf_counter() - started) * 1000, 3),
            )
            logger.exception("agent resume stream failed")
            yield _sse({"type": "error", "run_id": request.run_id, "error": str(exc)})

    return StreamingResponse(events(), media_type="text/event-stream")


@app.get("/agent/status")
def status() -> dict[str, Any]:
    result = _require_state().registry.execute("ft_status", {})
    return {"summary": result.get("summary"), "data": result.get("data"), "result": result}


@app.get("/agent/balance")
def balance() -> dict[str, Any]:
    result = _require_state().registry.execute("ft_balance", {})
    return {"summary": result.get("summary"), "data": result.get("data"), "result": result}


@app.get("/agent/profit")
def profit() -> dict[str, Any]:
    result = _require_state().registry.execute("ft_profit", {})
    return {"summary": result.get("summary"), "data": result.get("data"), "result": result}


@app.get("/agent/config-summary")
def config_summary() -> dict[str, Any]:
    result = _require_state().registry.execute("ft_show_config_sanitized", {})
    return {"summary": result.get("summary"), "config": result.get("data"), "result": result}


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    s = load_settings()
    uvicorn.run(app, host=s.agent_host, port=s.agent_port)


if __name__ == "__main__":
    run()
