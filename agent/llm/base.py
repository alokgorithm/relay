from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel


class ToolCall(BaseModel):
    """A tool call from the LLM."""
    id: str
    name: str
    arguments: dict[str, Any]


class LLMResponse(BaseModel):
    """Response from the LLM."""
    content: str | None = None
    tool_calls: list[ToolCall] = []
    finish_reason: str | None = None
    usage: dict[str, int] = {}
    raw: Any = None
    
    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0


class LLM(Protocol):
    """Protocol for LLM adapters."""
    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        json_schema: dict[str, Any] | None = None,
        temperature: float = 0.0,
    ) -> LLMResponse: ...
