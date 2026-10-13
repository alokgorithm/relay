"""Relay CLI — Autonomous customer support agent.

Commands:
  relay seed              Seed all connector databases with demo data
  relay run --ticket N    Process a single ticket
  relay shift             Process all open tickets (batch mode)
  relay resume --run ID   Resume a paused/crashed run
  relay approve ID        Approve a pending approval
  relay deny ID           Deny a pending approval
  relay customer-reply TICKET TEXT  Simulate a customer reply
  relay connectors list   List configured connectors and tools
  relay connectors tools  Show all discovered tools
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from importlib.util import find_spec
from io import TextIOWrapper
from pathlib import Path

import typer
import yaml
from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table

import agent

# Load environment variables
load_dotenv()

if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, TextIOWrapper):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                logging.getLogger(__name__).warning("Could not configure UTF-8 output", exc_info=True)

app = typer.Typer(
    name="relay",
    help="Relay — Autonomous customer support agent with MCP connectors",
    no_args_is_help=True,
)
connectors_app = typer.Typer(help="Manage connectors")
app.add_typer(connectors_app, name="connectors")

console = Console(force_terminal=True)

# Editable wheels may copy cli.py into site-packages while packages remain in
# the checkout. Resolve shared resources beside the agent package in both modes.
ROOT_DIR = Path(agent.__path__[0]).resolve().parent
RUNS_DIR = ROOT_DIR / "runs"


def setup_logging(verbose: bool = False) -> None:
    """Configure logging with rich handler."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
    )


def load_config() -> dict:
    """Load config.yaml with env var substitution."""
    config_path = ROOT_DIR / "config.yaml"
    if not config_path.exists():
        console.print("[red]config.yaml not found[/red]")
        raise typer.Exit(1)
    
    with open(config_path, encoding="utf-8") as f:
        raw = f.read()
    
    # Substitute ${ENV_VAR} patterns
    import re
    def replace_env(match):
        var_name = match.group(1)
        return os.environ.get(var_name, "")
    
    raw = re.sub(r'\$\{(\w+)\}', replace_env, raw)
    return yaml.safe_load(raw)


def create_llm(config: dict):
    """Create LLM instance from config."""
    from agent.llm.json_mode import JsonModeLLM
    from agent.llm.openai_compat import OpenAICompatLLM
    
    provider = config.get("provider", {})
    
    base_url = provider.get("base_url", os.environ.get("LLM_BASE_URL", ""))
    api_key = provider.get("api_key", os.environ.get("LLM_API_KEY", ""))
    model = provider.get("model", os.environ.get("LLM_MODEL", "gemini-2.0-flash"))
    tool_mode = os.environ.get("TOOL_MODE") or provider.get("tool_mode", "native")
    
    if not base_url or not api_key or not model:
        console.print("[red]LLM_BASE_URL, LLM_API_KEY and LLM_MODEL must be set in .env or config.yaml[/red]")
        raise typer.Exit(1)
    
    if tool_mode not in ("native", "json"):
        console.print("[red]TOOL_MODE must be native or json[/red]")
        raise typer.Exit(1)

    llm = OpenAICompatLLM(
        base_url=base_url,
        api_key=api_key,
        model=model,
        tool_mode=tool_mode,
        max_retries=provider.get("max_retries", 3),
        timeout=provider.get("timeout_s", 60),
    )
    
    if tool_mode == "json":
        return JsonModeLLM(llm)
    return llm


