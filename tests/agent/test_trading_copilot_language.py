from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from agent_platform.agents.trading_copilot import TradingCopilot
from agent_platform.agents.verifier import RuleVerifier
from agent_platform.config import Settings
from agent_platform.plugins.agent_meta_plugin import AgentMetaPlugin
from agent_platform.registry.tool_registry import ToolRegistry
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
        telegram_token="",
        telegram_chat_id="",
        trading_agent_doc_path=tmp_path / "TRADING_AGENT.md",
        local_config={},
    )


def _copilot(tmp_path: Path) -> TradingCopilot:
    db = AgentDB(tmp_path / "test.sqlite")
    registry = ToolRegistry(db)
    AgentMetaPlugin().register(registry)
    return TradingCopilot(
        settings=_settings(tmp_path),
        registry=registry,
        db=db,
        llm=MagicMock(),
        verifier=RuleVerifier(),
    )


def test_system_prompt_is_language_adaptive(tmp_path: Path) -> None:
    prompt = _copilot(tmp_path)._system_prompt()

    assert "Reply in the same language as the user" in prompt
    assert "English for English" in prompt
    assert "Chinese for Chinese" in prompt


def test_capability_response_uses_english_for_english_question(tmp_path: Path) -> None:
    answer = _copilot(tmp_path).ask(
        question="What tools can you use?",
        source="cli",
        user_id="local",
        chat_id="local",
    )["answer"]

    assert "I am your local Freqtrade Trading Copilot" in answer
    assert "Main tools" in answer
    assert "不会做" not in answer


def test_capability_response_uses_chinese_for_chinese_question(tmp_path: Path) -> None:
    answer = _copilot(tmp_path).ask(
        question="你现在能用什么工具？",
        source="cli",
        user_id="local",
        chat_id="local",
    )["answer"]

    assert "我是你的本地 Freqtrade Trading Copilot" in answer
    assert "主要工具" in answer
