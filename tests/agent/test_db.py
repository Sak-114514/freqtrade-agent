from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from agent_platform.storage.db import AgentDB, sanitize_data


@pytest.fixture
def db(tmp_path: Path) -> AgentDB:
    return AgentDB(tmp_path / "test.sqlite")


class TestAgentDBPragma:
    def test_wal_mode_enabled(self, db: AgentDB) -> None:
        with db.connect() as conn:
            row = conn.execute("PRAGMA journal_mode").fetchone()
        assert row[0] in ("wal",), f"expected WAL, got {row[0]}"

    def test_busy_timeout_set(self, db: AgentDB) -> None:
        with db.connect() as conn:
            row = conn.execute("PRAGMA busy_timeout").fetchone()
        assert row[0] == 5000, f"expected 5000, got {row[0]}"

    def test_synchronous_normal(self, db: AgentDB) -> None:
        with db.connect() as conn:
            row = conn.execute("PRAGMA synchronous").fetchone()
        assert row[0] in (1, "NORMAL", 2), f"expected NORMAL(1), got {row[0]}"


class TestAgentDBOperations:
    def test_save_and_get_conversation(self, db: AgentDB) -> None:
        cid = db.save_conversation(
            source="test", user_id="u1", chat_id="c1",
            question="hello", answer="world",
        )
        assert cid > 0
        mem = db.recent_memory()
        convos = mem["recent_conversations"]
        assert len(convos) >= 1
        assert convos[0]["q"] == "hello"

    def test_short_term_telegram_messages_keep_last_20_by_chat(self, db: AgentDB) -> None:
        for idx in range(25):
            db.save_short_term_message(
                source="telegram",
                user_id="u1",
                chat_id="c1",
                role="user" if idx % 2 == 0 else "assistant",
                content=f"message {idx}",
            )
        db.save_short_term_message(
            source="telegram",
            user_id="u1",
            chat_id="other",
            role="user",
            content="other chat",
        )

        rows = db.recent_short_term_messages(
            source="telegram",
            user_id="u1",
            chat_id="c1",
            limit=25,
        )
        assert len(rows) == 20
        assert rows[0]["content"] == "message 5"
        assert rows[-1]["content"] == "message 24"
        assert all(row["chat_id"] == "c1" for row in rows)

    def test_recent_memory_includes_telegram_short_term_context(self, db: AgentDB) -> None:
        db.save_short_term_message(
            source="telegram",
            user_id="u1",
            chat_id="c1",
            role="assistant",
            content="图表路径: /freqtrade/user_data/agent_charts/example.png",
        )

        memory = db.recent_memory(source="telegram", user_id="u1", chat_id="c1")

        assert memory["short_term_messages"][0]["role"] == "assistant"
        assert "example.png" in memory["short_term_messages"][0]["content"]

    def test_save_observation(self, db: AgentDB) -> None:
        oid = db.save_observation("test obs", tags=["unit"], importance=3)
        assert oid > 0
        obs = db.recent_observations()
        assert len(obs) >= 1
        assert obs[0]["text"] == "test obs"

    def test_agent_run_lifecycle(self, db: AgentDB) -> None:
        run_id = db.create_agent_run(
            source="test", user_id="u1", chat_id="c1",
            question="status?", plan="plan A",
        )
        assert run_id > 0
        db.finish_agent_run(run_id, answer="ok", used_llm=True, fallback_used=False, llm_error=None)
        run = db.get_run(run_id)
        assert run is not None
        assert run["run"]["question"] == "status?"
        assert run["run"]["used_llm"] == 1

    def test_permission_request_flow(self, db: AgentDB) -> None:
        req = db.create_permission_request(
            run_id=None, tool_name="ft_pause",
            args={"reason": "test"}, confirmation_text="confirm?",
            risk_notes="low risk",
        )
        assert req["status"] == "pending"
        req_id = req["id"]
        pending = db.pending_permission_requests()
        assert any(r["id"] == req_id for r in pending)
        db.update_permission_request(
            req_id,
            status="confirmed",
            executed=True,
            result_summary="done",
        )
        updated = db.get_permission_request(req_id)
        assert updated["status"] == "confirmed"

    def test_expire_permission_requests(self, db: AgentDB) -> None:
        req = db.create_permission_request(
            run_id=None, tool_name="web_search",
            args={"query": "btc"}, confirmation_text="confirm?",
            risk_notes="external web",
        )
        req_id = req["id"]
        future = datetime.now(UTC) + timedelta(minutes=11)
        expired = db.expire_permission_requests(now=future)
        assert expired == 1
        assert db.get_permission_request(req_id)["status"] == "expired"
        assert all(row["id"] != req_id for row in db.pending_permission_requests())

    def test_tool_call_audit(self, db: AgentDB) -> None:
        db.save_tool_call(
            tool_name="ft_health", args={}, result_summary="ok",
            success=True, error=None, latency_ms=42.5,
        )
        with db.connect() as conn:
            row = conn.execute("SELECT * FROM tool_calls WHERE tool_name = 'ft_health'").fetchone()
        assert row is not None
        assert row["latency_ms"] == 42.5

    def test_compact_conversations(self, db: AgentDB) -> None:
        for i in range(30):
            db.save_conversation(
                source="test", user_id="u1", chat_id="c1",
                question=f"q{i}", answer=f"a{i}",
            )
        db.compact_conversations(
            source="test",
            user_id="u1",
            chat_id="c1",
            keep_last=5,
            compact_batch=20,
        )
        summaries = db.recent_session_summaries(limit=5)
        assert len(summaries) >= 1

    def test_composite_memory_searches_profile_and_observations(self, db: AgentDB) -> None:
        db.save_observation("用户喜欢 dry-run 学习模式", tags=["preference"], importance=3)
        memory_id = db.save_composite_memory(
            memory_type="profile",
            text="用户偏好中文简短回答",
            tags=["preference"],
            importance=4,
        )

        hits = db.search_memory("偏好 中文", memory_types=["profile"], limit=5)

        assert any(hit["id"] == memory_id for hit in hits)
        assert all(hit["memory_type"] == "profile" for hit in hits)

    def test_behavior_record_from_run_is_searchable(self, db: AgentDB) -> None:
        run_id = db.create_agent_run(
            source="telegram",
            user_id="u1",
            chat_id="c1",
            question="查询新闻分析市场",
            plan="plan",
        )
        db.save_run_step(
            run_id=run_id,
            step_index=1,
            role="tool",
            content="web_search done",
            tool_name="web_search",
            args={"query": "bitcoin"},
            result_summary="返回 5 条新闻",
            success=True,
        )
        db.finish_agent_run(
            run_id,
            answer="基于新闻给出分析。",
            used_llm=True,
            fallback_used=False,
            llm_error=None,
        )

        record_id = db.record_behavior_from_run(run_id)
        records = db.behavior_records("新闻 工具", limit=5)
        hits = db.search_memory("新闻 工具", memory_types=["episodic"], limit=5)

        assert record_id is not None
        assert any(record["id"] == record_id for record in records)
        assert any(hit["source_table"] == "behavior_records" for hit in hits)

    def test_forget_memory_removes_composite_memory(self, db: AgentDB) -> None:
        memory_id = db.save_composite_memory(
            memory_type="semantic",
            text="临时测试记忆",
            tags=["tmp"],
        )

        assert db.forget_memory(memory_id, "composite") is True
        assert not db.search_memory("临时测试记忆", limit=5)


class TestSanitizeData:
    def test_redacts_password_key(self) -> None:
        result = sanitize_data({"password": "secret123", "name": "bot"})
        assert result["password"] == "***REDACTED***"
        assert result["name"] == "bot"

    def test_redacts_nested_token(self) -> None:
        result = sanitize_data({"config": {"api_token": "abc123"}})
        assert result["config"]["api_token"] == "***REDACTED***"

    def test_preserves_normal_data(self) -> None:
        data = {"profit": 100.5, "trades": 42}
        assert sanitize_data(data) == data

    def test_redacts_long_string_with_secret_pattern(self) -> None:
        result = sanitize_data("password=my_super_secret_value_123456789012345")
        assert result == "***REDACTED***"

    def test_short_string_not_redacted(self) -> None:
        result = sanitize_data("secret")
        assert result == "secret"

    def test_trading_keys_preserved(self) -> None:
        data = {
            "best_pair_profit_ratio": -0.0058,
            "profit_all_percent": -0.47,
            "trading_volume": 1590.74,
        }
        assert sanitize_data(data) == data
