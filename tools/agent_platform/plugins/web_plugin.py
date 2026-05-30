from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from agent_platform.config import Settings
from agent_platform.registry.permissions import PermissionAction, PermissionLevel
from agent_platform.registry.tool_registry import (
    ToolRegistry,
    ToolSpec,
    any_output_schema,
    object_schema,
)
from agent_platform.storage.db import sanitize_data


class TavilyApiError(RuntimeError):
    pass


class TavilyClient:
    def __init__(self, settings: Settings, timeout: float = 20.0) -> None:
        self.api_key = settings.tavily_api_key
        self.base_url = settings.tavily_base_url.rstrip("/")
        self.max_results = settings.tavily_max_results
        self.timeout = timeout
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"}:
            raise TavilyApiError("TAVILY_BASE_URL must use http or https.")

    def post(self, endpoint: str, payload: dict[str, Any]) -> Any:
        if not self.api_key:
            raise TavilyApiError("Missing TAVILY_API_KEY.")
        url = urljoin(f"{self.base_url}/", endpoint.lstrip("/"))
        request = Request(  # noqa: S310 - base URL scheme is validated in __init__.
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise TavilyApiError(f"Tavily HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError) as exc:
            raise TavilyApiError(f"Tavily unavailable: {exc}") from exc
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TavilyApiError("Tavily returned non-JSON response.") from exc


class WebPlugin:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = TavilyClient(settings)

    def register(self, registry: ToolRegistry) -> None:
        registry.register(
            ToolSpec(
                name="web_search",
                description="Search current web/news/finance sources with Tavily.",
                permission_level=PermissionLevel.L0,
                input_schema=object_schema(
                    {
                        "query": {"type": "string"},
                        "topic": {
                            "type": "string",
                            "enum": ["general", "news", "finance"],
                            "default": "general",
                        },
                        "time_range": {
                            "type": "string",
                            "enum": ["day", "week", "month", "year", "d", "w", "m", "y"],
                        },
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 10},
                    },
                    required=["query"],
                ),
                output_schema=any_output_schema(),
                handler=self._search,
                requires_confirmation=False,
                risk_notes=(
                    "External web search may incur Tavily API cost and can retrieve untrusted text."
                ),
                permission_default=PermissionAction.ASK,
            )
        )
        registry.register(
            ToolSpec(
                name="web_fetch",
                description="Fetch/extract clean content from a URL with Tavily.",
                permission_level=PermissionLevel.L0,
                input_schema=object_schema(
                    {
                        "url": {"type": "string"},
                        "format": {
                            "type": "string",
                            "enum": ["markdown", "text"],
                            "default": "markdown",
                        },
                    },
                    required=["url"],
                ),
                output_schema=any_output_schema(),
                handler=self._fetch,
                requires_confirmation=False,
                risk_notes=(
                    "External web fetch may incur Tavily API cost and can retrieve untrusted text."
                ),
                permission_default=PermissionAction.ASK,
            )
        )

    def _search(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or "").strip()
        if not query:
            return {"success": False, "summary": "web_search 缺少 query。"}
        max_results = int(args.get("max_results") or self.client.max_results)
        max_results = max(1, min(max_results, 10))
        payload: dict[str, Any] = {
            "query": query,
            "topic": str(args.get("topic") or "general"),
            "search_depth": "basic",
            "max_results": max_results,
            "include_answer": "basic",
            "include_raw_content": False,
        }
        if args.get("time_range"):
            payload["time_range"] = str(args["time_range"])
        data = self.client.post("search", payload)
        results = data.get("results") if isinstance(data, dict) else []
        return {
            "success": True,
            "summary": f"根据工具结果: web_search 返回 {len(results or [])} 条结果。",
            "data": sanitize_data(data),
        }

    def _fetch(self, args: dict[str, Any]) -> dict[str, Any]:
        url = str(args.get("url") or "").strip()
        if urlparse(url).scheme not in {"http", "https"}:
            return {"success": False, "summary": "web_fetch 需要 http/https URL。"}
        payload = {
            "urls": [url],
            "format": str(args.get("format") or "markdown"),
            "extract_depth": "basic",
        }
        data = self.client.post("extract", payload)
        results = data.get("results") if isinstance(data, dict) else []
        return {
            "success": True,
            "summary": f"根据工具结果: web_fetch 返回 {len(results or [])} 个 URL 内容。",
            "data": sanitize_data(data),
        }
