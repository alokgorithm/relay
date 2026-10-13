"""Database initialization and seeding for the Chat MCP connector."""

from datetime import UTC, datetime

try:
    from ..common.db import get_connection
except ImportError:
    from connectors.common.db import get_connection

CONNECTOR_NAME = "chat"


def init_db(drop_existing: bool = False) -> None:
    """Create chat database tables."""
    conn = get_connection(CONNECTOR_NAME)
    try:
        with conn:
            if drop_existing:
                conn.execute("DROP TABLE IF EXISTS approvals")
                conn.execute("DROP TABLE IF EXISTS messages")
                conn.execute("DROP TABLE IF EXISTS channels")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
                    author TEXT NOT NULL,
                    body TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS approvals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
                    requestor TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    amount INTEGER,
                    status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'denied')) DEFAULT 'pending',
                    decided_by TEXT,
                    decided_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_messages_channel_id ON messages(channel_id);
                CREATE INDEX IF NOT EXISTS idx_approvals_channel_id ON approvals(channel_id);
                CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);
                """
            )
    finally:
        conn.close()


def seed_data() -> None:
    """Populate default channels and welcome message."""
    conn = get_connection(CONNECTOR_NAME)
    now = datetime.now(UTC).isoformat()
    try:
        with conn:
            channels = ["support-ops", "approvals", "general"]
            for name in channels:
                conn.execute(
                    "INSERT OR IGNORE INTO channels (name, created_at) VALUES (?, ?)",
                    (name, now),
                )

            # Get support-ops channel id
            row = conn.execute(
                "SELECT id FROM channels WHERE name = ?",
                ("support-ops",),
            ).fetchone()

            if row:
                channel_id = row["id"]
                welcome_body = "Relay agent connected. Ready to process support tickets."
                # Avoid inserting duplicate welcome message if already seeded
                existing = conn.execute(
                    "SELECT id FROM messages WHERE channel_id = ? AND body = ?",
                    (channel_id, welcome_body),
                ).fetchone()
                if not existing:
                    conn.execute(
                        "INSERT INTO messages (channel_id, author, body, created_at) VALUES (?, ?, ?, ?)",
                        (channel_id, "relay-agent", welcome_body, now),
                    )
    finally:
        conn.close()


def main() -> None:
    """Run database initialization and seeding."""
    print("Initializing chat connector database...")
    init_db()
    print("Seeding initial chat data...")
    seed_data()
    print("Chat database setup completed successfully.")


if __name__ == "__main__":
    main()
