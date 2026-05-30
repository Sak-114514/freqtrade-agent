from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


SENSITIVE_KEY_RE = re.compile(
    r"(?:^|[_\-.])"
    r"(?:password|passwd|secret"
    r"|api[_\-]?key|apikey"
    r"|[_\-]?token"
    r"|bearer|jwt"
    r"|ws_token|private[_\-]?key)"
    r"(?:$|[_\-.])",
    re.IGNORECASE,
)
_SENSITIVE_VALUE_RE = re.compile(
    r"(?:password|secret|api[_\-]?key|access[_\-]?token|bearer|private[_\-]?key)\s*[:=]\s*\S+",
    re.IGNORECASE,
)
MEMORY_TYPES = {"profile", "semantic", "episodic", "procedural"}
MEMORY_SOURCE_TABLES = {
    "observations",
    "session_summaries",
    "composite_memories",
    "behavior_records",
}


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def sanitize_data(value: Any) -> Any:
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            if SENSITIVE_KEY_RE.search(str(key)):
                clean[str(key)] = "***REDACTED***"
            else:
                clean[str(key)] = sanitize_data(item)
        return clean
    if isinstance(value, list):
        return [sanitize_data(item) for item in value]
    if isinstance(value, str) and _SENSITIVE_VALUE_RE.search(value):
        return "***REDACTED***"
    return value


def to_json(value: Any) -> str:
    return json.dumps(sanitize_data(value), ensure_ascii=False, default=str)


