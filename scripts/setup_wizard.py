#!/usr/bin/env python3
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
USER_DATA = ROOT / "user_data"
CONFIG_EXAMPLE = USER_DATA / "config.example.json"
CONFIG_FILE = USER_DATA / "config.json"
ENV_EXAMPLE = USER_DATA / "agent_llm.env.example"
ENV_FILE = USER_DATA / "agent_llm.env"


TEXT = {
    "en": {
        "title": "Freqtrade Agent setup wizard",
        "lang": "Language / 语言: [1] English  [2] 中文",
        "existing": "{path} already exists.",
        "update": "Update this file? [y/N]: ",
        "created": "Created {path}",
        "updated": "Updated {path}",
        "kept": "Kept existing {path}",
        "api_url": "Freqtrade API base URL",
        "api_user": "Freqtrade API username",
        "api_password": "Freqtrade API password",
        "dry_run": "Keep Freqtrade dry_run enabled? [Y/n]: ",
        "dry_warning": (
            "Warning: dry_run=false is only for users who understand live trading risk. "
            "This Agent will still keep trading-control tools conservative/read-only."
        ),
        "llm_url": "OpenAI-compatible LLM base URL",
        "llm_model": "LLM model name",
        "llm_key": "LLM API key (blank is allowed)",
        "tavily": "Tavily API key for web tools (blank disables web search/fetch)",
        "tg_token": "Telegram bot token (blank disables Agent Telegram sending)",
        "tg_chat": "Telegram chat_id (blank disables Agent Telegram sending)",
        "done": "Setup complete.",
        "next": "Next steps",
        "docker": "Docker",
        "local": "Local Python",
        "smoke": "Smoke tests",
    },
    "zh": {
        "title": "Freqtrade Agent 使用引导程序",
        "lang": "Language / 语言: [1] English  [2] 中文",
        "existing": "{path} 已存在。",
        "update": "要更新这个文件吗？[y/N]: ",
        "created": "已创建 {path}",
        "updated": "已更新 {path}",
        "kept": "已保留现有 {path}",
        "api_url": "Freqtrade API 地址",
        "api_user": "Freqtrade API 用户名",
        "api_password": "Freqtrade API 密码",
        "dry_run": "是否保持 Freqtrade dry_run 启用？[Y/n]: ",
        "dry_warning": (
            "警告：dry_run=false 只适合已经理解实盘风险的用户。"
            "本 Agent 仍会保持交易控制工具为保守/只读边界。"
        ),
        "llm_url": "OpenAI-compatible LLM base URL",
        "llm_model": "LLM 模型名称",
        "llm_key": "LLM API key（允许留空）",
        "tavily": "Tavily API key，用于 web 工具（留空则禁用 web search/fetch）",
        "tg_token": "Telegram bot token（留空则禁用 Agent Telegram 发送）",
        "tg_chat": "Telegram chat_id（留空则禁用 Agent Telegram 发送）",
        "done": "配置完成。",
        "next": "下一步",
        "docker": "Docker",
        "local": "本地 Python",
        "smoke": "测试命令",
    },
}


def choose_language() -> str:
    raw = input(f"{TEXT['en']['lang']}\n> ").strip()
    return "zh" if raw in {"2", "zh", "cn", "中文"} else "en"


