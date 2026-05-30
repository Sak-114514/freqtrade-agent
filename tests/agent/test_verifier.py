from __future__ import annotations

from agent_platform.agents.verifier import RuleVerifier


class TestRuleVerifier:
    def setup_method(self) -> None:
        self.verifier = RuleVerifier()

    def test_sensitive_terms_redacted(self) -> None:
        result = self.verifier.verify(
            question="config?",
            answer="password is abc123 and token is xyz",
            tool_results=[{"success": True}],
            pending_action=None,
        )
        assert "[REDACTED]" in result.answer
        assert "sensitive_terms_redacted" in result.issues

    def test_l2_execution_claim_replaced(self) -> None:
        result = self.verifier.verify(
            question="forceenter BTC",
            answer="已强制买入 BTC/USDT",
            tool_results=[],
            pending_action=None,
        )
        assert "L2" in result.answer
        assert "l2_execution_claim_replaced" in result.issues

    def test_profit_guarantee_warning(self) -> None:
        result = self.verifier.verify(
            question="btc?",
            answer="保证收益, 稳赚不赔",
            tool_results=[{"success": True}],
            pending_action=None,
        )
        assert "风险提示" in result.answer
        assert "profit_guarantee_warning_added" in result.issues

    def test_dry_run_live_misstatement(self) -> None:
        result = self.verifier.verify(
            question="status?",
            answer="bot is running in live mode",
            tool_results=[{"success": True, "data": {"dry_run": True}}],
            pending_action=None,
        )
        assert "dry-run" in result.answer
        assert "live" not in result.answer.lower() or "dry-run" in result.answer

    def test_current_fact_without_tools(self) -> None:
        result = self.verifier.verify(
            question="当前余额是多少",
            answer="余额是 1000 USDT",
            tool_results=[],
            pending_action=None,
        )
        assert "不能编造" in result.answer
        assert "current_fact_without_tool_result" in result.issues

    def test_l1_execution_claim_with_pending(self) -> None:
        result = self.verifier.verify(
            question="暂停",
            answer="已暂停",
            tool_results=[],
            pending_action={"id": 1},
        )
        assert "pending_action" in result.answer or "pending" in result.answer.lower()
        assert "l1_execution_claim_replaced" in result.issues

    def test_tool_basis_prefix_not_added(self) -> None:
        result = self.verifier.verify(
            question="status?",
            answer="everything is fine",
            tool_results=[{"success": True}],
            pending_action=None,
        )
        assert result.answer == "everything is fine"
        assert result.ok is True

    def test_ok_when_all_good(self) -> None:
        result = self.verifier.verify(
            question="hello",
            answer="根据工具结果: hi there",
            tool_results=[{"success": True}],
            pending_action=None,
        )
        assert result.ok is True
        assert len(result.issues) == 0