class AgentDB:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT,
                    user_id TEXT,
                    chat_id TEXT,
                    question TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS short_term_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    user_id TEXT,
                    chat_id TEXT,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    tags TEXT NOT NULL,
                    importance INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tool_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tool_name TEXT NOT NULL,
                    args_json_sanitized TEXT NOT NULL,
                    result_summary TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    error TEXT,
                    latency_ms REAL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pending_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action_type TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    args_json_sanitized TEXT NOT NULL,
                    confirmation_text TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    confirmed INTEGER NOT NULL DEFAULT 0,
                    executed INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT,
                    user_id TEXT,
                    chat_id TEXT,
                    question TEXT NOT NULL,
                    plan TEXT NOT NULL,
                    answer TEXT,
                    used_llm INTEGER NOT NULL DEFAULT 0,
                    fallback_used INTEGER NOT NULL DEFAULT 0,
                    llm_error TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS run_steps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    step_index INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    tool_name TEXT,
                    args_json_sanitized TEXT,
                    result_summary TEXT,
                    success INTEGER,
                    permission_request_id INTEGER,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS permission_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER,
                    tool_name TEXT NOT NULL,
                    args_json_sanitized TEXT NOT NULL,
                    confirmation_text TEXT NOT NULL,
                    risk_notes TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    expires_at TEXT NOT NULL,
                    confirmed_at TEXT,
                    executed INTEGER NOT NULL DEFAULT 0,
                    result_summary TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS session_summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT,
                    user_id TEXT,
                    chat_id TEXT,
                    summary TEXT NOT NULL,
                    start_conversation_id INTEGER,
                    end_conversation_id INTEGER,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS composite_memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_type TEXT NOT NULL,
                    text TEXT NOT NULL,
                    tags TEXT NOT NULL DEFAULT '[]',
                    importance INTEGER NOT NULL DEFAULT 1,
                    confidence REAL NOT NULL DEFAULT 1.0,
                    source TEXT,
                    source_id INTEGER,
                    expires_at TEXT,
                    last_accessed TEXT,
                    access_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS behavior_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER,
                    source TEXT,
                    user_id TEXT,
                    chat_id TEXT,
                    trigger TEXT NOT NULL,
                    tools_used_json TEXT NOT NULL DEFAULT '[]',
                    outcome TEXT NOT NULL,
                    facts_json_sanitized TEXT NOT NULL DEFAULT '{}',
                    tags TEXT NOT NULL DEFAULT '[]',
                    importance INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS monitor_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    interval_minutes INTEGER NOT NULL DEFAULT 30,
                    pair TEXT,
                    change_threshold_pct REAL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_run_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS monitor_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rule_id INTEGER,
                    event_type TEXT NOT NULL,
                    trigger_reason TEXT NOT NULL,
                    answer TEXT,
                    tool_calls_json TEXT NOT NULL DEFAULT '[]',
                    sent INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS scheduled_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    description TEXT NOT NULL,
                    cron TEXT NOT NULL,
                    interval_minutes INTEGER,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    report_path TEXT,
                    last_run_at TEXT,
                    next_run_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS scheduled_job_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER NOT NULL,
                    job_name TEXT NOT NULL,
                    result_summary TEXT NOT NULL,
                    report_path TEXT,
                    success INTEGER NOT NULL,
                    error TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    tool_calls_json TEXT NOT NULL DEFAULT '[]'
                );

                CREATE TABLE IF NOT EXISTS telegram_dashboards (
                    chat_id TEXT PRIMARY KEY,
                    message_id INTEGER NOT NULL,
                    last_text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            self._ensure_column(conn, "tool_calls", "latency_ms", "REAL")
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_short_term_messages_scope
                ON short_term_messages (source, user_id, chat_id, id)
                """
            )
            self._ensure_memory_fts(conn)
            self._backfill_memory_fts(conn)

    def _ensure_memory_fts(self, conn: sqlite3.Connection) -> None:
        try:
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS agent_memory_fts USING fts5(
                    memory_type,
                    source_table,
                    source_id UNINDEXED,
                    text,
                    tags,
                    created_at UNINDEXED
                )
                """
            )
        except sqlite3.OperationalError:
            # Some Python/SQLite builds omit FTS5. Search helpers fall back to LIKE.
            return

    def _backfill_memory_fts(self, conn: sqlite3.Connection) -> None:
        if not self._fts_available(conn):
            return
        count = conn.execute("SELECT COUNT(*) FROM agent_memory_fts").fetchone()[0]
        if int(count or 0) > 0:
            return
        for row in conn.execute("SELECT id, text, tags, created_at FROM observations"):
            self._index_memory_row(
                conn,
                memory_type="semantic",
                source_table="observations",
                source_id=int(row["id"]),
                text=str(row["text"] or ""),
                tags=str(row["tags"] or "[]"),
                created_at=str(row["created_at"] or ""),
            )
        for row in conn.execute("SELECT id, summary, created_at FROM session_summaries"):
            self._index_memory_row(
                conn,
                memory_type="semantic",
                source_table="session_summaries",
                source_id=int(row["id"]),
                text=str(row["summary"] or ""),
                tags="[]",
                created_at=str(row["created_at"] or ""),
            )
        for row in conn.execute(
            "SELECT id, memory_type, text, tags, created_at FROM composite_memories"
        ):
            self._index_memory_row(
                conn,
                memory_type=str(row["memory_type"] or "semantic"),
                source_table="composite_memories",
                source_id=int(row["id"]),
                text=str(row["text"] or ""),
                tags=str(row["tags"] or "[]"),
                created_at=str(row["created_at"] or ""),
            )
        for row in conn.execute(
            """
            SELECT id, trigger, outcome, tools_used_json, tags, created_at
            FROM behavior_records
            """
        ):
            text = self._behavior_search_text(dict(row))
            self._index_memory_row(
                conn,
                memory_type="episodic",
                source_table="behavior_records",
                source_id=int(row["id"]),
                text=text,
                tags=str(row["tags"] or "[]"),
                created_at=str(row["created_at"] or ""),
            )

    def _ensure_column(
        self,
        conn: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        columns = {
            str(row["name"])
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _fts_available(self, conn: sqlite3.Connection) -> bool:
        try:
            conn.execute("SELECT 1 FROM agent_memory_fts LIMIT 1")
            return True
        except sqlite3.OperationalError:
            return False

    def _index_memory_row(
        self,
        conn: sqlite3.Connection,
        *,
        memory_type: str,
        source_table: str,
        source_id: int,
        text: str,
        tags: str,
        created_at: str,
    ) -> None:
        if not text.strip() or not self._fts_available(conn):
            return
        conn.execute(
            """
            INSERT INTO agent_memory_fts (
                memory_type, source_table, source_id, text, tags, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (memory_type, source_table, source_id, text, tags, created_at),
        )

    def _delete_memory_index(
        self,
        conn: sqlite3.Connection,
        *,
        source_table: str,
        source_id: int,
    ) -> None:
        if not self._fts_available(conn):
            return
        conn.execute(
            "DELETE FROM agent_memory_fts WHERE source_table = ? AND source_id = ?",
            (source_table, source_id),
        )

    def _normalise_memory_type(self, memory_type: str | None) -> str:
        value = (memory_type or "semantic").strip().lower()
        return value if value in MEMORY_TYPES else "semantic"

    def _normalise_tags(self, tags: list[str] | str | None) -> list[str]:
        if isinstance(tags, str):
            try:
                parsed = json.loads(tags)
            except json.JSONDecodeError:
                parsed = [tags]
            tags = parsed if isinstance(parsed, list) else [tags]
        return [
            str(tag).strip()
            for tag in (tags or [])
            if str(tag).strip()
        ][:12]

    def _memory_query_terms(self, query: str) -> list[str]:
        return [
            term.strip()
            for term in re.findall(r"[\w\u4e00-\u9fff]{2,}", query.lower())
            if term.strip()
        ][:8]

    def _memory_fts_query(self, query: str) -> str:
        terms = self._memory_query_terms(query)
        if not terms:
            return ""
        return " OR ".join(f'"{term}"' for term in terms)

    def _matches_terms(self, text: str, query: str) -> bool:
        terms = self._memory_query_terms(query)
        if not terms:
            return True
        lowered = text.lower()
        return any(term in lowered for term in terms)

    def _decode_json_list(self, value: Any) -> list[Any]:
        if isinstance(value, list):
            return value
        if value is None:
            return []
        try:
            parsed = json.loads(str(value))
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []

    def _decode_json_dict(self, value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if value is None:
            return {}
        try:
            parsed = json.loads(str(value))
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def save_conversation(
        self,
        *,
        source: str,
        user_id: str,
        chat_id: str,
        question: str,
        answer: str,
    ) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO conversations (source, user_id, chat_id, question, answer, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (source, user_id, chat_id, question, answer, utc_now_iso()),
            )
            return int(cursor.lastrowid)

    def save_short_term_message(
        self,
        *,
        source: str,
        user_id: str,
        chat_id: str,
        role: str,
        content: str,
        max_messages: int = 20,
    ) -> int:
        safe_role = role if role in {"user", "assistant"} else "assistant"
        safe_content = self._one_line(str(sanitize_data(content)), 1200)
        max_messages = max(1, min(100, int(max_messages)))
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO short_term_messages
                    (source, user_id, chat_id, role, content, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (source, user_id, chat_id, safe_role, safe_content, utc_now_iso()),
            )
            message_id = int(cursor.lastrowid)
            conn.execute(
                """
                DELETE FROM short_term_messages
                WHERE source = ?
                  AND COALESCE(user_id, '') = COALESCE(?, '')
                  AND COALESCE(chat_id, '') = COALESCE(?, '')
                  AND id NOT IN (
                      SELECT id
                      FROM short_term_messages
                      WHERE source = ?
                        AND COALESCE(user_id, '') = COALESCE(?, '')
                        AND COALESCE(chat_id, '') = COALESCE(?, '')
                      ORDER BY id DESC
                      LIMIT ?
                  )
                """,
                (source, user_id, chat_id, source, user_id, chat_id, max_messages),
            )
            return message_id

    def recent_short_term_messages(
        self,
        *,
        source: str,
        user_id: str,
        chat_id: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(100, int(limit)))
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, source, user_id, chat_id, role, content, created_at
                FROM short_term_messages
                WHERE source = ?
                  AND COALESCE(user_id, '') = COALESCE(?, '')
                  AND COALESCE(chat_id, '') = COALESCE(?, '')
                ORDER BY id DESC
                LIMIT ?
                """,
                (source, user_id, chat_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def save_observation(
        self,
        text: str,
        tags: list[str] | None = None,
        importance: int = 1,
    ) -> int:
        now = utc_now_iso()
        tags_json = json.dumps(self._normalise_tags(tags), ensure_ascii=False)
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO observations (text, tags, importance, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (text, tags_json, importance, now),
            )
            observation_id = int(cursor.lastrowid)
            self._index_memory_row(
                conn,
                memory_type="semantic",
                source_table="observations",
                source_id=observation_id,
                text=text,
                tags=tags_json,
                created_at=now,
            )
            return observation_id

    def save_tool_call(
        self,
        *,
        tool_name: str,
        args: dict[str, Any],
        result_summary: str,
        success: bool,
        error: str | None,
        latency_ms: float | None = None,
    ) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO tool_calls (
                    tool_name, args_json_sanitized, result_summary, success, error,
                    latency_ms, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tool_name,
                    to_json(args),
                    result_summary,
                    int(success),
                    error,
                    latency_ms,
                    utc_now_iso(),
                ),
            )
            return int(cursor.lastrowid)

    def create_pending_action(
        self,
        *,
        action_type: str,
        tool_name: str,
        args: dict[str, Any],
        confirmation_text: str,
        ttl_minutes: int = 10,
    ) -> dict[str, Any]:
        now = datetime.now(UTC).replace(microsecond=0)
        expires_at = (now + timedelta(minutes=ttl_minutes)).isoformat()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO pending_actions (
                    action_type, tool_name, args_json_sanitized, confirmation_text,
                    expires_at, confirmed, executed, created_at
                )
                VALUES (?, ?, ?, ?, ?, 0, 0, ?)
                """,
                (
                    action_type,
                    tool_name,
                    to_json(args),
                    confirmation_text,
                    expires_at,
                    now.isoformat(),
                ),
            )
            action_id = int(cursor.lastrowid)
        return {
            "id": action_id,
            "action_type": action_type,
            "tool_name": tool_name,
            "args_json_sanitized": sanitize_data(args),
            "confirmation_text": confirmation_text,
            "expires_at": expires_at,
            "confirmed": False,
            "executed": False,
        }

    def create_agent_run(
        self,
        *,
        source: str,
        user_id: str,
        chat_id: str,
        question: str,
        plan: str,
    ) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO agent_runs (
                    source, user_id, chat_id, question, plan, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (source, user_id, chat_id, question, plan, utc_now_iso()),
            )
            return int(cursor.lastrowid)

    def finish_agent_run(
        self,
        run_id: int,
        *,
        answer: str,
        used_llm: bool,
        fallback_used: bool,
        llm_error: str | None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE agent_runs
                SET answer = ?, used_llm = ?, fallback_used = ?,
                    llm_error = ?, completed_at = ?
                WHERE id = ?
                """,
                (answer, int(used_llm), int(fallback_used), llm_error, utc_now_iso(), run_id),
            )

    def save_run_step(
        self,
        *,
        run_id: int,
        step_index: int,
        role: str,
        content: str,
        tool_name: str | None = None,
        args: dict[str, Any] | None = None,
        result_summary: str | None = None,
        success: bool | None = None,
        permission_request_id: int | None = None,
    ) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO run_steps (
                    run_id, step_index, role, content, tool_name, args_json_sanitized,
                    result_summary, success, permission_request_id, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    step_index,
                    role,
                    content[:6000],
                    tool_name,
                    to_json(args or {}),
                    (result_summary or "")[:2000] if result_summary else None,
                    None if success is None else int(success),
                    permission_request_id,
                    utc_now_iso(),
                ),
            )
            return int(cursor.lastrowid)

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            run = conn.execute(
                """
                SELECT id, source, user_id, chat_id, question, plan, answer,
                       used_llm, fallback_used, llm_error, created_at, completed_at
                FROM agent_runs
                WHERE id = ?
                """,
                (run_id,),
            ).fetchone()
            if not run:
                return None
            steps = conn.execute(
                """
                SELECT id, step_index, role, content, tool_name, args_json_sanitized,
                       result_summary, success, permission_request_id, created_at
                FROM run_steps
                WHERE run_id = ?
                ORDER BY step_index, id
                """,
                (run_id,),
            ).fetchall()
            permissions = conn.execute(
                """
                SELECT id, run_id, tool_name, args_json_sanitized, confirmation_text,
                       risk_notes, status, expires_at, confirmed_at, executed,
                       result_summary, created_at
                FROM permission_requests
                WHERE run_id = ?
                ORDER BY id
                """,
                (run_id,),
            ).fetchall()
        return {
            "run": dict(run),
            "steps": [dict(row) for row in steps],
            "permission_requests": [dict(row) for row in permissions],
        }

    def create_permission_request(
        self,
        *,
        run_id: int | None,
        tool_name: str,
        args: dict[str, Any],
        confirmation_text: str,
        risk_notes: str,
        ttl_minutes: int = 10,
    ) -> dict[str, Any]:
        now = datetime.now(UTC).replace(microsecond=0)
        expires_at = (now + timedelta(minutes=ttl_minutes)).isoformat()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO permission_requests (
                    run_id, tool_name, args_json_sanitized, confirmation_text,
                    risk_notes, status, expires_at, executed, created_at
                )
                VALUES (?, ?, ?, ?, ?, 'pending', ?, 0, ?)
                """,
                (
                    run_id,
                    tool_name,
                    to_json(args),
                    confirmation_text,
                    risk_notes,
                    expires_at,
                    now.isoformat(),
                ),
            )
            request_id = int(cursor.lastrowid)
        return {
            "id": request_id,
            "run_id": run_id,
            "tool_name": tool_name,
            "args_json_sanitized": sanitize_data(args),
            "confirmation_text": confirmation_text,
            "risk_notes": risk_notes,
            "status": "pending",
            "expires_at": expires_at,
            "confirmed_at": None,
            "executed": False,
            "result_summary": None,
            "created_at": now.isoformat(),
        }

    def pending_permission_requests(self) -> list[dict[str, Any]]:
        self.expire_permission_requests()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, run_id, tool_name, args_json_sanitized, confirmation_text,
                       risk_notes, status, expires_at, confirmed_at, executed,
                       result_summary, created_at
                FROM permission_requests
                WHERE status = 'pending'
                ORDER BY id DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def expire_permission_requests(self, now: datetime | None = None) -> int:
        current = (now or datetime.now(UTC)).replace(microsecond=0)
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE permission_requests
                SET status = 'expired',
                    result_summary = COALESCE(result_summary, 'permission_request 已过期。')
                WHERE status = 'pending' AND expires_at < ?
                """,
                (current.isoformat(),),
            )
            return int(cursor.rowcount or 0)

    def get_permission_request(self, request_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT id, run_id, tool_name, args_json_sanitized, confirmation_text,
                       risk_notes, status, expires_at, confirmed_at, executed,
                       result_summary, created_at
                FROM permission_requests
                WHERE id = ?
                """,
                (request_id,),
            ).fetchone()
        return dict(row) if row else None

    def update_permission_request(
        self,
        request_id: int,
        *,
        status: str,
        executed: bool,
        result_summary: str | None,
    ) -> dict[str, Any] | None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE permission_requests
                SET status = ?, confirmed_at = ?, executed = ?, result_summary = ?
                WHERE id = ?
                """,
                (status, utc_now_iso(), int(executed), result_summary, request_id),
            )
        return self.get_permission_request(request_id)

    def save_session_summary(
        self,
        *,
        source: str,
        user_id: str,
        chat_id: str,
        summary: str,
        start_conversation_id: int | None,
        end_conversation_id: int | None,
    ) -> int:
        now = utc_now_iso()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO session_summaries (
                    source, user_id, chat_id, summary, start_conversation_id,
                    end_conversation_id, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source,
                    user_id,
                    chat_id,
                    summary,
                    start_conversation_id,
                    end_conversation_id,
                    now,
                ),
            )
            summary_id = int(cursor.lastrowid)
            self._index_memory_row(
                conn,
                memory_type="semantic",
                source_table="session_summaries",
                source_id=summary_id,
                text=summary,
                tags="[]",
                created_at=now,
            )
            return summary_id

    def recent_session_summaries(self, limit: int = 3) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, source, user_id, chat_id, summary, start_conversation_id,
                       end_conversation_id, created_at
                FROM session_summaries
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def compact_conversations(
        self,
        *,
        source: str,
        user_id: str,
        chat_id: str,
        keep_last: int = 5,
        compact_batch: int = 20,
    ) -> None:
        with self.connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
            if total <= keep_last + compact_batch:
                return
            rows = conn.execute(
                """
                SELECT id, question, answer, created_at
                FROM conversations
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                (compact_batch, keep_last),
            ).fetchall()
        oldest_first = list(reversed([dict(row) for row in rows]))
        if not oldest_first:
            return
        target_end_id = int(oldest_first[-1]["id"])
        with self.connect() as conn:
            latest_compacted = conn.execute(
                "SELECT COALESCE(MAX(end_conversation_id), 0) FROM session_summaries"
            ).fetchone()[0]
        if int(latest_compacted or 0) >= target_end_id:
            return
        summary_lines = ["事实/偏好/决策/未完成事项摘要:"]
        for row in oldest_first[-8:]:
            question = self._one_line(str(row["question"]), 80)
            answer = self._one_line(str(row["answer"]), 120)
            summary_lines.append(f"- Q: {question} | A: {answer}")
        self.save_session_summary(
            source=source,
            user_id=user_id,
            chat_id=chat_id,
            summary="\n".join(summary_lines)[:800],
            start_conversation_id=oldest_first[0]["id"],
            end_conversation_id=target_end_id,
        )

    def _one_line(self, text: str, limit: int) -> str:
        compact = " ".join(text.split())
        return compact if len(compact) <= limit else compact[: limit - 1] + "…"

    def upsert_monitor_rule(
        self,
        *,
        name: str,
        prompt: str,
        interval_minutes: int,
        pair: str | None,
        change_threshold_pct: float | None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        now = utc_now_iso()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO monitor_rules (
                    name, prompt, interval_minutes, pair, change_threshold_pct,
                    enabled, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    prompt,
                    interval_minutes,
                    pair,
                    change_threshold_pct,
                    int(enabled),
                    now,
                    now,
                ),
            )
            rule_id = int(cursor.lastrowid)
        return self.get_monitor_rule(rule_id) or {}

    def monitor_rules(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        query = """
            SELECT id, name, prompt, interval_minutes, pair, change_threshold_pct,
                   enabled, last_run_at, created_at, updated_at
            FROM monitor_rules
        """
        params: tuple[Any, ...] = ()
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY id DESC"
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def get_monitor_rule(self, rule_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT id, name, prompt, interval_minutes, pair, change_threshold_pct,
                       enabled, last_run_at, created_at, updated_at
                FROM monitor_rules
                WHERE id = ?
                """,
                (rule_id,),
            ).fetchone()
        return dict(row) if row else None

    def set_monitor_enabled(self, rule_id: int, enabled: bool) -> dict[str, Any] | None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE monitor_rules
                SET enabled = ?, updated_at = ?
                WHERE id = ?
                """,
                (int(enabled), utc_now_iso(), rule_id),
            )
        return self.get_monitor_rule(rule_id)

    def mark_monitor_run(self, rule_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE monitor_rules SET last_run_at = ?, updated_at = ? WHERE id = ?",
                (utc_now_iso(), utc_now_iso(), rule_id),
            )

    def save_monitor_event(
        self,
        *,
        rule_id: int | None,
        event_type: str,
        trigger_reason: str,
        answer: str | None,
        tool_calls: list[dict[str, Any]] | None,
        sent: bool,
        error: str | None = None,
    ) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO monitor_events (
                    rule_id, event_type, trigger_reason, answer, tool_calls_json,
                    sent, error, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rule_id,
                    event_type,
                    trigger_reason,
                    answer,
                    to_json(tool_calls or []),
                    int(sent),
                    error,
                    utc_now_iso(),
                ),
            )
            return int(cursor.lastrowid)

    def recent_observations(self, query: str = "", limit: int = 5) -> list[dict[str, Any]]:
        terms = [
            term.strip()
            for term in re.split(r"\s+", query.strip())
            if len(term.strip()) >= 2
        ][:5]
        with self.connect() as conn:
            if terms:
                where = " OR ".join("text LIKE ?" for _term in terms)
                params: tuple[Any, ...] = tuple(f"%{term}%" for term in terms) + (limit,)
                rows = conn.execute(
                    f"""
                    SELECT id, text, tags, importance, created_at
                    FROM observations
                    WHERE {where}
                    ORDER BY importance DESC, id DESC
                    LIMIT ?
                    """,
                    params,
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, text, tags, importance, created_at
                    FROM observations
                    ORDER BY importance DESC, id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return [dict(row) for row in rows]

    def recent_conversations(self, limit: int = 5) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, source, user_id, chat_id, question, answer, created_at
                FROM conversations
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_memory(
        self,
        *,
        source: str | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
    ) -> dict[str, Any]:
        observations = [
            {
                "id": row["id"],
                "text": self._one_line(str(row["text"]), 220),
                "tags": row["tags"],
                "importance": row["importance"],
            }
            for row in self.recent_observations(limit=3)
        ]
        conversations = [
            {
                "id": row["id"],
                "q": self._one_line(str(row["question"]), 90),
                "a": self._one_line(str(row["answer"]), 140),
            }
            for row in self.recent_conversations(limit=2)
        ]
        summaries = [
            {
                "id": row["id"],
                "summary": self._one_line(str(row["summary"]), 500),
            }
            for row in self.recent_session_summaries(limit=1)
        ]
        short_term_messages: list[dict[str, Any]] = []
        if source == "telegram" and chat_id:
            short_term_messages = [
                {
                    "id": row["id"],
                    "role": row["role"],
                    "content": self._one_line(str(row["content"]), 500),
                    "created_at": row["created_at"],
                }
                for row in self.recent_short_term_messages(
                    source=source,
                    user_id=user_id or "",
                    chat_id=chat_id,
                    limit=20,
                )
            ]
        return {
            "profile": {
                "mode": "极速轻记忆",
                "safety": "dry-run 学习阶段; 不实盘下单、不改策略、不关闭 dry_run。",
                "policy": "默认只注入短索引和 Telegram 短时上下文; 需要历史细节时调用 memory_recall。",
            },
            "short_term_messages": short_term_messages,
            "observations": observations,
            "recent_conversations": conversations,
            "session_summaries": summaries,
            "available_memory_tools": [
                "memory_recall",
                "memory_search_behavior",
                "memory_save_observation",
                "memory_save_preference",
                "memory_forget",
                "memory_compact_now",
            ],
        }

    def save_composite_memory(
        self,
        *,
        memory_type: str,
        text: str,
        tags: list[str] | None = None,
        importance: int = 1,
        confidence: float = 1.0,
        source: str | None = None,
        source_id: int | None = None,
        expires_at: str | None = None,
    ) -> int:
        clean_text = str(sanitize_data(text)).strip()
        if not clean_text:
            raise ValueError("memory text is empty")
        now = utc_now_iso()
        normalised_type = self._normalise_memory_type(memory_type)
        tags_json = json.dumps(self._normalise_tags(tags), ensure_ascii=False)
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO composite_memories (
                    memory_type, text, tags, importance, confidence, source,
                    source_id, expires_at, access_count, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    normalised_type,
                    clean_text,
                    tags_json,
                    max(1, min(int(importance or 1), 5)),
                    max(0.0, min(float(confidence or 1.0), 1.0)),
                    source,
                    source_id,
                    expires_at,
                    now,
                    now,
                ),
            )
            memory_id = int(cursor.lastrowid)
            self._index_memory_row(
                conn,
                memory_type=normalised_type,
                source_table="composite_memories",
                source_id=memory_id,
                text=clean_text,
                tags=tags_json,
                created_at=now,
            )
            return memory_id

    def search_memory(
        self,
        query: str = "",
        *,
        memory_types: list[str] | None = None,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit or 8), 20))
        types = {
            self._normalise_memory_type(item)
            for item in (memory_types or [])
            if str(item).strip()
        }
        rows: dict[tuple[str, int], dict[str, Any]] = {}
        self._search_memory_fts(query, types, limit, rows)
        self._search_memory_like(query, types, limit, rows)
        ranked = sorted(
            rows.values(),
            key=lambda item: (
                int(item.get("importance") or 1),
                str(item.get("created_at") or ""),
            ),
            reverse=True,
        )[:limit]
        self._mark_memory_accessed(ranked)
        return ranked

    def _search_memory_fts(
        self,
        query: str,
        memory_types: set[str],
        limit: int,
        out: dict[tuple[str, int], dict[str, Any]],
    ) -> None:
        fts_query = self._memory_fts_query(query)
        if not fts_query:
            return
        type_filter = ""
        params: list[Any] = [fts_query]
        if memory_types:
            placeholders = ", ".join("?" for _ in memory_types)
            type_filter = f" AND memory_type IN ({placeholders})"
            params.extend(sorted(memory_types))
        params.append(limit)
        with self.connect() as conn:
            if not self._fts_available(conn):
                return
            try:
                rows = conn.execute(
                    f"""
                    SELECT memory_type, source_table, source_id, text, tags, created_at
                    FROM agent_memory_fts
                    WHERE agent_memory_fts MATCH ? {type_filter}
                    ORDER BY rank
                    LIMIT ?
                    """,
                    tuple(params),
                ).fetchall()
            except sqlite3.OperationalError:
                return
        for row in rows:
            item = self._memory_item_from_index(dict(row))
            out[(item["source_table"], int(item["id"]))] = item

    def _search_memory_like(  # noqa: C901 - keeps LIKE fallback grouped by memory source.
        self,
        query: str,
        memory_types: set[str],
        limit: int,
        out: dict[tuple[str, int], dict[str, Any]],
    ) -> None:
        with self.connect() as conn:
            observations = conn.execute(
                """
                SELECT id, text, tags, importance, created_at
                FROM observations
                ORDER BY importance DESC, id DESC
                LIMIT ?
                """,
                (limit * 4,),
            ).fetchall()
            summaries = conn.execute(
                """
                SELECT id, summary, created_at
                FROM session_summaries
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit * 3,),
            ).fetchall()
            memories = conn.execute(
                """
                SELECT id, memory_type, text, tags, importance, confidence, source,
                       source_id, expires_at, last_accessed, access_count, created_at,
                       updated_at
                FROM composite_memories
                WHERE expires_at IS NULL OR expires_at > ?
                ORDER BY importance DESC, id DESC
                LIMIT ?
                """,
                (utc_now_iso(), limit * 4),
            ).fetchall()
            behaviors = conn.execute(
                """
                SELECT id, run_id, source, user_id, chat_id, trigger, tools_used_json,
                       outcome, facts_json_sanitized, tags, importance, created_at
                FROM behavior_records
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit * 4,),
            ).fetchall()
        for row in observations:
            if memory_types and "semantic" not in memory_types:
                continue
            if not self._matches_terms(str(row["text"] or ""), query):
                continue
            out.setdefault(
                ("observations", int(row["id"])),
                {
                    "id": row["id"],
                    "memory_type": "semantic",
                    "source_table": "observations",
                    "text": self._one_line(str(row["text"] or ""), 700),
                    "tags": self._decode_json_list(row["tags"]),
                    "importance": row["importance"],
                    "confidence": 1.0,
                    "created_at": row["created_at"],
                },
            )
        for row in summaries:
            if memory_types and "semantic" not in memory_types:
                continue
            if not self._matches_terms(str(row["summary"] or ""), query):
                continue
            out.setdefault(
                ("session_summaries", int(row["id"])),
                {
                    "id": row["id"],
                    "memory_type": "semantic",
                    "source_table": "session_summaries",
                    "text": self._one_line(str(row["summary"] or ""), 700),
                    "tags": ["summary"],
                    "importance": 2,
                    "confidence": 0.8,
                    "created_at": row["created_at"],
                },
            )
        for row in memories:
            memory_type = self._normalise_memory_type(str(row["memory_type"] or "semantic"))
            if memory_types and memory_type not in memory_types:
                continue
            if not self._matches_terms(str(row["text"] or ""), query):
                continue
            out.setdefault(
                ("composite_memories", int(row["id"])),
                {
                    "id": row["id"],
                    "memory_type": memory_type,
                    "source_table": "composite_memories",
                    "text": self._one_line(str(row["text"] or ""), 700),
                    "tags": self._decode_json_list(row["tags"]),
                    "importance": row["importance"],
                    "confidence": row["confidence"],
                    "source": row["source"],
                    "source_id": row["source_id"],
                    "expires_at": row["expires_at"],
                    "last_accessed": row["last_accessed"],
                    "access_count": row["access_count"],
                    "created_at": row["created_at"],
                },
            )
        for row in behaviors:
            if memory_types and "episodic" not in memory_types:
                continue
            text = self._behavior_search_text(dict(row))
            if not self._matches_terms(text, query):
                continue
            out.setdefault(
                ("behavior_records", int(row["id"])),
                self._behavior_memory_item(dict(row), text),
            )

    def _memory_item_from_index(self, row: dict[str, Any]) -> dict[str, Any]:
        source_table = str(row["source_table"])
        source_id = int(row["source_id"])
        with self.connect() as conn:
            if source_table == "composite_memories":
                memory = conn.execute(
                    """
                    SELECT id, memory_type, text, tags, importance, confidence, source,
                           source_id, expires_at, last_accessed, access_count, created_at
                    FROM composite_memories
                    WHERE id = ?
                    """,
                    (source_id,),
                ).fetchone()
                if memory:
                    item = dict(memory)
                    return {
                        "id": item["id"],
                        "memory_type": self._normalise_memory_type(item["memory_type"]),
                        "source_table": source_table,
                        "text": self._one_line(str(item["text"] or ""), 700),
                        "tags": self._decode_json_list(item["tags"]),
                        "importance": item["importance"],
                        "confidence": item["confidence"],
                        "source": item["source"],
                        "source_id": item["source_id"],
                        "expires_at": item["expires_at"],
                        "last_accessed": item["last_accessed"],
                        "access_count": item["access_count"],
                        "created_at": item["created_at"],
                    }
            if source_table == "observations":
                observation = conn.execute(
                    "SELECT id, text, tags, importance, created_at FROM observations WHERE id = ?",
                    (source_id,),
                ).fetchone()
                if observation:
                    item = dict(observation)
                    return {
                        "id": item["id"],
                        "memory_type": "semantic",
                        "source_table": source_table,
                        "text": self._one_line(str(item["text"] or ""), 700),
                        "tags": self._decode_json_list(item["tags"]),
                        "importance": item["importance"],
                        "confidence": 1.0,
                        "created_at": item["created_at"],
                    }
            if source_table == "session_summaries":
                summary = conn.execute(
                    "SELECT id, summary, created_at FROM session_summaries WHERE id = ?",
                    (source_id,),
                ).fetchone()
                if summary:
                    item = dict(summary)
                    return {
                        "id": item["id"],
                        "memory_type": "semantic",
                        "source_table": source_table,
                        "text": self._one_line(str(item["summary"] or ""), 700),
                        "tags": ["summary"],
                        "importance": 2,
                        "confidence": 0.8,
                        "created_at": item["created_at"],
                    }
        if source_table == "behavior_records":
            behavior = self.get_behavior_record(source_id)
            if behavior:
                return self._behavior_memory_item(behavior, self._behavior_search_text(behavior))
        return {
            "id": source_id,
            "memory_type": self._normalise_memory_type(str(row["memory_type"])),
            "source_table": source_table,
            "text": self._one_line(str(row["text"] or ""), 700),
            "tags": self._decode_json_list(row.get("tags")),
            "importance": 2,
            "confidence": 0.8,
            "created_at": row.get("created_at"),
        }

    def _mark_memory_accessed(self, items: list[dict[str, Any]]) -> None:
        composite_ids = [
            int(item["id"])
            for item in items
            if item.get("source_table") == "composite_memories" and item.get("id")
        ]
        if not composite_ids:
            return
        placeholders = ", ".join("?" for _ in composite_ids)
        with self.connect() as conn:
            conn.execute(
                f"""
                UPDATE composite_memories
                SET last_accessed = ?, access_count = access_count + 1
                WHERE id IN ({placeholders})
                """,
                (utc_now_iso(), *composite_ids),
            )

    def forget_memory(self, memory_id: int, memory_type: str = "composite") -> bool:
        table = str(memory_type or "composite").strip().lower()
        source_table = {
            "composite": "composite_memories",
            "observation": "observations",
            "behavior": "behavior_records",
            "summary": "session_summaries",
        }.get(table)
        if source_table is None:
            return False
        with self.connect() as conn:
            cursor = conn.execute(f"DELETE FROM {source_table} WHERE id = ?", (memory_id,))
            if cursor.rowcount:
                self._delete_memory_index(conn, source_table=source_table, source_id=memory_id)
            return bool(cursor.rowcount)

    def compact_memory_now(
        self,
        *,
        source: str = "api",
        user_id: str = "local",
        chat_id: str = "local",
    ) -> dict[str, Any]:
        before = len(self.recent_session_summaries(limit=1000))
        self.compact_conversations(source=source, user_id=user_id, chat_id=chat_id)
        summaries = self.recent_session_summaries(limit=1)
        after = len(self.recent_session_summaries(limit=1000))
        saved_memory_id: int | None = None
        if summaries and after > before:
            summary = str(summaries[0].get("summary") or "")
            existing = self.search_memory(summary[:120], memory_types=["semantic"], limit=3)
            if not any(item.get("text") == self._one_line(summary, 700) for item in existing):
                saved_memory_id = self.save_composite_memory(
                    memory_type="semantic",
                    text=summary,
                    tags=["compaction", "summary"],
                    importance=2,
                    confidence=0.7,
                    source="session_summaries",
                    source_id=int(summaries[0]["id"]),
                )
        return {
            "created_summary": after > before,
            "latest_summary": summaries[0] if summaries else None,
            "semantic_memory_id": saved_memory_id,
        }

    def save_behavior_record(
        self,
        *,
        run_id: int | None,
        source: str,
        user_id: str,
        chat_id: str,
        trigger: str,
        tools_used: list[str],
        outcome: str,
        facts: dict[str, Any],
        tags: list[str] | None = None,
        importance: int = 1,
    ) -> int:
        now = utc_now_iso()
        tags_json = json.dumps(self._normalise_tags(tags), ensure_ascii=False)
        tools_json = json.dumps(self._normalise_tags(tools_used), ensure_ascii=False)
        facts_json = to_json(facts)
        with self.connect() as conn:
            if run_id is not None:
                existing = conn.execute(
                    "SELECT id FROM behavior_records WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if existing:
                    return int(existing["id"])
            cursor = conn.execute(
                """
                INSERT INTO behavior_records (
                    run_id, source, user_id, chat_id, trigger, tools_used_json,
                    outcome, facts_json_sanitized, tags, importance, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    source,
                    user_id,
                    chat_id,
                    self._one_line(str(sanitize_data(trigger)), 1000),
                    tools_json,
                    self._one_line(str(sanitize_data(outcome)), 1000),
                    facts_json,
                    tags_json,
                    max(1, min(int(importance or 1), 5)),
                    now,
                ),
            )
            record_id = int(cursor.lastrowid)
            row = {
                "id": record_id,
                "trigger": trigger,
                "outcome": outcome,
                "tools_used_json": tools_json,
                "tags": tags_json,
                "created_at": now,
            }
            self._index_memory_row(
                conn,
                memory_type="episodic",
                source_table="behavior_records",
                source_id=record_id,
                text=self._behavior_search_text(row),
                tags=tags_json,
                created_at=now,
            )
            return record_id

    def record_behavior_from_run(self, run_id: int) -> int | None:
        run_data = self.get_run(run_id)
        if not run_data:
            return None
        run = run_data["run"]
        steps = run_data["steps"]
        permissions = run_data["permission_requests"]
        tools = [
            str(step["tool_name"])
            for step in steps
            if step.get("tool_name")
        ]
        tool_summaries = [
            {
                "tool_name": step.get("tool_name"),
                "summary": self._one_line(str(step.get("result_summary") or ""), 240),
                "success": bool(step.get("success")),
            }
            for step in steps
            if step.get("tool_name")
        ][:12]
        permission_summaries = [
            {
                "id": item.get("id"),
                "tool_name": item.get("tool_name"),
                "status": item.get("status"),
                "executed": bool(item.get("executed")),
                "summary": self._one_line(str(item.get("result_summary") or ""), 240),
            }
            for item in permissions
        ][:12]
        llm_error = str(run.get("llm_error") or "")
        outcome = (
            f"failed: {llm_error}"
            if llm_error
            else self._one_line(str(run.get("answer") or "completed"), 500)
        )
        tags = ["agent_run", str(run.get("source") or "api")]
        if llm_error:
            tags.append("llm_error")
        if permissions:
            tags.append("permission")
        tags.extend(tools[:6])
        return self.save_behavior_record(
            run_id=run_id,
            source=str(run.get("source") or "api"),
            user_id=str(run.get("user_id") or "local"),
            chat_id=str(run.get("chat_id") or "local"),
            trigger=str(run.get("question") or ""),
            tools_used=tools,
            outcome=outcome,
            facts={
                "question": run.get("question"),
                "answer": self._one_line(str(run.get("answer") or ""), 700),
                "used_llm": bool(run.get("used_llm")),
                "fallback_used": bool(run.get("fallback_used")),
                "llm_error": llm_error or None,
                "tool_summaries": tool_summaries,
                "permission_requests": permission_summaries,
            },
            tags=tags,
            importance=3 if permissions or llm_error else 2 if tools else 1,
        )

    def behavior_records(self, query: str = "", limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit or 20), 100))
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, run_id, source, user_id, chat_id, trigger, tools_used_json,
                       outcome, facts_json_sanitized, tags, importance, created_at
                FROM behavior_records
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit * 3,),
            ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            text = self._behavior_search_text(item)
            if not self._matches_terms(text, query):
                continue
            item["tools_used"] = self._decode_json_list(item.pop("tools_used_json", "[]"))
            item["facts"] = self._decode_json_dict(item.pop("facts_json_sanitized", "{}"))
            item["tags"] = self._decode_json_list(item.get("tags"))
            results.append(item)
            if len(results) >= limit:
                break
        return results

    def get_behavior_record(self, record_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT id, run_id, source, user_id, chat_id, trigger, tools_used_json,
                       outcome, facts_json_sanitized, tags, importance, created_at
                FROM behavior_records
                WHERE id = ?
                """,
                (record_id,),
            ).fetchone()
        return dict(row) if row else None

    def _behavior_search_text(self, row: dict[str, Any]) -> str:
        tools = ", ".join(str(item) for item in self._decode_json_list(row.get("tools_used_json")))
        facts = self._decode_json_dict(row.get("facts_json_sanitized"))
        snippets = [
            str(row.get("trigger") or ""),
            str(row.get("outcome") or ""),
            tools,
            str(facts.get("answer") or ""),
            " ".join(
                str(item.get("summary") or "")
                for item in facts.get("tool_summaries", [])
                if isinstance(item, dict)
            ),
        ]
        return " | ".join(item for item in snippets if item)

    def _behavior_memory_item(self, row: dict[str, Any], text: str) -> dict[str, Any]:
        return {
            "id": row.get("id"),
            "memory_type": "episodic",
            "source_table": "behavior_records",
            "text": self._one_line(text, 700),
            "tags": self._decode_json_list(row.get("tags")),
            "importance": row.get("importance") or 1,
            "confidence": 1.0,
            "created_at": row.get("created_at"),
            "run_id": row.get("run_id"),
            "source": row.get("source"),
            "tools_used": self._decode_json_list(row.get("tools_used_json")),
            "facts": self._decode_json_dict(row.get("facts_json_sanitized")),
        }

    def ensure_default_scheduled_jobs(self, jobs: list[dict[str, Any]]) -> None:
        now = datetime.now(UTC).replace(microsecond=0)
        with self.connect() as conn:
            for job in jobs:
                existing = conn.execute(
                    "SELECT id, enabled, next_run_at FROM scheduled_jobs WHERE name = ?",
                    (job["name"],),
                ).fetchone()
                interval = int(job.get("interval_minutes") or 1440)
                if existing:
                    conn.execute(
                        """
                        UPDATE scheduled_jobs
                        SET description = ?, cron = ?, interval_minutes = ?,
                            report_path = ?, updated_at = ?
                        WHERE name = ?
                        """,
                        (
                            job["description"],
                            job["cron"],
                            interval,
                            job.get("report_path"),
                            now.isoformat(),
                            job["name"],
                        ),
                    )
                    if not existing["next_run_at"]:
                        conn.execute(
                            "UPDATE scheduled_jobs SET next_run_at = ? WHERE name = ?",
                            ((now + timedelta(minutes=interval)).isoformat(), job["name"]),
                        )
                    continue
                conn.execute(
                    """
                    INSERT INTO scheduled_jobs (
                        name, description, cron, interval_minutes, enabled,
                        report_path, next_run_at, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job["name"],
                        job["description"],
                        job["cron"],
                        interval,
                        int(job.get("enabled", True)),
                        job.get("report_path"),
                        (now + timedelta(minutes=interval)).isoformat(),
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )

    def scheduled_jobs(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        query = """
            SELECT id, name, description, cron, interval_minutes, enabled,
                   report_path, last_run_at, next_run_at, created_at, updated_at
            FROM scheduled_jobs
        """
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY id"
        with self.connect() as conn:
            rows = conn.execute(query).fetchall()
        return [dict(row) for row in rows]

    def get_scheduled_job(self, job_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT id, name, description, cron, interval_minutes, enabled,
                       report_path, last_run_at, next_run_at, created_at, updated_at
                FROM scheduled_jobs
                WHERE id = ?
                """,
                (job_id,),
            ).fetchone()
        return dict(row) if row else None

    def set_scheduled_job_enabled(self, job_id: int, enabled: bool) -> dict[str, Any] | None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE scheduled_jobs
                SET enabled = ?, updated_at = ?
                WHERE id = ?
                """,
                (int(enabled), utc_now_iso(), job_id),
            )
        return self.get_scheduled_job(job_id)

    def mark_scheduled_job_run(self, job_id: int, *, interval_minutes: int) -> None:
        now = datetime.now(UTC).replace(microsecond=0)
        next_run_at = (now + timedelta(minutes=interval_minutes)).isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE scheduled_jobs
                SET last_run_at = ?, next_run_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now.isoformat(), next_run_at, now.isoformat(), job_id),
            )

    def save_scheduled_job_run(
        self,
        *,
        job_id: int,
        job_name: str,
        result_summary: str,
        report_path: str | None,
        success: bool,
        error: str | None,
        started_at: str,
        finished_at: str,
        tool_calls: list[dict[str, Any]] | None,
    ) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO scheduled_job_runs (
                    job_id, job_name, result_summary, report_path, success, error,
                    started_at, finished_at, tool_calls_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    job_name,
                    result_summary[:2000],
                    report_path,
                    int(success),
                    error,
                    started_at,
                    finished_at,
                    to_json(tool_calls or []),
                ),
            )
            return int(cursor.lastrowid)

    def scheduled_job_runs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, job_id, job_name, result_summary, report_path, success,
                       error, started_at, finished_at, tool_calls_json
                FROM scheduled_job_runs
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_telegram_dashboard(self, chat_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT chat_id, message_id, last_text, created_at, updated_at
                FROM telegram_dashboards
                WHERE chat_id = ?
                """,
                (chat_id,),
            ).fetchone()
        return dict(row) if row else None

    def upsert_telegram_dashboard(
        self,
        *,
        chat_id: str,
        message_id: int,
        last_text: str,
    ) -> dict[str, Any]:
        now = utc_now_iso()
        existing = self.get_telegram_dashboard(chat_id)
        with self.connect() as conn:
            if existing:
                conn.execute(
                    """
                    UPDATE telegram_dashboards
                    SET message_id = ?, last_text = ?, updated_at = ?
                    WHERE chat_id = ?
                    """,
                    (message_id, last_text, now, chat_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO telegram_dashboards (
                        chat_id, message_id, last_text, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (chat_id, message_id, last_text, now, now),
                )
        return self.get_telegram_dashboard(chat_id) or {}

    def save_run_state(self, run_id: int, messages_json: str) -> None:
        with self.connect() as conn:
            self._ensure_column(conn, "agent_runs", "state_json", "TEXT")
            conn.execute(
                "UPDATE agent_runs SET state_json = ? WHERE id = ?",
                (messages_json, run_id),
            )

    def load_run_state(self, run_id: int) -> str | None:
        with self.connect() as conn:
            self._ensure_column(conn, "agent_runs", "state_json", "TEXT")
            row = conn.execute(
                "SELECT state_json FROM agent_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        if row and row["state_json"]:
            return str(row["state_json"])
        return None
