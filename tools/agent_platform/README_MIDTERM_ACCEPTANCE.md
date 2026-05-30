# Trading Copilot Agent 中期验收说明

## 1. 当前定位

本项目是在 Freqtrade Docker 项目旁边新增的本地 Trading Copilot 控制层。它通过白名单工具连接 Freqtrade/FreqUI REST API、Telegram Bot API、SQLite memory/audit，并允许 LLM 以受控方式完成查询、分析、定时报告、主动提醒和 Telegram 置顶 dashboard。

当前仍处于学习与 dry-run 阶段：

- 必须保持 `dry_run=true`。
- 不修改交易策略文件。
- 不关闭 dry-run。
- 不实现 forceenter/forceexit。
- 不执行 shell/docker 任意命令。
- 不修改交易所 API key/secret。
- 所有交易相关回答必须基于工具结果。

## 2. 目录结构

```text
tools/agent_platform/
├── main.py                         # FastAPI 入口、AppState、API、后台服务生命周期
├── config.py                       # 配置加载: env > user_data/agent_llm.env > config.json > default
├── llm_client.py                   # OpenAI-compatible chat / streaming client
├── runtime_utils.py                # 结构化日志、内存滑动窗口限流
├── monitor.py                      # 主动 monitor 后台 loop，只发 Telegram 建议
├── scheduler.py                    # 定时信息任务后台 loop，写报告并刷新 dashboard
├── TRADING_AGENT.md                # 给 LLM 的项目约束与工具说明
├── README_MIDTERM_ACCEPTANCE.md    # 本验收说明
├── agents/
│   ├── trading_copilot.py          # Plan -> Tool Call -> Observe -> Answer 长链 Agent
│   └── verifier.py                 # 规则 verifier，过滤敏感信息和危险承诺
├── registry/
│   ├── tool_registry.py            # ToolSpec、权限、确认、审计、缓存、批量执行
│   └── permissions.py              # L0/L1/L2 与 allow/ask/deny
├── plugins/
│   ├── freqtrade_plugin.py         # Freqtrade REST API 只读和 L1 pending 工具
│   ├── logs_plugin.py              # 日志查询工具
│   ├── market_plugin.py            # 公共行情工具
│   ├── web_plugin.py               # Tavily web_search/web_fetch
│   ├── memory_plugin.py            # memory_recall / memory_save_observation
│   ├── monitor_plugin.py           # monitor_list/set/pause/resume/run_once
│   ├── scheduler_plugin.py         # scheduler_list/enable/disable/run_once
│   ├── telegram_dashboard_plugin.py # Telegram pinned dashboard preview/pin/update
│   └── agent_meta_plugin.py        # agent_capabilities 工具能力说明
├── schemas/
│   └── tool_outputs.py             # API 请求/响应 Pydantic model
└── storage/
    └── db.py                       # SQLite schema、memory、audit、permissions、dashboard state
```

相关外部文件：

```text
user_data/config.json               # Freqtrade / Telegram / api_server 配置
user_data/agent_llm.env             # LLM、Tavily、权限覆盖等本地环境变量
user_data/agent_memory.sqlite       # Agent SQLite memory/audit/runtime 状态
user_data/agent_reports/            # scheduler 生成的本地报告
telegram bridge                     # 可选：外部 Telegram bridge 调用 Agent API
```

## 3. 核心运行原理

### 3.1 Agent API Server

`main.py` 使用 FastAPI 启动本地服务，监听 `127.0.0.1:8090` 或 Docker 内部映射端口。`AppState` 在 lifespan 中创建：

- `Settings`
- `AgentDB`
- `FreqtradePlugin`
- `ToolRegistry`
- 所有 plugins
- `OpenAICompatibleClient`
- `TradingCopilot`
- `MonitorService`
- `SchedulerService`

关键 API：

- `GET /health`
- `GET /agent/tools`
- `GET /agent/memory/recent`
- `GET /agent/runs/{id}`
- `GET /agent/permissions/pending`
- `POST /agent/permissions/{id}/confirm`
- `POST /agent/ask`
- `POST /agent/ask/stream`
- `POST /agent/ask/resume`
- `GET /agent/monitors`
- `GET /agent/scheduler/jobs`

### 3.2 Tool Registry

每个工具通过 `ToolSpec` 注册：

- `name`
- `description`
- `permission_level`
- `input_schema`
- `output_schema`
- `handler`
- `requires_confirmation`
- `risk_notes`
- `permission_default`
- `permission_resolver`

权限模型：

