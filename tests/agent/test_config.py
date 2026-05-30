from __future__ import annotations

import json
from pathlib import Path

from agent_platform.config import discover_user_data_dir, load_settings


def test_user_data_env_override(monkeypatch, tmp_path: Path) -> None:
    user_data = tmp_path / "external_user_data"
    user_data.mkdir()
    monkeypatch.setenv("FREQTRADE_AGENT_USER_DATA_DIR", str(user_data))

    assert discover_user_data_dir() == user_data.resolve()


def test_load_settings_from_standalone_user_data(monkeypatch, tmp_path: Path) -> None:
    user_data = tmp_path / "user_data"
    user_data.mkdir()
    (user_data / "config.json").write_text(
        json.dumps(
            {
                "dry_run": True,
                "api_server": {"username": "api-user", "password": "api-pass"},
                "telegram": {"token": "telegram-token", "chat_id": "123"},
            }
        ),
        encoding="utf-8",
    )
    (user_data / "agent_llm.env").write_text(
        "LLM_BASE_URL=http://llm.example/v1\nLLM_MODEL=test-model\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FREQTRADE_AGENT_USER_DATA_DIR", raising=False)
    monkeypatch.delenv("FREQTRADE_CONFIG_PATH", raising=False)
    monkeypatch.delenv("FREQTRADE_AGENT_MEMORY_DB", raising=False)
    monkeypatch.delenv("FREQTRADE_API_USER", raising=False)
    monkeypatch.delenv("FREQTRADE_API_PASSWORD", raising=False)

    settings = load_settings()

    assert settings.user_data_dir == user_data
    assert settings.config_path == user_data / "config.json"
    assert settings.memory_db_path == user_data / "agent_memory.sqlite"
    assert settings.freqtrade_api_user == "api-user"
    assert settings.freqtrade_api_password == "api-pass"
    assert settings.llm_base_url == "http://llm.example/v1"
    assert settings.llm_model == "test-model"
