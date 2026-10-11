from __future__ import annotations
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from pydantic import BaseModel, Field


class Step(BaseModel):
    """A step in the agent's plan."""
    id: int
    goal: str
    done_when: str
    status: Literal["pending", "active", "done", "failed", "skipped"] = "pending"


class ActionRecord(BaseModel):
    """Saga / action log entry for exactly-once semantics."""
    key: str  # idempotency key: f"{ticket_id}:{step_id}:{tool}"
    connector: str
    tool: str
    args: dict[str, Any]
    status: Literal["started", "succeeded", "failed", "compensated", "unknown"] = "started"
    result: dict[str, Any] | None = None
    compensation: str | None = None  # e.g. "payments.cancel_refund"
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())


class HumanRequest(BaseModel):
    """A pending request for human input."""
    type: Literal["approval", "customer_reply", "escalation"]
    approval_id: int | None = None
    message: str
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())


class RunState(BaseModel):
    """Complete state of an agent run, persisted every step."""
    run_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    ticket_id: str = ""
    playbook: str | None = None
    plan: list[Step] = []
    facts: dict[str, Any] = {}  # order, customer, amounts, policy decisions
    actions: list[ActionRecord] = []
    pending_human: HumanRequest | None = None
    status: Literal[
        "running", "waiting_human", "waiting_customer",
        "verifying", "resolved", "escalated", "failed"
    ] = "running"
    messages: list[dict[str, Any]] = []  # LLM conversation history
    step_count: int = 0
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    
    def save(self, run_dir: Path) -> None:
        """Persist state to disk."""
        run_dir.mkdir(parents=True, exist_ok=True)
        state_file = run_dir / "state.json"
        self.updated_at = datetime.now().isoformat()
        state_file.write_text(self.model_dump_json(indent=2), encoding="utf-8")
    
    @classmethod
    def load(cls, run_dir: Path) -> RunState:
        """Load state from disk."""
        state_file = run_dir / "state.json"
        if not state_file.exists():
            raise FileNotFoundError(f"No state found at {state_file}")
        return cls.model_validate_json(state_file.read_text(encoding="utf-8"))
    
    def get_active_step(self) -> Step | None:
        """Get the currently active step."""
        for step in self.plan:
            if step.status == "active":
                return step
        return None
    
    def get_next_pending_step(self) -> Step | None:
        """Get the next pending step."""
        for step in self.plan:
            if step.status == "pending":
                return step
        return None
    
    def advance_step(self) -> Step | None:
        """Mark active step as done and activate the next pending step."""
        active = self.get_active_step()
        if active:
            active.status = "done"
        next_step = self.get_next_pending_step()
        if next_step:
            next_step.status = "active"
        return next_step
    
    def has_action_with_key(self, key: str) -> ActionRecord | None:
        """Check if an action with this idempotency key already exists."""
        for action in self.actions:
            if action.key == key:
                return action
        return None
    
    def get_unresolved_actions(self) -> list[ActionRecord]:
        """Get actions that are started or unknown (need reconciliation)."""
        return [a for a in self.actions if a.status in ("started", "unknown")]
