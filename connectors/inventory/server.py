"""Inventory MCP connector server simulating warehouse inventory and replacements."""

from datetime import datetime, timezone
import sqlite3
from typing import Any

from mcp.server.fastmcp import FastMCP

try:
    from ..common.annotations import (
        READ_ONLY,
        WRITE_DESTRUCTIVE,
        ToolAnnotations,
    )
    from ..common.chaos import chaos
    from ..common.db import dict_from_row, get_connection, rows_to_dicts, get_db_path
    from .seed import init_db, seed_data
except ImportError:
    from connectors.common.annotations import (
        READ_ONLY,
        WRITE_DESTRUCTIVE,
        ToolAnnotations,
    )
    from connectors.common.chaos import chaos
    from connectors.common.db import dict_from_row, get_connection, rows_to_dicts, get_db_path
    from connectors.inventory.seed import init_db, seed_data

CONNECTOR_NAME = "inventory"

mcp = FastMCP("inventory")

# Tool annotation mappings for policy enforcement and auditing
TOOL_ANNOTATIONS: dict[str, ToolAnnotations] = {
    "get_product": READ_ONLY,
    "search_products": READ_ONLY,
    "get_stock": READ_ONLY,
    "check_stock_by_sku": READ_ONLY,
    "create_replacement_order": WRITE_DESTRUCTIVE,
    "get_replacement_order": READ_ONLY,
    "list_replacements_for_order": READ_ONLY,
}


def _ensure_database() -> None:
    """Ensure database tables and initial seed data exist."""
    init_db(drop_existing=False)
    conn = get_connection(CONNECTOR_NAME)
    try:
        count = conn.execute("SELECT COUNT(*) AS cnt FROM products").fetchone()["cnt"]
        if count == 0:
            seed_data()
    finally:
        conn.close()


@mcp.tool(
    name="get_product",
    description="Get product details including price, category, and current stock by product ID. [READ]",
)
def get_product(product_id: int) -> dict[str, Any]:
    """Retrieve full product details with current stock quantity.

    Args:
        product_id: Unique product identifier.

    Returns:
        dict: Product details including stock_quantity.
    """
    chaos.maybe_fail("get_product", CONNECTOR_NAME)
    _ensure_database()

    conn = get_connection(CONNECTOR_NAME)
    try:
        row = conn.execute(
            """
            SELECT id, name, sku, description, price, stock_quantity, category
            FROM products
            WHERE id = ?
            """,
            (product_id,),
        ).fetchone()

        if not row:
            raise ValueError(f"Product with ID {product_id} not found")
        return dict_from_row(row)
    finally:
        conn.close()


get_product._annotations = READ_ONLY


@mcp.tool(
    name="search_products",
    description="Search products by name, SKU, or category. [READ]",
)
def search_products(query: str) -> list[dict[str, Any]]:
    """Search for products matching the query string in name, SKU, or category.

    Args:
        query: Search term to match against name, SKU, or category.

    Returns:
        list[dict]: Matching product records.
    """
    chaos.maybe_fail("search_products", CONNECTOR_NAME)
    _ensure_database()

    conn = get_connection(CONNECTOR_NAME)
    try:
        pattern = f"%{query.strip()}%"
        rows = conn.execute(
            """
            SELECT id, name, sku, description, price, stock_quantity, category
            FROM products
            WHERE name LIKE ? OR sku LIKE ? OR category LIKE ?
            ORDER BY id ASC
            """,
            (pattern, pattern, pattern),
        ).fetchall()
        return rows_to_dicts(rows)
    finally:
        conn.close()


search_products._annotations = READ_ONLY


@mcp.tool(
    name="get_stock",
    description="Get current available stock quantity for a product ID. [READ]",
)
def get_stock(product_id: int) -> dict[str, Any]:
    """Check current stock quantity for a specified product ID.

    Args:
        product_id: Unique product identifier.

    Returns:
        dict: Product identification, stock quantity, and in-stock boolean flag.
    """
    chaos.maybe_fail("get_stock", CONNECTOR_NAME)
    _ensure_database()

    conn = get_connection(CONNECTOR_NAME)
    try:
        row = conn.execute(
            """
            SELECT id, name, sku, stock_quantity
            FROM products
            WHERE id = ?
            """,
            (product_id,),
        ).fetchone()

        if not row:
            raise ValueError(f"Product with ID {product_id} not found")

        stock = row["stock_quantity"]
        return {
            "product_id": row["id"],
            "name": row["name"],
            "sku": row["sku"],
            "stock_quantity": stock,
            "in_stock": stock > 0,
        }
    finally:
        conn.close()


get_stock._annotations = READ_ONLY


