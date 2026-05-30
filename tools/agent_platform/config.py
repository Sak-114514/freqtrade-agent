from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_AGENT_HOST = "127.0.0.1"
DEFAULT_AGENT_PORT = 8090
DEFAULT_FREQTRADE_API_BASE_URL = "http://127.0.0.1:8080"
DEFAULT_LLM_BASE_URL = "http://127.0.0.1:1234/v1"
DEFAULT_DOCKER_LLM_BASE_URL = "http://host.docker.internal:1234/v1"
DEFAULT_LLM_MODEL = "local-model"
DEFAULT_LLM_TIMEOUT_SECONDS = 30.0
DEFAULT_DOCKER_LLM_TIMEOUT_SECONDS = 30.0
DEFAULT_LLM_ENV_FILE = "agent_llm.env"
DEFAULT_TAVILY_BASE_URL = "https://api.tavily.com"
DEFAULT_TAVILY_MAX_RESULTS = 5
DEFAULT_AGENT_MAX_STEPS = 12
DEFAULT_MONITOR_INTERVAL_SECONDS = 60


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    agent_host: str
    agent_port: int
    agent_public_url: str
    freqtrade_api_base_url: str
    freqtrade_api_user: str
    freqtrade_api_password: str
    config_path: Path
    user_data_dir: Path
    memory_db_path: Path
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_timeout_seconds: float
    llm_env_file_path: Path
    tavily_api_key: str
    tavily_base_url: str
    tavily_max_results: int
    agent_max_steps: int
    permission_overrides: dict[str, str]
    monitor_interval_seconds: int
    telegram_token: str
    telegram_chat_id: str
    trading_agent_doc_path: Path
    local_config: dict[str, Any]


def discover_user_data_dir() -> Path:
    explicit = os.getenv("FREQTRADE_AGENT_USER_DATA_DIR") or os.getenv("USER_DATA_DIR")
    if explicit:
        return Path(explicit).expanduser().resolve()
    candidates = [
        Path.cwd() / "user_data",
        project_root() / "user_data",
        Path.cwd().parent / "user_data",
        Path("/freqtrade/user_data"),
        Path(__file__).resolve().parents[3] / "user_data",
    ]
    for candidate in candidates:
        if (candidate / "config.json").exists():
            return candidate
    return candidates[0]


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def default_llm_base_url() -> str:
    if Path("/.dockerenv").exists() or Path("/freqtrade/user_data").exists():
        return DEFAULT_DOCKER_LLM_BASE_URL
    return DEFAULT_LLM_BASE_URL


def default_llm_timeout_seconds() -> float:
    if Path("/.dockerenv").exists() or Path("/freqtrade/user_data").exists():
        return DEFAULT_DOCKER_LLM_TIMEOUT_SECONDS
    return DEFAULT_LLM_TIMEOUT_SECONDS


def resolve_llm_env_file(user_data_dir: Path) -> Path:
    raw_path = os.getenv("FREQTRADE_AGENT_LLM_ENV_FILE", DEFAULT_LLM_ENV_FILE)
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = user_data_dir / path
    return path


def resolve_config_path(user_data_dir: Path) -> Path:
    raw_path = os.getenv("FREQTRADE_CONFIG_PATH")
    if not raw_path:
        return user_data_dir / "config.json"
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = user_data_dir / path
    return path


def resolve_memory_db_path(user_data_dir: Path) -> Path:
    raw_path = os.getenv("FREQTRADE_AGENT_MEMORY_DB")
    if not raw_path:
        return user_data_dir / "agent_memory.sqlite"
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = user_data_dir / path
    return path


def unquote_env_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def load_env_file(path: Path) -> dict[str, str]:
    env_values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return env_values

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        env_values[key] = unquote_env_value(value)
    return env_values


