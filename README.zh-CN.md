# Freqtrade Agent

[English README](README.md)

Freqtrade Agent 是一个独立的本地 Trading Copilot 控制层，用于连接
[Freqtrade 官方项目](https://github.com/freqtrade/freqtrade)。它作为单独的
FastAPI 服务运行，通过 REST API 连接 Freqtrade/FreqUI，并通过工具注册表、
权限系统、verifier、记忆和审计日志控制所有动作。

本项目不是 Freqtrade fork，不复制、不内置、不修改 Freqtrade 源码。只要你的
Freqtrade API Server 可访问，就可以把本项目放在旁边独立运行。

## 功能

- 本地 FastAPI Agent Server：`127.0.0.1:8090`。
- OpenAI-compatible LLM 工具调用循环。
- Freqtrade REST API 只读工具和低风险控制工具权限流。
- Memory v2：复合记忆、行为记录、Telegram 短时上下文、SQLite FTS 检索和审计日志。
- 可选 Tavily `web_search` / `web_fetch` 工具。
- 可选 Telegram 能力：权限按钮、置顶 dashboard、图表发送。
- PNG 图表工具：交易概览图、K 线预览图。
- Dry-run guard：如果 Freqtrade 显示 `dry_run=false`，Agent 会更保守并拒绝交易控制动作。

## 快速开始

克隆仓库后，运行使用引导程序：

```bash
python scripts/setup_wizard.py
```

引导程序会创建或更新本地配置文件：

- `user_data/config.json`
- `user_data/agent_llm.env`

这些文件可能包含密码或 API key，默认不会提交到 Git。

引导程序会询问：

- Freqtrade API 地址、用户名、密码。
- `dry_run=true/false`。新手建议保持 `dry_run=true`。
- OpenAI-compatible LLM base URL、模型名、可选 API key。
- 可选 Tavily API key。
- 可选 Telegram bot token 和 chat_id。

如果你选择 `dry_run=false`，Agent 也不会因此开放高风险工具：不 forceenter/
forceexit、不改策略、不改交易所密钥、不提供 shell/docker 执行工具。

## Docker 启动

```bash
docker compose -f docker-compose.agent.yml up -d --build
```

示例 compose 假设 Freqtrade API 发布在宿主机 `127.0.0.1:8080`，因此容器里使用：

```bash
FREQTRADE_API_BASE_URL=http://host.docker.internal:8080
```

如果 Agent 和 Freqtrade 在同一个 Docker network，可以改成：

```bash
FREQTRADE_API_BASE_URL=http://freqtrade:8080
```

## 本地 Python 启动

```bash
python -m pip install -e ".[dev]"
python tools/freqtrade_agent_server.py
```

## 测试命令

```bash
curl http://127.0.0.1:8090/health
curl http://127.0.0.1:8090/agent/tools
curl -X POST http://127.0.0.1:8090/agent/ask \
  -H "Content-Type: application/json" \
  -d '{"question":"你现在能用什么工具？","source":"cli","user_id":"local","chat_id":"local"}'
```

## 项目结构

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
│   └── agent_charts/          # 生成的 PNG 图表
├── Dockerfile
├── docker-compose.agent.yml
└── pyproject.toml
```

## 配置优先级

配置读取优先级：

1. 环境变量。
2. `user_data/agent_llm.env`。
3. `user_data/config.json`。
4. 安全默认值。

常用环境变量：

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

Telegram 是可选能力。如果你在 `user_data/config.json` 里配置了
`telegram.token` 和 `telegram.chat_id`，Agent 可以通过 Telegram Bot API
发送 dashboard、权限请求和图表图片。

LLM 可用时，Agent 会尽量使用和用户相同的语言回复。Telegram 输出会要求纯文本：
不使用 Markdown、不使用代码块、不使用花哨格式。

## 工具说明

Agent 通过白名单 Tool Registry 暴露工具：

- Freqtrade 只读工具：状态、余额、收益、持仓/最近交易、日志、配置摘要、白名单、
  系统信息、市场列表和已分析 K 线。
- 行情工具：公开 Binance ticker 和多币种行情快照。
- Web 工具：通过 Tavily search/fetch 查询新闻和外部上下文，通常需要确认，因为可能调用付费外部 API。
- 记忆工具：召回、行为记录搜索、偏好保存、压缩和遗忘。
- Monitor / Scheduler 工具：Telegram 主动建议和定时报告。
- 图表 / Telegram 工具：本地 PNG 图表预览、Telegram 图表发送和置顶 dashboard 更新。

高风险能力不会作为工具暴露：实盘下单、forceenter/forceexit、shell、Docker、
策略修改和交易所凭据修改。

## 测试

```bash
python -m compileall -q tools scripts
ruff check tools tests scripts
pytest tests/agent
```

测试会 mock Freqtrade/LLM 相关接口，不会执行真实交易。

## 安全边界

- 不提供 `forceenter` / `forceexit`。
- 不修改策略。
- 不提供 shell 或 Docker 执行工具。
- 不修改交易所 key/secret。
- 不自动关闭 `dry_run`。
- L1 控制动作需要权限确认流程。
- L2 高风险动作不作为可执行工具暴露。

## 与已有 Freqtrade 项目一起使用

你可以把 Freqtrade 放在另一个目录，然后让本项目指向它的 `user_data`：

```bash
export FREQTRADE_AGENT_USER_DATA_DIR=/path/to/freqtrade/user_data
export FREQTRADE_API_BASE_URL=http://127.0.0.1:8080
python tools/freqtrade_agent_server.py
```

这是从原来内嵌式目录迁移到独立仓库的推荐方式。
