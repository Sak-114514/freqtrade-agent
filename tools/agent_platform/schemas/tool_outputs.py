from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1)
    source: str = "api"
    user_id: str = "local"
    chat_id: str = "local"


class AskResumeRequest(BaseModel):
    run_id: int = Field(..., ge=1)
    question: str | None = None
    source: str | None = None
    user_id: str | None = None
    chat_id: str | None = None


class ToolCallView(BaseModel):
    tool_name: str
    args: dict[str, Any] = Field(default_factory=dict)
    success: bool
    summary: str
    permission_level: str | None = None
    permission: str | None = None
    permission_required: bool = False
    permission_request: dict[str, Any] | None = None
    denied: bool = False
    latency_ms: float | None = None


class PendingActionView(BaseModel):
    id: int
    action_type: str
    tool_name: str
    args_json_sanitized: dict[str, Any]
    confirmation_text: str
    expires_at: str
    confirmed: bool
    executed: bool


class AskResponse(BaseModel):
    answer: str
    plan: str
    tool_calls: list[ToolCallView]
    pending_action: dict[str, Any] | None = None
    permission_requests: list[dict[str, Any]] = Field(default_factory=list)
    used_llm: bool
    fallback_used: bool
    run_id: int | None = None
    steps: list[dict[str, Any]] = Field(default_factory=list)
    llm_error: str | None = None
    memory_used: dict[str, Any] = Field(default_factory=dict)
    memory_hits: list[dict[str, Any]] = Field(default_factory=list)
    behavior_record_id: int | None = None
    summary: str | None = None


class HealthResponse(BaseModel):
    status: str
    agent: str
    bind: str
    public_url: str
    dry_run: bool | None = None
    freqtrade: dict[str, Any]
    llm: dict[str, Any]
    memory_db_path: str
