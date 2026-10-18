from __future__ import annotations

import json
import logging
import math
import re
import time
from urllib.parse import urlparse
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Any

from openai import APIStatusError, APITimeoutError, OpenAI, RateLimitError

from agent.llm.base import LLMResponse, ToolCall

logger = logging.getLogger(__name__)

_INITIAL_RATE_LIMIT_WAIT_S = 4.0
_MAX_RATE_LIMIT_WAIT_S = 120.0


def _retry_after_seconds(error: RateLimitError) -> float | None:
    """Read a provider delay without assuming a particular plan or token quota."""
    header = error.response.headers.get("retry-after")
    if header:
        try:
            delay = float(header)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(header)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                delay = max(0.0, retry_at.timestamp() - time.time())
            except (ValueError, TypeError, OverflowError):
                delay = None
        if delay is not None and math.isfinite(delay) and delay >= 0:
            return delay

    # Groq-style hints include "24.4s", "1m2.5s", and spelled-out units.
    number = r"(?:\d+(?:\.\d*)?|\.\d+)"
    unit = r"(?:milliseconds?|ms|minutes?|mins?|m|seconds?|secs?|s)(?![a-z])"
    match = re.search(
        rf"(?:try|retry)\s+again\s+in\s+((?:{number}\s*{unit}\s*(?:and\s*)?)+)",
        str(error),
        flags=re.IGNORECASE,
    )
    if match:
        delay = 0.0
        for value, units in re.findall(rf"({number})\s*({unit})", match.group(1), flags=re.IGNORECASE):
            units = units.lower()
            multiplier = 0.001 if units.startswith(("ms", "millisecond")) else 60.0 if units.startswith("m") else 1.0
            delay += float(value) * multiplier
        if math.isfinite(delay):
            return delay
    return None


class OpenAICompatLLM:
    """LLM adapter for any OpenAI-compatible endpoint.
    
    Covers: Gemini, Groq, OpenRouter, Ollama, LM Studio, Claude, etc.
    """
    _openrouter: bool = False
    _session_id: str | None = None
    
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        tool_mode: str = "native",  # native | json
        max_retries: int = 5,
        timeout: float = 60.0,
    ):
        self.model = model
        self.tool_mode = tool_mode
        self.max_retries = max(1, max_retries)
        self._openrouter = urlparse(base_url).hostname in {"openrouter.ai", "www.openrouter.ai"}
        self._session_id: str | None = None
        self.client = OpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            max_retries=0,  # We handle retries ourselves
        )

    def set_session_id(self, session_id: str) -> None:
        """Keep one OpenRouter agent run on the provider that owns its cache."""
        self._session_id = session_id[:256]
    
    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        json_schema: dict[str, Any] | None = None,
        temperature: float = 0.0,
    ) -> LLMResponse:
        """Send a chat completion request with retry logic."""
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        
        if tools and self.tool_mode == "native":
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        
        if json_schema:
            kwargs["response_format"] = {"type": "json_object"}
        if getattr(self, "_openrouter", False) and getattr(self, "_session_id", None):
            # OpenRouter forwards this top-level field and uses it for sticky
            # routing, preserving provider prompt-cache affinity per run.
            kwargs["extra_body"] = {"session_id": self._session_id}
        
        last_error = None
        rate_limit_backoff = _INITIAL_RATE_LIMIT_WAIT_S
        for attempt in range(1, self.max_retries + 1):
            try:
                logger.debug(f"LLM call attempt {attempt}/{self.max_retries}")
                response = self.client.chat.completions.create(**kwargs)
                parsed = self._parse_response(response)
                allowed_tools = {tool["function"]["name"] for tool in (tools or [])} if self.tool_mode == "native" else set()
                if any(tc.name not in allowed_tools for tc in parsed.tool_calls):
                    raise ValueError("LLM returned a tool that was not offered")
                return parsed
            except RateLimitError as e:
                last_error = e
                if attempt < self.max_retries:
                    suggested = _retry_after_seconds(e)
                    wait = max(rate_limit_backoff, suggested or 0.0)
                    if wait > _MAX_RATE_LIMIT_WAIT_S:
                        raise RuntimeError(
                            f"LLM rate limited: requested wait {wait:.1f}s exceeds the "
                            f"{_MAX_RATE_LIMIT_WAIT_S:.0f}s retry wait limit; retry later."
                        ) from e
                    # Repeated 429s show that the last wait was insufficient, even
                    # when a provider keeps returning the same short retry hint.
                    rate_limit_backoff = min(wait * 2, _MAX_RATE_LIMIT_WAIT_S)
                    logger.warning(f"Rate limited. Retrying in {wait:.1f}s...")
                    time.sleep(wait)
            except APITimeoutError as e:
                last_error = e
                rate_limit_backoff = _INITIAL_RATE_LIMIT_WAIT_S
                wait = min(2 ** attempt, 30)
                if attempt < self.max_retries:
                    logger.warning(f"Timeout. Retrying in {wait}s...")
                    time.sleep(wait)
            except APIStatusError as e:
                rate_limit_backoff = _INITIAL_RATE_LIMIT_WAIT_S
                if e.status_code in (500, 502, 503):
                    last_error = e
                    wait = min(2 ** attempt, 30)
                    if attempt < self.max_retries:
                        logger.warning(f"Server error {e.status_code}. Retrying in {wait}s...")
                        time.sleep(wait)
                else:
                    raise
        
        raise RuntimeError(f"LLM call failed after {self.max_retries} attempts: {last_error}") from last_error
    
    def _parse_response(self, response) -> LLMResponse:
        """Parse an OpenAI API response into our LLMResponse model."""
        if not response.choices:
            raise ValueError("LLM returned no completion choices")
        choice = response.choices[0]
        if choice.finish_reason in ("length", "content_filter"):
            raise ValueError(f"LLM completion is incomplete: {choice.finish_reason}")
        message = choice.message
        
        tool_calls = []
        if message.tool_calls:
            for tc in message.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError as e:
                    raise ValueError(f"Invalid JSON arguments for {tc.function.name}") from e
                if not isinstance(args, dict):
                    # JSON syntax parsed, but the provider returned an invalid envelope.
                    raise ValueError(f"Arguments for {tc.function.name} must be a JSON object")  # noqa: TRY004
                tool_calls.append(ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=args,
                ))
        
        usage = {}
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens or 0,
                "completion_tokens": response.usage.completion_tokens or 0,
                "total_tokens": response.usage.total_tokens or 0,
            }
            details = getattr(response.usage, "prompt_tokens_details", None)
            cached = getattr(details, "cached_tokens", None) if details is not None else None
            if type(cached) is int and cached >= 0:
                usage["cached_prompt_tokens"] = cached
            cache_writes = getattr(details, "cache_write_tokens", None) if details is not None else None
            if type(cache_writes) is int and cache_writes >= 0:
                usage["cache_write_tokens"] = cache_writes
        
        return LLMResponse(
            content=message.content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason,
            usage=usage,
        )
