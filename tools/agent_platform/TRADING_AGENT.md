# Local Freqtrade Trading Copilot

## Project structure

- `tools/agent_platform/main.py`: FastAPI Agent API server.
- `tools/agent_platform/llm_client.py`: OpenAI-compatible chat completions client.
- `tools/agent_platform/registry/`: Tool Registry and permission rules.
- `tools/agent_platform/plugins/`: Whitelisted Freqtrade, logs, web, monitor,
  scheduler and memory tools.
- `tools/agent_platform/scheduler.py`: Local scheduled information jobs.
- `tools/agent_platform/storage/db.py`: SQLite memory and audit log.
- `tools/agent_platform/agents/`: Trading Copilot loop and verifier.

## Freqtrade API

- Default API base URL: `http://127.0.0.1:8080/api/v1`.
- Docker service API base URL: `http://freqtrade:8080/api/v1`.
- API credentials are read from `user_data/config.json` under `api_server`.
- Environment overrides:
  - `FREQTRADE_API_BASE_URL`
  - `FREQTRADE_API_USER`
  - `FREQTRADE_API_PASSWORD`

## Config file

- Config path: `user_data/config.json`.
- API server config path: `user_data/config.json.api_server`.
- Memory database path: `user_data/agent_memory.sqlite`.
- Scheduler reports path: `user_data/agent_reports/`.

## LLM config

- Runtime environment variables have highest priority:
  - `LLM_BASE_URL` or `OPENAI_BASE_URL`
  - `LLM_API_KEY` or `OPENAI_API_KEY`
  - `LLM_MODEL` or `OPENAI_MODEL`
  - `LLM_TIMEOUT_SECONDS`
- Agent runtime:
  - `AGENT_MAX_STEPS`
  - `AGENT_PERMISSION_OVERRIDES`, for example `web_search:allow,web_fetch:allow`
  - `AGENT_MONITOR_INTERVAL_SECONDS`
- Tavily web tools:
  - `TAVILY_API_KEY`
  - `TAVILY_BASE_URL`
  - `TAVILY_MAX_RESULTS`
- Manual local config file: `user_data/agent_llm.env`.
- Example templates:
  - `user_data/config.example.json`
  - `user_data/agent_llm.env.example`
  - `tools/agent_platform/agent_llm.env.example`
- Do not commit real API keys.
- Do not include `/chat/completions` in the base URL.
- Default local endpoint: `http://127.0.0.1:1234/v1`.
- Default Docker endpoint: `http://host.docker.internal:1234/v1`.
- GLM Coding Plan endpoint: `https://open.bigmodel.cn/api/coding/paas/v4`.

## Dry-run requirement

- `dry_run` must remain `true`.
- If `show_config` reports `dry_run=false`, the agent must enter read-only mode.
- Never turn off dry-run.

## Docker and proxy notes

- Keep the existing `freqtrade-local:bilingual-freqai` image.
- Keep the existing proxy on `host.docker.internal:7897`.
- Keep `NO_PROXY` entries for `localhost`, `127.0.0.1`, `freqtrade` and `freqtrade-agent`.
- Do not break FreqUI on `127.0.0.1:8080`.
- Agent API is exposed only on host `127.0.0.1:8090`.

## Allowed tools

Tools use OpenCode-style permission actions: `allow`, `ask`, or `deny`.
Current defaults:

- `ft_ping`
- `ft_health`
- `ft_status`
- `ft_balance`
- `ft_profit`
- `ft_profit_all`
- `ft_count`
- `ft_performance`
- `ft_stats`
- `ft_daily`
- `ft_weekly`
- `ft_monthly`
- `ft_entries`
- `ft_exits`
- `ft_mix_tags`
- `ft_trades_recent`
- `ft_trade_detail`
- `ft_open_trade_custom_data`
- `ft_trade_custom_data`
- `ft_logs`
- `ft_show_config_sanitized`
- `ft_whitelist`
- `ft_blacklist`
- `ft_locks`
- `ft_version`
- `ft_sysinfo`
- `ft_plot_config`
- `ft_strategy_info`
- `ft_markets`
- `ft_pair_candles`
- `ft_pair_history`
- `ft_background_tasks`
- `ft_available_pairs`
- `memory_recall`
- `memory_search_behavior`
- `memory_save_observation`
- `memory_save_preference`

