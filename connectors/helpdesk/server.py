"""Helpdesk MCP connector server simulating a support ticket system (Zendesk-like)."""

from datetime import datetime, timezone
import sqlite3
from typing import Any

from mcp.server.fastmcp import FastMCP

try:
    from ..common.annotations import (
        READ_ONLY,
        WRITE_SAFE,
        WRITE_IDEMPOTENT,
        ToolAnnotations,
    )
    from ..common.chaos import chaos
    from ..common.db import dict_from_row, get_connection, rows_to_dicts, get_db_path
    from .seed import VALID_AUTHORS, VALID_STATUSES, init_db, seed_data
except ImportError:
    from connectors.common.annotations import (
        READ_ONLY,
        WRITE_SAFE,
        WRITE_IDEMPOTENT,
        ToolAnnotations,
    )
    from connectors.common.chaos import chaos
    from connectors.common.db import dict_from_row, get_connection, rows_to_dicts, get_db_path
    from connectors.helpdesk.seed import VALID_AUTHORS, VALID_STATUSES, init_db, seed_data

CONNECTOR_NAME = "helpdesk"

mcp = FastMCP("helpdesk")

# Tool annotation mappings for policy enforcement and audit logging
TOOL_ANNOTATIONS: dict[str, ToolAnnotations] = {
    "list_open_tickets": READ_ONLY,
    "get_ticket": READ_ONLY,
    "get_tickets_by_customer": READ_ONLY,
    "add_reply": WRITE_SAFE,
    "add_internal_note": WRITE_SAFE,
    "set_status": WRITE_IDEMPOTENT,
    "set_tags": WRITE_IDEMPOTENT,
}


def _ensure_database() -> None:
    """Ensure database tables exist before executing queries."""
    db_path = get_db_path(CONNECTOR_NAME)
    if not db_path.exists():
        init_db(drop_existing=False)
        seed_data()


@mcp.tool(
    name="list_open_tickets",
    description="List all support tickets with status 'open' or 'pending'. [READ ONLY]",
)
def list_open_tickets() -> list[dict[str, Any]]:
    """Return all support tickets currently awaiting attention (status: open or pending).

    Returns:
        list[dict]: List of open and pending ticket summaries.
    """
    chaos.maybe_fail("list_open_tickets", CONNECTOR_NAME)
    _ensure_database()

    conn = get_connection(CONNECTOR_NAME)
    try:
        rows = conn.execute(
            """
            SELECT id, subject, body, customer_email, status, priority, tags, created_at, updated_at
            FROM tickets
            WHERE status IN ('open', 'pending')
            ORDER BY id ASC
            """
        ).fetchall()
        return rows_to_dicts(rows)
    finally:
        conn.close()


list_open_tickets._annotations = READ_ONLY


@mcp.tool(
    name="get_ticket",
    description="Get ticket details along with its full conversation history and replies. [READ ONLY]",
)
def get_ticket(ticket_id: int) -> dict[str, Any]:
    """Retrieve full ticket details and all associated replies and internal notes.

    Args:
        ticket_id: The ID of the support ticket.

    Returns:
        dict: The ticket record with a 'replies' list containing all messages and notes.
    """
    chaos.maybe_fail("get_ticket", CONNECTOR_NAME)
    _ensure_database()

    conn = get_connection(CONNECTOR_NAME)
    try:
        ticket_row = conn.execute(
            """
            SELECT id, subject, body, customer_email, status, priority, tags, created_at, updated_at
            FROM tickets
            WHERE id = ?
            """,
            (ticket_id,),
        ).fetchone()

        if not ticket_row:
            raise ValueError(f"Ticket #{ticket_id} not found")

        replies_rows = conn.execute(
            """
            SELECT id, ticket_id, author, body, is_internal, created_at
            FROM ticket_replies
            WHERE ticket_id = ?
            ORDER BY id ASC
            """,
            (ticket_id,),
        ).fetchall()

        ticket = dict_from_row(ticket_row)
        ticket["replies"] = rows_to_dicts(replies_rows)
        return ticket
    finally:
        conn.close()


