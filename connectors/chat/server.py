"""Chat MCP connector server simulating team chat and approval workflows."""

import sqlite3
from datetime import UTC, datetime
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import StrictInt

try:
    from ..common.annotations import (
        READ_ONLY,
        WRITE_IDEMPOTENT,
        WRITE_SAFE,
        ToolAnnotations,
    )
    from ..common.chaos import chaos
    from ..common.db import dict_from_row, get_connection, get_db_path, rows_to_dicts
    from .seed import init_db, seed_data
except ImportError:
    from connectors.chat.seed import init_db, seed_data
    from connectors.common.annotations import (
        READ_ONLY,
        WRITE_IDEMPOTENT,
        WRITE_SAFE,
        ToolAnnotations,
    )
    from connectors.common.chaos import chaos
    from connectors.common.db import (
        dict_from_row,
        get_connection,
        get_db_path,
        rows_to_dicts,
    )

CONNECTOR_NAME = "chat"

mcp = FastMCP("chat")

# Tool annotation mappings for policy enforcement and auditing
TOOL_ANNOTATIONS: dict[str, ToolAnnotations] = {
    "list_channels": READ_ONLY,
    "get_messages": READ_ONLY,
    "post_message": WRITE_SAFE,
    "request_approval": WRITE_SAFE,
    "get_approval": READ_ONLY,
    "list_pending_approvals": READ_ONLY,
    "decide_approval": WRITE_IDEMPOTENT,
}


def _ensure_database() -> None:
    """Ensure database tables and initial seed data exist."""
    db_path = get_db_path(CONNECTOR_NAME)
    if not db_path.exists():
        init_db(drop_existing=False)
        seed_data()


def _get_channel_by_name(conn: sqlite3.Connection, channel_name: str) -> sqlite3.Row:
    """Find a channel by name, accepting names with or without leading '#'.

    Raises ValueError if the channel is not found.
    """
    clean_name = channel_name.strip().lstrip("#")
    row = conn.execute(
        "SELECT * FROM channels WHERE name = ? OR name = ?",
        (clean_name, f"#{clean_name}"),
    ).fetchone()

    if not row:
        available_rows = conn.execute("SELECT name FROM channels").fetchall()
        available = [r["name"] for r in available_rows]
        raise ValueError(
            f"Channel '{channel_name}' not found. Available channels: {', '.join(available)}"
        )
    return row


@mcp.tool(
    name="list_channels",
    annotations=READ_ONLY.to_mcp_annotations(),
    description="List all channels in the chat workspace. [READ]",
)
def list_channels() -> list[dict[str, Any]]:
    """List all available chat channels."""
    chaos.maybe_fail("list_channels", CONNECTOR_NAME)
    _ensure_database()

    conn = get_connection(CONNECTOR_NAME)
    try:
        rows = conn.execute("SELECT id, name, created_at FROM channels ORDER BY id ASC").fetchall()
        return rows_to_dicts(rows)
    finally:
        conn.close()


setattr(list_channels, "_annotations", READ_ONLY)  # noqa: B010 - Dynamic function compatibility attribute.


@mcp.tool(
    name="get_messages",
    annotations=READ_ONLY.to_mcp_annotations(),
    description="Get recent messages from a channel (accepts channel name with or without '#'). [READ]",
)
def get_messages(channel_name: str, limit: int = 20) -> list[dict[str, Any]]:
    """Get recent messages from a specified channel."""
    chaos.maybe_fail("get_messages", CONNECTOR_NAME)
    _ensure_database()

    if limit <= 0:
        limit = 20

    conn = get_connection(CONNECTOR_NAME)
    try:
        channel = _get_channel_by_name(conn, channel_name)
        rows = conn.execute(
            """
            SELECT m.id, m.channel_id, c.name AS channel_name, m.author, m.body, m.created_at
            FROM messages m
            JOIN channels c ON m.channel_id = c.id
            WHERE m.channel_id = ?
            ORDER BY m.id DESC
            LIMIT ?
            """,
            (channel["id"], limit),
        ).fetchall()
        # Return in chronological order
        results = rows_to_dicts(rows)
        results.reverse()
        return results
    finally:
        conn.close()


