# Relay — Autonomous Customer-Support Operations Agent

Relay turns a natural-language support ticket into actions across local company systems. An LLM selects tools and plans next steps; Python enforces refund policies, manages approvals, persists execution, and verifies outcomes through system-of-record reads.

Built for the CentrAlign AI **AI Employee / Autonomous Company Operator** challenge. The scope is intentionally narrow: customer support using fictional company data, not a production or universal desktop agent.

## What works

- Real MCP stdio servers for shop, payments, helpdesk, chat, and optional inventory, backed by SQLite. Actions actually change state.
- Model-driven planning/tool selection, with tool results returned as observations and explicit replanning rather than automatic step advancement.
- Company policies and Markdown playbooks, persisted working memory, and customer ticket context.
- Refund checks using fresh connector evidence, not model-authored facts: identity match, delivery window, previous refunds, positive integer amounts, and high-value approvals.
- Human approval bound to exact action arguments. The model cannot approve itself or edit a different ticket.
- Durable write-ahead action log, atomic state snapshots, refund idempotency, and read-back reconciliation of ambiguous writes.
- Completion requires verified side effects and a terminal ticket outcome; resolved/waiting-customer outcomes also require a verified public reply.
- Batch processing, audit artifacts, optional read-only web dashboard.

## Setup