get_ticket._annotations = READ_ONLY


@mcp.tool(
    name="get_tickets_by_customer",
    description="Get all support tickets submitted by a specific customer email. [READ ONLY]",
)
def get_tickets_by_customer(customer_email: str) -> list[dict[str, Any]]:
    """Retrieve all tickets belonging to a customer email address.

    Args:
        customer_email: The customer's email address.

    Returns:
        list[dict]: List of tickets belonging to the customer.
    """
    chaos.maybe_fail("get_tickets_by_customer", CONNECTOR_NAME)
    _ensure_database()

    clean_email = customer_email.strip().lower()
    conn = get_connection(CONNECTOR_NAME)
    try:
        rows = conn.execute(
            """
            SELECT id, subject, body, customer_email, status, priority, tags, created_at, updated_at
            FROM tickets
            WHERE LOWER(customer_email) = ?
            ORDER BY id ASC
            """,
            (clean_email,),
        ).fetchall()
        return rows_to_dicts(rows)
    finally:
        conn.close()


get_tickets_by_customer._annotations = READ_ONLY


@mcp.tool(
    name="add_reply",
    description="Add a customer-facing public reply to a support ticket. [WRITE]",
)
def add_reply(ticket_id: int, body: str, author: str = "agent") -> dict[str, Any]:
    """Post a customer-facing message to a ticket conversation.

    Args:
        ticket_id: The ID of the support ticket.
        body: The text of the reply to send to the customer.
        author: The sender role ('agent', 'customer', or 'system'). Defaults to 'agent'.

    Returns:
        dict: The newly created reply record.
    """
    chaos.maybe_fail("add_reply", CONNECTOR_NAME)
    _ensure_database()

    if not body or not body.strip():
        raise ValueError("Reply body cannot be empty")

    clean_author = author.strip().lower()
    if clean_author not in VALID_AUTHORS:
        raise ValueError(
            f"Invalid author '{author}'. Must be one of: {', '.join(VALID_AUTHORS)}"
        )

    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection(CONNECTOR_NAME)
    try:
        with conn:
            ticket = conn.execute(
                "SELECT id FROM tickets WHERE id = ?", (ticket_id,)
            ).fetchone()
            if not ticket:
                raise ValueError(f"Ticket #{ticket_id} not found")

            cursor = conn.execute(
                """
                INSERT INTO ticket_replies (ticket_id, author, body, is_internal, created_at)
                VALUES (?, ?, ?, 0, ?)
                """,
                (ticket_id, clean_author, body.strip(), now),
            )
            reply_id = cursor.lastrowid

            conn.execute(
                "UPDATE tickets SET updated_at = ? WHERE id = ?",
                (now, ticket_id),
            )

            reply = conn.execute(
                "SELECT id, ticket_id, author, body, is_internal, created_at FROM ticket_replies WHERE id = ?",
                (reply_id,),
            ).fetchone()
            return dict_from_row(reply)
    finally:
        conn.close()


add_reply._annotations = WRITE_SAFE