These read-only Freqtrade and memory lookup/save tools default to `allow`.

Memory maintenance tools default to `ask`:

- `memory_forget`
- `memory_compact_now`

Web tools default to `ask`:

- `web_search`
- `web_fetch`

Monitor tools default to `ask`:

- `monitor_list`
- `monitor_set`
- `monitor_pause`
- `monitor_resume`
- `monitor_run_once`

Scheduler tools:

- `scheduler_list`: defaults to `allow`.
- `scheduler_enable`: defaults to `ask`.
- `scheduler_disable`: defaults to `ask`.
- `scheduler_run_once`: defaults to `ask`.

Telegram dashboard tools:

- `telegram_dashboard_preview`: defaults to `allow`; builds a read-only preview.
- `telegram_dashboard_pin`: first pin defaults to `ask`; existing dashboard updates are `allow`.

L1 low-risk control tools default to `ask` and remain pending-only in Phase 1:

- `ft_start`
- `ft_pause`
- `ft_stop`
- `ft_reload_config`

## Forbidden tools and actions

L2 actions are always denied in Phase 1:

- `forceenter`
- `forceexit`
- `delete_trade`
- `cancel_order`
- blacklist add/delete
- modifying `config.json`
- modifying strategy files
- arbitrary file read
- arbitrary file write
- executing shell
- executing docker commands
- turning off `dry_run`
- changing `stake_amount`
- changing leverage
- changing trading mode
- changing exchange API key/secret

## Start commands

Manual:

```bash
python tools/freqtrade_agent_server.py
```

Docker:

```bash
docker compose -f docker-compose.agent.yml up -d
docker compose -f docker-compose.agent.yml up -d --no-build --force-recreate freqtrade-agent
```

## API endpoints

- `GET /health`
- `GET /agent/tools`
- `GET /agent/memory/recent`
- `GET /agent/runs/{run_id}`
- `GET /agent/permissions/pending`
- `POST /agent/permissions/{request_id}/confirm`
- `GET /agent/monitors`
- `GET /agent/scheduler/jobs`
- `POST /agent/scheduler/jobs/{job_id}/run`
- `POST /agent/scheduler/jobs/{job_id}/enable`
- `POST /agent/scheduler/jobs/{job_id}/disable`
- `POST /agent/ask`

## Scheduled information jobs

Default jobs:

- `hourly_bot_health_check`: checks health, open trades and logs every hour.
- `daily_trading_report`: writes a daily report to
  `user_data/agent_reports/daily_report_example.md`.
- `daily_profit_summary`: summarizes `profit`, `drawdown` and `winrate`.
- `daily_log_error_scan`: scans recent logs for errors and warnings.
- `daily_market_snapshot`: summarizes market context; external web search remains
  an `ask` permission tool.
- `scheduled_observation_save`: saves a short observation to SQLite.

Scheduled jobs write `scheduled_jobs`, `scheduled_job_runs`, conversations,
tool calls and Markdown reports. They never place orders and never modify
strategy or trading configuration.

## Security strategy

- LLM cannot access the filesystem, shell, docker, database or raw HTTP clients.
- LLM can only request registered Tool Registry tools.
- Tool Registry enforces permission actions.
- All tool calls are logged in SQLite.
- Tool calls include `latency_ms` for long-run audits.
- All conversations are logged in SQLite.
- Scheduler job runs are logged in SQLite and fixed Markdown report paths.
- Ask tools generate `permission_request` records and are not executed before confirmation.
- Pending Freqtrade control actions are not executed in Phase 1.
- Sensitive keys are redacted before returning config or audit data.

## LLM answer rules

- Answer mainly in Chinese.
- Keep key English terms: `dry-run`, `open trades`, `profit`, `drawdown`, `stake`, `timeframe`.
- Current status, profit, balance, logs, config, positions and health must come from tool output.
- Do not invent current profit, balance, open trades, logs, market data or configuration.
- Say "不知道" or explain tool failure when facts are unavailable.
- Clearly separate tool facts from inference.
- For information questions, use the structure: conclusion, data source tools,
  key metrics/tool facts, inference, uncertainty, risk warning, follow-up
  questions.
- Refuse L2 operations.
- Create `permission_request` for ask tools and ask for confirmation.
- Never claim a pending action has executed.
- Proactive monitor output is advice only, not investment advice, and must state no trades were executed.