async def _run_ticket(ticket_id: str, config: dict, run_id: str | None = None) -> None:
    """Internal async ticket runner."""
    from agent.hub import ConnectorHub
    from agent.loop import AgentLoop
    from agent.policy import PolicyEngine
    
    hub = ConnectorHub()
    hub.load_config()
    
    banner = f"Processing Ticket #{ticket_id}" if not run_id else f"Resuming Run {run_id} (Ticket #{ticket_id})"
    console.print(Panel(f"[bold]{banner}[/bold]", style="blue"))
    
    try:
        console.print("Starting connectors...", style="dim")
        await hub.start_all()
        
        # Show discovered tools
        tool_count = len(hub.tools)
        connector_count = len(hub.connectors)
        console.print(f"  [green][OK][/green] {connector_count} connectors, {tool_count} tools discovered")
        
        llm = create_llm(config)
        policy = PolicyEngine()
        
        agent_config = config.get("agent", {})
        loop = AgentLoop(
            llm=llm,
            hub=hub,
            policy=policy,
            max_steps=agent_config.get("max_steps", 30),
            max_llm_calls=agent_config.get("max_llm_calls", 20),
        )
        
        state = await loop.run_ticket(ticket_id, run_id=run_id)
        
        # Display result
        status_colors = {
            "resolved": "green", "escalated": "yellow",
            "waiting_human": "cyan", "waiting_customer": "cyan",
            "failed": "red",
        }
        color = status_colors.get(state.status, "white")
        
        console.print(f"\n[{color}]Result: {state.status.upper()}[/{color}]")
        console.print(f"Run ID: {state.run_id}")
        console.print(f"Steps executed: {state.step_count}")
        console.print(f"Actions: {len(state.actions)}")
        usage = state.facts.get("_llm_usage", {})
        if usage:
            console.print(
                f"LLM usage (returned responses): {usage.get('calls', 0)} calls, "
                f"{usage.get('prompt_tokens', 0):,} input tokens, "
                f"{usage.get('completion_tokens', 0):,} output tokens"
            )
        
        if state.facts.get("summary"):
            console.print(Panel(state.facts["summary"], title="Summary"))
        
        run_dir = RUNS_DIR / state.run_id
        console.print(f"\nRun artifacts: [link=file://{run_dir}]{run_dir}[/link]")
        
    finally:
        await hub.shutdown()


async def _run_shift(config: dict) -> None:
    """Process all open tickets."""
    from agent.hub import ConnectorHub
    from agent.loop import AgentLoop
    from agent.policy import PolicyEngine
    from agent.prompts.templates import SHIFT_REPORT_TEMPLATE, TICKET_DETAIL_TEMPLATE
    
    hub = ConnectorHub()
    hub.load_config()
    
    console.print(Panel("[bold]Starting Shift — Processing All Open Tickets[/bold]", style="blue"))
    
    try:
        await hub.start_all()
        llm = create_llm(config)
        policy = PolicyEngine()
        
        agent_config = config.get("agent", {})
        loop = AgentLoop(
            llm=llm, hub=hub, policy=policy,
            max_steps=agent_config.get("max_steps", 30),
            max_llm_calls=agent_config.get("max_llm_calls", 20),
        )
        
        # Get all open tickets
        tickets = await hub.call_tool("helpdesk__list_open_tickets", {})
        ticket_list = tickets if isinstance(tickets, list) else tickets.get("tickets", [])
        if not isinstance(ticket_list, list) or any(not isinstance(ticket, dict) for ticket in ticket_list):
            raise ValueError("Helpdesk returned an invalid ticket list")
        
        console.print(f"Found {len(ticket_list)} open tickets")
        
        # Process each ticket
        results = []
        total_refunded = 0
        approvals_requested = 0
        errors = []
        
        for ticket in ticket_list:
            tid = str(ticket.get("id", ""))
            subject = ticket.get("subject", "")
            console.print(f"\n{'='*60}")
            console.print(f"Processing Ticket #{tid}: {subject}")
            
            try:
                state = await loop.run_ticket(tid)
                results.append(state)
                
                # Track metrics
                for action in state.actions:
                    if action.tool == "create_refund" and action.status == "succeeded":
                        total_refunded += action.args.get("amount", 0)
                    if action.tool == "request_approval":
                        approvals_requested += 1
                
                color = "green" if state.status == "resolved" else "yellow" if state.status in ("escalated", "waiting_human") else "red"
                console.print(f"  [{color}]{state.status.upper()}[/{color}]")
            except Exception as e:
                logging.getLogger(__name__).exception("Failed to process ticket %s", tid)
                errors.append((tid, str(e)))
                console.print(f"  [red]ERROR: {e}[/red]")
        
        # Generate shift report
        resolved = sum(1 for r in results if r.status == "resolved")
        escalated = sum(1 for r in results if r.status == "escalated")
        waiting = sum(1 for r in results if r.status in ("waiting_customer", "waiting_human"))
        failed = sum(1 for r in results if r.status == "failed") + len(errors)
        
        ticket_details = "\n".join(
            TICKET_DETAIL_TEMPLATE.format(
                ticket_id=r.ticket_id,
                subject=r.facts.get("ticket_subject", "Unknown"),
                status=r.status,
                decision=r.facts.get("summary", "No summary"),
                actions=", ".join(f"{a.connector}.{a.tool}" for a in r.actions),
                verification="Passed" if r.status == "resolved" else "N/A",
            )
            for r in results
        )
        
        report = SHIFT_REPORT_TEMPLATE.format(
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M"),
            total=len(ticket_list),
            resolved=resolved,
            escalated=escalated,
            waiting=waiting,
            failed=failed,
            total_refunded=total_refunded,
            approvals_requested=approvals_requested,
            approvals_granted=sum(1 for r in results for a in r.actions if a.tool == "decide_approval" and a.result and a.result.get("decision") == "approved"),
            approvals_denied=sum(1 for r in results for a in r.actions if a.tool == "decide_approval" and a.result and a.result.get("decision") == "denied"),
            ticket_details=ticket_details + "\n" + "\n".join(
                f"- Ticket #{tid}: FAILED — {error}" for tid, error in errors
            ),
            flags="\n".join(
                f"- Ticket #{r.ticket_id}: {r.facts.get('security_flag', '')}" 
                for r in results if r.facts.get("social_engineering_detected")
            ) or "None",
        )
        
        # Save report
        report_path = RUNS_DIR / f"shift_report_{datetime.now().strftime('%Y%m%d_%H%M')}.md"
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report, encoding="utf-8")
        
        console.print(f"\n{'='*60}")
        console.print(Panel(f"Shift complete: {resolved} resolved, {escalated} escalated, {waiting} waiting, {failed} failed", style="green"))
        console.print(f"Report: {report_path}")
        
    finally:
        await hub.shutdown()


