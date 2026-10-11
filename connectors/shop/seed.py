"""Database initialization and seeding for the Shop MCP connector."""

from datetime import datetime, timedelta, timezone
import json
import sqlite3
from typing import Any

try:
    from ..common.db import get_connection, get_db_path
except ImportError:
    from connectors.common.db import get_connection, get_db_path

CONNECTOR_NAME = "shop"


def init_db(drop_existing: bool = True) -> None:
    """Create shop database tables."""
    conn = get_connection(CONNECTOR_NAME)
    try:
        with conn:
            if drop_existing:
                conn.execute("DROP TABLE IF EXISTS shipments")
                conn.execute("DROP TABLE IF EXISTS orders")
                conn.execute("DROP TABLE IF EXISTS customers")

            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS customers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    email TEXT NOT NULL UNIQUE,
                    phone TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY,
                    customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
                    status TEXT NOT NULL CHECK(status IN ('delivered', 'shipped', 'processing', 'cancelled')),
                    total_amount INTEGER NOT NULL,
                    currency TEXT NOT NULL DEFAULT 'INR',
                    items_json TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS shipments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
                    carrier TEXT NOT NULL,
                    tracking_number TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL CHECK(status IN ('delivered', 'in_transit', 'shipped')),
                    shipped_at TIMESTAMP,
                    delivered_at TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_customers_email ON customers(email);
                CREATE INDEX IF NOT EXISTS idx_orders_customer_id ON orders(customer_id);
                CREATE INDEX IF NOT EXISTS idx_shipments_order_id ON shipments(order_id);
                """
            )
    finally:
        conn.close()


def seed_data() -> None:
    """Populate customers, orders, and shipments for all demo scenarios."""
    conn = get_connection(CONNECTOR_NAME)
    now = datetime.now(timezone.utc)

    customers = [
        {"id": 1, "name": "Ananya Sharma", "email": "ananya@example.com", "phone": "+91-9876543210"},
        {"id": 2, "name": "Rohan Mehta", "email": "rohan@example.com", "phone": "+91-9876543211"},
        {"id": 3, "name": "Priya Patel", "email": "priya@example.com", "phone": "+91-9876543212"},
        {"id": 4, "name": "Vikram Singh", "email": "vikram@example.com", "phone": "+91-9876543213"},
        {"id": 5, "name": "Neha Gupta", "email": "neha@example.com", "phone": "+91-9876543214"},
        {"id": 6, "name": "Amit Kumar", "email": "amit@example.com", "phone": "+91-9876543215"},
    ]

    orders = [
        # Customer 1: Ananya
        {
            "id": 1042,
            "customer_id": 1,
            "status": "delivered",
            "total_amount": 3200,
            "currency": "INR",
            "items": [{"name": "Smartphone Screen Guard", "sku": "SGR-001", "quantity": 1, "price": 3200}],
            "created_days_ago": 6,
            "carrier": "BlueDart",
            "tracking_number": "BD1042001",
            "shipment_status": "delivered",
            "shipped_days_ago": 5,
            "delivered_days_ago": 4,
        },
        {
            "id": 1043,
            "customer_id": 1,
            "status": "delivered",
            "total_amount": 18000,
            "currency": "INR",
            "items": [{"name": 'Gaming Laptop 15"', "sku": "LAP-001", "quantity": 1, "price": 18000}],
            "created_days_ago": 14,
            "carrier": "Delhivery",
            "tracking_number": "DL1043002",
            "shipment_status": "delivered",
            "shipped_days_ago": 12,
            "delivered_days_ago": 10,
        },
        # Customer 2: Rohan
        {
            "id": 1044,
            "customer_id": 2,
            "status": "delivered",
            "total_amount": 4500,
            "currency": "INR",
            "items": [{"name": "Wireless Headphones", "sku": "HPH-001", "quantity": 1, "price": 4500}],
            "created_days_ago": 8,
            "carrier": "DTDC",
            "tracking_number": "DT1044003",
            "shipment_status": "delivered",
            "shipped_days_ago": 7,
            "delivered_days_ago": 5,
        },
        {
            "id": 1045,
            "customer_id": 2,
            "status": "delivered",
            "total_amount": 2800,
            "currency": "INR",
            "items": [{"name": "Cotton Kurta Set", "sku": "KRT-001", "quantity": 1, "price": 2800}],
            "created_days_ago": 50,
            "carrier": "BlueDart",
            "tracking_number": "BD1045004",
            "shipment_status": "delivered",
            "shipped_days_ago": 48,
            "delivered_days_ago": 45,
        },
        # Customer 3: Priya
        {
            "id": 1046,
            "customer_id": 3,
            "status": "delivered",
            "total_amount": 6500,
            "currency": "INR",
            "items": [{"name": "Ceramic Dinner Set", "sku": "CER-001", "quantity": 1, "price": 6500}],
            "created_days_ago": 6,
            "carrier": "Delhivery",
            "tracking_number": "DL1046005",
            "shipment_status": "delivered",
            "shipped_days_ago": 5,
            "delivered_days_ago": 3,
        },
        # Customer 4: Vikram
        {
            "id": 1047,
            "customer_id": 4,
            "status": "shipped",
            "total_amount": 1500,
            "currency": "INR",
            "items": [{"name": "Running Shoes", "sku": "SHO-001", "quantity": 1, "price": 1500}],
            "created_days_ago": 3,
            "carrier": "BlueDart",
            "tracking_number": "BD1047006",
            "shipment_status": "in_transit",
            "shipped_days_ago": 2,
            "delivered_days_ago": None,
        },
        {
            "id": 1048,
            "customer_id": 4,
            "status": "delivered",
            "total_amount": 9200,
            "currency": "INR",
            "items": [{"name": "Bluetooth Speaker", "sku": "SPK-001", "quantity": 1, "price": 9200}],
            "created_days_ago": 5,
            "carrier": "DTDC",
            "tracking_number": "DT1048007",
            "shipment_status": "delivered",
            "shipped_days_ago": 4,
            "delivered_days_ago": 2,
        },
        # Customer 5: Neha
        {
            "id": 1049,
            "customer_id": 5,
            "status": "delivered",
            "total_amount": 3800,
            "currency": "INR",
            "items": [{"name": "Yoga Mat Premium", "sku": "YGA-001", "quantity": 1, "price": 3800}],
            "created_days_ago": 10,
            "carrier": "Delhivery",
            "tracking_number": "DL1049008",
            "shipment_status": "delivered",
            "shipped_days_ago": 9,
            "delivered_days_ago": 7,
        },
        {
            "id": 1050,
            "customer_id": 5,
            "status": "delivered",
            "total_amount": 4200,
            "currency": "INR",
            "items": [{"name": "Stainless Steel Water Bottle", "sku": "BTL-001", "quantity": 1, "price": 4200}],
            "created_days_ago": 4,
            "carrier": "BlueDart",
            "tracking_number": "BD1050009",
            "shipment_status": "delivered",
            "shipped_days_ago": 3,
            "delivered_days_ago": 1,
        },
        # Customer 6: Amit
        {
            "id": 1051,
            "customer_id": 6,
            "status": "delivered",
            "total_amount": 2100,
            "currency": "INR",
            "items": [{"name": "Notebook Set", "sku": "NTB-001", "quantity": 1, "price": 2100}],
            "created_days_ago": 9,
            "carrier": "DTDC",
            "tracking_number": "DT1051010",
            "shipment_status": "delivered",
            "shipped_days_ago": 8,
            "delivered_days_ago": 6,
        },
    ]

    try:
        with conn:
            for cust in customers:
                created_at = (now - timedelta(days=60)).isoformat()
                conn.execute(
                    "INSERT INTO customers (id, name, email, phone, created_at) VALUES (?, ?, ?, ?, ?)",
                    (cust["id"], cust["name"], cust["email"], cust["phone"], created_at),
                )

            for ord_data in orders:
                created_at = (now - timedelta(days=ord_data["created_days_ago"])).isoformat()
                conn.execute(
                    """
                    INSERT INTO orders (id, customer_id, status, total_amount, currency, items_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ord_data["id"],
                        ord_data["customer_id"],
                        ord_data["status"],
                        ord_data["total_amount"],
                        ord_data["currency"],
                        json.dumps(ord_data["items"]),
                        created_at,
                    ),
                )

                shipped_at = (now - timedelta(days=ord_data["shipped_days_ago"])).isoformat() if ord_data["shipped_days_ago"] else None
                delivered_at = (now - timedelta(days=ord_data["delivered_days_ago"])).isoformat() if ord_data["delivered_days_ago"] else None

                conn.execute(
                    """
                    INSERT INTO shipments (order_id, carrier, tracking_number, status, shipped_at, delivered_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ord_data["id"],
                        ord_data["carrier"],
                        ord_data["tracking_number"],
                        ord_data["shipment_status"],
                        shipped_at,
                        delivered_at,
                    ),
                )
    finally:
        conn.close()

    print(f"Seeded {len(customers)} customers, {len(orders)} orders, {len(orders)} shipments into shop.db")


if __name__ == "__main__":
    init_db(drop_existing=True)
    seed_data()