setattr(get_messages, "_annotations", READ_ONLY)  # noqa: B010 - Dynamic function compatibility attribute.


@mcp.tool(
    name="post_message",
    annotations=WRITE_SAFE.to_mcp_annotations(),
    description="Post a new message to a channel. [WRITE]",
)
def post_message(
    channel_name: str,
    body: str,
    author: str = "relay-agent",
) -> dict[str, Any]:
    """Post a message to a channel."""
    chaos.maybe_fail("post_message", CONNECTOR_NAME)
    _ensure_database()

    if not body.strip():
        raise ValueError("Message body cannot be empty.")

    conn = get_connection(CONNECTOR_NAME)
    try:
        channel = _get_channel_by_name(conn, channel_name)
        now = datetime.now(UTC).isoformat()
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO messages (channel_id, author, body, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (channel["id"], author, body, now),
            )
            msg_id = cursor.lastrowid

        return {
            "id": msg_id,
            "channel_id": channel["id"],
            "channel_name": channel["name"],
            "author": author,
            "body": body,
            "created_at": now,
        }
    finally:
        conn.close()


setattr(post_message, "_annotations", WRITE_SAFE)  # noqa: B010 - Dynamic function compatibility attribute.


@mcp.tool(
    name="request_approval",
    annotations=WRITE_SAFE.to_mcp_annotations(),
    description="Create an approval request and post notification to the channel. [WRITE]",
)
def request_approval(
    channel_name: str,
    summary: str,
    amount: StrictInt | None = None,
    requestor: str = "relay-agent",
) -> dict[str, Any]:
    """Create an approval request and post notification to the channel."""
    chaos.maybe_fail("request_approval", CONNECTOR_NAME)
    _ensure_database()

    if not summary.strip():
        raise ValueError("Approval summary cannot be empty.")
    if amount is not None and (type(amount) is not int or amount <= 0):
        raise ValueError("Approval amount must be a positive integer")

    conn = get_connection(CONNECTOR_NAME)
    try:
        channel = _get_channel_by_name(conn, channel_name)
        now = datetime.now(UTC).isoformat()
        with conn:
            # 1. Insert approval record
            cursor = conn.execute(
                """
                INSERT INTO approvals (channel_id, requestor, summary, amount, status, created_at)
                VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (channel["id"], requestor, summary, amount, now),
            )
            approval_id = cursor.lastrowid

            # 2. Post notification message in channel
            amount_str = f" (${amount})" if amount is not None else ""
            msg_body = f"Approval Request #{approval_id} by {requestor}: {summary}{amount_str} [Status: pending]"
            conn.execute(
                """
                INSERT INTO messages (channel_id, author, body, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (channel["id"], requestor, msg_body, now),
            )

        return {
            "id": approval_id,
            "channel_id": channel["id"],
            "channel_name": channel["name"],
            "requestor": requestor,
            "summary": summary,
            "amount": amount,
            "status": "pending",
            "decided_by": None,
            "decided_at": None,
            "created_at": now,
        }
    finally:
        conn.close()


setattr(request_approval, "_annotations", WRITE_SAFE)  # noqa: B010 - Dynamic function compatibility attribute.


@mcp.tool(
    name="get_approval",
    annotations=READ_ONLY.to_mcp_annotations(),
    description="Get current approval status and details by approval ID. [READ]",
)
def get_approval(approval_id: int) -> dict[str, Any]:
    """Retrieve an approval request by its ID."""
    chaos.maybe_fail("get_approval", CONNECTOR_NAME)
    _ensure_database()

    conn = get_connection(CONNECTOR_NAME)
    try:
        row = conn.execute(
            """
            SELECT a.id, a.channel_id, c.name AS channel_name, a.requestor,
                   a.summary, a.amount, a.status, a.decided_by, a.decided_at, a.created_at
            FROM approvals a
            JOIN channels c ON a.channel_id = c.id
            WHERE a.id = ?
            """,
            (approval_id,),
        ).fetchone()

        if not row:
            raise ValueError(f"Approval request #{approval_id} not found.")

        return dict_from_row(row)
    finally:
        conn.close()


setattr(get_approval, "_annotations", READ_ONLY)  # noqa: B010 - Dynamic function compatibility attribute.


@mcp.tool(
    name="list_pending_approvals",
    annotations=READ_ONLY.to_mcp_annotations(),
    description="List all currently pending approval requests. [READ]",
)
def list_pending_approvals() -> list[dict[str, Any]]:
    """List all pending approval requests."""
    chaos.maybe_fail("list_pending_approvals", CONNECTOR_NAME)
    _ensure_database()

    conn = get_connection(CONNECTOR_NAME)
    try:
        rows = conn.execute(
            """
            SELECT a.id, a.channel_id, c.name AS channel_name, a.requestor,
                   a.summary, a.amount, a.status, a.decided_by, a.decided_at, a.created_at
            FROM approvals a
            JOIN channels c ON a.channel_id = c.id
            WHERE a.status = 'pending'
            ORDER BY a.id ASC
            """
        ).fetchall()
        return rows_to_dicts(rows)
    finally:
        conn.close()


setattr(list_pending_approvals, "_annotations", READ_ONLY)  # noqa: B010 - Dynamic function compatibility attribute.


@mcp.tool(
    name="decide_approval",
    annotations=WRITE_IDEMPOTENT.to_mcp_annotations(),
    description="Decide an approval request with 'approved' or 'denied'. [WRITE]",
)
def decide_approval(
    approval_id: int,
    decision: str,
    decided_by: str = "manager",
) -> dict[str, Any]:
    """Approve or deny an approval request and post the decision to the channel."""
    chaos.maybe_fail("decide_approval", CONNECTOR_NAME)
    _ensure_database()

    norm_decision = decision.strip().lower()
    if norm_decision in ("approved", "approve"):
        norm_decision = "approved"
    elif norm_decision in ("denied", "deny", "rejected", "reject"):
        norm_decision = "denied"
    else:
        raise ValueError("decision must be 'approved' or 'denied'")

    if not decided_by.strip():
        raise ValueError("Approval decision requires a human identity")
    conn = get_connection(CONNECTOR_NAME)
    try:
        # Serialize the read/decision/message transaction; a final decision is immutable.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT a.id, a.channel_id, c.name AS channel_name, a.requestor,
                   a.summary, a.amount, a.status, a.decided_by, a.decided_at, a.created_at
            FROM approvals a
            JOIN channels c ON a.channel_id = c.id
            WHERE a.id = ?
            """,
            (approval_id,),
        ).fetchone()

        if not row:
            raise ValueError(f"Approval request #{approval_id} not found.")

        current_status = row["status"]
        if current_status != "pending":
            if current_status == norm_decision:
                # Idempotent response if already decided with the same decision
                return dict_from_row(row)
            raise ValueError(
                f"Approval #{approval_id} has already been {current_status} by {row['decided_by']}."
            )

        now = datetime.now(UTC).isoformat()
        channel_id = row["channel_id"]
        summary = row["summary"]

        with conn:
            conn.execute(
                """
                UPDATE approvals
                SET status = ?, decided_by = ?, decided_at = ?
                WHERE id = ?
                """,
                (norm_decision, decided_by, now, approval_id),
            )

            # Post decision message in the channel
            msg_body = f"Approval Request #{approval_id} ({summary}) was {norm_decision} by {decided_by}."
            conn.execute(
                """
                INSERT INTO messages (channel_id, author, body, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (channel_id, decided_by, msg_body, now),
            )

        # Retrieve and return updated row
        updated = conn.execute(
            """
            SELECT a.id, a.channel_id, c.name AS channel_name, a.requestor,
                   a.summary, a.amount, a.status, a.decided_by, a.decided_at, a.created_at
            FROM approvals a
            JOIN channels c ON a.channel_id = c.id
            WHERE a.id = ?
            """,
            (approval_id,),
        ).fetchone()

        return dict_from_row(updated)
    finally:
        conn.close()


setattr(decide_approval, "_annotations", WRITE_IDEMPOTENT)  # noqa: B010 - Dynamic function compatibility attribute.


if __name__ == "__main__":
    mcp.run(transport="stdio")