- `allow`: 直接执行。
- `ask`: 生成 permission request，Telegram/API 确认后再执行。
- `deny`: 拒绝执行。

新增的动态权限用于 dashboard：

- `telegram_dashboard_pin` 第一次没有 dashboard message 时为 `ask`。
- 已有 dashboard message 后为 `allow`，后续刷新只编辑同一条置顶消息。

工具调用都会写入 `tool_calls`，permission 会写入 `permission_requests`。短 TTL 缓存用于 Freqtrade 只读查询和行情工具，减少连续多工具调用延迟。

### 3.3 LLM 长链流程

`TradingCopilot` 的基本循环：

```text
用户问题
-> 构造短 system prompt + 轻量 memory
-> LLM 生成 plan/tool calls
-> Tool Registry 执行工具
-> 观察工具返回
-> 如需 ask 权限则暂停并保存 run_state
-> 用户确认后 resume
-> LLM 基于工具结果回答
-> verifier 检查
-> 保存 conversation、run_steps、tool_calls
```

当 LLM 不可用、超时或返回无效工具调用时，系统会 fallback 到规则路由；但不会编造当前状态、余额、收益、持仓、日志、行情或新闻。

### 3.4 Memory 与 Audit

SQLite 文件为：

```text
user_data/agent_memory.sqlite
```

主要表：

- `conversations`: 用户问题和回答。
- `observations`: 用户要求记住的短观察。
- `tool_calls`: 工具调用审计。
- `agent_runs`: 每轮 Agent run 元数据。
- `run_steps`: LLM/tool 每一步。
- `permission_requests`: ask 权限请求。
- `composite_memories`: profile/semantic/episodic/procedural 复合记忆。
- `behavior_records`: 每轮 Agent 的行为记录、工具链、权限和结果摘要。
- `monitor_rules`: 主动监控规则。
- `monitor_events`: 主动提醒事件。
- `scheduled_jobs`: 定时任务。
- `scheduled_job_runs`: 定时任务运行记录。
- `telegram_dashboards`: Telegram pinned dashboard 的 chat/message 状态。

记忆策略是复合轻量化：默认只注入短摘要和索引，需要历史细节时才调用
`memory_recall`；需要排查“刚才为什么卡住/上次用了哪些工具”时调用
`memory_search_behavior`。检索层优先使用 SQLite FTS5，不可用时回退到 LIKE。

### 3.5 Telegram Bridge

Freqtrade 原生 Telegram bridge 已扩展为：

- Freqtrade 原生命令继续保留，例如 `/status`、`/profit`、`/balance`。
- 普通自然语言消息转发到 Agent `/agent/ask/stream`。
- 收到消息后先发占位消息，handler 释放，Agent 后台流式更新同一条消息。
- ask 权限通过 inline buttons 确认。
- 确认后调用 `/agent/permissions/{id}/confirm`，再调用 `/agent/ask/resume` 续跑。

这样 Telegram 不再等待完整 LLM 工具链，前台会先有反馈，完整答案随后更新。

## 4. 当前工具能力

### 4.1 Freqtrade 只读工具

- `ft_ping`
- `ft_health`
- `ft_status`
- `ft_balance`
- `ft_profit`
- `ft_profit_all`
- `ft_count`
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
- `ft_performance`
- `ft_stats`
- `ft_daily`
- `ft_weekly`
- `ft_monthly`
- `ft_entries`
- `ft_exits`
- `ft_mix_tags`
- `ft_plot_config`
- `ft_strategy_info`
- `ft_markets`
- `ft_pair_candles`
- `ft_pair_history`
- `ft_background_tasks`
- `ft_available_pairs`

用途：读取机器人健康、持仓、收益、余额、日志、配置摘要、策略元信息、交易表现、K 线、pairlist、本地数据和后台任务。

说明：`ft_pair_history`、`ft_background_tasks`、`ft_available_pairs` 对应 Freqtrade webserver-mode 路由；当前 trading mode 下可能返回 “Bot is not in the correct state”，属于上游运行模式限制。

### 4.2 行情与 Web

- `market_ticker`
- `market_snapshot`
- `web_search`
- `web_fetch`

行情工具读取公共行情，不使用交易所私钥。Web 工具使用 Tavily，默认 ask，避免无意产生外部 API 成本。

### 4.3 Memory

- `memory_recall`
- `memory_search_behavior`
- `memory_save_observation`
- `memory_save_preference`
- `memory_forget`
- `memory_compact_now`

