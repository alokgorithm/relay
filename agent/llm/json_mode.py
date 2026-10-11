from __future__ import annotations
import json
import logging
from typing import Any

from agent.llm.base import LLM, LLMResponse, ToolCall

logger = logging.getLogger(__name__)

JSON_TOOL_SYSTEM = """You are a tool-using assistant. When you need to use a tool, respond ONLY with a JSON object:
{"thought": "your reasoning", "tool": "tool_name", "args": {"param": "value"}}

When you don't need a tool, respond with:
{"thought": "your reasoning", "response": "your response text"}

Available tools:
{tool_descriptions}"""


class JsonModeLLM:
    """Wraps an LLM to use JSON-based tool calling for models without native support.
    
    Converts tool schemas to text descriptions, parses JSON responses into ToolCalls.
    Includes repair logic: if the model returns invalid JSON, feed the error back (max 2 retries).
    """
    
    def __init__(self, inner: Any):  # inner implements the LLM protocol
        self.inner = inner
    
    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        json_schema: dict[str, Any] | None = None,
        temperature: float = 0.0,
    ) -> LLMResponse:
        if not tools:
            return self.inner.chat(messages, temperature=temperature)
        
        # Convert tools to text descriptions
        tool_desc = self._format_tools(tools)
        
        # Modify system message to include tool descriptions
        modified_messages = self._inject_tool_system(messages, tool_desc)
        
        # Try to get valid JSON response with repair
        for repair_attempt in range(3):
            response = self.inner.chat(modified_messages, temperature=temperature)
            
            if not response.content:
                return response
            
            try:
                parsed = json.loads(response.content)
                if "tool" in parsed:
                    return LLMResponse(
                        content=parsed.get("thought"),
                        tool_calls=[ToolCall(
                            id=f"json_{repair_attempt}",
                            name=parsed["tool"],
                            arguments=parsed.get("args", {}),
                        )],
                        finish_reason="tool_calls",
                        usage=response.usage,
                    )
                elif "response" in parsed:
                    return LLMResponse(
                        content=parsed["response"],
                        tool_calls=[],
                        finish_reason="stop",
                        usage=response.usage,
                    )
                else:
                    return LLMResponse(
                        content=response.content,
                        tool_calls=[],
                        finish_reason="stop",
                        usage=response.usage,
                    )
            except json.JSONDecodeError as e:
                logger.warning(f"JSON repair attempt {repair_attempt + 1}: {e}")
                modified_messages.append({"role": "assistant", "content": response.content})
                modified_messages.append({
                    "role": "user",
                    "content": f"Your response was not valid JSON. Error: {e}. Please respond with valid JSON only."
                })
        
        # Give up on JSON, return as plain text
        return LLMResponse(
            content=response.content if response else None,
            tool_calls=[],
            finish_reason="stop",
        )
    
    def _format_tools(self, tools: list[dict[str, Any]]) -> str:
        lines = []
        for tool in tools:
            func = tool.get("function", {})
            name = func.get("name", "unknown")
            desc = func.get("description", "")
            params = func.get("parameters", {})
            props = params.get("properties", {})
            required = params.get("required", [])
            
            param_strs = []
            for pname, pschema in props.items():
                req = "*" if pname in required else ""
                ptype = pschema.get("type", "any")
                pdesc = pschema.get("description", "")
                param_strs.append(f"    {pname}{req} ({ptype}): {pdesc}")
            
            lines.append(f"- {name}: {desc}")
            if param_strs:
                lines.extend(param_strs)
        
        return "\n".join(lines)
    
    def _inject_tool_system(self, messages: list[dict[str, Any]], tool_desc: str) -> list[dict[str, Any]]:
        system_content = JSON_TOOL_SYSTEM.format(tool_descriptions=tool_desc)
        modified = []
        system_found = False
        for msg in messages:
            if msg["role"] == "system":
                modified.append({
                    "role": "system",
                    "content": msg["content"] + "\n\n" + system_content
                })
                system_found = True
            else:
                modified.append(msg.copy())
        
        if not system_found:
            modified.insert(0, {"role": "system", "content": system_content})
        
        return modified
