from __future__ import annotations

import json
from collections.abc import Generator
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from agent_platform.config import Settings


class LLMError(RuntimeError):
    pass


class OpenAICompatibleClient:
    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.llm_base_url.rstrip("/")
        self.api_key = settings.llm_api_key
        self.model = settings.llm_model
        self.timeout = settings.llm_timeout_seconds
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"}:
            raise LLMError("LLM_BASE_URL must use http or https.")

    def _build_request(
        self,
        payload: dict[str, Any],
        *,
        stream: bool = False,
    ) -> Request:
        headers = {
            "Accept": "text/event-stream" if stream else "application/json",
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        url = urljoin(f"{self.base_url}/", "chat/completions")
        return Request(  # noqa: S310 - base URL scheme is validated in __init__.
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers=headers,
        )

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_choice: str = "auto",
    ) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.1,
            "tools": tools,
            "tool_choice": tool_choice,
        }
        request = self._build_request(payload)
        try:
            with urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise LLMError(f"LLM HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError) as exc:
            raise LLMError(f"LLM unavailable: {exc}") from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMError("LLM returned non-JSON response.") from exc

        try:
            return data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"LLM response missing choices[0].message: {data}") from exc

    def chat_stream(  # noqa: C901 - SSE chunks and tool-call deltas are parsed together.
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_choice: str = "auto",
    ) -> Generator[dict[str, Any], None, None]:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.1,
            "tools": tools,
            "tool_choice": tool_choice,
            "stream": True,
        }
        request = self._build_request(payload, stream=True)
        try:
            response = urlopen(request, timeout=self.timeout)  # noqa: S310
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise LLMError(f"LLM HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError) as exc:
            raise LLMError(f"LLM unavailable: {exc}") from exc

        tool_calls_map: dict[int, dict[str, Any]] = {}
        try:
            for line in response:
                decoded = line.decode("utf-8").strip()
                if not decoded or not decoded.startswith("data: "):
                    continue
                data_str = decoded[6:]
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                choice = chunk.get("choices", [{}])[0]
                delta = choice.get("delta", {})

                content = delta.get("content")
                if content:
                    yield {"type": "text_delta", "content": content}

                delta_tool_calls = delta.get("tool_calls")
                if delta_tool_calls:
                    for tc in delta_tool_calls:
                        idx = tc.get("index", 0)
                        if idx not in tool_calls_map:
                            tool_calls_map[idx] = {
                                "id": tc.get("id", ""),
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            }
                        entry = tool_calls_map[idx]
                        if tc.get("id"):
                            entry["id"] = tc["id"]
                        fn = tc.get("function", {})
                        if fn.get("name"):
                            entry["function"]["name"] += fn["name"]
                        if fn.get("arguments"):
                            entry["function"]["arguments"] += fn["arguments"]

                finish_reason = choice.get("finish_reason")
                if finish_reason:
                    if finish_reason == "tool_calls" and tool_calls_map:
                        calls = [tool_calls_map[i] for i in sorted(tool_calls_map)]
                        yield {"type": "tool_calls", "tool_calls": calls}
                    elif finish_reason == "stop":
                        pass
        finally:
            response.close()
