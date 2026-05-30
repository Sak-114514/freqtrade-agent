from __future__ import annotations

import json
from collections.abc import Generator
from typing import Any

from agent_platform.agents.verifier import RuleVerifier
from agent_platform.config import Settings
from agent_platform.llm_client import LLMError, OpenAICompatibleClient
from agent_platform.registry.tool_registry import ToolRegistry
from agent_platform.storage.db import AgentDB


RUN_SCOPED_APPROVAL_GROUPS: dict[str, frozenset[str]] = {
    "web_external": frozenset({"web_search", "web_fetch"}),
    "freqtrade_l1_control": frozenset(
        {"ft_start", "ft_pause", "ft_stop", "ft_reload_config"}
    ),
    "monitor_control": frozenset(
        {"monitor_set", "monitor_pause", "monitor_resume", "monitor_run_once"}
    ),
    "scheduler_control": frozenset(
        {"scheduler_enable", "scheduler_disable", "scheduler_run_once"}
    ),
}


class TradingCopilot:
    def __init__(
        self,
        *,
        settings: Settings,
        registry: ToolRegistry,
        db: AgentDB,
        llm: OpenAICompatibleClient,
        verifier: RuleVerifier,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.db = db
        self.llm = llm
        self.verifier = verifier

    def ask(self, *, question: str, source: str, user_id: str, chat_id: str) -> dict[str, Any]:
        question = question.strip()
        plan = self._plan(question)
        run_id = self.db.create_agent_run(
            source=source,
            user_id=user_id,
            chat_id=chat_id,
            question=question,
            plan=plan,
        )
        steps: list[dict[str, Any]] = []
        self._record_step(run_id, steps, "user", question)
        memory_used = self.db.recent_memory(source=source, user_id=user_id, chat_id=chat_id)
        llm_error: str | None = None

        if self._is_capability_question(question):
            response = self._capability_response(
                question=question,
                plan=plan,
                run_id=run_id,
                steps=steps,
                memory_used=memory_used,
            )
        elif source in {"scheduler", "monitor"} and self._should_use_fast_info_route(
            question,
            source,
        ):
            response = self._fast_info_response(
                question=question,
                plan=plan,
                run_id=run_id,
                steps=steps,
                memory_used=memory_used,
            )
        else:
            try:
                response = self._ask_llm(
                    question=question,
                    plan=plan,
                    run_id=run_id,
                    steps=steps,
                    memory_used=memory_used,
                )
            except LLMError as exc:
                llm_error = str(exc)
                response = self._fallback(
                    question=question,
                    plan=plan,
                    run_id=run_id,
                    steps=steps,
                    memory_used=memory_used,
                    reason=llm_error,
                )
            except Exception as exc:
                llm_error = f"agent_error: {exc}"
                response = self._fallback(
                    question=question,
                    plan=plan,
                    run_id=run_id,
                    steps=steps,
                    memory_used=memory_used,
                    reason=llm_error,
                )

        if self._should_save_observation(question) and not self._has_tool_call(
            response,
            "memory_save_observation",
        ):
            saved = self.registry.execute(
                "memory_save_observation",
                {"text": self._observation_text(question), "tags": ["user"], "importance": 2},
                run_id=run_id,
            )
            response["tool_calls"].append(self._tool_view(saved, {}))
            self._record_step(
                run_id,
                steps,
                "tool",
                str(saved.get("summary") or ""),
                tool_name="memory_save_observation",
                args={},
                result=saved,
            )

        self._save_conversation(source, user_id, chat_id, question, response["answer"])
        self.db.compact_conversations(source=source, user_id=user_id, chat_id=chat_id)
        self.db.finish_agent_run(
            run_id,
            answer=response["answer"],
            used_llm=bool(response.get("used_llm")),
            fallback_used=bool(response.get("fallback_used")),
            llm_error=llm_error or response.get("llm_error"),
        )
        behavior_record_id = self.db.record_behavior_from_run(run_id)
        response["run_id"] = run_id
        response["steps"] = steps
        response["llm_error"] = llm_error or response.get("llm_error")
        response["memory_used"] = memory_used
        response["memory_hits"] = response.get("memory_hits") or []
        response["behavior_record_id"] = behavior_record_id
        return response

    def _ask_llm(  # noqa: C901 - the long-chain loop keeps permission flow in one place.
        self,
        *,
        question: str,
        plan: str,
        run_id: int,
        steps: list[dict[str, Any]],
        memory_used: dict[str, Any],
    ) -> dict[str, Any]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt()},
            {"role": "system", "content": self._memory_prompt(memory_used)},
            {"role": "user", "content": f"Plan: {plan}\n\nUser question: {question}"},
        ]

        tool_results: list[dict[str, Any]] = []
        tool_views: list[dict[str, Any]] = []
        permission_requests: list[dict[str, Any]] = []
        pending_action: dict[str, Any] | None = None
        required_tools = self._required_tools_for_question(question)

        for _iteration in range(self.settings.agent_max_steps):
            try:
                message = self.llm.chat(messages=messages, tools=self.registry.openai_tools())
            except LLMError as exc:
                if not tool_results:
                    raise
                answer = (
                    self._answer_from_tools(tool_results, question=question)
                    + "\n\nLLM 在观察工具结果后超时, 已基于已取得的工具结果给出保守总结。"
                )
                return self._verified_response(
                    question=question,
                    answer=answer,
                    plan=plan,
                    tool_results=tool_results,
                    used_llm=True,
                    fallback_used=False,
                    tool_views=tool_views,
                    pending_action=pending_action,
                    permission_requests=permission_requests,
                    steps=steps,
                    memory_used=memory_used,
                    llm_error=str(exc),
                )
            self._record_step(
                run_id,
                steps,
                "assistant",
                json.dumps(message, ensure_ascii=False, default=str),
            )
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                answer = str(message.get("content") or "").strip()
                if not answer:
                    raise LLMError("LLM returned no answer and no tool call.")
                implied_tool = self._implied_permission_tool(question, answer)
                if implied_tool:
                    name, args = implied_tool
                    result = self.registry.execute(name, args, run_id=run_id)
                    tool_results.append(result)
                    tool_views.append(self._tool_view(result, args))
                    permission_request = result.get("permission_request")
                    if isinstance(permission_request, dict):
                        permission_requests.append(permission_request)
                        pending_action = permission_request
                    self._record_step(
                        run_id,
                        steps,
                        "tool",
                        json.dumps(result, ensure_ascii=False, default=str),
                        tool_name=name,
                        args=args,
                        result=result,
                    )
                    if result.get("permission_required"):
                        answer = self._permission_answer(result)
                        return self._verified_response(
                            question=question,
                            answer=answer,
                            plan=plan,
                            tool_results=tool_results,
                            used_llm=True,
                            fallback_used=False,
                            tool_views=tool_views,
                            pending_action=pending_action,
                            permission_requests=permission_requests,
                            steps=steps,
                            memory_used=memory_used,
                        )
                self._ensure_required_tools(
                    required_tools=required_tools,
                    question=question,
                    run_id=run_id,
                    steps=steps,
                    tool_results=tool_results,
                    tool_views=tool_views,
                )
                if required_tools and tool_results:
                    permission_result = self._first_permission_result(tool_results)
                    answer = (
                        self._permission_answer(permission_result)
                        if permission_result
                        else self._answer_from_tools(tool_results, question=question)
                    )
                return self._verified_response(
                    question=question,
                    answer=answer,
                    plan=plan,
                    tool_results=tool_results,
                    used_llm=True,
                    fallback_used=False,
                    tool_views=tool_views,
                    pending_action=pending_action,
                    permission_requests=permission_requests,
                    steps=steps,
                    memory_used=memory_used,
                )

            blocked_by_permission = False
            messages.append(message)
            for call in tool_calls:
                function = call.get("function") or {}
                name = str(function.get("name") or "")
                args = self._parse_tool_args(function.get("arguments"))
                if not self.registry.get(name):
                    raise LLMError(f"LLM requested invalid tool: {name}")
                result = self.registry.execute(name, args, run_id=run_id)
                tool_results.append(result)
                tool_views.append(self._tool_view(result, args))
                permission_request = result.get("permission_request")
                if isinstance(permission_request, dict):
                    permission_requests.append(permission_request)
                    pending_action = permission_request
                if result.get("pending_action"):
                    pending_action = result["pending_action"]
                self._record_step(
                    run_id,
                    steps,
                    "tool",
                    json.dumps(result, ensure_ascii=False, default=str),
                    tool_name=name,
                    args=args,
                    result=result,
                )
                if result.get("permission_required"):
                    answer = self._permission_answer(result)
                    blocked_by_permission = True
                    break
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id", name),
                        "name": name,
                        "content": self._llm_tool_content(result),
                    }
                )
            if blocked_by_permission:
                return self._verified_response(
                    question=question,
                    answer=answer,
                    plan=plan,
                    tool_results=tool_results,
                    used_llm=True,
                    fallback_used=False,
                    tool_views=tool_views,
                    pending_action=pending_action,
                    permission_requests=permission_requests,
                    steps=steps,
                    memory_used=memory_used,
                )

        self._ensure_required_tools(
            required_tools=required_tools,
            question=question,
            run_id=run_id,
            steps=steps,
            tool_results=tool_results,
            tool_views=tool_views,
        )
        answer = self._answer_from_tools(tool_results, question=question)
        return self._verified_response(
            question=question,
            answer=answer,
            plan=plan,
            tool_results=tool_results,
            used_llm=True,
            fallback_used=False,
            tool_views=tool_views,
            pending_action=pending_action,
            permission_requests=permission_requests,
            steps=steps,
            memory_used=memory_used,
        )

    def _deny_high_risk(
        self,
        *,
        question: str,
        plan: str,
        run_id: int,
        steps: list[dict[str, Any]],
        memory_used: dict[str, Any],
    ) -> dict[str, Any]:
        answer = (
            "我不能执行这个请求。它触及 L2 高风险边界: forceenter/forceexit、"
            "关闭 dry_run、真实下单、改策略、改交易配置、shell/docker、交易所密钥"
            "或删除交易数据都不会提供工具入口。\n\n"
            "当前系统定位是 Trading Information Copilot, 只负责看、查、解释、总结、"
            "提醒和记录。"
        )
        self._record_step(run_id, steps, "assistant", answer)
        return self._verified_response(
            question=question,
            answer=answer,
            plan=plan,
            tool_results=[],
            used_llm=False,
            fallback_used=False,
            permission_requests=[],
            steps=steps,
            memory_used=memory_used,
        )

    def _capability_response(
        self,
        *,
        question: str,
        plan: str,
        run_id: int,
        steps: list[dict[str, Any]],
        memory_used: dict[str, Any],
    ) -> dict[str, Any]:
        result = self.registry.execute("agent_capabilities", {}, run_id=run_id)
        self._record_step(
            run_id,
            steps,
            "tool",
            json.dumps(result, ensure_ascii=False, default=str),
            tool_name="agent_capabilities",
            args={},
            result=result,
        )
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        groups = data.get("groups") if isinstance(data.get("groups"), dict) else {}

        def names(group: str) -> str:
            items = groups.get(group) if isinstance(groups.get(group), list) else []
            return ", ".join(str(item.get("name")) for item in items if item.get("name")) or "无"

        answer = (
            "我是你的本地 Freqtrade Trading Copilot。能帮你看机器人状态、收益、余额、"
            "持仓、日志、配置、行情和新闻; 也能做记忆、监控和定时提醒。\n\n"
            f"主要工具: Freqtrade 查询({names('freqtrade_readonly')}), "
            f"行情({names('market')}), Web({names('web')}), 记忆({names('memory')})。\n"
            f"需要确认的控制类: {names('freqtrade_l1_control')}; "
            f"{names('monitor')}; {names('scheduler')}。\n"
            "不会做: forceenter/forceexit、实盘下单、关闭 dry_run、改策略/config、"
            "shell/docker、改交易所密钥。"
        )
        return self._verified_response(
            question=question,
            answer=answer,
            plan=plan,
            tool_results=[result],
            used_llm=False,
            fallback_used=False,
            tool_views=[self._tool_view(result, {})],
            steps=steps,
            memory_used=memory_used,
        )

    def _pending_tool_action(
        self,
        *,
        question: str,
        plan: str,
        run_id: int,
        steps: list[dict[str, Any]],
        memory_used: dict[str, Any],
        tool_name: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        result = self.registry.execute(tool_name, args, run_id=run_id)
        self._record_step(
            run_id,
            steps,
            "tool",
            json.dumps(result, ensure_ascii=False, default=str),
            tool_name=tool_name,
            args=args,
            result=result,
        )
        answer = (
            self._permission_answer(result)
            if result.get("permission_required")
            else self._answer_from_tools([result], question=question)
        )
        return self._verified_response(
            question=question,
            answer=answer,
            plan=plan,
            tool_results=[result],
            used_llm=False,
            fallback_used=False,
            tool_views=[self._tool_view(result, args)],
            pending_action=result.get("pending_action") or result.get("permission_request"),
            permission_requests=[
                result["permission_request"]
            ]
            if isinstance(result.get("permission_request"), dict)
            else [],
            steps=steps,
            memory_used=memory_used,
        )

    def _fast_info_response(
        self,
        *,
        question: str,
        plan: str,
        run_id: int,
        steps: list[dict[str, Any]],
        memory_used: dict[str, Any],
    ) -> dict[str, Any]:
        tool_names = self._required_tools_for_question(question)
        tool_results: list[dict[str, Any]] = []
        tool_views: list[dict[str, Any]] = []
        for name in tool_names:
            args = self._default_args_for_tool(name, question)
            result = self.registry.execute(name, args, run_id=run_id)
            tool_results.append(result)
            tool_views.append(self._tool_view(result, args))
            self._record_step(
                run_id,
                steps,
                "tool",
                json.dumps(result, ensure_ascii=False, default=str),
                tool_name=name,
                args=args,
                result=result,
            )
            if result.get("permission_required"):
                break

        permission_result = self._first_permission_result(tool_results)
        answer = (
            self._permission_answer(permission_result)
            if permission_result
            else self._answer_from_tools(tool_results, question=question)
        )
        return self._verified_response(
            question=question,
            answer=answer,
            plan=plan,
            tool_results=tool_results,
            used_llm=False,
            fallback_used=False,
            tool_views=tool_views,
            pending_action=(
                permission_result.get("pending_action")
                or permission_result.get("permission_request")
                if permission_result
                else None
            ),
            permission_requests=[
                permission_result["permission_request"]
            ]
            if permission_result and isinstance(permission_result.get("permission_request"), dict)
            else [],
            steps=steps,
            memory_used=memory_used,
        )

    def _fallback(
        self,
        *,
        question: str,
        plan: str,
        run_id: int,
        steps: list[dict[str, Any]],
        memory_used: dict[str, Any],
        reason: str,
    ) -> dict[str, Any]:
        if self._is_forbidden_l2_request(question):
            answer = (
                "LLM 不可用或无效, 已使用规则 fallback。\n"
                "我不能执行这个请求。它触及 L2 高风险边界: forceenter/forceexit、"
                "关闭 dry_run、真实下单、改策略、改交易配置、shell/docker、交易所密钥"
                "或删除交易数据都不会提供工具入口。\n\n"
                f"Fallback reason: {reason}"
            )
            return self._verified_response(
                question=question,
                answer=answer,
                plan=plan,
                tool_results=[],
                used_llm=False,
                fallback_used=True,
                permission_requests=[],
                steps=steps,
                memory_used=memory_used,
                llm_error=reason,
            )

        lowered = question.lower()
        tool_names = self._required_tools_for_question(question)
        if tool_names:
            pass
        elif any(
            term in lowered
            for term in ["状态", "status", "持仓", "open trade", "open trades"]
        ):
            tool_names = ["ft_health", "ft_status"]
        elif any(term in lowered for term in ["收益", "profit", "亏损"]):
            tool_names = ["ft_profit"]
        elif any(term in lowered for term in ["余额", "balance", "钱包"]):
            tool_names = ["ft_balance"]
        elif any(term in lowered for term in ["日志", "logs", "log", "报错", "error"]):
            tool_names = ["ft_logs"]
        elif any(term in lowered for term in ["配置", "config", "策略"]):
            tool_names = ["ft_show_config_sanitized"]
        else:
            answer = (
                "LLM 不可用或没有返回有效工具调用, 已使用规则 fallback。\n"
                "当前仅支持状态、收益、余额、日志、配置查询。\n"
                f"Fallback reason: {reason}"
            )
            return self._verified_response(
                question=question,
                answer=answer,
                plan=plan,
                tool_results=[],
                used_llm=False,
                fallback_used=True,
                permission_requests=[],
                steps=steps,
                memory_used=memory_used,
                llm_error=reason,
            )

        tool_results = []
        for name in tool_names:
            args = self._default_args_for_tool(name, question)
            result = self.registry.execute(name, args, run_id=run_id)
            tool_results.append(result)
            self._record_step(
                run_id,
                steps,
                "tool",
                json.dumps(result, ensure_ascii=False, default=str),
                tool_name=name,
                args=args,
                result=result,
            )
        answer = self._answer_from_tools(tool_results, question=question)
        return self._verified_response(
            question=question,
            answer=f"{answer}\n\nLLM 不可用或无效, 已使用规则 fallback。",
            plan=plan,
            tool_results=tool_results,
            used_llm=False,
            fallback_used=True,
            permission_requests=[],
            steps=steps,
            memory_used=memory_used,
            llm_error=reason,
        )

    def _is_forbidden_l2_request(self, question: str) -> bool:
        lowered = question.lower()
        forbidden_terms = [
            "forceenter",
            "force enter",
            "force_entry",
            "forcebuy",
            "force buy",
            "强制买",
            "强制买入",
            "forceexit",
            "force exit",
            "force_exit",
            "forcesell",
            "force sell",
            "强制卖",
            "强制卖出",
            "关 dry_run",
            "关闭 dry_run",
            "dry_run=false",
            "dry_run false",
            "实盘",
            "live trading",
            "改策略",
            "修改策略",
            "改配置",
            "修改配置",
            "stake_amount",
            "leverage",
            "trading_mode",
            "api key",
            "api secret",
            "shell",
            "docker",
            "delete trade",
            "删除交易",
            "cancel order",
            "撤单",
        ]
        return any(term in lowered for term in forbidden_terms)

    def _verified_response(
        self,
        *,
        question: str,
        answer: str,
        plan: str,
        tool_results: list[dict[str, Any]],
        used_llm: bool,
        fallback_used: bool,
        tool_views: list[dict[str, Any]] | None = None,
        pending_action: dict[str, Any] | None = None,
        permission_requests: list[dict[str, Any]] | None = None,
        steps: list[dict[str, Any]] | None = None,
        memory_used: dict[str, Any] | None = None,
        llm_error: str | None = None,
    ) -> dict[str, Any]:
        pending_action = pending_action or self._extract_pending_action(tool_results)
        permission_requests = permission_requests or [
            result["permission_request"]
            for result in tool_results
            if isinstance(result.get("permission_request"), dict)
        ]
        verification = self.verifier.verify(
            question=question,
            answer=answer,
            tool_results=tool_results,
            pending_action=pending_action,
        )
        views = (
            tool_views
            if tool_views is not None
            else [self._tool_view(item, {}) for item in tool_results]
        )
        return {
            "answer": verification.answer,
            "summary": verification.answer,
            "plan": plan,
            "tool_calls": views,
            "pending_action": pending_action,
            "permission_requests": permission_requests,
            "used_llm": used_llm,
            "fallback_used": fallback_used,
            "steps": steps or [],
            "llm_error": llm_error,
            "memory_used": memory_used or {},
            "memory_hits": self._memory_hits(tool_results),
            "verifier": {"ok": verification.ok, "issues": verification.issues},
        }

    def _answer_from_tools(
        self,
        tool_results: list[dict[str, Any]],
        *,
        question: str = "",
    ) -> str:
        if not tool_results:
            return "没有可用工具结果, 不能回答当前 Freqtrade 状态。"
        successful = [result for result in tool_results if result.get("success")]
        failed = [result for result in tool_results if not result.get("success")]
        source_lines = [
            "- {name}: {status}, {latency}ms".format(
                name=result.get("tool_name", "unknown"),
                status="success" if result.get("success") else "failed",
                latency=result.get("latency_ms", "n/a"),
            )
            for result in tool_results
        ]
        metric_lines = [
            f"- {result.get('summary') or result}" for result in successful[:8]
        ]
        failure_lines = [
            f"- {result.get('tool_name')}: {result.get('summary') or result.get('error')}"
            for result in failed[:5]
        ]
        conclusion = self._conclusion_from_tools(question, successful, failed)
        lines = [
            "根据工具结果:",
            "结论",
            f"- {conclusion}",
            "",
            "数据来源工具",
            *source_lines,
            "",
            "关键指标 / 工具返回的事实",
            *(metric_lines or ["- 没有成功的工具结果。"]),
            "",
            "基于事实的推测",
            f"- {self._inference_from_tools(question, successful)}",
        ]
        if failure_lines:
            lines.extend(["", "不确定信息 / 工具失败", *failure_lines])
        else:
            lines.extend(
                [
                    "",
                    "不确定信息",
                    "- 未调用到的外部行情、链上数据或宏观数据不在本轮结论内。",
                ]
            )
        lines.extend(
            [
                "",
                "风险提示",
                "- 当前回答只基于本轮工具结果, 不构成投资建议。",
                "- Agent 未执行任何交易, 未修改策略, 未关闭 dry-run。",
                "",
                "可继续追问",
                "- 为什么没有开仓?",
                "- 最近有没有报错?",
                "- 当前配置和 strategy/timeframe/stake 是什么?",
            ]
        )
        return "\n".join(lines)

    def _conclusion_from_tools(
        self,
        question: str,
        successful: list[dict[str, Any]],
        failed: list[dict[str, Any]],
    ) -> str:
        if not successful:
            return "本轮没有拿到成功工具结果, 不能编造当前状态。"
        summaries = " ".join(str(result.get("summary") or "") for result in successful)
        lowered = question.lower()
        if any(term in lowered for term in ["状态", "status", "健康", "health"]):
            if "当前没有 open trades" in summaries:
                return "机器人健康/状态查询已完成, 当前没有 open trades。"
            return "机器人健康/状态查询已完成, 具体状态见关键指标。"
        if any(term in lowered for term in ["收益", "profit", "亏损"]):
            return "收益查询已完成, profit/drawdown/winrate 以工具结果为准。"
        if any(term in lowered for term in ["余额", "balance", "钱包"]):
            return "dry-run 钱包余额查询已完成, balance 以工具结果为准。"
        if any(term in lowered for term in ["日志", "logs", "报错", "error"]):
            return "日志查询已完成, 是否有异常以 ft_logs 摘要为准。"
        if any(term in lowered for term in ["配置", "config", "策略"]):
            return "配置摘要查询已完成, 敏感字段已脱敏。"
        if failed:
            return "已拿到部分工具事实, 但也有工具失败, 结论需要保守看待。"
        return "查询已完成, 以下是本轮工具返回的事实摘要。"

    def _inference_from_tools(self, question: str, successful: list[dict[str, Any]]) -> str:
        if not successful:
            return "没有成功工具结果, 不做推测。"
        lowered = question.lower()
        summaries = " ".join(str(result.get("summary") or "") for result in successful)
        if any(term in lowered for term in ["为什么没有开仓", "不开仓", "没有开仓"]):
            return (
                "若当前 open trades 为空, 常见原因包括策略信号未触发、pairlist 限制、"
                "风控/资金/最小下单限制或市场条件不足; 具体原因需要结合更长日志和策略信号。"
            )
        if "当前没有 open trades" in summaries:
            return "当前没有 open trades, 因此本轮没有持仓风险暴露。"
        if any(term in lowered for term in ["日志", "logs", "报错", "error"]):
            return "如果 ft_logs 未显示 ERROR/WARNING, 只能说明最近日志窗口未发现明显异常。"
        if any(term in lowered for term in ["收益", "profit", "亏损"]):
            return "profit 和 drawdown 反映 dry-run 运行表现, 不能外推为未来收益。"
        return "本轮推测仅基于上方工具事实, 没有使用未验证的外部信息。"

    def _first_permission_result(
        self,
        tool_results: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        for result in tool_results:
            if result.get("permission_required"):
                return result
        return None

    def _required_tools_for_question(self, question: str) -> list[str]:  # noqa: C901
        lowered = question.lower()
        tools: list[str] = []

        def add(*names: str) -> None:
            for name in names:
                if name not in tools and self.registry.get(name):
                    tools.append(name)

        if any(term in lowered for term in ["为什么没有开仓", "不开仓", "没有开仓"]):
            add("ft_health", "ft_status", "ft_logs", "ft_show_config_sanitized", "ft_whitelist")
        if any(term in lowered for term in ["状态", "status", "持仓", "open trade", "open trades"]):
            add("ft_health", "ft_status")
        if any(term in lowered for term in ["健康", "health", "运行正常"]):
            add("ft_health")
        if any(term in lowered for term in ["收益", "profit", "亏损"]):
            add("ft_profit")
        if any(term in lowered for term in ["余额", "balance", "钱包"]):
            add("ft_balance")
        if any(term in lowered for term in ["日志", "logs", "log", "报错", "error"]):
            add("ft_logs")
        if any(term in lowered for term in ["配置", "config", "策略", "timeframe", "stake"]):
            add("ft_show_config_sanitized")
        if any(term in lowered for term in ["最近交易", "交易记录", "recent trades"]):
            add("ft_trades_recent")
        if any(term in lowered for term in ["白名单", "whitelist", "交易对"]):
            add("ft_whitelist")
        if any(term in lowered for term in ["联网", "搜索", "新闻", "news"]):
            add("web_search")
        if any(
            term in lowered
            for term in [
                "行情",
                "价格",
                "ticker",
                "market",
                "btc",
                "bitcoin",
                "比特币",
                "eth",
                "ethereum",
                "以太",
            ]
        ):
            add("market_snapshot")
        if any(term in lowered for term in ["定时任务", "scheduler", "scheduled job", "jobs"]):
            add("scheduler_list")
        return tools

    def _should_use_fast_info_route(self, question: str, source: str) -> bool:
        if not self._required_tools_for_question(question):
            return False
        lowered = question.lower()
        if self._is_chart_question(lowered):
            return False
        if any(term in lowered for term in ["联网", "web_search", "web fetch", "web_fetch"]):
            return source == "telegram"
        if source == "telegram":
            return True
        fast_terms = [
            "状态",
            "status",
            "收益",
            "profit",
            "余额",
            "balance",
            "日志",
            "logs",
            "配置",
            "config",
            "持仓",
            "open trades",
            "open trade",
            "为什么没有开仓",
            "最近交易",
            "白名单",
            "定时任务",
        ]
        return any(term in lowered for term in fast_terms)

    def _is_chart_question(self, lowered: str) -> bool:
        chart_terms = [
            "图表",
            "画图",
            "发图",
            "发一张图",
            "k线",
            "k-line",
            "candlestick",
            "chart",
            "收益曲线",
            "回撤图",
            "png",
        ]
        return any(term in lowered for term in chart_terms)

    def _is_capability_question(self, question: str) -> bool:
        lowered = question.lower()
        capability_terms = [
            "你是谁",
            "介绍一下自己",
            "能操控什么工具",
            "能够操控什么工具",
            "能看到什么工具",
            "能够看到什么工具",
            "有哪些工具",
            "可用工具",
            "工具列表",
            "能做什么",
            "你能做什么",
            "能干什么",
            "能够干什么",
            "可以干什么",
            "有什么能力",
            "能帮我什么",
            "能操作什么",
            "能够操作什么",
            "操控什么",
            "权限",
            "capabilities",
            "available tools",
            "what tools",
        ]
        return any(term in lowered for term in capability_terms)

    def _scheduler_action_from_text(self, question: str) -> tuple[str, dict[str, Any]] | None:
        lowered = question.lower()
        action_terms = ["触发", "运行", "生成", "手动", "run", "start"]
        disable_terms = ["禁用", "暂停定时", "disable"]
        enable_terms = ["启用", "恢复定时", "enable"]
        if not any(term in lowered for term in action_terms + disable_terms + enable_terms):
            return None

        job_name = self._scheduler_job_name_from_text(lowered)
        if not job_name:
            return None
        job_id = self._scheduled_job_id(job_name)
        if not job_id:
            return None
        if any(term in lowered for term in disable_terms):
            return "scheduler_disable", {"job_id": job_id}
        if any(term in lowered for term in enable_terms):
            return "scheduler_enable", {"job_id": job_id}
        return "scheduler_run_once", {"job_id": job_id}

    def _scheduler_job_name_from_text(self, lowered: str) -> str | None:
        if (
            "hourly_bot_health_check" in lowered
            or "health check" in lowered
            or "健康检查" in lowered
        ):
            return "hourly_bot_health_check"
        if "daily_trading_report" in lowered or "日报" in lowered or "daily report" in lowered:
            return "daily_trading_report"
        if "daily_profit_summary" in lowered or "收益摘要" in lowered:
            return "daily_profit_summary"
        if "daily_log_error_scan" in lowered or "日志错误" in lowered or "错误扫描" in lowered:
            return "daily_log_error_scan"
        if "daily_market_snapshot" in lowered or "行情摘要" in lowered or "市场快照" in lowered:
            return "daily_market_snapshot"
        if "scheduled_observation_save" in lowered or "定时保存观察" in lowered:
            return "scheduled_observation_save"
        return None

    def _scheduled_job_id(self, name: str) -> int | None:
        for job in self.db.scheduled_jobs():
            if job.get("name") == name:
                return int(job["id"])
        return None

    def _ensure_required_tools(
        self,
        *,
        required_tools: list[str],
        question: str,
        run_id: int,
        steps: list[dict[str, Any]],
        tool_results: list[dict[str, Any]],
        tool_views: list[dict[str, Any]],
    ) -> bool:
        existing = {str(result.get("tool_name")) for result in tool_results}
        added = False
        for name in required_tools:
            if name in existing:
                continue
            args = self._default_args_for_tool(name, question)
            result = self.registry.execute(name, args, run_id=run_id)
            tool_results.append(result)
            tool_views.append(self._tool_view(result, args))
            self._record_step(
                run_id,
                steps,
                "tool",
                json.dumps(result, ensure_ascii=False, default=str),
                tool_name=name,
                args=args,
                result=result,
            )
            existing.add(name)
            added = True
            if result.get("permission_required"):
                break
        return added

    def _default_args_for_tool(self, name: str, question: str = "") -> dict[str, Any]:
        if name == "ft_trades_recent":
            return {"limit": 10}
        if name == "web_search":
            return {"query": "crypto market news", "topic": "news", "time_range": "day"}
        if name == "market_snapshot":
            return {"pairs": self._market_pairs_from_question(question)}
        return {}

    def _market_pairs_from_question(self, question: str) -> list[str]:
        lowered = question.lower()
        pairs: list[str] = []
        if any(term in lowered for term in ["btc", "bitcoin", "比特币"]):
            pairs.append("BTC/USDT")
        if any(term in lowered for term in ["eth", "ethereum", "以太", "以太坊"]):
            pairs.append("ETH/USDT")
        if not pairs:
            pairs.append("BTC/USDT")
        return pairs

    def _permission_answer(self, result: dict[str, Any]) -> str:
        request = result.get("permission_request") or {}
        request_id = request.get("id", "unknown")
        tool_name = result.get("tool_name", "unknown")
        risk_notes = result.get("risk_notes") or request.get("risk_notes") or ""
        expires_at = request.get("expires_at", "unknown")
        return (
            "需要确认后才能继续。\n"
            f"- permission_request: #{request_id}\n"
            f"- tool: {tool_name}\n"
            f"- expires_at: {expires_at}\n"
            f"- risk: {risk_notes}\n"
            "确认前我不会执行这个工具。"
        )

    def _system_prompt(self) -> str:
        return (
            "你是本地 Freqtrade Trading Copilot。自然、简短、直接回答, 不用模板和 emoji。\n"
            "面向 Telegram 时使用纯文本: 不要 Markdown, 不要 **粗体**, 不要反引号代码块, 少用符号列表。\n"
            "交易/行情/新闻事实需要工具; 能力介绍不用查交易状态。\n"
            "需要工具就调用; 需要确认就返回 permission_required。\n"
            "拒绝真实下单、forceenter/forceexit、关闭 dry_run、改策略/config、shell/docker。"
        )

    def _memory_prompt(self, memory_used: dict[str, Any]) -> str:
        prompt = (
            "默认不要塞旧对话。需要历史/偏好/上次结论时调用 memory_recall; "
            "需要排查刚才为什么卡住、用了哪些工具、权限怎么走时调用 memory_search_behavior; "
            "用户明确说记住偏好时调用 memory_save_preference。"
        )
        short_term = memory_used.get("short_term_messages")
        if isinstance(short_term, list) and short_term:
            lines = [
                "短时 Telegram 上下文, 仅用于理解代词和续接上一轮, 不包含工具调用链:",
            ]
            for item in short_term[-20:]:
                if not isinstance(item, dict):
                    continue
                role = "用户" if item.get("role") == "user" else "Agent"
                content = str(item.get("content") or "").strip()
                if content:
                    lines.append(f"{role}: {content}")
            lines.append("如果用户说“发过来/发一下/那个/上面/继续”, 优先用这里最近一轮的对象、路径或意图。")
            prompt = prompt + "\n\n" + "\n".join(lines)
        return prompt

    def _plan(self, _question: str) -> str:
        return (
            "LLM 判断意图 -> 选择白名单工具 -> Registry 执行或生成 ask 权限请求 -> "
            "观察结果 -> verifier 检查 -> 中文回答。"
        )

    def _parse_tool_args(self, raw: Any) -> dict[str, Any]:
        if not raw:
            return {}
        if isinstance(raw, dict):
            return raw
        try:
            data = json.loads(str(raw))
        except json.JSONDecodeError as exc:
            raise LLMError(f"Invalid tool arguments JSON: {raw}") from exc
        if not isinstance(data, dict):
            raise LLMError("Tool arguments must be an object.")
        return data

    def _llm_tool_content(self, result: dict[str, Any]) -> str:
        compact: dict[str, Any] = {
            "tool_name": result.get("tool_name"),
            "success": result.get("success"),
            "summary": result.get("summary") or result.get("error"),
        }
        if result.get("permission_required"):
            compact["permission_required"] = True
            compact["permission_request"] = result.get("permission_request")
        data = result.get("data")
        if isinstance(data, dict):
            compact["data"] = self._compact_tool_data(data)
        elif isinstance(data, list):
            compact["data"] = data[:5]
        return json.dumps(compact, ensure_ascii=False, default=str)

    def _compact_tool_data(self, data: dict[str, Any]) -> dict[str, Any]:
        keys = [
            "dry_run",
            "runmode",
            "state",
            "strategy",
            "timeframe",
            "stake_currency",
            "stake_amount",
            "max_open_trades",
            "trade_count",
            "closed_trade_count",
            "profit_all_coin",
            "profit_all_percent",
            "winrate",
            "max_drawdown",
            "current",
            "max",
            "total",
            "total_bot",
            "starting_capital_pct",
            "whitelist",
            "length",
            "method",
            "log_count",
        ]
        compact = {key: data[key] for key in keys if key in data}
        if "tickers" in data and isinstance(data["tickers"], list):
            tickers = []
            for item in data["tickers"][:5]:
                if not isinstance(item, dict):
                    continue
                ticker = item.get("ticker")
                if not isinstance(ticker, dict):
                    continue
                tickers.append(
                    {
                        "pair": item.get("pair"),
                        "lastPrice": ticker.get("lastPrice"),
                        "priceChangePercent": ticker.get("priceChangePercent"),
                        "highPrice": ticker.get("highPrice"),
                        "lowPrice": ticker.get("lowPrice"),
                        "quoteVolume": ticker.get("quoteVolume"),
                    }
                )
            compact["tickers"] = tickers
        return compact

    def _implied_permission_tool(
        self,
        question: str,
        answer: str,
    ) -> tuple[str, dict[str, Any]] | None:
        lowered_question = question.lower()
        lowered_answer = answer.lower()
        search_terms = ["联网", "搜索", "web_search", "news", "新闻", "search"]
        permission_terms = ["permission", "确认", "授权", "需要确认", "ask"]
        if (
            any(term in lowered_question for term in search_terms)
            and any(term in lowered_answer for term in permission_terms)
            and self.registry.get("web_search")
        ):
            args: dict[str, Any] = {
                "query": self._search_query_from_question(question),
                "topic": (
                    "news"
                    if any(term in lowered_question for term in ["新闻", "news"])
                    else "general"
                ),
                "max_results": 5,
            }
            if any(term in lowered_question for term in ["今天", "today", "今日"]):
                args["time_range"] = "day"
            return "web_search", args
        return None

    def _search_query_from_question(self, question: str) -> str:
        query = question.strip()
        for token in ["联网搜索一下", "联网搜索", "搜索一下", "请搜索", "帮我搜索"]:
            query = query.replace(token, "")
        query = query.strip(" ,.")
        query = query.strip("\uFF0C\u3002")
        return query or question.strip()

    def _tool_view(self, result: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        return {
            "tool_name": str(result.get("tool_name") or "unknown"),
            "args": args,
            "success": bool(result.get("success")),
            "summary": str(result.get("summary") or ""),
            "permission_level": result.get("permission_level"),
            "permission": result.get("permission"),
            "permission_required": bool(result.get("permission_required")),
            "permission_request": result.get("permission_request"),
            "denied": bool(result.get("denied")),
            "latency_ms": result.get("latency_ms"),
        }

    def _extract_pending_action(self, tool_results: list[dict[str, Any]]) -> dict[str, Any] | None:
        for result in tool_results:
            pending = result.get("pending_action") or result.get("permission_request")
            if isinstance(pending, dict):
                return pending
        return None

    def _record_step(
        self,
        run_id: int,
        steps: list[dict[str, Any]],
        role: str,
        content: str,
        *,
        tool_name: str | None = None,
        args: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        permission_request = result.get("permission_request") if isinstance(result, dict) else None
        permission_request_id = (
            int(permission_request["id"])
            if isinstance(permission_request, dict) and permission_request.get("id")
            else None
        )
        step_index = len(steps) + 1
        step_id = self.db.save_run_step(
            run_id=run_id,
            step_index=step_index,
            role=role,
            content=content,
            tool_name=tool_name,
            args=args or {},
            result_summary=str(result.get("summary") or "") if result else None,
            success=bool(result.get("success")) if result else None,
            permission_request_id=permission_request_id,
        )
        steps.append(
            {
                "id": step_id,
                "step_index": step_index,
                "role": role,
                "content": content[:2000],
                "tool_name": tool_name,
                "summary": str(result.get("summary") or "") if result else None,
                "success": bool(result.get("success")) if result else None,
                "permission_request_id": permission_request_id,
            }
        )

    def _should_save_observation(self, question: str) -> bool:
        stripped = question.strip()
        lowered = stripped.lower()
        prefixes = ("记住", "请记住", "帮我记住", "保存观察", "保存一下", "remember ")
        if stripped.startswith(prefixes) or lowered.startswith(prefixes):
            return True
        return "保存观察:" in stripped or "保存观察\uFF1A" in stripped

    def _observation_text(self, question: str) -> str:
        text = question
        for prefix in ["记住", "保存观察", "remember"]:
            text = text.replace(prefix, "")
        return text.strip()[:500]

    def _save_conversation(
        self,
        source: str,
        user_id: str,
        chat_id: str,
        question: str,
        answer: str,
    ) -> None:
        self.db.save_conversation(
            source=source,
            user_id=user_id,
            chat_id=chat_id,
            question=question,
            answer=answer,
        )
        if source == "telegram":
            self.db.save_short_term_message(
                source=source,
                user_id=user_id,
                chat_id=chat_id,
                role="user",
                content=question,
            )
            self.db.save_short_term_message(
                source=source,
                user_id=user_id,
                chat_id=chat_id,
                role="assistant",
                content=answer,
            )

    def _has_tool_call(self, response: dict[str, Any], tool_name: str) -> bool:
        return any(
            isinstance(item, dict) and item.get("tool_name") == tool_name
            for item in response.get("tool_calls", [])
        )

    def ask_stream(  # noqa: C901 - streaming loop keeps OpenAI tool semantics in one place.
        self,
        *,
        question: str,
        source: str,
        user_id: str,
        chat_id: str,
    ) -> Generator[dict[str, Any], None, None]:
        question = question.strip()
        plan = self._plan(question)
        run_id = self.db.create_agent_run(
            source=source, user_id=user_id, chat_id=chat_id,
            question=question, plan=plan,
        )
        steps: list[dict[str, Any]] = []
        self._record_step(run_id, steps, "user", question)
        memory_used = self.db.recent_memory(source=source, user_id=user_id, chat_id=chat_id)
        llm_error: str | None = None

        if self._is_capability_question(question):
            response = self._capability_response(
                question=question,
                plan=plan,
                run_id=run_id,
                steps=steps,
                memory_used=memory_used,
            )
            yield {"type": "complete", "data": response, "run_id": run_id}
            self._finish_run(run_id, response, llm_error=None)
            return

        if source in {"scheduler", "monitor"} and self._should_use_fast_info_route(
            question,
            source,
        ):
            response = self._fast_info_response(
                question=question, plan=plan, run_id=run_id,
                steps=steps, memory_used=memory_used,
            )
            yield {"type": "complete", "data": response, "run_id": run_id}
            self._finish_run(run_id, response, llm_error=None)
            return

        if self._is_forbidden_l2_request(question):
            response = self._deny_high_risk(
                question=question, plan=plan, run_id=run_id,
                steps=steps, memory_used=memory_used,
            )
            yield {"type": "complete", "data": response, "run_id": run_id}
            self._finish_run(run_id, response, llm_error=None)
            return

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt()},
            {"role": "system", "content": self._memory_prompt(memory_used)},
            {"role": "user", "content": f"Plan: {plan}\n\nUser question: {question}"},
        ]

        tool_results: list[dict[str, Any]] = []
        tool_views: list[dict[str, Any]] = []
        permission_requests: list[dict[str, Any]] = []
        pending_action: dict[str, Any] | None = None

        for _iteration in range(self.settings.agent_max_steps):
            try:
                stream = self.llm.chat_stream(
                    messages=messages,
                    tools=self.registry.openai_tools(),
                )
            except LLMError as exc:
                llm_error = str(exc)
                if tool_results:
                    answer = self._answer_from_tools(tool_results, question=question)
                    answer += "\n\nLLM 在观察工具结果后超时, 已基于已取得的工具结果给出保守总结。"
                    response = self._verified_response(
                        question=question, answer=answer, plan=plan,
                        tool_results=tool_results, used_llm=True,
                        fallback_used=False, tool_views=tool_views,
                        pending_action=pending_action,
                        permission_requests=permission_requests,
                        steps=steps, memory_used=memory_used,
                        llm_error=llm_error,
                    )
                    yield {"type": "complete", "data": response, "run_id": run_id}
                    self._finish_run(run_id, response, llm_error)
                    return
                response = self._fallback(
                    question=question, plan=plan, run_id=run_id,
                    steps=steps, memory_used=memory_used, reason=llm_error,
                )
                yield {"type": "complete", "data": response, "run_id": run_id}
                self._finish_run(run_id, response, llm_error)
                return

            text_chunks: list[str] = []
            tool_calls: list[dict[str, Any]] | None = None

            try:
                for event in stream:
                    if event["type"] == "text_delta":
                        text_chunks.append(event["content"])
                        yield {
                            "type": "text_delta",
                            "content": event["content"],
                            "run_id": run_id,
                        }
                    elif event["type"] == "tool_calls":
                        tool_calls = event["tool_calls"]
            except LLMError as exc:
                llm_error = str(exc)
                if tool_results:
                    answer = self._answer_from_tools(tool_results, question=question)
                    answer += (
                        "\n\nLLM stream 在观察工具结果后中断, "
                        "已基于已取得的工具结果给出保守总结。"
                    )
                    response = self._verified_response(
                        question=question, answer=answer, plan=plan,
                        tool_results=tool_results, used_llm=True,
                        fallback_used=False, tool_views=tool_views,
                        pending_action=pending_action,
                        permission_requests=permission_requests,
                        steps=steps, memory_used=memory_used,
                        llm_error=llm_error,
                    )
                    yield {"type": "complete", "data": response, "run_id": run_id}
                    self._finish_run(run_id, response, llm_error)
                    return
                response = self._fallback(
                    question=question, plan=plan, run_id=run_id,
                    steps=steps, memory_used=memory_used, reason=llm_error,
                )
                yield {"type": "complete", "data": response, "run_id": run_id}
                self._finish_run(run_id, response, llm_error)
                return

            full_text = "".join(text_chunks)
            assistant_msg: dict[str, Any] = {"role": "assistant"}
            if full_text:
                assistant_msg["content"] = full_text
            self._record_step(run_id, steps, "assistant",
                              json.dumps(assistant_msg, ensure_ascii=False, default=str))

            if not tool_calls:
                answer = full_text.strip()
                if not answer:
                    llm_error = "LLM returned no answer and no tool call."
                    response = self._fallback(
                        question=question, plan=plan, run_id=run_id,
                        steps=steps, memory_used=memory_used, reason=llm_error,
                    )
                    yield {"type": "complete", "data": response, "run_id": run_id}
                    self._finish_run(run_id, response, llm_error)
                    return

                implied_tool = self._implied_permission_tool(question, answer)
                if implied_tool:
                    name, args = implied_tool
                    result = self.registry.execute(name, args, run_id=run_id)
                    tool_results.append(result)
                    tool_views.append(self._tool_view(result, args))
                    pr = result.get("permission_request")
                    if isinstance(pr, dict):
                        permission_requests.append(pr)
                        pending_action = pr
                    self._record_step(run_id, steps, "tool",
                                      json.dumps(result, ensure_ascii=False, default=str),
                                      tool_name=name, args=args, result=result)
                    if result.get("permission_required"):
                        response = self._build_final_response(
                            question, plan, tool_results, tool_views,
                            pending_action, permission_requests, steps,
                            memory_used, self._permission_answer(result),
                            used_llm=True, llm_error=None,
                        )
                        yield {"type": "complete", "data": response, "run_id": run_id}
                        self._finish_run(run_id, response, llm_error=None)
                        return

                response = self._build_final_response(
                    question, plan, tool_results, tool_views,
                    pending_action, permission_requests, steps,
                    memory_used, answer, used_llm=True, llm_error=None,
                )
                yield {"type": "complete", "data": response, "run_id": run_id}
                self._finish_run(run_id, response, llm_error=None)
                return

            messages.append(assistant_msg)
            prepared_calls: list[dict[str, Any]] = []
            for call in tool_calls:
                function = call.get("function") or {}
                name = str(function.get("name") or "")
                args = self._parse_tool_args(function.get("arguments"))
                if not self.registry.get(name):
                    llm_error = f"LLM requested invalid tool: {name}"
                    response = self._fallback(
                        question=question, plan=plan, run_id=run_id,
                        steps=steps, memory_used=memory_used, reason=llm_error,
                    )
                    yield {"type": "complete", "data": response, "run_id": run_id}
                    self._finish_run(run_id, response, llm_error)
                    return
                prepared_calls.append({"call": call, "name": name, "args": args})

            for prepared in prepared_calls:
                yield {
                    "type": "tool_start",
                    "tool_name": prepared["name"],
                    "args": prepared["args"],
                    "run_id": run_id,
                }

            results = self.registry.execute_batch(prepared_calls, run_id=run_id)
            for prepared, result in zip(prepared_calls, results, strict=False):
                call = prepared["call"]
                name = prepared["name"]
                args = prepared["args"]
                tool_results.append(result)
                tool_views.append(self._tool_view(result, args))
                pr = result.get("permission_request")
                if isinstance(pr, dict):
                    permission_requests.append(pr)
                    pending_action = pr
                self._record_step(run_id, steps, "tool",
                                  json.dumps(result, ensure_ascii=False, default=str),
                                  tool_name=name, args=args, result=result)
                yield {
                    "type": "tool_result",
                    "tool_name": name,
                    "result": self._tool_view(result, args),
                    "run_id": run_id,
                }

                if result.get("permission_required"):
                    assistant_msg["tool_calls"] = tool_calls
                    self.db.save_run_state(
                        run_id,
                        json.dumps(messages, ensure_ascii=False, default=str),
                    )
                    answer = self._permission_answer(result)
                    response = self._build_final_response(
                        question, plan, tool_results, tool_views,
                        pending_action, permission_requests, steps,
                        memory_used, answer, used_llm=True, llm_error=None,
                    )
                    yield {"type": "permission_required", "data": response, "run_id": run_id}
                    self._finish_run(run_id, response, llm_error=None)
                    return

                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", name),
                    "name": name,
                    "content": self._llm_tool_content(result),
                })

        answer = self._answer_from_tools(tool_results, question=question)
        response = self._build_final_response(
            question, plan, tool_results, tool_views,
            pending_action, permission_requests, steps,
            memory_used, answer, used_llm=True, llm_error=None,
        )
        yield {"type": "complete", "data": response, "run_id": run_id}
        self._finish_run(run_id, response, llm_error=None)

    def resume(  # noqa: C901 - mirrors the streaming tool-call loop after permission resume.
        self,
        *,
        run_id: int,
        question: str,
        source: str,
        user_id: str,
        chat_id: str,
    ) -> Generator[dict[str, Any], None, None]:
        state_json = self.db.load_run_state(run_id)
        if not state_json:
            yield {
                "type": "error",
                "error": f"run #{run_id} has no saved state",
                "run_id": run_id,
            }
            return

        try:
            messages: list[dict[str, Any]] = json.loads(state_json)
        except json.JSONDecodeError:
            yield {"type": "error", "error": f"run #{run_id} state corrupt", "run_id": run_id}
            return
        confirmed_results = self._append_confirmed_permission_observations(run_id, messages)
        approved_tools = self._run_scoped_approved_tools(confirmed_results)
        executed_tool_cache = self._seed_executed_tool_cache(confirmed_results)
        if approved_tools:
            messages.append(
                {
                    "role": "system",
                    "content": self._resume_permission_context(
                        approved_tools=approved_tools,
                    ),
                }
            )

        steps: list[dict[str, Any]] = []
        memory_used = self.db.recent_memory(source=source, user_id=user_id, chat_id=chat_id)
        tool_results: list[dict[str, Any]] = list(confirmed_results)
        tool_views: list[dict[str, Any]] = [
            self._tool_view(result, {}) for result in confirmed_results
        ]
        permission_requests: list[dict[str, Any]] = []
        pending_action: dict[str, Any] | None = None
        plan = self._plan(question)
        llm_error: str | None = None

        for _iteration in range(self.settings.agent_max_steps):
            try:
                stream = self.llm.chat_stream(
                    messages=messages,
                    tools=self.registry.openai_tools(),
                )
            except LLMError as exc:
                llm_error = str(exc)
                response = self._fallback(
                    question=question, plan=plan, run_id=run_id,
                    steps=steps, memory_used=memory_used, reason=llm_error,
                )
                yield {"type": "complete", "data": response, "run_id": run_id}
                self._finish_run(run_id, response, llm_error)
                return

            text_chunks: list[str] = []
            tool_calls: list[dict[str, Any]] | None = None

            try:
                for event in stream:
                    if event["type"] == "text_delta":
                        text_chunks.append(event["content"])
                        yield {
                            "type": "text_delta",
                            "content": event["content"],
                            "run_id": run_id,
                        }
                    elif event["type"] == "tool_calls":
                        tool_calls = event["tool_calls"]
            except LLMError as exc:
                llm_error = str(exc)
                response = self._fallback(
                    question=question, plan=plan, run_id=run_id,
                    steps=steps, memory_used=memory_used, reason=llm_error,
                )
                yield {"type": "complete", "data": response, "run_id": run_id}
                self._finish_run(run_id, response, llm_error)
                return

            full_text = "".join(text_chunks)
            assistant_msg: dict[str, Any] = {"role": "assistant"}
            if full_text:
                assistant_msg["content"] = full_text
            self._record_step(run_id, steps, "assistant",
                              json.dumps(assistant_msg, ensure_ascii=False, default=str))

            if not tool_calls:
                answer = full_text.strip()
                if not answer or answer in {"根据工具结果", "根据工具结果:"}:
                    answer = self._answer_from_tools(tool_results, question=question)
                response = self._build_final_response(
                    question, plan, tool_results, tool_views,
                    pending_action, permission_requests, steps,
                    memory_used, answer, used_llm=True, llm_error=None,
                )
                yield {"type": "complete", "data": response, "run_id": run_id}
                self._finish_run(run_id, response, llm_error=None)
                return

            messages.append(assistant_msg)
            prepared_calls: list[dict[str, Any]] = []
            prepared_results: list[dict[str, Any] | None] = []
            batch_calls: list[dict[str, Any]] = []
            batch_indexes: list[int] = []
            for call in tool_calls:
                function = call.get("function") or {}
                name = str(function.get("name") or "")
                args = self._parse_tool_args(function.get("arguments"))
                if not self.registry.get(name):
                    break

                cache_key = self._tool_cache_key(name, args)
                cached_result = executed_tool_cache.get(cache_key)
                prepared = {"call": call, "name": name, "args": args}
                prepared_calls.append(prepared)
                if cached_result is not None:
                    prepared_results.append(self._reused_tool_result(cached_result))
                else:
                    prepared_results.append(None)
                    batch_indexes.append(len(prepared_calls) - 1)
                    batch_calls.append(prepared)

            for prepared in prepared_calls:
                yield {
                    "type": "tool_start",
                    "tool_name": prepared["name"],
                    "args": prepared["args"],
                    "run_id": run_id,
                }

            batch_results = self.registry.execute_batch(
                batch_calls,
                run_id=run_id,
                force_tools=approved_tools,
            )
            for index, result in zip(batch_indexes, batch_results, strict=False):
                prepared_results[index] = result
                if result.get("success"):
                    prepared = prepared_calls[index]
                    executed_tool_cache[
                        self._tool_cache_key(prepared["name"], prepared["args"])
                    ] = dict(result)

            for prepared, result in zip(prepared_calls, prepared_results, strict=False):
                if result is None:
                    result = {
                        "success": False,
                        "tool_name": prepared["name"],
                        "summary": "工具未返回结果。",
                    }
                call = prepared["call"]
                name = prepared["name"]
                args = prepared["args"]
                tool_results.append(result)
                tool_views.append(self._tool_view(result, args))
                pr = result.get("permission_request")
                if isinstance(pr, dict):
                    permission_requests.append(pr)
                    pending_action = pr
                self._record_step(run_id, steps, "tool",
                                  json.dumps(result, ensure_ascii=False, default=str),
                                  tool_name=name, args=args, result=result)
                yield {
                    "type": "tool_result",
                    "tool_name": name,
                    "result": self._tool_view(result, args),
                    "run_id": run_id,
                }

                if result.get("permission_required"):
                    assistant_msg["tool_calls"] = tool_calls
                    self.db.save_run_state(
                        run_id,
                        json.dumps(messages, ensure_ascii=False, default=str),
                    )
                    answer = self._permission_answer(result)
                    response = self._build_final_response(
                        question, plan, tool_results, tool_views,
                        pending_action, permission_requests, steps,
                        memory_used, answer, used_llm=True, llm_error=None,
                    )
                    yield {"type": "permission_required", "data": response, "run_id": run_id}
                    self._finish_run(run_id, response, llm_error=None)
                    return

                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", name),
                    "name": name,
                    "content": self._llm_tool_content(result),
                })

        answer = self._answer_from_tools(tool_results, question=question)
        response = self._build_final_response(
            question, plan, tool_results, tool_views,
            pending_action, permission_requests, steps,
            memory_used, answer, used_llm=True, llm_error=None,
        )
        yield {"type": "complete", "data": response, "run_id": run_id}
        self._finish_run(run_id, response, llm_error=None)

    def _append_confirmed_permission_observations(
        self,
        run_id: int,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        existing_tool_call_ids = {
            str(message.get("tool_call_id"))
            for message in messages
            if message.get("role") == "tool" and message.get("tool_call_id")
        }
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, tool_name, args_json_sanitized, result_summary, executed, status
                FROM permission_requests
                WHERE run_id = ? AND status = 'confirmed'
                ORDER BY id
                """,
                (run_id,),
            ).fetchall()

        changed = False
        confirmed_results: list[dict[str, Any]] = []
        for row in rows:
            tool_name = str(row["tool_name"])
            args = self._safe_json_object(row["args_json_sanitized"] or "{}")
            content = {
                "tool_name": tool_name,
                "success": bool(row["executed"]),
                "args": args,
                "summary": row["result_summary"] or "permission confirmed.",
                "permission_request_id": row["id"],
            }
            result = {
                "success": bool(row["executed"]),
                "tool_name": tool_name,
                "args": args,
                "summary": str(content["summary"]),
                "permission": "allow",
                "permission_request": {"id": row["id"], "run_id": run_id},
            }
            confirmed_results.append(result)
            tool_call = self._find_unobserved_tool_call(
                messages,
                tool_name=tool_name,
                existing_tool_call_ids=existing_tool_call_ids,
            )
            if not tool_call:
                continue
            tool_call_id = str(tool_call.get("id") or tool_name)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "name": tool_name,
                    "content": json.dumps(content, ensure_ascii=False, default=str),
                }
            )
            existing_tool_call_ids.add(tool_call_id)
            changed = True

        if changed:
            self.db.save_run_state(run_id, json.dumps(messages, ensure_ascii=False, default=str))
        return confirmed_results

    def _run_scoped_approved_tools(self, confirmed_results: list[dict[str, Any]]) -> set[str]:
        approved = {
            str(result.get("tool_name"))
            for result in confirmed_results
            if result.get("success") and result.get("tool_name")
        }
        for group_tools in RUN_SCOPED_APPROVAL_GROUPS.values():
            if approved.intersection(group_tools):
                approved.update(group_tools)
        return approved

    def _resume_permission_context(
        self,
        *,
        approved_tools: set[str],
    ) -> str:
        approved = ", ".join(sorted(approved_tools))
        approved_groups = ", ".join(self._approved_group_names(approved_tools)) or "custom"
        return (
            "Permission resume context: 用户已经批准本轮 run 内的相关工具调用。"
            f"已批准的 run-scoped 工具组: {approved_groups}。"
            f"当前 run 内已临时允许的工具: {approved}。"
            "同一工具组内的后续工具可继续执行, 不要再次请求同类授权; "
            "工具结果足够时必须停止调用工具并用中文总结。"
            "优先基于 web_search 返回的 title/content/url/published_date 回答; "
            "只有必须核对原文细节时才调用 web_fetch。"
            "如果是 monitor/scheduler/Freqtrade L1 控制类工具, 也使用同一套本轮授权续跑流程。"
            "未注册工具、L2 高风险工具、关闭 dry_run、实盘下单、改策略和 shell/docker 仍不可用。"
            "所有结论必须区分工具事实和基于事实的推测, 并说明未执行任何交易。"
        )

    def _approved_group_names(self, approved_tools: set[str]) -> list[str]:
        return [
            group_name
            for group_name, group_tools in RUN_SCOPED_APPROVAL_GROUPS.items()
            if approved_tools.intersection(group_tools)
        ]

    def _seed_executed_tool_cache(
        self,
        confirmed_results: list[dict[str, Any]],
    ) -> dict[tuple[str, str], dict[str, Any]]:
        cache: dict[tuple[str, str], dict[str, Any]] = {}
        for result in confirmed_results:
            if not result.get("success") or not result.get("tool_name"):
                continue
            args = result.get("args")
            if not isinstance(args, dict):
                args = {}
            cache[self._tool_cache_key(str(result["tool_name"]), args)] = dict(result)
        return cache

    def _tool_cache_key(self, tool_name: str, args: dict[str, Any]) -> tuple[str, str]:
        return (
            tool_name,
            json.dumps(args or {}, ensure_ascii=False, sort_keys=True, default=str),
        )

    def _safe_json_object(self, raw: str) -> dict[str, Any]:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _reused_tool_result(self, result: dict[str, Any]) -> dict[str, Any]:
        reused = dict(result)
        reused["reused"] = True
        reused["latency_ms"] = 0.0
        summary = str(reused.get("summary") or "工具结果已复用。")
        if "复用" not in summary:
            reused["summary"] = f"复用同一 run 内已执行的工具结果: {summary}"
        return reused

    def _find_unobserved_tool_call(
        self,
        messages: list[dict[str, Any]],
        *,
        tool_name: str,
        existing_tool_call_ids: set[str],
    ) -> dict[str, Any] | None:
        for message in reversed(messages):
            if message.get("role") != "assistant":
                continue
            tool_calls = message.get("tool_calls")
            if not isinstance(tool_calls, list):
                continue
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") or {}
                if str(function.get("name") or "") != tool_name:
                    continue
                tool_call_id = str(call.get("id") or tool_name)
                if tool_call_id not in existing_tool_call_ids:
                    return call
        return None

    def _build_final_response(
        self,
        question: str,
        plan: str,
        tool_results: list[dict[str, Any]],
        tool_views: list[dict[str, Any]],
        pending_action: dict[str, Any] | None,
        permission_requests: list[dict[str, Any]],
        steps: list[dict[str, Any]],
        memory_used: dict[str, Any],
        answer: str,
        *,
        used_llm: bool,
        llm_error: str | None,
    ) -> dict[str, Any]:
        return self._verified_response(
            question=question, answer=answer, plan=plan,
            tool_results=tool_results, used_llm=used_llm,
            fallback_used=False, tool_views=tool_views,
            pending_action=pending_action,
            permission_requests=permission_requests,
            steps=steps, memory_used=memory_used,
            llm_error=llm_error,
        )

    def _finish_run(
        self,
        run_id: int,
        response: dict[str, Any],
        llm_error: str | None,
    ) -> None:
        question = ""
        with self.db.connect() as conn:
            row = conn.execute("SELECT question FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
            if row:
                question = row["question"]
        if self._should_save_observation(question) and not self._has_tool_call(
            response,
            "memory_save_observation",
        ):
            saved = self.registry.execute(
                "memory_save_observation",
                {"text": self._observation_text(question), "tags": ["user"], "importance": 2},
                run_id=run_id,
            )
            response["tool_calls"].append(self._tool_view(saved, {}))
        source = "api"
        user_id = "local"
        chat_id = "local"
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT source, user_id, chat_id FROM agent_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row:
                source = row["source"] or "api"
                user_id = row["user_id"] or "local"
                chat_id = row["chat_id"] or "local"
        self._save_conversation(source, user_id, chat_id, question, response["answer"])
        self.db.compact_conversations(source=source, user_id=user_id, chat_id=chat_id)
        self.db.finish_agent_run(
            run_id, answer=response["answer"],
            used_llm=bool(response.get("used_llm")),
            fallback_used=bool(response.get("fallback_used")),
            llm_error=llm_error or response.get("llm_error"),
        )
        behavior_record_id = self.db.record_behavior_from_run(run_id)
        response["run_id"] = run_id
        response["llm_error"] = llm_error or response.get("llm_error")
        response["memory_used"] = response.get("memory_used") or {}
        response["behavior_record_id"] = behavior_record_id

    def _memory_hits(self, tool_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        hits: list[dict[str, Any]] = []
        for result in tool_results:
            if result.get("tool_name") not in {"memory_recall", "memory_search_behavior"}:
                continue
            data = result.get("data") if isinstance(result.get("data"), dict) else {}
            if not isinstance(data, dict):
                continue
            items = data.get("memories") or data.get("behavior_records") or []
            if isinstance(items, list):
                hits.extend(items[:5])
        return hits[:10]