@app.command()
def seed():
    """Seed all connector databases with demo data."""
    console.print("[bold]Seeding databases...[/bold]")
    
    import subprocess
    python = sys.executable
    
    seeders = [
        ("shop", [python, "-m", "connectors.shop.seed"]),
        ("payments", [python, "-m", "connectors.payments.seed"]),
        ("helpdesk", [python, "-m", "connectors.helpdesk.seed"]),
        ("chat", [python, "-m", "connectors.chat.seed"]),
    ]
    
    # Check if inventory connector exists
    inventory_seed = ROOT_DIR / "connectors" / "inventory" / "seed.py"
    if inventory_seed.exists():
        seeders.append(("inventory", [python, "-m", "connectors.inventory.seed"]))
    
    failures = []
    for name, cmd in seeders:
        try:
            result = subprocess.run(cmd, cwd=str(ROOT_DIR), capture_output=True, text=True, check=False)
            if result.returncode == 0:
                console.print(f"  [green][OK][/green] {name}")
            else:
                failures.append(name)
                console.print(f"  [red][FAIL][/red] {name}: {result.stderr[:200]}")
        except OSError as e:
            failures.append(name)
            console.print(f"  [red][FAIL][/red] {name}: {e}")

    if failures:
        raise typer.Exit(1)
    console.print("[green]Done![/green]")


