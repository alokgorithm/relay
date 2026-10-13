from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class Tracer:
    """Event tracer that writes structured events to trace.jsonl.
    
    Events have a stable format suitable for future Go service consumption.
    """
    
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.trace_file = run_dir / "trace.jsonl"
        run_dir.mkdir(parents=True, exist_ok=True)
    
    def event(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        """Write a trace event."""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "type": event_type,
            "data": data or {},
        }
        with open(self.trace_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    
    def tool_call(self, connector: str, tool: str, args: dict, key: str | None = None) -> None:
        self.event("tool_call", {"connector": connector, "tool": tool, "args": args, "idempotency_key": key})
    
    def tool_result(self, connector: str, tool: str, result: Any, success: bool = True) -> None:
        self.event("tool_result", {"connector": connector, "tool": tool, "result": result, "success": success})
    
    def llm_call(self, messages_count: int, tools_count: int) -> None:
        self.event("llm_call", {"messages": messages_count, "tools": tools_count})
    
    def llm_response(self, content: str | None, tool_calls: int, usage: dict) -> None:
        self.event("llm_response", {"content_preview": (content or "")[:200], "tool_calls": tool_calls, "usage": usage})
    
    def plan_created(self, steps: list[dict]) -> None:
        self.event("plan_created", {"steps": steps})
    
    def step_started(self, step_id: int, goal: str) -> None:
        self.event("step_started", {"step_id": step_id, "goal": goal})
    
    def step_completed(self, step_id: int, status: str) -> None:
        self.event("step_completed", {"step_id": step_id, "status": status})
    
    def verification(self, results: dict) -> None:
        self.event("verification", results)
    
    def error(self, message: str, details: dict | None = None) -> None:
        self.event("error", {"message": message, **(details or {})})
    
    def run_completed(self, status: str, summary: str) -> None:
        self.event("run_completed", {"status": status, "summary": summary})
