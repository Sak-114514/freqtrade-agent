from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType


def _load_wizard() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts" / "setup_wizard.py"
    spec = importlib.util.spec_from_file_location("setup_wizard", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _prepare_wizard(monkeypatch, tmp_path: Path) -> ModuleType:
    wizard = _load_wizard()
    user_data = tmp_path / "user_data"
    user_data.mkdir()
    config_example = user_data / "config.example.json"
    env_example = user_data / "agent_llm.env.example"
    config_example.write_text(
        json.dumps(
            {
                "dry_run": True,
                "api_server": {
                    "enabled": True,
                    "listen_ip_address": "0.0.0.0",
                    "listen_port": 8080,
                    "username": "freqtrade",
                    "password": "change-me",
                },
                "telegram": {"enabled": False, "token": "", "chat_id": ""},
            }
        ),
        encoding="utf-8",
    )
    env_example.write_text(
        "LLM_BASE_URL=http://127.0.0.1:1234/v1\n"
        "LLM_MODEL=local-model\n"
        "LLM_API_KEY=\n"
        "TAVILY_API_KEY=\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(wizard, "ROOT", tmp_path)
    monkeypatch.setattr(wizard, "USER_DATA", user_data)
    monkeypatch.setattr(wizard, "CONFIG_EXAMPLE", config_example)
    monkeypatch.setattr(wizard, "CONFIG_FILE", user_data / "config.json")
    monkeypatch.setattr(wizard, "ENV_EXAMPLE", env_example)
    monkeypatch.setattr(wizard, "ENV_FILE", user_data / "agent_llm.env")
    return wizard


def test_setup_wizard_writes_dry_run_false_config(monkeypatch, tmp_path: Path) -> None:
    wizard = _prepare_wizard(monkeypatch, tmp_path)
    answers = iter(["api-user", "api-pass", "n", "telegram-token", "12345"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    wizard.configure_json("en")

    config = json.loads((tmp_path / "user_data" / "config.json").read_text(encoding="utf-8"))
    assert config["dry_run"] is False
    assert config["api_server"]["username"] == "api-user"
    assert config["api_server"]["password"] == "api-pass"
    assert config["telegram"]["enabled"] is True


def test_setup_wizard_writes_env_values(monkeypatch, tmp_path: Path) -> None:
    wizard = _prepare_wizard(monkeypatch, tmp_path)
    answers = iter([
        "http://127.0.0.1:8080",
        "http://llm.example/v1",
        "glm-test",
        "llm-key",
        "tavily-key",
    ])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    wizard.configure_env("zh")

    env = (tmp_path / "user_data" / "agent_llm.env").read_text(encoding="utf-8")
    assert "FREQTRADE_API_BASE_URL=http://127.0.0.1:8080" in env
    assert "LLM_BASE_URL=http://llm.example/v1" in env
    assert "LLM_MODEL=glm-test" in env
    assert "LLM_API_KEY=llm-key" in env
    assert "TAVILY_API_KEY=tavily-key" in env


def test_setup_wizard_keeps_existing_file_by_default(monkeypatch, tmp_path: Path) -> None:
    wizard = _prepare_wizard(monkeypatch, tmp_path)
    target = tmp_path / "user_data" / "config.json"
    target.write_text('{"dry_run": true, "sentinel": "keep"}\n', encoding="utf-8")
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")

    wizard.configure_json("en")

    config = json.loads(target.read_text(encoding="utf-8"))
    assert config["sentinel"] == "keep"