@mcp.tool(
    name="check_stock_by_sku",
    description="Check if a product is in stock by its SKU code. [READ]",
)
def check_stock_by_sku(sku: str) -> dict[str, Any]:
    """Check stock availability for a product given its SKU.

    Args:
        sku: Product SKU (e.g., 'SGR-001', 'LAP-001').

    Returns:
        dict: Product details with stock quantity and in_stock flag.
    """
    chaos.maybe_fail("check_stock_by_sku", CONNECTOR_NAME)
    _ensure_database()

    clean_sku = sku.strip()
    conn = get_connection(CONNECTOR_NAME)
    try:
        row = conn.execute(
            """
            SELECT id, name, sku, price, stock_quantity, category
            FROM products
            WHERE UPPER(sku) = UPPER(?)
            """,
            (clean_sku,),
        ).fetchone()

        if not row:
            raise ValueError(f"Product with SKU '{sku}' not found")

        stock = row["stock_quantity"]
        return {
            "product_id": row["id"],
            "name": row["name"],
            "sku": row["sku"],
            "price": row["price"],
            "stock_quantity": stock,
            "in_stock": stock > 0,
            "category": row["category"],
        }
    finally:
        conn.close()


check_stock_by_sku._annotations = READ_ONLY


@mcp.tool(
    name="create_replacement_order",
    description="Create a replacement order for an existing order and decrement inventory stock. [WRITE, DESTRUCTIVE]",
)
def create_replacement_order(
    original_order_id: int,
    product_id: int,
) -> dict[str, Any]:
    """Create a replacement order for a customer order and decrement available stock.

    Validates that the product exists and has stock > 0 before decrementing and creating the order.

    Args:
        original_order_id: Original shop order ID for which replacement is being issued.
        product_id: Product ID being provided as replacement.

    Returns:
        dict: Created replacement order details and updated remaining stock.

    Raises:
        ValueError: If product does not exist or product is out of stock.
    """
    chaos.maybe_fail("create_replacement_order", CONNECTOR_NAME)
    _ensure_database()

    conn = get_connection(CONNECTOR_NAME)
    try:
        with conn:
            product = conn.execute(
                """
                SELECT id, name, sku, price, stock_quantity, category
                FROM products
                WHERE id = ?
                """,
                (product_id,),
            ).fetchone()

            if not product:
                raise ValueError(f"Product with ID {product_id} not found")

            current_stock = product["stock_quantity"]
            if current_stock <= 0:
                raise ValueError(
                    f"Product #{product_id} ({product['name']}, SKU: {product['sku']}) "
                    f"is out of stock (current stock: {current_stock}). Cannot create replacement order."
                )

            # Decrement stock
            conn.execute(
                """
                UPDATE products
                SET stock_quantity = stock_quantity - 1
                WHERE id = ?
                """,
                (product_id,),
            )

            now = datetime.now(timezone.utc).isoformat()
            cursor = conn.execute(
                """
                INSERT INTO replacement_orders (original_order_id, product_id, status, created_at)
                VALUES (?, ?, 'pending', ?)
                """,
                (original_order_id, product_id, now),
            )
            replacement_id = cursor.lastrowid

            return {
                "id": replacement_id,
                "original_order_id": original_order_id,
                "product_id": product_id,
                "product_name": product["name"],
                "sku": product["sku"],
                "status": "pending",
                "remaining_stock": current_stock - 1,
                "created_at": now,
            }
    finally:
        conn.close()


create_replacement_order._annotations = WRITE_DESTRUCTIVE


@mcp.tool(
    name="get_replacement_order",
    description="Get replacement order status and details by replacement ID. [READ]",
)
def get_replacement_order(replacement_id: int) -> dict[str, Any]:
    """Retrieve details and status of a replacement order.

    Args:
        replacement_id: ID of the replacement order.

    Returns:
        dict: Replacement order record with associated product details.
    """
    chaos.maybe_fail("get_replacement_order", CONNECTOR_NAME)
    _ensure_database()

    conn = get_connection(CONNECTOR_NAME)
    try:
        row = conn.execute(
            """
            SELECT r.id, r.original_order_id, r.product_id, p.name AS product_name,
                   p.sku AS product_sku, r.status, r.created_at
            FROM replacement_orders r
            JOIN products p ON r.product_id = p.id
            WHERE r.id = ?
            """,
            (replacement_id,),
        ).fetchone()

        if not row:
            raise ValueError(f"Replacement order #{replacement_id} not found")
        return dict_from_row(row)
    finally:
        conn.close()


get_replacement_order._annotations = READ_ONLY


@mcp.tool(
    name="list_replacements_for_order",
    description="List all replacement orders created for a specific original order ID. [READ]",
)
def list_replacements_for_order(order_id: int) -> list[dict[str, Any]]:
    """List all replacement orders created for an original shop order.

    Args:
        order_id: Original shop order ID.

    Returns:
        list[dict]: List of replacement orders.
    """
    chaos.maybe_fail("list_replacements_for_order", CONNECTOR_NAME)
    _ensure_database()

    conn = get_connection(CONNECTOR_NAME)
    try:
        rows = conn.execute(
            """
            SELECT r.id, r.original_order_id, r.product_id, p.name AS product_name,
                   p.sku AS product_sku, r.status, r.created_at
            FROM replacement_orders r
            JOIN products p ON r.product_id = p.id
            WHERE r.original_order_id = ?
            ORDER BY r.id ASC
            """,
            (order_id,),
        ).fetchall()
        return rows_to_dicts(rows)
    finally:
        conn.close()


list_replacements_for_order._annotations = READ_ONLY


if __name__ == "__main__":
    mcp.run(transport="stdio")
