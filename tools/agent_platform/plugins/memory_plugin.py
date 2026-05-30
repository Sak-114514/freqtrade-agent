from __future__ import annotations

from typing import Any

from agent_platform.registry.permissions import PermissionAction, PermissionLevel
from agent_platform.registry.tool_registry import (
    ToolRegistry,
    ToolSpec,
    any_output_schema,
    object_schema,
)
from agent_platform.storage.db import AgentDB


class MemoryPlugin:
    def __init__(self, db: AgentDB) -> None:
        self.db = db

    def register(self, registry: ToolRegistry) -> None:
        registry.register(
            ToolSpec(
                name="memory_recall",
                description=(
                    "Recall non-sensitive composite memory across profile, semantic, "
                    "episodic and procedural memory."
                ),
                permission_level=PermissionLevel.L0,
                input_schema=object_schema(
                    {
                        "query": {"type": "string"},
                        "memory_types": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": ["profile", "semantic", "episodic", "procedural"],
                            },
                        },
                        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    }
                ),
                output_schema=any_output_schema(),
                handler=self._recall,
                requires_confirmation=False,
                risk_notes="Read-only memory lookup.",
                permission_default=PermissionAction.ALLOW,
            )
        )
        registry.register(
            ToolSpec(
                name="memory_search_behavior",
                description=(
                    "Search behavior records: prior runs, tool calls, permission flow, "
                    "failures and monitor/scheduler outcomes."
                ),
                permission_level=PermissionLevel.L0,
                input_schema=object_schema(
                    {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                    }
                ),
                output_schema=any_output_schema(),
                handler=self._search_behavior,
                requires_confirmation=False,
                risk_notes="Read-only behavior/audit lookup.",
                permission_default=PermissionAction.ALLOW,
            )
        )
        registry.register(
            ToolSpec(
                name="memory_save_observation",
                description="Save a short non-sensitive observation when the user explicitly asks.",
                permission_level=PermissionLevel.L0,
                input_schema=object_schema(
                    {
                        "text": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "importance": {"type": "integer", "minimum": 1, "maximum": 5},
                    },
                    required=["text"],
                ),
                output_schema=any_output_schema(),
                handler=self._save,
                requires_confirmation=False,
                risk_notes="Never store passwords, tokens, exchange keys or private data.",
                permission_default=PermissionAction.ALLOW,
            )
        )
        registry.register(
            ToolSpec(
                name="memory_save_preference",
                description=(
                    "Save a user preference or stable working rule when explicitly requested."
                ),
                permission_level=PermissionLevel.L0,
                input_schema=object_schema(
                    {
                        "text": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "importance": {"type": "integer", "minimum": 1, "maximum": 5},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                    required=["text"],
                ),
                output_schema=any_output_schema(),
                handler=self._save_preference,
                requires_confirmation=False,
                risk_notes="Never store secrets or private exchange credentials.",
                permission_default=PermissionAction.ALLOW,
            )
        )
        registry.register(
            ToolSpec(
                name="memory_forget",
                description="Forget one memory item by id and memory source type.",
                permission_level=PermissionLevel.L1,
                input_schema=object_schema(
                    {
                        "memory_id": {"type": "integer", "minimum": 1},
                        "memory_type": {
                            "type": "string",
                            "enum": ["composite", "observation", "behavior", "summary"],
                        },
                    },
                    required=["memory_id"],
                ),
                output_schema=any_output_schema(),
                handler=self._forget,
                requires_confirmation=True,
                risk_notes="Destructive memory deletion; confirmation required.",
                permission_default=PermissionAction.ASK,
            )
        )
        registry.register(
            ToolSpec(
                name="memory_compact_now",
                description="Compact older conversations into short searchable memory.",
                permission_level=PermissionLevel.L1,
                input_schema=object_schema(
                    {
                        "source": {"type": "string"},
                        "user_id": {"type": "string"},
                        "chat_id": {"type": "string"},
                    }
                ),
                output_schema=any_output_schema(),
                handler=self._compact_now,
                requires_confirmation=True,
                risk_notes="Creates distilled memory from existing conversation history.",
                permission_default=PermissionAction.ASK,
            )
        )

    def _recall(self, args: dict[str, Any]) -> dict[str, Any]:
        memory_types = args.get("memory_types")
        types = [str(item) for item in memory_types] if isinstance(memory_types, list) else None
        memories = self.db.search_memory(
            str(args.get("query") or ""),
            memory_types=types,
            limit=int(args.get("limit") or 8),
        )
        return {
            "success": True,
            "summary": f"根据工具结果: memory_recall 返回 {len(memories)} 条复合记忆。",
            "data": {"memories": memories},
        }

    def _search_behavior(self, args: dict[str, Any]) -> dict[str, Any]:
        records = self.db.behavior_records(
            str(args.get("query") or ""),
            limit=int(args.get("limit") or 10),
        )
        return {
            "success": True,
            "summary": f"根据工具结果: memory_search_behavior 返回 {len(records)} 条行为记录。",
            "data": {"behavior_records": records},
        }

    def _save(self, args: dict[str, Any]) -> dict[str, Any]:
        text = str(args.get("text") or "").strip()
        if not text:
            return {"success": False, "summary": "memory_save_observation 缺少 text。"}
        for observation in self.db.recent_observations(limit=50):
            if str(observation.get("text") or "").strip() == text:
                return {
                    "success": True,
                    "summary": (
                        "根据工具结果: observation 已存在, "
                        f"复用 #{observation.get('id')}。"
                    ),
                    "data": {"id": observation.get("id"), "deduped": True},
                }
        tags = args.get("tags") if isinstance(args.get("tags"), list) else []
        importance = int(args.get("importance") or 1)
        observation_id = self.db.save_observation(text, tags=tags, importance=importance)
        return {
            "success": True,
            "summary": f"根据工具结果: 已保存 observation #{observation_id}。",
            "data": {"id": observation_id},
        }

    def _save_preference(self, args: dict[str, Any]) -> dict[str, Any]:
        text = str(args.get("text") or "").strip()
        if not text:
            return {"success": False, "summary": "memory_save_preference 缺少 text。"}
        tags = args.get("tags") if isinstance(args.get("tags"), list) else []
        memory_id = self.db.save_composite_memory(
            memory_type="profile",
            text=text,
            tags=["preference", *[str(tag) for tag in tags]],
            importance=int(args.get("importance") or 3),
            confidence=float(args.get("confidence") or 1.0),
            source="user",
        )
        return {
            "success": True,
            "summary": f"根据工具结果: 已保存 profile memory #{memory_id}。",
            "data": {"id": memory_id, "memory_type": "profile"},
        }

    def _forget(self, args: dict[str, Any]) -> dict[str, Any]:
        memory_id = int(args.get("memory_id") or 0)
        memory_type = str(args.get("memory_type") or "composite")
        if memory_id <= 0:
            return {"success": False, "summary": "memory_forget 缺少有效 memory_id。"}
        deleted = self.db.forget_memory(memory_id, memory_type)
        return {
            "success": deleted,
            "summary": (
                f"根据工具结果: 已删除 {memory_type} memory #{memory_id}。"
                if deleted
                else f"未找到 {memory_type} memory #{memory_id}。"
            ),
            "data": {"id": memory_id, "memory_type": memory_type, "deleted": deleted},
        }

    def _compact_now(self, args: dict[str, Any]) -> dict[str, Any]:
        result = self.db.compact_memory_now(
            source=str(args.get("source") or "api"),
            user_id=str(args.get("user_id") or "local"),
            chat_id=str(args.get("chat_id") or "local"),
        )
        return {
            "success": True,
            "summary": (
                "根据工具结果: memory_compact_now 已执行, "
                f"created_summary={result.get('created_summary')}。"
            ),
            "data": result,
        }
