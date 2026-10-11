"""Database initialization and seeding for the Inventory MCP connector."""

from datetime import datetime, timezone
import sqlite3
from typing import Any

try:
    from ..common.db import get_connection
except ImportError:
    from connectors.common.db import get_connection

CONNECTOR_NAME = "inventory"

SEED_PRODUCTS: list[dict[str, Any]] = [
    {
        "name": "Smartphone Screen Guard",
        "sku": "SGR-001",
        "description": "Tempered glass screen protector with anti-scratch coating and oleophobic finish.",
        "price": 3200,
        "stock_quantity": 15,
        "category": "Accessories",
    },
    {
        "name": 'Gaming Laptop 15"',
        "sku": "LAP-001",
        "description": "High-performance gaming laptop with 15.6-inch high refresh display and dedicated GPU.",
        "price": 18000,
        "stock_quantity": 3,
        "category": "Electronics",
    },
    {
        "name": "Wireless Headphones",
        "sku": "HPH-001",
        "description": "Over-ear wireless headphones with active noise cancellation and 30-hour battery life.",
        "price": 4500,
        "stock_quantity": 8,
        "category": "Electronics",
    },
    {
        "name": "Cotton Kurta Set",
        "sku": "KRT-001",
        "description": "Premium handcrafted cotton kurta set with ethnic pattern and tailored fit.",
        "price": 2800,
        "stock_quantity": 20,
        "category": "Apparel",
    },
    {
        "name": "Ceramic Dinner Set",
        "sku": "CER-001",
        "description": "16-piece artisan ceramic dinner set including plates, side bowls, and mugs.",
        "price": 6500,
        "stock_quantity": 5,
        "category": "Home & Kitchen",
    },
    {
        "name": "Running Shoes",
        "sku": "SHO-001",
        "description": "Breathable lightweight athletic running shoes with responsive cushioned soles.",
        "price": 1500,
        "stock_quantity": 12,
        "category": "Footwear",
    },
    {
        "name": "Bluetooth Speaker",
        "sku": "SPK-001",
        "description": "Portable waterproof Bluetooth speaker with 360-degree surround sound and rich bass.",
        "price": 9200,
        "stock_quantity": 0,  # OUT OF STOCK - for testing
        "category": "Electronics",
    },
    {
        "name": "Yoga Mat Premium",
        "sku": "YGA-001",
        "description": "High-density eco-friendly non-slip yoga mat with alignment guides and carry strap.",
        "price": 3800,
        "stock_quantity": 7,
        "category": "Fitness",
    },
    {
        "name": "Stainless Steel Water Bottle",
        "sku": "BTL-001",
        "description": "Double-walled vacuum insulated stainless steel water bottle, keeps cold 24h / hot 12h.",
        "price": 4200,
        "stock_quantity": 25,
        "category": "Home & Kitchen",
    },
    {
        "name": "Notebook Set",
        "sku": "NTB-001",
        "description": "Pack of 3 hardcover ruled notebooks with archival-grade acid-free paper.",
        "price": 2100,
        "stock_quantity": 30,
        "category": "Stationery",
    },
]


def init_db(drop_existing: bool = True) -> None:
    """Create inventory database tables."""
    conn = get_connection(CONNECTOR_NAME)
    try:
        with conn:
            if drop_existing:
                conn.execute("DROP TABLE IF EXISTS replacement_orders")
                conn.execute("DROP TABLE IF EXISTS products")

            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    sku TEXT NOT NULL UNIQUE,
                    description TEXT,
                    price INTEGER NOT NULL,
                    stock_quantity INTEGER NOT NULL DEFAULT 0,
                    category TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS replacement_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    original_order_id INTEGER NOT NULL,
                    product_id INTEGER NOT NULL REFERENCES products(id),
                    status TEXT NOT NULL CHECK(status IN ('pending', 'shipped', 'delivered', 'cancelled')) DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_products_sku ON products(sku);
                CREATE INDEX IF NOT EXISTS idx_products_category ON products(category);
                CREATE INDEX IF NOT EXISTS idx_replacement_orders_original_order_id ON replacement_orders(original_order_id);
                CREATE INDEX IF NOT EXISTS idx_replacement_orders_product_id ON replacement_orders(product_id);
                """
            )
    finally:
        conn.close()


def seed_data() -> None:
    """Seed initial products data."""
    conn = get_connection(CONNECTOR_NAME)
    try:
        with conn:
            for item in SEED_PRODUCTS:
                conn.execute(
                    """
                    INSERT INTO products (name, sku, description, price, stock_quantity, category)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(sku) DO UPDATE SET
                        name = excluded.name,
                        description = excluded.description,
                        price = excluded.price,
                        stock_quantity = excluded.stock_quantity,
                        category = excluded.category
                    """,
                    (
                        item["name"],
                        item["sku"],
                        item["description"],
                        item["price"],
                        item["stock_quantity"],
                        item["category"],
                    ),
                )
    finally:
        conn.close()


def print_summary() -> None:
    """Print summary of inventory database contents."""
    conn = get_connection(CONNECTOR_NAME)
    try:
        products = conn.execute("SELECT * FROM products ORDER BY id ASC").fetchall()
        replacements = conn.execute("SELECT * FROM replacement_orders ORDER BY id ASC").fetchall()

        print("=" * 60)
        print(f"Inventory Database Summary ({CONNECTOR_NAME}.db)")
        print("=" * 60)
        print(f"Total Products: {len(products)}")
        for p in products:
            print(
                f"  Product #{p['id']} | {p['name']} (SKU: {p['sku']}) | "
                f"INR {p['price']} | Stock: {p['stock_quantity']} | Category: {p['category']}"
            )
        print(f"\nTotal Replacement Orders: {len(replacements)}")
        for r in replacements:
            print(
                f"  Replacement #{r['id']} | Original Order #{r['original_order_id']} | "
                f"Product #{r['product_id']} | Status: {r['status']}"
            )
        print("=" * 60)
    finally:
        conn.close()


def main() -> None:
    """Run database reset and seeding."""
    print("Initializing inventory database (dropping existing tables)...")
    init_db(drop_existing=True)
    print("Seeding inventory products...")
    seed_data()
    print_summary()
    print("Inventory database setup completed successfully.")


if __name__ == "__main__":
    main()