Use Python **3.12+** and run commands from the repository root. With [uv](https://docs.astral.sh/uv/):

```sh
uv sync --extra dev --extra viewer
```

Or with Python/pip:

```sh
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# macOS/Linux instead:
# source .venv/bin/activate
python -m pip install -e ".[dev,viewer]"
```

With uv, prefix commands below with `uv run` if the environment is not activated. The viewer extra is optional; the dev extra installs pytest.

### Configure a model

Copy `.env.example` to `.env` and set:

```env
LLM_BASE_URL=http://localhost:11434/v1
LLM_API_KEY=local-not-a-secret
LLM_MODEL=qwen3:8b
TOOL_MODE=native
```

This is a local Ollama configuration example, **not a bundled model or a validated quality guarantee**. Start your local provider separately and download its model. Alternatively supply a hosted OpenAI-compatible endpoint, its API key, and a currently available tool-capable model. `TOOL_MODE=json` enables structured JSON tool selection when native calling is unavailable.

Hosted providers receive fictional ticket content and observations; token charges/rate limits depend on the provider. No business credentials are needed. Never commit `.env` or real customer data.

### Run

```sh
relay seed
relay connectors tools
relay run --ticket 1
relay shift
```

`python cli.py` can replace `relay` in these commands. **`seed` resets the demo databases**; do not reseed between approval and resume.

## Commands

| Command | Purpose |
|---|---|
| `relay seed` | Reset all five fictional system databases |
| `relay connectors list` | List configured connectors |
| `relay connectors tools` | Start MCP servers and inspect discovered tools |
| `relay run --ticket 1` | Let the configured LLM process a ticket |
| `relay shift` | Process the open queue and save a shift report |
| `relay resume --run <run_id>` | Reconcile and resume a saved run |
| `relay approve <approval_id>` | Simulate a human manager approving |
| `relay deny <approval_id>` | Simulate a human manager denying |
| `relay customer-reply 10 "My order number is 1042"` | Add a customer response; resume the original run afterward |
| `relay viewer --port 8000` | Run the optional dashboard at http://127.0.0.1:8000 |

Approval IDs come from the chat dashboard or the saved run's `pending_human.approval_id`. Approving does not automatically restart the worker; use `resume` with the printed run ID. On approved resume, the runtime replays the exact saved action through fresh safety checks before asking the model to continue. The model does not need to reconstruct or replan the approved write.

## Architecture and decisions

```text
Ticket → Runner / persisted state → LLM plan + tool selection
                                    ↓ observations / replan
                           Policy + approval checks
                                    ↓
                          Write-ahead action log
                                    ↓
                           MCP Connector Hub
                                    ↓
            Shop | Payments | Helpdesk | Chat | Inventory
                              SQLite systems
                                    ↓
                  Read-back verification → evidence
```

- **MCP:** standard tool discovery separates company integrations from orchestration. Connector scopes are checked at discovery and invocation.
- **Policies outside prompts:** refund evidence is re-read before execution. Ticket text and model memory cannot authorize a high-value refund or substitute for verified identity.
- **Durable execution:** write intent is logged before dispatch. Refund retries use the same key; ambiguous writes are reconciled, not assumed failed.
- **Conservative non-idempotent writes:** append operations are not blindly retried. When read-back cannot establish what happened, the run fails safely for manual review.
- **Verification distinct from execution:** a successful tool response or an LLM `finish` request is not sufficient proof of completion.
- **Memory:** playbooks/policies are company context; state/facts/observations are working memory. This is not a learning or vector-memory system.

Detailed vision: [DESIGN.md](DESIGN.md). A concise implementation explanation and repeatable recording plan are in [TECHNICAL_WALKTHROUGH.md](TECHNICAL_WALKTHROUGH.md) and [DEMO.md](DEMO.md). The design document includes planned/stretch work; this README describes current scope.

### Agent or workflow?

Relay is a **bounded, playbook-guided autonomous agent**, not a fixed sequence executor and not a general-purpose company/desktop operator. The model interprets ticket text, selects a playbook, proposes/revises its plan, chooses tools/arguments, and observes results. Python supplies the execution envelope: permissions, approvals, durable continuation, reconciliation, verification, and budgets. Deterministically replaying an approved action is reliable execution, not simulated model autonomy.

Its entry point is a support ticket, not an arbitrary company goal. Integrations are discoverable, but known sensitive policies/verifiers still contain connector-specific logic. Additional domains require procedures, tools, safety checks, and verification contracts; plugging in a connector alone does not establish universal generalization.

Live checks using `nvidia/nemotron-3-super-120b-a12b:free` have completed the basic refund, high-value approval/resume, and order-tracking flows. The email-mismatch run preserved payment state and recorded the mismatch, but exhausted its bounded budget before escalation; its tightened escalation instructions still need a live retest. A separate real-MCP check (without an LLM) confirmed that an attempted mismatched refund is denied by fresh identity evidence before payment/approval creation. These observations are not a statistical reliability benchmark.

## Token usage and cost controls

These controls apply to paid and free providers alike:

- Planning sends a short playbook catalog; execution includes only the selected full workflow.
- Business tool schemas are filtered by the selected playbook's `Connectors Needed` section. Unknown/malformed selections fall back to all available business tools, rather than silently blocking the task.
- Outgoing snapshots omit duplicated ticket text, superseded plan snapshots, and repeated write results. Durable history and complete tool-call/result pairs remain available; there is no arbitrary history truncation.
- Three identical error-only tool batches without state progress stop the run instead of consuming the full execution budget.
- `state.json` stores cumulative returned-response usage under `facts._llm_usage`; `trace.jsonl` includes `llm_usage` events. The CLI displays calls and input/output tokens.

Set the per-ticket budget in `config.yaml`:

```yaml
agent:
  max_steps: 30
  max_llm_calls: 20
```

The call count persists across approval/customer resumes. Budget exhaustion marks the run failed, never completed; increase the configured budget and resume if more work is justified. `null` disables this extra call limit. This counts returned adapter responses, **not** every underlying API request: provider retries and JSON-mode repair calls can add requests/billing. Usage reflects fields the model reports; your provider dashboard remains authoritative. It is not a hard monetary/token cap.

Offline regression fixtures compare serialized request sizes before/after optimization. Those byte comparisons are not token counts or guaranteed cost savings. Actual costs still depend on model pricing, reasoning/output tokens, retries, task complexity, and history growth. Full observation history is retained, so long-running tasks can still become expensive.

For production: measure cost per **verified completed task**, use a cheaper model for routine work with explicit escalation, add provider-backed spend caps, and implement tested evidence-preserving history compaction. Never remove identity, approval, or read-back checks merely to save tokens.

## Validation

```sh
python -m pytest tests -q
python eval/run_eval.py
```

Tests and evaluation use temporary databases/run paths, not the demo data. No API key is required.

The offline evaluation has **five checks**:
1. A **scripted model** drives the real loop and MCP tools through a refund, customer reply, status update, and verification.
2. Repeated-key refund deduplication (not a chaos benchmark).
3. High-value policy threshold and simulated-human approval/refund primitives.
4. Policy denial checks and social-engineering detection examples.
5. Inventory discovery, replacement creation/read-back, and stock change.

Regression tests also cover runtime pause/resume, crash/WAL recovery, ambiguous writes, malformed model responses, scopes, and payment safety. **Offline passes do not prove live-model autonomy, prompt-injection immunity, or arbitrary-workflow generalization.** Record an actual LLM-driven run for the submission video.

## Demo and evidence

Seeded scenarios include: simple damage refund (#1), high-value approval (#2), existing refund (#3), expired window (#4), email mismatch (#5), order tracking (#6), authority-claim injection (#7), fault/recovery candidates (#8–9), and missing details (#10). Faults require `CHAOS_SEED`; ticket numbers alone do not inject failures. Do not promise store credit: there is no store-credit connector.

For a short video:
1. Show `connectors tools`, the dashboard, and ticket #1 before execution.
2. Run `relay run --ticket 1` with a real configured model. Show the refund, public reply, status, and `verification.json`.
3. Run ticket #2, show the pause and exact approval action, approve it, then resume the **same** run.
4. Show a duplicate refund/identity mismatch denial, and the resulting evidence rather than just narration.
5. Show a second task such as tracking (#6), or the inventory experiment below.
6. End with limitations and how you would productionize the system.

Each `runs/<run_id>/` contains `state.json`, `trace.jsonl`, `actions.jsonl`, `verification.json`, and `summary.md`. Batch reports are saved under `runs/`. Runtime artifacts are gitignored; include sanitized screenshots/video evidence separately if desired.

### Connector extensibility experiment

Add this under `connectors:` in `connectors.yaml`:

```yaml
  inventory:
    command: ["python", "-m", "connectors.inventory"]
    scopes: [read, write]
```

Start a **new CLI invocation** to discover it; there is no hot reload in an already running worker. The replacement playbook is already present at `company/playbooks/replacement.md`. The connector integration is tested; model-driven replacement behavior still needs a live walkthrough. Known write verifiers are implemented in Python, so an arbitrary new write tool also needs a verifier before it can be reported completed safely.

## Assumptions and limitations

- Fictional company, trusted local connector implementations, sequential local execution, amounts represented as integer rupees in this sandbox.
- No real Shopify/Stripe/Zendesk integration, browser/desktop automation, multi-tenant authentication, deployment, scheduler, or durable task queue.
- Simulated manager CLI is not authenticated enterprise approval infrastructure.
- Refund deduplication depends on connector-supported keys. This is **not universal exactly-once execution or a complete compensating saga**; irreversible refunds are not rolled back automatically.
- Refund policy reads and execution are not a distributed transaction; concurrent worker races need production coordination. Fresh identity evidence does not establish the customer's intent for an arbitrary order belonging to that same customer.
- Finite execution budget and model-dependent tool calling; weak/rate-limited models may fail or require help. A failed verification does not automatically repair every workflow.
- Injection heuristics are not a security proof. Hard controls protect specific known operations, not every possible extension.
- Company lessons are not an implemented feedback-learning loop. Connector/playbook extensibility does not establish open-ended task generalization.

## Next steps

1. Evaluate real model runs across all ticket scenarios and deterministic faults; measure outcome correctness and escalation quality.
2. Bind sensitive operations to verified request entities and add authenticated, action-specific approval identities.
3. Add connector-specific verification contracts and durable idempotency for all writes.
4. Introduce per-ticket locks, queue workers, scheduling, authentication, secret management, and observability.
5. Add a file/browser workflow only when it shares the same execution, recovery, and verification guarantees.

## Components and submission checklist

Python, official MCP SDK/FastMCP, OpenAI client (configurable OpenAI-compatible provider), Pydantic, SQLite, Typer/Rich, PyYAML, python-dotenv, and optional FastAPI/Uvicorn. Offline evaluation uses a scripted model; runtime uses the model configured by the submitter. AI coding assistance was used during review/fixes; be ready to explain and modify the safety/recovery logic yourself.

Before submitting:
- [ ] Publish the source repository without secrets or real customer data.
- [ ] Test setup on a clean checkout.
- [ ] Record a real LLM-driven demo and share an accessible video/live-demo link.
- [ ] State the exact provider/model actually used and any API costs.
- [ ] Review the safety, approval, reconciliation, and verification paths for the technical discussion.
- [ ] Confirm the deadline with the recruiter: the provided JD states **October 9, 5:30 PM IST**.
