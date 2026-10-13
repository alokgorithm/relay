# Relay demo script

This 5–7 minute demonstration shows actual state changes in fictional local systems. Do not use real credentials or real customer data.

## Before recording

1. Configure a tool-capable model in `.env`; the local Ollama example in `README.md` is only a template.
2. From the repository root, install dependencies and run `relay seed`.
3. In a separate terminal, optionally start the read-only dashboard with `relay viewer --port 8000`.
4. Keep a terminal ready for `relay connectors tools` and one for the agent commands below.

## Scene 1 — discover integrations

Run:

```powershell
relay connectors tools
```

Explain that these are real MCP tools discovered from isolated local processes, and that scope checks hide writes from read-only integrations.

## Scene 2 — autonomous refund

Run:

```powershell
relay run --ticket 1
```

Narrate only what appears in the result: the agent selects a playbook, observes order/delivery/refund data, applies policy, and creates one idempotent refund. Open the resulting `runs/<run-id>/summary.md` and `verification.json`; show the public reply, terminal ticket state, and read-back evidence.

## Scene 3 — approval and durable continuation

Run ticket 2. It must pause before any payment change:

```powershell
relay run --ticket 2
relay approve <approval-id>
relay resume --run <run-id>
```

Show that resume keeps the same run ID and that the high-value refund is tied to the exact approved action. Mention that a second resume is safe: it reconciles persisted state rather than issuing a second refund.

## Scene 4 — safety and adaptation

Run the mismatched-email ticket:

```powershell
relay run --ticket 5
```

Show the escalation/internal evidence and confirm payment data is unchanged. This demonstrates that ticket text cannot override the policy boundary.

If time permits, run ticket 6 to show the same loop resolving a shipment-status request without a financial action.

## Scene 5 — extensibility and limits

Show `connectors/inventory/` and `company/playbooks/replacement.md`. Explain that the core loop is unchanged, while a new connector/playbook broadens the supported workflow. Close with the limitations in `README.md`: the prototype is a local, bounded proof of concept—not a claim of production browser/desktop control or universal autonomy.
