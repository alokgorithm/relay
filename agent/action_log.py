from __future__ import annotations
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from agent.state import ActionRecord, RunState

logger = logging.getLogger(__name__)


class ActionLog:
    """Write-ahead action log for exactly-once side effects.
    
    Every side-effecting action is recorded BEFORE execution with status 'started'
    and an idempotency key. After execution, status is updated to 'succeeded' or 'failed'.
    
    On resume after a crash:
    - 'started' actions: ambiguous - must reconcile by reading state
    - 'unknown' actions: timeout after send - must reconcile before retry
    - 'succeeded' actions: done, skip
    - 'failed' actions: can retry with same idempotency key
    """
    
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.log_file = run_dir / "actions.jsonl"
        run_dir.mkdir(parents=True, exist_ok=True)
    
    def make_key(self, ticket_id: str, step_id: int, tool: str) -> str:
        """Generate an idempotency key."""
        return f"{ticket_id}:{step_id}:{tool}"
    
    def record_start(self, state: RunState, key: str, connector: str, tool: str, args: dict[str, Any]) -> ActionRecord:
        """Record that an action is about to start (write-ahead)."""
        # Check for existing action with same key
        existing = state.has_action_with_key(key)
        if existing and existing.status == "succeeded":
            logger.info(f"Action {key} already succeeded, skipping")
            return existing
        
        record = ActionRecord(
            key=key,
            connector=connector,
            tool=tool,
            args=args,
            status="started",
        )
        state.actions.append(record)
        self._append_to_log(record)
        return record
    
    def record_success(self, state: RunState, key: str, result: dict[str, Any]) -> None:
        """Record that an action succeeded."""
        for action in state.actions:
            if action.key == key and action.status in ("started", "unknown"):
                action.status = "succeeded"
                action.result = result
                action.timestamp = datetime.now().isoformat()
                self._append_to_log(action)
                return
        logger.warning(f"No started/unknown action found for key {key}")
    
    def record_failure(self, state: RunState, key: str, error: str) -> None:
        """Record that an action failed."""
        for action in state.actions:
            if action.key == key and action.status in ("started", "unknown"):
                action.status = "failed"
                action.result = {"error": error}
                action.timestamp = datetime.now().isoformat()
                self._append_to_log(action)
                return
    
    def record_unknown(self, state: RunState, key: str) -> None:
        """Record that an action's outcome is unknown (timeout after send)."""
        for action in state.actions:
            if action.key == key and action.status == "started":
                action.status = "unknown"
                action.timestamp = datetime.now().isoformat()
                self._append_to_log(action)
                return
    
    def get_actions_needing_reconciliation(self, state: RunState) -> list[ActionRecord]:
        """Get actions that need reconciliation (started or unknown status)."""
        return state.get_unresolved_actions()
    
    def _append_to_log(self, record: ActionRecord) -> None:
        """Append an action record to the JSONL log file."""
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(record.model_dump_json() + "\n")