@app.command()
def run(
    ticket: int = typer.Option(..., "--ticket", "-t", help="Ticket ID to process"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging"),
):
    """Process a single support ticket."""
    setup_logging(verbose)
    config = load_config()
    asyncio.run(_run_ticket(str(ticket), config))


@app.command()
def shift(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging"),
):
    """Process all open tickets (batch mode)."""
    setup_logging(verbose)
    config = load_config()
    asyncio.run(_run_shift(config))


@app.command()
def resume(
    run_id: str = typer.Option(..., "--run", "-r", help="Run ID to resume"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging"),
):
    """Resume a paused or crashed run."""
    setup_logging(verbose)
    from agent.state import RunState
    
    run_dir = RUNS_DIR / run_id
    if not run_dir.exists():
        console.print(f"[red]Run {run_id} not found[/red]")
        raise typer.Exit(1)
    
    state = RunState.load(run_dir)
    console.print(f"Resuming run {run_id} for ticket #{state.ticket_id} (status: {state.status})")
    
    config = load_config()
    asyncio.run(_run_ticket(state.ticket_id, config, run_id=run_id))


@app.command()
def approve(
    approval_id: int = typer.Argument(..., help="Approval ID to approve"),
):
    """Approve a pending approval request."""
    async def _approve():
        from agent.hub import ConnectorHub
        hub = ConnectorHub()
        hub.load_config()
        try:
            await hub.start_all()
            result = await hub.call_tool("chat__decide_approval", {
                "approval_id": approval_id,
                "decision": "approved",
                "decided_by": "manager",
            })
            console.print(f"[green]Approval {approval_id} APPROVED[/green]")
            console.print(json.dumps(result, indent=2))
        finally:
            await hub.shutdown()
    
    asyncio.run(_approve())


@app.command()
def deny(
    approval_id: int = typer.Argument(..., help="Approval ID to deny"),
):
    """Deny a pending approval request."""
    async def _deny():
        from agent.hub import ConnectorHub
        hub = ConnectorHub()
        hub.load_config()
        try:
            await hub.start_all()
            result = await hub.call_tool("chat__decide_approval", {
                "approval_id": approval_id,
                "decision": "denied",
                "decided_by": "manager",
            })
            console.print(f"[red]Approval {approval_id} DENIED[/red]")
            console.print(json.dumps(result, indent=2))
        finally:
            await hub.shutdown()
    
    asyncio.run(_deny())


@app.command(name="customer-reply")
def customer_reply(
    ticket_id: int = typer.Argument(..., help="Ticket ID"),
    text: str = typer.Argument(..., help="Reply text"),
):
    """Simulate a customer reply to a ticket."""
    async def _reply():
        from agent.hub import ConnectorHub
        hub = ConnectorHub()
        hub.load_config()
        try:
            await hub.start_all()
            await hub.call_tool("helpdesk__add_reply", {
                "ticket_id": ticket_id,
                "body": text,
                "author": "customer",
            })
            # Also update ticket status back to open
            await hub.call_tool("helpdesk__set_status", {
                "ticket_id": ticket_id,
                "status": "open",
            })
            console.print(f"[green]Customer reply added to ticket #{ticket_id}[/green]")
        finally:
            await hub.shutdown()
    
    asyncio.run(_reply())


@connectors_app.command(name="list")
def connectors_list():
    """List configured connectors."""
    async def _list():
        from agent.hub import ConnectorHub
        hub = ConnectorHub()
        hub.load_config()
        
        table = Table(title="Connectors")
        table.add_column("Name", style="cyan")
        table.add_column("Scopes", style="green")
        table.add_column("Command")
        
        for name, config in hub.connectors.items():
            table.add_row(name, ", ".join(config.scopes), " ".join(config.command))
        
        console.print(table)
    
    asyncio.run(_list())


@connectors_app.command(name="tools")
def connectors_tools():
    """Show all discovered tools from connected connectors."""
    async def _tools():
        from agent.hub import ConnectorHub
        hub = ConnectorHub()
        hub.load_config()
        
        try:
            await hub.start_all()
            
            table = Table(title="Discovered Tools")
            table.add_column("Connector", style="cyan")
            table.add_column("Tool", style="green")
            table.add_column("Description")
            table.add_column("R/W", justify="center")
            
            for _name, tool in sorted(hub.tools.items()):
                rw = "R" if tool.is_read_only else "W"
                table.add_row(tool.connector_name, tool.name, tool.description[:60], rw)
            
            console.print(table)
        finally:
            await hub.shutdown()
    
    asyncio.run(_tools())


@app.command()
def viewer(
    port: int = typer.Option(8000, "--port", "-p", help="Port for demo viewer dashboard"),
):
    """Launch the read-only web viewer dashboard for mock systems and runs."""
    if find_spec("uvicorn") is None or find_spec("fastapi") is None:
        console.print('[red]Install viewer dependencies: python -m pip install -e ".[viewer]"[/red]')
        raise typer.Exit(1)
    import uvicorn
    console.print(f"[green]Starting Demo Viewer Dashboard at http://127.0.0.1:{port}[/green]")
    uvicorn.run("viewers.app:app", host="127.0.0.1", port=port, reload=False)


if __name__ == "__main__":
    app()
