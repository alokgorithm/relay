"""Shop MCP connector server simulating an e-commerce shop (orders, customers, shipments)."""

from datetime import datetime
import json
import sqlite3
from typing import Any

from mcp.server.fastmcp import FastMCP

try:
    from ..common.annotations import READ_ONLY, ToolAnnotations
    from ..common.chaos import chaos
    from ..common.db import dict_from_row, get_connection, rows_to_dicts, get_db_path
    from .seed import init_db, seed_data
except ImportError:
    from connectors.common.annotations import READ_ONLY, ToolAnnotations
    from connectors.common.chaos import chaos
    from connectors.common.db import dict_from_row, get_connection, rows_to_dicts, get_db_path
    from connectors.shop.seed import init_db, seed_data

CONNECTOR_NAME = "shop"

mcp = FastMCP("shop")

TOOL_ANNOTATIONS: dict[str, ToolAnnotations] = {
    "get_order": READ_ONLY,
    "search_orders": READ_ONLY,
    "get_customer": READ_ONLY,
    "get_customer_by_email": READ_ONLY,
    "get_shipment": READ_ONLY,
}


def _ensure_database() -> None:
    """Ensure database tables exist before executing queries."""
    db_path = get_db_path(CONNECTOR_NAME)
    if not db_path.exists():
        init_db(drop_existing=False)
        seed_data()


@mcp.tool(
    name="get_order",
    description="Get order details including customer info, status, amounts, and item line details. [READ ONLY]",
)
def get_order(order_id: int) -> dict[str, Any]:
    """Retrieve full details for an order.

    Args:
        order_id: The primary order ID (e.g. 1042).

    Returns:
        dict: Order details with customer information and parsed item list.
    """
    chaos.maybe_fail("get_order", CONNECTOR_NAME)
    _ensure_database()

    conn = get_connection(CONNECTOR_NAME)
    try:
        row = conn.execute(
            """
            SELECT o.id, o.customer_id, o.status, o.total_amount, o.currency,
                   o.items_json, o.created_at,
                   c.name as customer_name, c.email as customer_email, c.phone as customer_phone
            FROM orders o
            JOIN customers c ON o.customer_id = c.id
            WHERE o.id = ?
            """,
            (order_id,),
        ).fetchone()

        if not row:
            return {"error": f"Order #{order_id} not found", "found": False}

        data = dict_from_row(row)
        if "items_json" in data and data["items_json"]:
            try:
                data["items"] = json.loads(data["items_json"])
            except json.JSONDecodeError:
                data["items"] = []
        else:
            data["items"] = []

        data["found"] = True
        return data
    finally:
        conn.close()


@mcp.tool(
    name="search_orders",
    description="Search orders associated with a customer's email address. [READ ONLY]",
)
def search_orders(customer_email: str) -> dict[str, Any]:
    """Retrieve all orders placed by a customer identified by email.

    Args:
        customer_email: The email address of the customer.

    Returns:
        dict: List of orders with customer summary.
    """
    chaos.maybe_fail("search_orders", CONNECTOR_NAME)
    _ensure_database()

    conn = get_connection(CONNECTOR_NAME)
    try:
        rows = conn.execute(
            """
            SELECT o.id, o.customer_id, o.status, o.total_amount, o.currency,
                   o.items_json, o.created_at,
                   c.name as customer_name, c.email as customer_email
            FROM orders o
            JOIN customers c ON o.customer_id = c.id
            WHERE LOWER(c.email) = LOWER(?)
            ORDER BY o.id DESC
            """,
            (customer_email.strip(),),
        ).fetchall()

        order_list = []
        for row in rows:
            od = dict_from_row(row)
            if "items_json" in od and od["items_json"]:
                try:
                    od["items"] = json.loads(od["items_json"])
                except json.JSONDecodeError:
                    od["items"] = []
            order_list.append(od)

        return {
            "customer_email": customer_email,
            "orders": order_list,
            "count": len(order_list),
        }
    finally:
        conn.close()


@mcp.tool(
    name="get_customer",
    description="Get customer profile by customer ID. [READ ONLY]",
)
def get_customer(customer_id: int) -> dict[str, Any]:
    """Retrieve customer profile by ID.

    Args:
        customer_id: The unique customer ID.

    Returns:
        dict: Customer profile details.
    """
    chaos.maybe_fail("get_customer", CONNECTOR_NAME)
    _ensure_database()

    conn = get_connection(CONNECTOR_NAME)
    try:
        row = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
        if not row:
            return {"error": f"Customer #{customer_id} not found", "found": False}
        res = dict_from_row(row)
        res["found"] = True
        return res
    finally:
        conn.close()


@mcp.tool(
    name="get_customer_by_email",
    description="Get customer profile by email address. [READ ONLY]",
)
def get_customer_by_email(email: str) -> dict[str, Any]:
    """Retrieve customer profile by email address.

    Args:
        email: The email address to look up.

    Returns:
        dict: Customer profile details.
    """
    chaos.maybe_fail("get_customer_by_email", CONNECTOR_NAME)
    _ensure_database()

    conn = get_connection(CONNECTOR_NAME)
    try:
        row = conn.execute(
            "SELECT * FROM customers WHERE LOWER(email) = LOWER(?)",
            (email.strip(),),
        ).fetchone()
        if not row:
            return {"error": f"Customer with email '{email}' not found", "found": False}
        res = dict_from_row(row)
        res["found"] = True
        return res
    finally:
        conn.close()


@mcp.tool(
    name="get_shipment",
    description="Get shipment and delivery tracking status for an order. [READ ONLY]",
)
def get_shipment(order_id: int) -> dict[str, Any]:
    """Retrieve shipment details including carrier, tracking number, and delivery date.

    Args:
        order_id: The order ID associated with the shipment.

    Returns:
        dict: Shipment tracking details.
    """
    chaos.maybe_fail("get_shipment", CONNECTOR_NAME)
    _ensure_database()

    conn = get_connection(CONNECTOR_NAME)
    try:
        row = conn.execute(
            """
            SELECT s.id, s.order_id, s.carrier, s.tracking_number, s.status,
                   s.shipped_at, s.delivered_at, o.status as order_status
            FROM shipments s
            JOIN orders o ON s.order_id = o.id
            WHERE s.order_id = ?
            """,
            (order_id,),
        ).fetchone()

        if not row:
            return {"error": f"No shipment record for order #{order_id}", "found": False}

        res = dict_from_row(row)
        res["found"] = True

        # Calculate days since delivery if delivered
        if res.get("delivered_at"):
            try:
                delivered_dt = datetime.fromisoformat(res["delivered_at"])
                # Handle naive vs aware
                if delivered_dt.tzinfo is None:
                    now = datetime.now()
                else:
                    from datetime import timezone
                    now = datetime.now(timezone.utc)
                delta = now - delivered_dt
                res["days_since_delivery"] = max(0, delta.days)
            except Exception:
                res["days_since_delivery"] = None
        else:
            res["days_since_delivery"] = None

        return res
    finally:
        conn.close()