def setting_value(names: tuple[str, ...], file_env: dict[str, str], default: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    for name in names:
        value = file_env.get(name)
        if value:
            return value
    return default


def float_setting_value(names: tuple[str, ...], file_env: dict[str, str], default: float) -> float:
    raw_value = setting_value(names, file_env, str(default))
    try:
        return float(raw_value)
    except ValueError:
        return default


def int_setting_value(names: tuple[str, ...], file_env: dict[str, str], default: int) -> int:
    raw_value = setting_value(names, file_env, str(default))
    try:
        return int(raw_value)
    except ValueError:
        return default


def parse_permission_overrides(raw: str) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for item in raw.split(","):
        if not item.strip() or ":" not in item:
            continue
        name, action = item.split(":", 1)
        action = action.strip().lower()
        if action in {"allow", "ask", "deny"}:
            overrides[name.strip()] = action
    return overrides


def load_settings() -> Settings:
    user_data_dir = discover_user_data_dir()
    config_path = resolve_config_path(user_data_dir)
    llm_env_file_path = resolve_llm_env_file(user_data_dir)
    llm_file_env = load_env_file(llm_env_file_path)
    local_config = load_json(config_path)
    api_server = local_config.get("api_server") or {}
    telegram_config = local_config.get("telegram") or {}

    agent_host = os.getenv("FREQTRADE_AGENT_HOST", DEFAULT_AGENT_HOST)
    agent_port = int(os.getenv("FREQTRADE_AGENT_PORT", str(DEFAULT_AGENT_PORT)))
    default_public_url = (
        f"http://127.0.0.1:{agent_port}"
        if agent_host == "0.0.0.0"  # noqa: S104 - container bind is host-mapped to 127.0.0.1.
        else f"http://{agent_host}:{agent_port}"
    )

    return Settings(
        agent_host=agent_host,
        agent_port=agent_port,
        agent_public_url=os.getenv("FREQTRADE_AGENT_PUBLIC_URL", default_public_url),
        freqtrade_api_base_url=os.getenv(
            "FREQTRADE_API_BASE_URL",
            DEFAULT_FREQTRADE_API_BASE_URL,
        ).rstrip("/"),
        freqtrade_api_user=str(os.getenv("FREQTRADE_API_USER") or api_server.get("username", "")),
        freqtrade_api_password=str(
            os.getenv("FREQTRADE_API_PASSWORD") or api_server.get("password", "")
        ),
        config_path=config_path,
        user_data_dir=user_data_dir,
        memory_db_path=resolve_memory_db_path(user_data_dir),
        llm_base_url=setting_value(
            ("LLM_BASE_URL", "OPENAI_BASE_URL"),
            llm_file_env,
            default_llm_base_url(),
        ).rstrip("/"),
        llm_api_key=setting_value(("LLM_API_KEY", "OPENAI_API_KEY"), llm_file_env, ""),
        llm_model=setting_value(("LLM_MODEL", "OPENAI_MODEL"), llm_file_env, DEFAULT_LLM_MODEL),
        llm_timeout_seconds=float_setting_value(
            ("LLM_TIMEOUT_SECONDS",),
            llm_file_env,
            default_llm_timeout_seconds(),
        ),
        llm_env_file_path=llm_env_file_path,
        tavily_api_key=setting_value(("TAVILY_API_KEY",), llm_file_env, ""),
        tavily_base_url=setting_value(
            ("TAVILY_BASE_URL",),
            llm_file_env,
            DEFAULT_TAVILY_BASE_URL,
        ).rstrip("/"),
        tavily_max_results=int_setting_value(
            ("TAVILY_MAX_RESULTS",),
            llm_file_env,
            DEFAULT_TAVILY_MAX_RESULTS,
        ),
        agent_max_steps=max(
            1,
            int_setting_value(("AGENT_MAX_STEPS",), llm_file_env, DEFAULT_AGENT_MAX_STEPS),
        ),
        permission_overrides=parse_permission_overrides(
            setting_value(("AGENT_PERMISSION_OVERRIDES",), llm_file_env, "")
        ),
        monitor_interval_seconds=max(
            10,
            int_setting_value(
                ("AGENT_MONITOR_INTERVAL_SECONDS",),
                llm_file_env,
                DEFAULT_MONITOR_INTERVAL_SECONDS,
            ),
        ),
        telegram_token=str(
            os.getenv("FREQTRADE_TELEGRAM_TOKEN") or telegram_config.get("token", "")
        ),
        telegram_chat_id=str(
            os.getenv("FREQTRADE_TELEGRAM_CHAT_ID") or telegram_config.get("chat_id", "")
        ),
        trading_agent_doc_path=Path(__file__).resolve().parent / "TRADING_AGENT.md",
        local_config=local_config,
    )


def load_trading_agent_doc(settings: Settings) -> str:
    try:
        return settings.trading_agent_doc_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "TRADING_AGENT.md missing. Use read-only Freqtrade tools only."