def prompt_value(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default


def prompt_yes_no(prompt: str, *, default: bool) -> bool:
    value = input(prompt).strip().lower()
    if not value:
        return default
    return value in {"y", "yes", "1", "true", "是", "好"}


def load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    return data if isinstance(data, dict) else {}


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def parse_env(path: Path) -> tuple[list[str], dict[str, str]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return [], {}
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return lines, values


def write_env(path: Path, template_lines: list[str], values: dict[str, str]) -> None:
    seen: set[str] = set()
    out: list[str] = []
    for line in template_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, _old = stripped.split("=", 1)
            key = key.strip()
            if key in values:
                out.append(f"{key}={values[key]}")
                seen.add(key)
                continue
        out.append(line)
    for key, value in values.items():
        if key not in seen:
            out.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


def should_update(path: Path, lang: str) -> bool:
    if not path.exists():
        return True
    print(TEXT[lang]["existing"].format(path=path.relative_to(ROOT)))
    return prompt_yes_no(TEXT[lang]["update"], default=False)


def ensure_from_example(target: Path, example: Path) -> None:
    if not target.exists() and example.exists():
        shutil.copyfile(example, target)


def configure_json(lang: str) -> None:
    t = TEXT[lang]
    existed = CONFIG_FILE.exists()
    if not should_update(CONFIG_FILE, lang):
        print(t["kept"].format(path=CONFIG_FILE.relative_to(ROOT)))
        return
    ensure_from_example(CONFIG_FILE, CONFIG_EXAMPLE)
    config = load_json(CONFIG_FILE) or load_json(CONFIG_EXAMPLE)
    api_server = config.setdefault("api_server", {})
    telegram = config.setdefault("telegram", {})

    api_server["enabled"] = True
    api_server["listen_ip_address"] = str(api_server.get("listen_ip_address") or "0.0.0.0")
    api_server["listen_port"] = int(api_server.get("listen_port") or 8080)
    api_server["username"] = prompt_value(t["api_user"], str(api_server.get("username") or "freqtrade"))
    api_server["password"] = prompt_value(t["api_password"], str(api_server.get("password") or "change-me"))

    dry_run = prompt_yes_no(t["dry_run"], default=True)
    config["dry_run"] = dry_run
    if not dry_run:
        print(t["dry_warning"])

    telegram["enabled"] = bool(telegram.get("enabled", False))
    telegram["token"] = prompt_value(t["tg_token"], str(telegram.get("token") or ""))
    telegram["chat_id"] = prompt_value(t["tg_chat"], str(telegram.get("chat_id") or ""))
    if telegram["token"] and telegram["chat_id"]:
        telegram["enabled"] = True

    write_json(CONFIG_FILE, config)
    verb = "updated" if existed else "created"
    print(t[verb].format(path=CONFIG_FILE.relative_to(ROOT)))


def configure_env(lang: str) -> None:
    t = TEXT[lang]
    existed = ENV_FILE.exists()
    if not should_update(ENV_FILE, lang):
        print(t["kept"].format(path=ENV_FILE.relative_to(ROOT)))
        return
    ensure_from_example(ENV_FILE, ENV_EXAMPLE)
    template_lines, values = parse_env(ENV_FILE)
    if not template_lines:
        template_lines, values = parse_env(ENV_EXAMPLE)

    values["FREQTRADE_API_BASE_URL"] = prompt_value(
        t["api_url"],
        values.get("FREQTRADE_API_BASE_URL", "http://127.0.0.1:8080"),
    )
    values["LLM_BASE_URL"] = prompt_value(
        t["llm_url"],
        values.get("LLM_BASE_URL", "http://127.0.0.1:1234/v1"),
    )
    values["LLM_MODEL"] = prompt_value(t["llm_model"], values.get("LLM_MODEL", "local-model"))
    values["LLM_API_KEY"] = prompt_value(t["llm_key"], values.get("LLM_API_KEY", ""))
    values["TAVILY_API_KEY"] = prompt_value(t["tavily"], values.get("TAVILY_API_KEY", ""))
    values.setdefault("LLM_TIMEOUT_SECONDS", "60")
    values.setdefault("TAVILY_BASE_URL", "https://api.tavily.com")
    values.setdefault("TAVILY_MAX_RESULTS", "5")
    values.setdefault("AGENT_MAX_STEPS", "12")
    values.setdefault("AGENT_MONITOR_INTERVAL_SECONDS", "60")

    write_env(ENV_FILE, template_lines, values)
    verb = "updated" if existed else "created"
    print(t[verb].format(path=ENV_FILE.relative_to(ROOT)))


def print_next_steps(lang: str) -> None:
    t = TEXT[lang]
    print(f"\n{t['done']}\n")
    print(f"{t['next']}:\n")
    print(f"{t['docker']}:")
    print("  docker compose -f docker-compose.agent.yml up -d --build")
    print(f"\n{t['local']}:")
    print('  python -m pip install -e ".[dev]"')
    print("  python tools/freqtrade_agent_server.py")
    print(f"\n{t['smoke']}:")
    print("  curl http://127.0.0.1:8090/health")
    print("  curl http://127.0.0.1:8090/agent/tools")
    print(
        "  curl -X POST http://127.0.0.1:8090/agent/ask "
        "-H 'Content-Type: application/json' "
        "-d '{\"question\":\"What tools can you use?\",\"source\":\"cli\","
        "\"user_id\":\"local\",\"chat_id\":\"local\"}'"
    )


def main() -> int:
    lang = choose_language()
    print(f"\n{TEXT[lang]['title']}\n")
    USER_DATA.mkdir(parents=True, exist_ok=True)
    configure_json(lang)
    configure_env(lang)
    print_next_steps(lang)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
