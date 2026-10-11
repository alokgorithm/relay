"""Payments MCP connector server simulating a payment processor (charges, refunds, idempotency)."""

from datetime import datetime, timezone
import sqlite3
from typing import Any

from mcp.server.fastmcp import FastMCP

try:
    from ..common.annotations import (
        READ_ONLY,
        WRITE_IDEMPOTENT,
        ToolAnnotations,
    )
    from ..common.chaos import chaos
    from ..common.db import dict_from_row, get_connection, rows_to_dicts, get_db_path
    from .seed import init_db, seed_data
except ImportError:
    from connectors.common.annotations import (
        READ_ONLY,
        WRITE_IDEMPOTENT,
        ToolAnnotations,
    )
    from connectors.common.chaos import chaos
    from connectors.common.db import dict_from_row, get_connection, rows_to_dicts, get_db_path
    from connectors.payments.seed import init_db, seed_data

CONNECTOR_NAME = "payments"

mcp = FastMCP("payments")

# Tool annotation mappings for policy enforcement, idempotency, and audit logging
TOOL_ANNOTATIONS: dict[str, ToolAnnotations] = {
    "get_charge": READ_ONLY,
    "get_charge_by_order": READ_ONLY,
    "list_refunds": READ_ONLY,
    "list_refunds_by_order": READ_ONLY,
    "create_refund": WRITE_IDEMPOTENT,
}


def _ensure_database() -> None:
    """Ensure database tables exist before executing queries."""
    db_path = get_db_path(CONNECTOR_NAME)
    if not db_path.exists():
        init_db(drop_existing=False)
        seed_data()


@mcp.tool(
    name="get_charge",
    description="Get payment charge details by charge ID. [READ ONLY]",
)
def get_charge(charge_id: int) -> dict[str, Any]:
    """Retrieve details of a charge by its primary ID.

    Args:
        charge_id: The ID of the charge.

    Returns:
        dict: The charge record.
    """
    chaos.maybe_fail("get_charge", CONNECTOR_NAME)
    _ensure_database()

    conn = get_connection(CONNECTOR_NAME)
    try:
        row = conn.execute(
            """
            SELECT id, order_id, amount, currency, status, customer_email, description, created_at
            FROM charges
            WHERE id = ?
            """,
            (charge_id,),
        ).fetchone()

        if not row:
            raise ValueError(f"Charge with ID {charge_id} not found")
        return dict_from_row(row)
    finally:
        conn.close()


get_charge._annotations = READ_ONLY


@mcp.tool(
    name="get_charge_by_order",
    description="Get payment charge details for a specific shop order ID. [READ ONLY]",
)
def get_charge_by_order(order_id: int) -> dict[str, Any]:
    """Retrieve the charge associated with a specific order ID.

    Args:
        order_id: The order ID from the shop connector.

    Returns:
        dict: The charge record.
    """
    chaos.maybe_fail("get_charge_by_order", CONNECTOR_NAME)
    _ensure_database()

    conn = get_connection(CONNECTOR_NAME)
    try:
        row = conn.execute(
            """
            SELECT id, order_id, amount, currency, status, customer_email, description, created_at
            FROM charges
            WHERE order_id = ?
            """,
            (order_id,),
        ).fetchone()

        if not row:
            raise ValueError(f"Charge for order #{order_id} not found")
        return dict_from_row(row)
    finally:
        conn.close()


get_charge_by_order._annotations = READ_ONLY


@mcp.tool(
    name="list_refunds",
    description="List all refunds for a specific payment charge ID. [READ ONLY]",
)
def list_refunds(charge_id: int) -> list[dict[str, Any]]:
    """List all refunds associated with a charge.

    Args:
        charge_id: The ID of the charge.

    Returns:
        list[dict]: List of refund records.
    """
    chaos.maybe_fail("list_refunds", CONNECTOR_NAME)
    _ensure_database()

    conn = get_connection(CONNECTOR_NAME)
    try:
        rows = conn.execute(
            """
            SELECT id, charge_id, amount, currency, status, reason, idempotency_key, created_at
            FROM refunds
            WHERE charge_id = ?
            ORDER BY id ASC
            """,
            (charge_id,),
        ).fetchall()
        return rows_to_dicts(rows)
    finally:
        conn.close()


list_refunds._annotations = READ_ONLY


