from __future__ import annotations
import json
import re
import time
import logging
from typing import Any

from openai import OpenAI
from openai import APITimeoutError, RateLimitError, APIStatusError

from agent.llm.base import LLM, LLMResponse, ToolCall

logger = logging.getLogger(__name__)


class OpenAICompatLLM:
    """LLM adapter for any OpenAI-compatible endpoint.
    
    Covers: Gemini, Groq, OpenRouter, Ollama, LM Studio, Claude, etc.
    """
    
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
        self.max_retries = max_retries
        self.client = OpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            max_retries=0,  # We handle retries ourselves
        )
    
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
        
        last_error = None
        for attempt in range(1, self.max_retries + 1):
            try:
                logger.debug(f"LLM call attempt {attempt}/{self.max_retries}")
                response = self.client.chat.completions.create(**kwargs)
                return self._parse_response(response)
            except RateLimitError as e:
                last_error = e
                # Parse retry duration if provided in error message (e.g. Groq "try again in 24.4s")
                match = re.search(r"try again in ([\d\.]+)s", str(e))
                if match:
                    wait = float(match.group(1)) + 1.0
                else:
                    wait = min(2 ** attempt * 2, 35)
                logger.warning(f"Rate limited. Retrying in {wait:.1f}s...")
                time.sleep(wait)
            except APITimeoutError as e:
                last_error = e
                wait = min(2 ** attempt, 30)
                logger.warning(f"Timeout. Retrying in {wait}s...")
                time.sleep(wait)
            except APIStatusError as e:
                if e.status_code in (500, 502, 503):
                    last_error = e
                    wait = min(2 ** attempt, 30)
                    logger.warning(f"Server error {e.status_code}. Retrying in {wait}s...")
                    time.sleep(wait)
                else:
                    raise
        
        raise RuntimeError(f"LLM call failed after {self.max_retries} attempts: {last_error}")
    
    def _parse_response(self, response) -> LLMResponse:
        """Parse an OpenAI API response into our LLMResponse model."""
        choice = response.choices[0]
        message = choice.message
        
        tool_calls = []
        if message.tool_calls:
            for tc in message.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {"_raw": tc.function.arguments}
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
        
        return LLMResponse(
            content=message.content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason,
            usage=usage,
        )
