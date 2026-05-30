# Freqtrade Agent

[中文说明](README.zh-CN.md)

Freqtrade Agent is a standalone local Trading Copilot control layer for
[Freqtrade](https://github.com/freqtrade/freqtrade). It runs as a separate
FastAPI service, talks to Freqtrade/FreqUI through the REST API, and keeps
actions behind a tool registry, permission layer, verifier, memory and audit log.

This project is not a Freqtrade fork. It does not vendor or modify Freqtrade
source code. You can run it beside an existing Freqtrade project as long as the
Freqtrade API server is reachable.

## What It Provides

- Local FastAPI Agent Server on `127.0.0.1:8090`.
- OpenAI-compatible LLM tool-calling loop.
- Freqtrade REST API read-only tools and pending/confirmed low-risk controls.
- Memory v2 with composite memory, behavior records, short-term Telegram
  context, SQLite FTS search and audit logs.
- Optional Tavily `web_search` / `web_fetch` tools.
- Optional Telegram bridge features: permissions, dashboard pinning and chart
  sending.
- PNG chart tools for trading overview and candlestick previews.
- Dry-run guard: if Freqtrade reports `dry_run=false`, the Agent stays
  conservative and refuses trading-control actions.

## Quick Start

Clone this repository, then run the setup wizard:

```bash
python scripts/setup_wizard.py
```

The wizard creates or updates local-only files:

- `user_data/config.json`
- `user_data/agent_llm.env`

These files can contain passwords or API keys and are ignored by Git.

The wizard asks for:

- Freqtrade API URL, username and password.
- `dry_run=true/false`. New users should keep `dry_run=true`.
- OpenAI-compatible LLM base URL, model and optional API key.
- Optional Tavily API key.
- Optional Telegram bot token and chat id.

If you choose `dry_run=false`, the Agent still does not unlock high-risk tools:
no forceenter/forceexit, no strategy edits, no exchange credential edits and no
shell/docker execution tools.

## Run With Docker

```bash
docker compose -f docker-compose.agent.yml up -d --build
```

The example compose file assumes Freqtrade publishes its API on the host at
`127.0.0.1:8080`, so the container uses:

```bash
FREQTRADE_API_BASE_URL=http://host.docker.internal:8080
```

If the agent joins the same Docker network as Freqtrade, set:

```bash
FREQTRADE_API_BASE_URL=http://freqtrade:8080
```

## Run Locally

```bash
python -m pip install -e ".[dev]"
python tools/freqtrade_agent_server.py
```

## Smoke Tests

```bash
curl http://127.0.0.1:8090/health
curl http://127.0.0.1:8090/agent/tools
curl -X POST http://127.0.0.1:8090/agent/ask \
  -H "Content-Type: application/json" \
  -d '{"question":"What tools can you use?","source":"cli","user_id":"local","chat_id":"local"}'
```

## Layout

```text
.
├── scripts/
│   └── setup_wizard.py
├── tools/
│   ├── freqtrade_agent_server.py
│   └── agent_platform/
│       ├── main.py
│       ├── config.py
│       ├── agents/
│       ├── plugins/
│       ├── registry/
│       ├── schemas/
│       └── storage/
├── tests/agent/
├── user_data/
│   ├── config.example.json
│   ├── agent_llm.env.example
│   └── agent_charts/          # generated PNG chart files
├── Dockerfile
├── docker-compose.agent.yml
└── pyproject.toml
```

## Configuration

Config precedence is:

1. Environment variables.
2. `user_data/agent_llm.env`.
3. `user_data/config.json`.
4. Safe defaults.

Important environment variables:

```bash
FREQTRADE_AGENT_USER_DATA_DIR=./user_data
FREQTRADE_CONFIG_PATH=./user_data/config.json
FREQTRADE_AGENT_MEMORY_DB=./user_data/agent_memory.sqlite
FREQTRADE_API_BASE_URL=http://127.0.0.1:8080
FREQTRADE_API_USER=freqtrade
FREQTRADE_API_PASSWORD=change-me
LLM_BASE_URL=http://127.0.0.1:1234/v1
LLM_MODEL=local-model
LLM_API_KEY=
```

## Telegram

Telegram is optional. If you provide `telegram.token` and `telegram.chat_id` in
`user_data/config.json`, the Agent can send dashboard updates, permission
requests and chart images through the Telegram Bot API.

The Agent replies in the same language as the user when the LLM is used. For
Telegram, it asks the model to use plain text only: no Markdown, no code blocks
and no decorative formatting.

## Tool Overview

The Agent exposes tools through a whitelist registry:

- Freqtrade read-only tools: status, balance, profit, open/recent trades, logs,
  config summary, whitelist, sysinfo, markets and analyzed candles.
- Market tools: public Binance ticker and market snapshots.
- Web tools: Tavily search/fetch for news and external context, usually behind
  confirmation because it may call a paid external API.
- Memory tools: recall, behavior search, preference save, compact and forget.
- Monitor and scheduler tools: Telegram-only suggestions and scheduled reports.
- Chart and Telegram tools: local PNG chart previews, Telegram chart sending
  and pinned dashboard updates.

High-risk tools such as live order placement, forceenter/forceexit, shell,
Docker, strategy edits and exchange credential changes are not exposed.

## Tests

```bash
python -m compileall -q tools scripts
ruff check tools tests scripts
pytest tests/agent
```

The tests use mocked Freqtrade/LLM surfaces where possible. They do not execute
real trades.

## Safety Boundaries

- No `forceenter` / `forceexit`.
- No strategy edits.
- No shell or Docker execution tools.
- No exchange key/secret mutation.
- No automatic disabling of `dry_run`.
- L1 controls require permission flow.
- L2 operations are not exposed as executable tools.

## Using Beside An Existing Freqtrade Project

You can keep Freqtrade in another folder and point this repo at its `user_data`:

```bash
export FREQTRADE_AGENT_USER_DATA_DIR=/path/to/freqtrade/user_data
export FREQTRADE_API_BASE_URL=http://127.0.0.1:8080
python tools/freqtrade_agent_server.py
```

This is the recommended migration path from the original in-tree setup.

## Before Publishing

Do not commit local secrets or generated runtime data:

- `user_data/config.json`
- `user_data/agent_llm.env`
- `user_data/*.sqlite`
- `user_data/agent_charts/`
- caches such as `.pytest_cache/`, `.ruff_cache/`, `.DS_Store`