@mcp.tool(
    name="list_refunds_by_order",
    description="List all refunds for a specific shop order ID. [READ ONLY]",
)
def list_refunds_by_order(order_id: int) -> list[dict[str, Any]]:
    """List all refunds associated with an order ID.

    Args:
        order_id: The order ID from the shop connector.

    Returns:
        list[dict]: List of refund records.
    """
    chaos.maybe_fail("list_refunds_by_order", CONNECTOR_NAME)
    _ensure_database()

    conn = get_connection(CONNECTOR_NAME)
    try:
        rows = conn.execute(
            """
            SELECT r.id, r.charge_id, r.amount, r.currency, r.status, r.reason, r.idempotency_key, r.created_at
            FROM refunds r
            JOIN charges c ON r.charge_id = c.id
            WHERE c.order_id = ?
            ORDER BY r.id ASC
            """,
            (order_id,),
        ).fetchall()
        return rows_to_dicts(rows)
    finally:
        conn.close()


list_refunds_by_order._annotations = READ_ONLY


@mcp.tool(
    name="create_refund",
    description="Create a refund for a payment charge with idempotency key protection. [WRITE, DESTRUCTIVE, IDEMPOTENT]",
)
def create_refund(
    charge_id: int,
    amount: int,
    reason: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Issue a refund for a given charge.

    CRITICAL IDEMPOTENCY:
    If a refund with the same idempotency_key already exists, the existing refund
    record is returned immediately without creating a duplicate.

    Args:
        charge_id: The ID of the charge to refund.
        amount: The refund amount in integer currency units (e.g., INR).
        reason: Explanation for why the refund is being issued.
        idempotency_key: Unique client-provided key ensuring exactly-once processing.

    Returns:
        dict: The refund record (either existing or freshly created).
    """
    chaos.maybe_fail("create_refund", CONNECTOR_NAME)
    _ensure_database()

    if not idempotency_key or not idempotency_key.strip():
        raise ValueError("An idempotency_key is required to process a refund")

    if amount <= 0:
        raise ValueError(f"Refund amount must be greater than zero, got {amount}")

    conn = get_connection(CONNECTOR_NAME)
    try:
        with conn:
            # Check for existing refund with same idempotency key
            existing = conn.execute(
                "SELECT * FROM refunds WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing:
                return dict(existing)  # Return existing, don't create duplicate

            # Verify charge exists
            charge = conn.execute(
                "SELECT * FROM charges WHERE id = ?", (charge_id,)
            ).fetchone()
            if not charge:
                raise ValueError(f"Charge with ID {charge_id} not found")

            # Check existing refunds against charge amount
            refunded_row = conn.execute(
                """
                SELECT COALESCE(SUM(amount), 0) AS total_refunded
                FROM refunds
                WHERE charge_id = ? AND status = 'succeeded'
                """,
                (charge_id,),
            ).fetchone()
            total_refunded = refunded_row["total_refunded"] if refunded_row else 0
            remaining = charge["amount"] - total_refunded

            if amount > remaining:
                raise ValueError(
                    f"Refund amount {amount} exceeds remaining refundable amount {remaining} "
                    f"for charge #{charge_id} (total charge: {charge['amount']}, already refunded: {total_refunded})"
                )

            # Insert refund record
            now = datetime.now(timezone.utc).isoformat()
            cursor = conn.execute(
                """
                INSERT INTO refunds (charge_id, amount, currency, status, reason, idempotency_key, created_at)
                VALUES (?, ?, ?, 'succeeded', ?, ?, ?)
                """,
                (
                    charge_id,
                    amount,
                    charge["currency"],
                    reason,
                    idempotency_key.strip(),
                    now,
                ),
            )
            refund_id = cursor.lastrowid

            # Update charge status: 'refunded' if fully refunded, else 'partially_refunded'
            new_total = total_refunded + amount
            new_status = "refunded" if new_total >= charge["amount"] else "partially_refunded"
            conn.execute(
                "UPDATE charges SET status = ? WHERE id = ?",
                (new_status, charge_id),
            )

            # Return newly created refund
            refund = conn.execute(
                "SELECT * FROM refunds WHERE id = ?", (refund_id,)
            ).fetchone()
            return dict(refund)
    finally:
        conn.close()


create_refund._annotations = WRITE_IDEMPOTENT


if __name__ == "__main__":
    mcp.run(transport="stdio")
