from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from agent_platform.storage.db import AgentDB
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def db(tmp_path: Path) -> AgentDB:
    return AgentDB(tmp_path / "test_api.sqlite")


@pytest.fixture
def mock_state(db: AgentDB, tmp_path: Path):
    from agent_platform.agents.trading_copilot import TradingCopilot
    from agent_platform.agents.verifier import RuleVerifier
    from agent_platform.config import Settings
    from agent_platform.registry.tool_registry import ToolRegistry

    settings = MagicMock(spec=Settings)
    settings.agent_host = "127.0.0.1"
    settings.agent_port = 8090
    settings.agent_public_url = "http://127.0.0.1:8090"
    settings.llm_base_url = "http://localhost:1234/v1"
    settings.llm_model = "test"
    settings.llm_api_key = ""
    settings.llm_timeout_seconds = 30.0
    settings.llm_env_file_path = tmp_path / "env"
    settings.freqtrade_api_base_url = "http://localhost:8080"
    settings.freqtrade_api_user = "test"
    settings.freqtrade_api_password = "test"
    settings.tavily_api_key = ""
    settings.tavily_base_url = "https://api.tavily.com"
    settings.tavily_max_results = 5
    settings.agent_max_steps = 3
    settings.permission_overrides = {}
    settings.monitor_interval_seconds = 3600
    settings.telegram_token = ""
    settings.telegram_chat_id = ""
    settings.trading_agent_doc_path = tmp_path / "doc.md"
    settings.trading_agent_doc_path.write_text("# test doc\n")
    settings.memory_db_path = db.path
    settings.config_path = tmp_path / "config.json"
    settings.user_data_dir = tmp_path
    settings.local_config = {}

    registry = ToolRegistry(db)

    def ping_handler(_a: dict) -> dict:
        return {"success": True, "summary": "pong", "data": {"status": "pong"}}

    def dummy_handler(_a: dict) -> dict:
        return {"success": True, "summary": "dummy", "data": {}}

    from agent_platform.registry.permissions import PermissionAction, PermissionLevel
    from agent_platform.registry.tool_registry import ToolSpec, any_output_schema, object_schema

    registry.register(
        ToolSpec(
            name="ft_ping",
            description="ping",
            permission_level=PermissionLevel.L0,
            input_schema=object_schema(),
            output_schema=any_output_schema(),
            handler=ping_handler,
            requires_confirmation=False,
            risk_notes="test",
            permission_default=PermissionAction.ALLOW,
        )
    )
    for name in ["ft_profit", "ft_daily", "ft_stats", "ft_trades_recent", "ft_pair_candles"]:
        registry.register(
            ToolSpec(
                name=name,
                description=name,
                permission_level=PermissionLevel.L0,
                input_schema=object_schema(),
                output_schema=any_output_schema(),
                handler=dummy_handler,
                requires_confirmation=False,
                risk_notes="test",
                permission_default=PermissionAction.ALLOW,
            )
        )
    from agent_platform.plugins.chart_plugin import ChartPlugin

    ChartPlugin(settings=settings, registry=registry).register(registry)

    copilot = TradingCopilot(
        settings=settings, registry=registry, db=db,
        llm=MagicMock(), verifier=RuleVerifier(),
    )

    state = MagicMock()
    state.settings = settings
    state.db = db
    state.registry = registry
    state.copilot = copilot
    state.freqtrade_plugin = MagicMock()
    state.freqtrade_plugin.is_dry_run.return_value = True
    state.monitor = MagicMock()
    state.scheduler = MagicMock()
    return state


@pytest.fixture
def app_with_state(mock_state):
    import agent_platform.main as main_mod

    original = main_mod.state
    main_mod.state = mock_state
    from agent_platform.main import app
    yield app
    main_mod.state = original


@pytest.mark.asyncio
async def test_health_endpoint(app_with_state):
    transport = ASGITransport(app=app_with_state)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["dry_run"] is True
    assert data["freqtrade"]["ok"] is True


@pytest.mark.asyncio
async def test_tools_endpoint(app_with_state):
    transport = ASGITransport(app=app_with_state)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/agent/tools")
    assert resp.status_code == 200
    tools = resp.json()["tools"]
    names = [t["name"] for t in tools]
    assert "ft_ping" in names
    assert "chart_trade_overview_preview" in names
    assert "telegram_chart_send" in names


@pytest.mark.asyncio
async def test_memory_recent_endpoint(app_with_state):
    transport = ASGITransport(app=app_with_state)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/agent/memory/recent")
    assert resp.status_code == 200
    data = resp.json()
    assert "observations" in data
    assert "recent_conversations" in data
    assert "short_term_messages" in data


