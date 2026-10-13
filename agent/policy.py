from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent


@dataclass
class PolicyDecision:
    """Result of a policy check."""
    action: Literal["allow", "deny", "needs_approval"]
    reason: str
    rule: str = ""


class PolicyEngine:
    """Enforces company policies as hard limits outside the LLM.
    
    Policies are loaded from company/policies.yaml and enforced regardless
    of what the LLM decides. This prevents prompt injection or model errors
    from bypassing safety rules.
    """
    
    def __init__(self, policies_path: Path | None = None):
        self.policies_path = policies_path or (ROOT_DIR / "company" / "policies.yaml")
        self.policies: dict[str, Any] = {}
        self.load()
    
    def load(self) -> None:
        """Load policies from YAML."""
        if self.policies_path.exists():
            with open(self.policies_path, "r", encoding="utf-8") as f:
                self.policies = yaml.safe_load(f) or {}
            logger.info(f"Loaded policies from {self.policies_path}")
        else:
            logger.warning(f"No policies file found at {self.policies_path}")
    
    def check_action(self, connector: str, tool: str, args: dict[str, Any], facts: dict[str, Any]) -> PolicyDecision:
        """Check if an action is allowed by policy.
        
        This is the hard enforcement layer. Even if the LLM decides to do something,
        this layer can deny it.
        """
        # Check blocked actions
        blocked = self.policies.get("blocked_actions", [])
        action_clean = f"{connector}.{tool}".lower().replace("_", " ")
        for blocked_pattern in blocked:
            pat_clean = blocked_pattern.lower().replace("_", " ")
            if pat_clean in action_clean:
                return PolicyDecision(action="deny", reason=f"Action matches blocked pattern: {blocked_pattern}", rule="blocked_actions")
        
        if connector == "chat" and tool == "decide_approval":
            return PolicyDecision(action="deny", reason="Only a human may decide approvals", rule="human_approval")

        # Refund-specific checks
        if tool == "create_refund":
            return self._check_refund_policy(args, facts)
        
        return PolicyDecision(action="allow", reason="No policy restrictions")
    
    def _check_refund_policy(self, args: dict[str, Any], facts: dict[str, Any]) -> PolicyDecision:
        """Apply refund-specific policy rules."""
        refund_policy = self.policies.get("refunds", {})
        
        amount = args.get("amount")
        if type(amount) is not int or amount <= 0:
            return PolicyDecision(action="deny", reason="Refund amount must be a positive integer", rule="refund_amount")

        # These values must be supplied from connector evidence, not LLM memory.
        # Missing evidence must never turn into a permissive default.
        # Check email match
        if refund_policy.get("require_email_match", True):
            ticket_email = facts.get("ticket_email", "")
            order_email = facts.get("order_customer_email", "")
            if not all(isinstance(email, str) and email.strip() for email in (ticket_email, order_email)):
                return PolicyDecision(action="deny", reason="Missing customer identity evidence", rule="require_email_match")
            if ticket_email.strip().lower() != order_email.strip().lower():
                return PolicyDecision(
                    action="deny",
                    reason=f"Email mismatch: ticket from '{ticket_email}' but order belongs to '{order_email}'. Possible fraud.",
                    rule="require_email_match",
                )
        
        # Check refund window
        window_days = refund_policy.get("window_days", 30)
        days_since_delivery = facts.get("days_since_delivery")
        if type(days_since_delivery) is not int or days_since_delivery < 0:
            return PolicyDecision(action="deny", reason="Missing or invalid delivery evidence", rule="window_days")
        if days_since_delivery > window_days:
            return PolicyDecision(
                action="deny",
                reason=f"Delivery was {days_since_delivery} days ago, outside {window_days}-day refund window.",
                rule="window_days",
            )
        
        # Check max refunds per order
        max_refunds = refund_policy.get("max_refunds_per_order", 1)
        existing_refunds = facts.get("existing_refund_count")
        if type(existing_refunds) is not int or existing_refunds < 0:
            return PolicyDecision(action="deny", reason="Missing or invalid refund history", rule="max_refunds_per_order")
        if existing_refunds >= max_refunds:
            return PolicyDecision(
                action="deny",
                reason=f"Order already has {existing_refunds} refund(s), max is {max_refunds}.",
                rule="max_refunds_per_order",
            )
        
        # Check auto-approve threshold
        auto_approve_max = refund_policy.get("auto_approve_max", 5000)
        if amount > auto_approve_max:
            return PolicyDecision(
                action="needs_approval",
                reason=f"Refund amount {amount} exceeds auto-approve limit of {auto_approve_max}.",
                rule="auto_approve_max",
            )
        
        return PolicyDecision(action="allow", reason="Refund within policy limits")
    
    def get_refund_policy_summary(self) -> str:
        """Get a human-readable summary of refund policies for the LLM."""
        rp = self.policies.get("refunds", {})
        return (
            f"Refund window: {rp.get('window_days', 30)} days from delivery. "
            f"Auto-approve up to \u20b9{rp.get('auto_approve_max', 5000)}. "
            f"Email on ticket must match order customer email. "
            f"Max {rp.get('max_refunds_per_order', 1)} refund(s) per order."
        )
    
    def get_untrusted_sources(self) -> list[str]:
        """Get list of untrusted data sources."""
        return self.policies.get("untrusted_sources", [])
    
    def is_social_engineering(self, text: str) -> bool:
        """Basic check for social engineering patterns in text."""
        patterns = [
            "i am the ceo", "i'm the ceo", "executive order",
            "i am the manager", "i'm the manager", "direct order",
            "refund all", "i'm your boss", "i am your boss",
            "failure to comply", "you will be fired", "terminated",
            "this is an order from", "as the owner",
            "override", "bypass policy", "skip verification",
        ]
        text_lower = text.lower()
        return any(p in text_lower for p in patterns)
