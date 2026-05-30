# Freqtrade Agent

Local controlled Trading Copilot Agent for Freqtrade. It runs as a separate
FastAPI service, talks to Freqtrade through the REST API, and keeps all trading
actions behind an explicit tool registry and permission layer.

This repository is standalone. It does not import Freqtrade Python modules and
does not require patching Freqtrade source code. Freqtrade/FreqUI can run in a
separate Docker project as long as its REST API is reachable.

## What It Provides

- Local FastAPI Agent Server on `127.0.0.1:8090`.
- OpenAI-compatible LLM tool-calling loop.
- Freqtrade REST API read-only tools and pending-only L1 controls.
- Memory v2: composite memory, behavior records, SQLite FTS search, audit log.
- Optional Tavily `web_search` / `web_fetch` tools.
- Optional Telegram dashboard pinning through Bot API.
- PNG chart tools for trading overview and candlestick previews, with optional
  Telegram photo sending behind confirmation.
- Dry-run guard: if `dry_run` is not true, control tools are refused.

## Layout

```text
.
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

Create local config files:

```bash
cp user_data/config.example.json user_data/config.json
cp user_data/agent_llm.env.example user_data/agent_llm.env
```

Then edit:

- `user_data/config.json`: Freqtrade `api_server` username/password and
  optional Telegram token/chat id.
- `user_data/agent_llm.env`: LLM, Tavily and permission settings.

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

Precedence is environment variables first, then `user_data/agent_llm.env`, then
`user_data/config.json`, then safe defaults.

## Run Locally

```bash
python -m pip install -e ".[dev]"
python tools/freqtrade_agent_server.py
```

Smoke tests:

```bash
curl http://127.0.0.1:8090/health
curl http://127.0.0.1:8090/agent/tools
curl -X POST http://127.0.0.1:8090/agent/ask \
  -H "Content-Type: application/json" \
  -d '{"question":"现在状态怎么样","source":"cli","user_id":"local","chat_id":"local"}'
```

## Run With Docker

```bash
docker compose -f docker-compose.agent.yml up -d --build
```

The example compose file assumes Freqtrade publishes its API on the host at
`127.0.0.1:8080`, so the container uses
`FREQTRADE_API_BASE_URL=http://host.docker.internal:8080`.

If the agent joins the same Docker network as Freqtrade, set:

```bash
FREQTRADE_API_BASE_URL=http://freqtrade:8080
```

## Tests

```bash
python -m compileall tools/agent_platform
ruff check tools tests
pytest
```

The tests use mocked Freqtrade/LLM surfaces where possible. They do not execute
real trades.

## Safety Boundaries

- No `forceenter` / `forceexit`.
- No strategy edits.
- No shell or Docker execution tools.
- No exchange key/secret mutation.
- No disabling `dry_run`.
- L1 controls create permission requests first.
- L2 operations are not exposed as executable tools.

## Using Beside An Existing Freqtrade Project

You can keep Freqtrade in another folder and point this repo at its `user_data`:

```bash
export FREQTRADE_AGENT_USER_DATA_DIR=/path/to/freqtrade/user_data
export FREQTRADE_API_BASE_URL=http://127.0.0.1:8080
python tools/freqtrade_agent_server.py
```

This is the recommended migration path from the original in-tree setup.