@pytest.mark.asyncio
async def test_memory_search_endpoint(app_with_state, mock_state):
    mock_state.db.save_composite_memory(
        memory_type="profile",
        text="用户偏好 dry-run 学习模式",
        tags=["preference"],
        importance=4,
    )
    transport = ASGITransport(app=app_with_state)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/agent/memory/search", params={"q": "dry-run 偏好"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert any(item["memory_type"] == "profile" for item in data["memories"])


@pytest.mark.asyncio
async def test_behavior_memory_endpoint(app_with_state, mock_state):
    mock_state.db.save_behavior_record(
        run_id=None,
        source="telegram",
        user_id="u1",
        chat_id="c1",
        trigger="刚才为什么卡住",
        tools_used=["web_search"],
        outcome="等待权限确认",
        facts={"answer": "需要确认 web_search"},
        tags=["permission"],
        importance=3,
    )
    transport = ASGITransport(app=app_with_state)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/agent/memory/behavior", params={"q": "卡住 权限"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["behavior_records"][0]["tools_used"] == ["web_search"]


@pytest.mark.asyncio
async def test_ask_endpoint_is_async(app_with_state, mock_state):
    import agent_platform.main as main_mod

    main_mod.rate_limiter.reset()
    mock_state.copilot.ask = MagicMock(return_value={
        "answer": "test answer",
        "plan": "test plan",
        "tool_calls": [],
        "pending_action": None,
        "permission_requests": [],
        "used_llm": False,
        "fallback_used": False,
        "steps": [],
        "llm_error": None,
        "memory_used": {},
    })
    transport = ASGITransport(app=app_with_state)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/agent/ask", json={
            "question": "hello",
            "source": "test",
            "user_id": "u1",
            "chat_id": "c1",
        })
    assert resp.status_code == 200
    assert resp.json()["answer"] == "test answer"
    main_mod.rate_limiter.reset()


@pytest.mark.asyncio
async def test_ask_endpoint_rate_limit(app_with_state, mock_state):
    import agent_platform.main as main_mod

    main_mod.rate_limiter.reset()
    mock_state.copilot.ask = MagicMock(return_value={
        "answer": "ok",
        "plan": "",
        "tool_calls": [],
        "pending_action": None,
        "permission_requests": [],
        "used_llm": False,
        "fallback_used": False,
        "steps": [],
        "llm_error": None,
        "memory_used": {},
    })
    transport = ASGITransport(app=app_with_state)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for _ in range(12):
            resp = await client.post("/agent/ask", json={
                "question": "hello",
                "source": "test-rate",
                "user_id": "u1",
                "chat_id": "c1",
            })
            assert resp.status_code == 200
        limited = await client.post("/agent/ask", json={
            "question": "hello",
            "source": "test-rate",
            "user_id": "u1",
            "chat_id": "c1",
        })
    assert limited.status_code == 429
    assert limited.json()["detail"]["error"] == "rate_limited"
    main_mod.rate_limiter.reset()


@pytest.mark.asyncio
async def test_confirm_permission_refreshes_dashboard_for_scheduler_change(
    app_with_state,
    mock_state,
):
    from agent_platform.registry.permissions import PermissionAction, PermissionLevel
    from agent_platform.registry.tool_registry import ToolSpec, any_output_schema, object_schema

    mock_state.registry.register(
        ToolSpec(
            name="scheduler_enable",
            description="enable",
            permission_level=PermissionLevel.L1,
            input_schema=object_schema(),
            output_schema=any_output_schema(),
            handler=lambda _args: {"success": True, "summary": "enabled"},
            requires_confirmation=False,
            risk_notes="test",
            permission_default=PermissionAction.ASK,
        )
    )
    mock_state.refresh_telegram_dashboard = MagicMock(
        return_value={"success": True, "summary": "dashboard refreshed"}
    )
    pending = mock_state.registry.execute("scheduler_enable", {}, run_id=1)

    transport = ASGITransport(app=app_with_state)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/agent/permissions/{pending['permission_request']['id']}/confirm"
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["dashboard_refresh"]["summary"] == "dashboard refreshed"
    mock_state.refresh_telegram_dashboard.assert_called_once()


@pytest.mark.asyncio
async def test_non_blocking_ask(app_with_state, mock_state):
    import time

    def slow_ask(**kwargs):
        time.sleep(0.5)
        return {"answer": "slow", "plan": "", "tool_calls": [], "used_llm": False,
                "fallback_used": False, "steps": [], "permission_requests": [],
                "pending_action": None, "llm_error": None, "memory_used": {}}

    mock_state.copilot.ask = slow_ask
    transport = ASGITransport(app=app_with_state)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        health_task = asyncio.create_task(client.get("/health"))
        ask_task = asyncio.create_task(client.post("/agent/ask", json={
            "question": "slow", "source": "test", "user_id": "u1", "chat_id": "c1",
        }))
        health_resp = await health_task
        ask_resp = await ask_task

    assert health_resp.status_code == 200
    assert ask_resp.status_code == 200
