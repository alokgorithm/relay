# Relay technical walkthrough

Relay is a bounded autonomous customer-support operator for a fictional retail company. It accepts a support ticket, interprets the requested outcome, selects a playbook, operates mock business systems through MCP, and reports completion only after reading those systems back.

It deliberately demonstrates a narrow workflow deeply rather than claiming universal desktop automation. The prototype uses only fictional SQLite-backed services and never needs company credentials.

## The runtime

```text
support ticket
  -> LLM chooses a playbook and plan
  -> LLM chooses the next MCP/control action
  -> policy, scopes, approval and durability checks
  -> MCP connector call
  -> persisted observation and replanning
  -> system-of-record verification
  -> concise audit summary
```

The LLM handles interpretation and adaptation. Python remains the trusted execution envelope:

- `agent/loop.py` owns the state machine, pauses/resumes, control tools, and final outcome.
- `agent/hub.py` discovers MCP tools at runtime, filters them for the selected playbook, enforces connector scopes, and applies safe retry rules.
- `agent/policy.py` rechecks refund evidence from current system data rather than trusting model-generated facts.
- `agent/action_log.py` writes action intent before any side effect, so a later resume can reconcile an uncertain outcome.
- `agent/verifier.py` reads the systems of record after execution; a successful tool response or an LLM claim is not sufficient.

## Why the boundaries matter

The agent can decide **what** to attempt, but cannot decide that a payment is safe, approve itself, or invent verification. For a refund it must establish current identity, delivery window, prior-refund, amount, and charge facts. A refund over the configured threshold becomes an immutable, action-specific manager request. On resume, the stored approved action is replayed only after fresh safety checks.

For a timeout or response loss, Relay records the write as uncertain and reads the remote system before retrying. Refunds use connector-supported idempotency keys. Append-style operations without a safe reconciliation path are not blindly retried.

## Demonstrating autonomy

Use a configured tool-capable OpenAI-compatible model and run these commands from the repository root:

```powershell
relay seed
relay connectors tools
relay run --ticket 1
```

Ticket 1 asks for a refund for a damaged order. A successful run should show a planned sequence, system observations, exactly one refund, an agent reply, a closed ticket, and a `verification.json` artifact under `runs/<run-id>/`.

Then demonstrate a human gate:

```powershell
relay run --ticket 2
relay approve <approval-id>
relay resume --run <run-id>
```

The first run should stop before the INR 18,000 refund. After approval, resume the same run; it reuses the bound action rather than asking the model to reconstruct it.

Finally run a read-only request and a safety case:

```powershell
relay run --ticket 6
relay run --ticket 5
```

Ticket 6 should return actual shipment information. Ticket 5 has an email mismatch and must escalate without changing payment state.

## Extensibility

Each business system is an isolated MCP server configured in `connectors.yaml`. A playbook names the connectors needed for a workflow; the runtime discovers their schemas and only supplies that relevant subset to the model. The included inventory connector and `replacement` playbook demonstrate a second workflow without changing the central planning loop.

New sensitive write operations still require an explicit policy and verification contract. This is intentional: discovery makes integration reusable, but it must not turn arbitrary new side effects into trusted completion claims.

## Evidence and evaluation

Each run persists `state.json`, `trace.jsonl`, `actions.jsonl`, `verification.json`, and `summary.md`. The source tree includes offline tests and an evaluation harness; neither requires provider credentials:

```powershell
python -m pytest tests -q
python eval/run_eval.py
```

For a live, isolated provider check, use `python eval/live_validate.py --ticket 1`. It writes sanitized evidence and keeps raw database/provider configuration out of the shareable output.

## Limitations and production path

Relay does not claim browser/desktop automation, arbitrary company goals, production authentication, multi-tenancy, scheduler/queue workers, or universal exactly-once delivery. Its local connectors and manager approval are demonstrative.

Productionizing the approach would add authenticated identities and delegated credentials, durable queue workers and per-task locks, distributed tracing/metrics, connector-specific authorization and verification contracts, provider spend controls, secure company-memory management, and measured evaluation against real sanctioned workflows.
