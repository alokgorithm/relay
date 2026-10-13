"""Payments MCP connector server simulating a payment processor (charges, refunds, idempotency)."""

from datetime import UTC, datetime
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import StrictInt

try:
    from ..common.annotations import (
        READ_ONLY,
        WRITE_IDEMPOTENT,
        ToolAnnotations,
    )
    from ..common.chaos import chaos
    from ..common.db import dict_from_row, get_connection, get_db_path, rows_to_dicts
    from .seed import init_db, seed_data
except ImportError:
    from connectors.common.annotations import (
        READ_ONLY,
        WRITE_IDEMPOTENT,
        ToolAnnotations,
    )
    from connectors.common.chaos import chaos
    from connectors.common.db import (
        dict_from_row,
        get_connection,
        get_db_path,
        rows_to_dicts,
    )
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
    annotations=READ_ONLY.to_mcp_annotations(),
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


setattr(get_charge, "_annotations", READ_ONLY)  # noqa: B010 - Dynamic function compatibility attribute.


@mcp.tool(
    name="get_charge_by_order",
    annotations=READ_ONLY.to_mcp_annotations(),
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


setattr(get_charge_by_order, "_annotations", READ_ONLY)  # noqa: B010 - Dynamic function compatibility attribute.


@mcp.tool(
    name="list_refunds",
    annotations=READ_ONLY.to_mcp_annotations(),
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


setattr(list_refunds, "_annotations", READ_ONLY)  # noqa: B010 - Dynamic function compatibility attribute.


@mcp.tool(
    name="list_refunds_by_order",
    annotations=READ_ONLY.to_mcp_annotations(),
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


setattr(list_refunds_by_order, "_annotations", READ_ONLY)  # noqa: B010 - Dynamic function compatibility attribute.


@mcp.tool(
    name="create_refund",
    annotations=WRITE_IDEMPOTENT.to_mcp_annotations(),
    description="Create a refund for a payment charge with idempotency key protection. [WRITE, DESTRUCTIVE, IDEMPOTENT]",
)
def create_refund(
    charge_id: StrictInt,
    amount: StrictInt,
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

    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise ValueError("An idempotency_key is required to process a refund")

    if type(amount) is not int or amount <= 0:
        raise ValueError(f"Refund amount must be a positive integer, got {amount}")
    if type(charge_id) is not int or charge_id <= 0:
        raise ValueError("charge_id must be a positive integer")
    idempotency_key = idempotency_key.strip()

    conn = get_connection(CONNECTOR_NAME)
    try:
        with conn:
            # Lock before reading the balance/key so concurrent writers cannot overspend.
            conn.execute("BEGIN IMMEDIATE")
            # Check for existing refund with same idempotency key
            existing = conn.execute(
                "SELECT * FROM refunds WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing:
                if existing["charge_id"] != charge_id or existing["amount"] != amount:
                    raise ValueError("Idempotency key is already bound to a different charge or amount")
                return dict(existing)  # Reason changes do not change the financial operation.

            # Verify charge exists
            charge = conn.execute(
                "SELECT * FROM charges WHERE id = ?", (charge_id,)
            ).fetchone()
            if not charge:
                raise ValueError(f"Charge with ID {charge_id} not found")
            if charge["status"] not in ("succeeded", "partially_refunded"):
                raise ValueError(f"Charge #{charge_id} is not refundable (status: {charge['status']})")

            # Check existing refunds against charge amount
            refunded_row = conn.execute(
                """
                SELECT COALESCE(SUM(CASE WHEN status = 'succeeded' THEN amount ELSE 0 END), 0) AS total_refunded,
                       COALESCE(SUM(amount), 0) AS total_reserved
                FROM refunds
                WHERE charge_id = ? AND status IN ('succeeded', 'pending')
                """,
                (charge_id,),
            ).fetchone()
            total_refunded = refunded_row["total_refunded"] if refunded_row else 0
            remaining = charge["amount"] - refunded_row["total_reserved"]

            if amount > remaining:
                raise ValueError(
                    f"Refund amount {amount} exceeds remaining refundable amount {remaining} "
                    f"for charge #{charge_id} (total charge: {charge['amount']}, already refunded: {total_refunded})"
                )

            # Insert refund record
            now = datetime.now(UTC).isoformat()
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


setattr(create_refund, "_annotations", WRITE_IDEMPOTENT)  # noqa: B010 - Dynamic function compatibility attribute.


if __name__ == "__main__":
    mcp.run(transport="stdio")
