from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


SENSITIVE_RE = re.compile(
    r"(password|passwd|token|secret|jwt_secret_key|ws_token|api[_ -]?key|api[_ -]?secret)",
    re.IGNORECASE,
)
PROFIT_GUARANTEE_RE = re.compile(r"(保证收益|稳赚|确定会涨|确定会跌|必涨|必跌)", re.IGNORECASE)
CURRENT_FACT_RE = re.compile(
    r"(余额|balance|持仓|open trades|收益|profit|亏损|日志|logs|配置|config|行情|market)",
    re.IGNORECASE,
)
L2_EXECUTED_RE = re.compile(
    r"(已强制买|已强制卖|已强平|已关闭 dry|已修改策略|已执行 shell|已执行 docker)",
    re.IGNORECASE,
)
L1_EXECUTED_RE = re.compile(
    r"(已暂停|已启动|已停止|已重载配置|已经暂停|已经启动)",
    re.IGNORECASE,
)


@dataclass
class VerificationResult:
    answer: str
    ok: bool
    issues: list[str] = field(default_factory=list)


class RuleVerifier:
    def verify(
        self,
        *,
        question: str,
        answer: str,
        tool_results: list[dict[str, Any]],
        pending_action: dict[str, Any] | None,
    ) -> VerificationResult:
        issues: list[str] = []
        safe_answer = answer

        if SENSITIVE_RE.search(safe_answer):
            safe_answer = SENSITIVE_RE.sub("[REDACTED]", safe_answer)
            issues.append("sensitive_terms_redacted")

        if L2_EXECUTED_RE.search(safe_answer):
            safe_answer = (
                "我不能执行 L2 高风险操作。本阶段 forceenter/forceexit/"
                "改策略/shell/docker 均被拒绝。"
            )
            issues.append("l2_execution_claim_replaced")

        if PROFIT_GUARANTEE_RE.search(safe_answer):
            safe_answer = safe_answer + "\n\n风险提示: 我不能保证收益, 也不能确定预测涨跌。"
            issues.append("profit_guarantee_warning_added")

        if "live" in safe_answer.lower() and self._tools_show_dry_run(tool_results):
            safe_answer = safe_answer.replace("live", "dry-run")
            issues.append("dry_run_live_misstatement_replaced")

        has_tool_result = any(result.get("success") for result in tool_results)
        if CURRENT_FACT_RE.search(question) and not has_tool_result and not pending_action:
            safe_answer = "我没有拿到工具结果, 不能编造当前状态/收益/余额/持仓/日志/配置。"
            issues.append("current_fact_without_tool_result")

        if L1_EXECUTED_RE.search(safe_answer) and pending_action:
            safe_answer = (
                "该请求属于 L1 低风险控制操作, 第一版只生成 pending_action, "
                "没有直接执行。请完成二次确认流程后再执行。"
            )
            issues.append("l1_execution_claim_replaced")

        return VerificationResult(answer=safe_answer, ok=not issues, issues=issues)

    def _tools_show_dry_run(self, tool_results: list[dict[str, Any]]) -> bool:
        for result in tool_results:
            data = result.get("data")
            if isinstance(data, dict) and data.get("dry_run") is True:
                return True
        return False