仅保存非敏感短观察、偏好和行为摘要，不保存 token/password/API key。
`memory_forget` 和 `memory_compact_now` 是 L1 ask 工具，需要确认后执行。

### 4.4 Monitor

- `monitor_list`
- `monitor_set`
- `monitor_pause`
- `monitor_resume`
- `monitor_run_once`

monitor 只产生 Telegram 建议，不执行交易。创建、暂停、恢复、手动运行均走 ask 权限。

### 4.5 Scheduler

- `scheduler_list`
- `scheduler_enable`
- `scheduler_disable`
- `scheduler_run_once`

scheduler 只运行信息任务，写本地报告，更新下一次运行时间，不执行交易。

### 4.6 Telegram Dashboard

- `telegram_dashboard_preview`: 只生成预览文本，不发消息。
- `telegram_dashboard_pin`: 发送/编辑/置顶 dashboard 消息。

Dashboard 内容包括：

- bot health
- `dry-run`
- strategy/timeframe
- exchange/stake
- scheduled jobs
- monitor rules
- last update
- “未执行任何交易; 这不是投资建议”提示

自动刷新触发点：

- `monitor_set`
- `monitor_pause`
- `monitor_resume`
- `monitor_run_once`
- `scheduler_enable`
- `scheduler_disable`
- `scheduler_run_once`
- scheduler 后台任务实际运行完成后
- scheduler API enable/disable/run 后

自动刷新只在已存在 dashboard message 时生效；不会自动创建第一条置顶消息。第一条仍需要用户确认。

## 5. 安全边界

明确禁止：

- forceenter / forceexit
- delete trade
- cancel order
- blacklist add/delete
- 修改 `config.json`
- 修改策略文件
- 任意文件读写
- shell/docker 执行
- 关闭 dry-run
- 改 stake_amount
- 改 leverage
- 改 trading_mode
- 改交易所 API key/secret

如果 show_config 显示 `dry_run=false`，Agent 进入只读模式，拒绝所有 L1/L2 动作。

## 6. 启动与测试

启动 Agent：

```bash
python tools/freqtrade_agent_server.py
```

Docker 重启：

```bash
docker compose -f docker-compose.agent.yml restart freqtrade-agent
```

健康检查：

```bash
curl http://127.0.0.1:8090/health
```

工具列表：

```bash
curl http://127.0.0.1:8090/agent/tools
```

Dashboard 预览：

```bash
curl -X POST http://127.0.0.1:8090/agent/ask \
  -H "Content-Type: application/json" \
  -d '{"question":"预览 Telegram dashboard","source":"cli","user_id":"local","chat_id":"local"}'
```

首次置顶 dashboard：

```bash
curl -X POST http://127.0.0.1:8090/agent/ask \
  -H "Content-Type: application/json" \
  -d '{"question":"把当前定时任务、策略情况和主动监控条件置顶到 Telegram 顶部 dashboard","source":"cli","user_id":"local","chat_id":"local"}'
```

确认 permission：

```bash
curl -X POST http://127.0.0.1:8090/agent/permissions/<id>/confirm
```

刷新 dashboard：

```bash
curl -X POST http://127.0.0.1:8090/agent/ask \
  -H "Content-Type: application/json" \
  -d '{"question":"刷新一下 Telegram 置顶 dashboard","source":"cli","user_id":"local","chat_id":"local"}'
```

开发验证：

```bash
python3 -m ruff check tools/agent_platform tests/agent --no-cache
PYTHONPYCACHEPREFIX=/private/tmp/freqtrade_agent_pycache python3 -m compileall -q tools/agent_platform
PYTHONPATH=tools pytest -q tests/agent
```

## 7. 中期验收状态

已完成：

- 本地 FastAPI Agent Server。
- OpenAI-compatible LLM 接入。
- Tool Registry、权限、确认、审计。
- Freqtrade REST API 只读查询。
- Telegram 自然语言 bridge。
- Telegram inline button 权限确认与 resume。
- 长链工具调用。
- Web search/fetch。
- Memory 与 session summary。
- Monitor 主动建议。
- Scheduler 定时信息任务。
- Telegram pinned dashboard。
- Dashboard 首次确认、后续编辑刷新。
- Dashboard 在 monitor/scheduler 状态变化后自动刷新。
- 单元测试、ruff、compileall。

未做或后续阶段：

- L1 Freqtrade start/pause/stop/reload_config 的真实执行。
- 真正的自动交易控制。
- embedding/vector memory。
- Prometheus/OpenTelemetry 指标。
- 更完整的前端 Web dashboard。