@mcp.tool(
    name="add_internal_note",
    description="Add a private internal note to a support ticket (hidden from customer). [WRITE]",
)
def add_internal_note(ticket_id: int, body: str) -> dict[str, Any]:
    """Add a private note visible only to support agents and internal staff.

    Args:
        ticket_id: The ID of the support ticket.
        body: The note text.

    Returns:
        dict: The newly created internal note record.
    """
    chaos.maybe_fail("add_internal_note", CONNECTOR_NAME)
    _ensure_database()

    if not body or not body.strip():
        raise ValueError("Internal note body cannot be empty")

    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection(CONNECTOR_NAME)
    try:
        with conn:
            ticket = conn.execute(
                "SELECT id FROM tickets WHERE id = ?", (ticket_id,)
            ).fetchone()
            if not ticket:
                raise ValueError(f"Ticket #{ticket_id} not found")

            cursor = conn.execute(
                """
                INSERT INTO ticket_replies (ticket_id, author, body, is_internal, created_at)
                VALUES (?, 'agent', ?, 1, ?)
                """,
                (ticket_id, body.strip(), now),
            )
            reply_id = cursor.lastrowid

            conn.execute(
                "UPDATE tickets SET updated_at = ? WHERE id = ?",
                (now, ticket_id),
            )

            reply = conn.execute(
                "SELECT id, ticket_id, author, body, is_internal, created_at FROM ticket_replies WHERE id = ?",
                (reply_id,),
            ).fetchone()
            return dict_from_row(reply)
    finally:
        conn.close()


add_internal_note._annotations = WRITE_SAFE


@mcp.tool(
    name="set_status",
    description="Update the status of a support ticket. [WRITE, IDEMPOTENT]",
)
def set_status(ticket_id: int, status: str) -> dict[str, Any]:
    """Update ticket status (open, pending, waiting_customer, resolved, escalated, closed).

    Args:
        ticket_id: The ID of the ticket to update.
        status: The new status string.

    Returns:
        dict: The updated ticket record.
    """
    chaos.maybe_fail("set_status", CONNECTOR_NAME)
    _ensure_database()

    clean_status = status.strip().lower()
    if clean_status not in VALID_STATUSES:
        raise ValueError(
            f"Invalid status '{status}'. Must be one of: {', '.join(VALID_STATUSES)}"
        )

    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection(CONNECTOR_NAME)
    try:
        with conn:
            ticket = conn.execute(
                "SELECT id FROM tickets WHERE id = ?", (ticket_id,)
            ).fetchone()
            if not ticket:
                raise ValueError(f"Ticket #{ticket_id} not found")

            conn.execute(
                "UPDATE tickets SET status = ?, updated_at = ? WHERE id = ?",
                (clean_status, now, ticket_id),
            )

            updated = conn.execute(
                """
                SELECT id, subject, body, customer_email, status, priority, tags, created_at, updated_at
                FROM tickets
                WHERE id = ?
                """,
                (ticket_id,),
            ).fetchone()
            return dict_from_row(updated)
    finally:
        conn.close()


set_status._annotations = WRITE_IDEMPOTENT


@mcp.tool(
    name="set_tags",
    description="Set or replace the tags on a support ticket (comma-separated string). [WRITE, IDEMPOTENT]",
)
def set_tags(ticket_id: int, tags: str) -> dict[str, Any]:
    """Update tags associated with a ticket.

    Args:
        ticket_id: The ID of the support ticket.
        tags: Comma-separated tag string (e.g. 'damaged,refund,order-1042').

    Returns:
        dict: The updated ticket record.
    """
    chaos.maybe_fail("set_tags", CONNECTOR_NAME)
    _ensure_database()

    # Normalize tags string
    normalized_tags = ",".join(
        t.strip() for t in tags.split(",") if t.strip()
    )

    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection(CONNECTOR_NAME)
    try:
        with conn:
            ticket = conn.execute(
                "SELECT id FROM tickets WHERE id = ?", (ticket_id,)
            ).fetchone()
            if not ticket:
                raise ValueError(f"Ticket #{ticket_id} not found")

            conn.execute(
                "UPDATE tickets SET tags = ?, updated_at = ? WHERE id = ?",
                (normalized_tags, now, ticket_id),
            )

            updated = conn.execute(
                """
                SELECT id, subject, body, customer_email, status, priority, tags, created_at, updated_at
                FROM tickets
                WHERE id = ?
                """,
                (ticket_id,),
            ).fetchone()
            return dict_from_row(updated)
    finally:
        conn.close()


set_tags._annotations = WRITE_IDEMPOTENT


if __name__ == "__main__":
    mcp.run(transport="stdio")
