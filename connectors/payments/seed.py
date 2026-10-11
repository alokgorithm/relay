"""Database initialization and seeding for the Payments MCP connector."""

from datetime import datetime, timedelta, timezone
import sqlite3
from typing import Any

try:
    from ..common.db import get_connection
except ImportError:
    from connectors.common.db import get_connection

CONNECTOR_NAME = "payments"


def init_db(drop_existing: bool = True) -> None:
    """Create payments database tables."""
    conn = get_connection(CONNECTOR_NAME)
    try:
        with conn:
            if drop_existing:
                conn.execute("DROP TABLE IF EXISTS refunds")
                conn.execute("DROP TABLE IF EXISTS charges")

            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS charges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL UNIQUE,
                    amount INTEGER NOT NULL,
                    currency TEXT NOT NULL DEFAULT 'INR',
                    status TEXT NOT NULL CHECK(status IN ('succeeded', 'refunded', 'partially_refunded')) DEFAULT 'succeeded',
                    customer_email TEXT NOT NULL,
                    description TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS refunds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    charge_id INTEGER NOT NULL REFERENCES charges(id) ON DELETE CASCADE,
                    amount INTEGER NOT NULL,
                    currency TEXT NOT NULL DEFAULT 'INR',
                    status TEXT NOT NULL CHECK(status IN ('succeeded', 'pending', 'failed')) DEFAULT 'succeeded',
                    reason TEXT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_charges_order_id ON charges(order_id);
                CREATE INDEX IF NOT EXISTS idx_refunds_charge_id ON refunds(charge_id);
                CREATE INDEX IF NOT EXISTS idx_refunds_idempotency_key ON refunds(idempotency_key);
                """
            )
    finally:
        conn.close()


def seed_data() -> None:
    """Populate charges matching shop orders (1042-1051) and initial refund for #1044."""
    conn = get_connection(CONNECTOR_NAME)
    now = datetime.now(timezone.utc)

    # Orders matching shop connector:
    # 1042: ₹3200 (delivered 4 days ago)
    # 1043: ₹18000 (delivered 10 days ago)
    # 1044: ₹4500 (delivered 5 days ago, already refunded)
    # 1045: ₹2800 (delivered 45 days ago)
    # 1046: ₹6500 (delivered 3 days ago)
    # 1047: ₹1500 (shipped but not delivered yet)
    # 1048: ₹9200 (delivered 2 days ago)
    # 1049: ₹3800 (delivered 7 days ago)
    # 1050: ₹4200 (delivered 1 day ago)
    # 1051: ₹2100 (delivered 6 days ago)
    charges_to_seed = [
        {
            "order_id": 1042,
            "amount": 3200,
            "currency": "INR",
            "status": "succeeded",
            "customer_email": "ananya@example.com",
            "description": "Order #1042 payment",
            "days_ago": 5,
        },
        {
            "order_id": 1043,
            "amount": 18000,
            "currency": "INR",
            "status": "succeeded",
            "customer_email": "ananya@example.com",
            "description": "Order #1043 payment",
            "days_ago": 11,
        },
        {
            "order_id": 1044,
            "amount": 4500,
            "currency": "INR",
            "status": "refunded",  # Seeded as refunded
            "customer_email": "rohan@example.com",
            "description": "Order #1044 payment",
            "days_ago": 6,
        },
        {
            "order_id": 1045,
            "amount": 2800,
            "currency": "INR",
            "status": "succeeded",
            "customer_email": "rohan@example.com",
            "description": "Order #1045 payment",
            "days_ago": 46,
        },
        {
            "order_id": 1046,
            "amount": 6500,
            "currency": "INR",
            "status": "succeeded",
            "customer_email": "priya@example.com",
            "description": "Order #1046 payment",
            "days_ago": 4,
        },
        {
            "order_id": 1047,
            "amount": 1500,
            "currency": "INR",
            "status": "succeeded",
            "customer_email": "vikram@example.com",
            "description": "Order #1047 payment",
            "days_ago": 3,
        },
        {
            "order_id": 1048,
            "amount": 9200,
            "currency": "INR",
            "status": "succeeded",
            "customer_email": "vikram@example.com",
            "description": "Order #1048 payment",
            "days_ago": 3,
        },
        {
            "order_id": 1049,
            "amount": 3800,
            "currency": "INR",
            "status": "succeeded",
            "customer_email": "neha@example.com",
            "description": "Order #1049 payment",
            "days_ago": 8,
        },
        {
            "order_id": 1050,
            "amount": 4200,
            "currency": "INR",
            "status": "succeeded",
            "customer_email": "neha@example.com",
            "description": "Order #1050 payment",
            "days_ago": 2,
        },
        {
            "order_id": 1051,
            "amount": 2100,
            "currency": "INR",
            "status": "succeeded",
            "customer_email": "amit@example.com",
            "description": "Order #1051 payment",
            "days_ago": 7,
        },
    ]

    try:
        with conn:
            for item in charges_to_seed:
                created_at = (now - timedelta(days=item["days_ago"])).isoformat()
                conn.execute(
                    """
                    INSERT INTO charges (order_id, amount, currency, status, customer_email, description, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["order_id"],
                        item["amount"],
                        item["currency"],
                        item["status"],
                        item["customer_email"],
                        item["description"],
                        created_at,
                    ),
                )

            # Order #1044 already refunded: seed ONE refund with status 'succeeded'
            row_1044 = conn.execute(
                "SELECT id FROM charges WHERE order_id = ?", (1044,)
            ).fetchone()
            if row_1044:
                charge_1044_id = row_1044["id"]
                refund_created_at = (now - timedelta(days=4)).isoformat()
                conn.execute(
                    """
                    INSERT INTO refunds (charge_id, amount, currency, status, reason, idempotency_key, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        charge_1044_id,
                        4500,
                        "INR",
                        "succeeded",
                        "Customer return requested and approved",
                        "seed_refund_order_1044",
                        refund_created_at,
                    ),
                )
    finally:
        conn.close()


def print_summary() -> None:
    """Print a summary of the seeded data in the database."""
    conn = get_connection(CONNECTOR_NAME)
    try:
        charges = conn.execute("SELECT * FROM charges ORDER BY order_id ASC").fetchall()
        refunds = conn.execute("SELECT * FROM refunds ORDER BY id ASC").fetchall()

        print("=" * 60)
        print(f"Payments Database Summary ({CONNECTOR_NAME}.db)")
        print("=" * 60)
        print(f"Total Charges: {len(charges)}")
        for c in charges:
            print(
                f"  Charge #{c['id']} | Order #{c['order_id']} | {c['currency']} {c['amount']} | {c['status']} | {c['customer_email']}"
            )
        print(f"\nTotal Refunds: {len(refunds)}")
        for r in refunds:
            print(
                f"  Refund #{r['id']} | Charge #{r['charge_id']} | {r['currency']} {r['amount']} | {r['status']} | key={r['idempotency_key']}"
            )
        print("=" * 60)
    finally:
        conn.close()


def main() -> None:
    """Run database reset and seeding."""
    print("Initializing payments database (dropping existing tables)...")
    init_db(drop_existing=True)
    print("Seeding charges and refunds...")
    seed_data()
    print_summary()
    print("Payments database seeding complete.")


if __name__ == "__main__":
    main()
