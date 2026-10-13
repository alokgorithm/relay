# Relay — Founding Engineer Submission Brief

## Project

Relay is a bounded autonomous customer-support operations agent. It interprets natural-language tickets, chooses company playbooks and MCP tools, executes real changes in fictional SQLite business systems, requests action-specific approvals, recovers durable execution state, and verifies outcomes.

This is a working proof of concept for CentrAlign AI's **AI Employee / Autonomous Company Operator** problem. It is not a universal desktop operator or a production enterprise platform.

## Why this scope

Customer support provides concrete objectives, policies, permissions, financial risks, partial failures, and verifiable business outcomes. A narrow environment makes actual execution and recovery observable instead of relying on simulated autonomy. The same loop handles refunds and shipment inquiries; procedures and integrations are separated from orchestration.

## Architecture and important decisions

- **LLM decision loop:** interpretation, playbook selection, planning, tool selection, observations, and adaptation.
- **MCP connector host:** discovers tool schemas from isolated shop, payments, helpdesk, chat, and optional inventory processes.
- **Deterministic safety:** scope checks and fresh refund-policy evidence outside the model. Human approval is bound to exact action arguments.
- **Durable execution:** write-ahead action log, atomic state snapshots, connector-supported refund idempotency, and reconciliation of ambiguous outcomes.
- **Approved-action continuation:** resume replays the exact approved action through fresh safety checks, rather than asking the model to reconstruct it.
- **Verification:** system-of-record read-back plus terminal ticket evidence. Successful tool responses and model narration are insufficient.
- **Cost controls:** selected-workflow context/tool filtering, compact snapshots, identical-error stopping, cumulative returned-response usage, and configurable per-ticket call budgets.

Detailed explanations: [README.md](README.md), [DESIGN.md](DESIGN.md), [TECHNICAL_WALKTHROUGH.md](TECHNICAL_WALKTHROUGH.md).

## Demonstrated results

| Scenario | Evidence and qualification |
|---|---|
| Basic refund | A real Nemotron run issued the refund, replied, and resolved the ticket; all read-back checks passed. |
| High-value approval/resume | Live run paused before payment, reused the pending request, completed one INR 18,000 refund after simulated-manager approval, and passed six checks. Terminal resume added no refund or model call. |
| Order tracking | Live run selected `order_status`, returned tracking number BD1047006, resolved the ticket, and passed three checks with no financial changes. |
| Email mismatch | Live run recognized the mismatch and preserved payment state but exhausted its bounded budget before escalation. The revised immediate-escalation instruction has **not** been live-retested. Separate real-MCP tests deny mismatched refunds and exercise escalation without a provider. |

Saved live evidence is under `runs/live_validation/`. The shareable archive includes only sanitized, all-passing summary/verification pairs; it excludes failed runs, private environment files, state/traces, and databases. Exported artifacts are for inspection, not resumable sandbox environments.

Automated tests, offline evaluation, clean editable/wheel installation, connector discovery, and dashboard page checks have passed. Run the commands in README to obtain the current exact test count. Offline scripted-model checks do not measure live-model reasoning quality or statistical reliability.

## Models, components, costs, and assumptions

- Successful live runs: **OpenRouter**, `nvidia/nemotron-3-super-120b-a12b:free`, native tool calling.
- Groq `openai/gpt-oss-120b` was also attempted, but account TPM limits prevented completion. OpenRouter's `openai/gpt-oss-120b:free` was unavailable through the API.
- Python 3.12+, MCP SDK/FastMCP, OpenAI SDK with compatible endpoints, Pydantic, SQLite, Typer/Rich, PyYAML, python-dotenv; optional FastAPI/Uvicorn viewer.
- Offline evaluation uses a scripted model and consumes no provider tokens.
- Fictional company data, trusted local connectors, sequential local operation, integer rupee amounts, and simulated manager identity.
- Recorded successful approval run: 16 model responses / 125,328 reported tokens. Tracking run: 9 / 44,602. These are observations, not cost guarantees. Failed runs also consume quota; provider usage is authoritative.
- AI coding assistance was used during implementation review and fixes. The submitter must understand and own the implementation.

## Limitations and what comes next

No browser/desktop control, arbitrary-company-goal intake, authenticated enterprise approval infrastructure, multi-tenancy, scheduler, or durable task queue. Idempotency is not universal exactly-once execution; supported policies/verifiers contain system-specific logic. Concurrent policy-read/write races and arbitrary connector security need production coordination. Model quality, availability, context growth, and cost remain important limitations.

Next: live scenario benchmarks and cost per verified completion; authenticated approvals and request-entity binding; per-ticket locking and queue workers; connector verification contracts; a verified file/browser workflow sharing the same execution envelope. Do not add broad computer control before these guarantees are tested.

## Final submission actions

- [x] Source, setup/run instructions, architecture, assumptions, components, limitations, and next steps documented.
- [x] Clean installation checked in temporary editable and non-editable wheel environments.
- [x] Repeatable offline tests and opt-in isolated live validation provided.
- [x] Demo recording script and interview walkthrough provided.
- [ ] Retest live ticket #5 when provider quota permits: `python eval/live_validate.py --ticket 5 --max-calls 12`.
- [ ] Record a genuine live demo using [DEMO.md](DEMO.md); a recording script or evidence archive is **not** a video.
- [ ] Publish the repository/source archive and share accessible source/video links. No Git remote was configured during validation; nothing has been pushed.
- [ ] Confirm the deadline/extension with the recruiter. The supplied JD says **October 9, 5:30 PM IST**; an extension has not been verified.
- [ ] Submit the application through the form given in the JD: https://forms.gle/CeaiXnsQ5oqx4dFx6.

## Build the source handoff

```sh
python eval/package_submission.py --include-evidence
```

Creates `dist/relay-submission.zip` from current on-disk source, including uncommitted files. It excludes `.env`, databases, caches, binaries, and private runtime history and checks for configured/pattern-matched credentials. Inspect the archive before sharing; automated scanning is not a substitute for a security review. This command neither commits nor publishes anything.
