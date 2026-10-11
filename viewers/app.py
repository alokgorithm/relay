"""Demo viewers dashboard for Relay — read-only visualization of all mock business systems and runs."""

from datetime import datetime
import json
from pathlib import Path
import sqlite3
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
import uvicorn

app = FastAPI(title="Relay Demo Viewer", description="Read-only view of Acme Store systems and Relay agent runs")

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
RUNS_DIR = ROOT_DIR / "runs"


def _query_db(db_name: str, query: str, params: tuple = ()) -> list[dict[str, Any]]:
    db_path = DATA_DIR / f"{db_name}.db"
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute(query, params)
        rows = cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


CSS = """
<style>
  :root { --bg: #0f172a; --card: #1e293b; --text: #f8fafc; --muted: #94a3b8; --border: #334155; --accent: #38bdf8; --green: #4ade80; --yellow: #facc15; --red: #f87171; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 24px; }
  h1, h2, h3 { margin-top: 0; }
  .nav { display: flex; gap: 12px; margin-bottom: 24px; border-bottom: 1px solid var(--border); padding-bottom: 12px; flex-wrap: wrap; }
  .nav a { color: var(--accent); text-decoration: none; font-weight: 500; padding: 6px 12px; border-radius: 6px; background: rgba(56, 189, 248, 0.1); }
  .nav a:hover { background: rgba(56, 189, 248, 0.2); }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 20px; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; margin-bottom: 20px; overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; text-align: left; }
  th, td { padding: 8px 10px; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); text-transform: uppercase; font-size: 11px; letter-spacing: 0.5px; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 600; text-transform: uppercase; }
  .badge-green { background: rgba(74, 222, 128, 0.2); color: var(--green); }
  .badge-yellow { background: rgba(250, 204, 21, 0.2); color: var(--yellow); }
  .badge-red { background: rgba(248, 113, 113, 0.2); color: var(--red); }
  .badge-blue { background: rgba(56, 189, 248, 0.2); color: var(--accent); }
  pre { background: #0b1120; padding: 12px; border-radius: 6px; overflow-x: auto; font-size: 12px; border: 1px solid var(--border); }
</style>
"""

HEADER = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Relay — Acme Store Demo Viewer</title>
  {css}
</head>
<body>
  <h1>⚡ Relay — Acme Store Operations Viewer</h1>
  <div class="nav">
    <a href="/">Overview</a>
    <a href="/shop">Shop & Orders</a>
    <a href="/payments">Payments & Refunds</a>
    <a href="/helpdesk">Helpdesk Tickets</a>
    <a href="/chat">Chat & Approvals</a>
    <a href="/inventory">Inventory & Replacements</a>
    <a href="/runs">Agent Runs</a>
  </div>
""".format(css=CSS)

FOOTER = """
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    tickets = _query_db("helpdesk", "SELECT id, subject, customer_email, status, created_at FROM tickets ORDER BY id ASC")
    charges = _query_db("payments", "SELECT id, order_id, amount, status, customer_email FROM charges ORDER BY id ASC")
    refunds = _query_db("payments", "SELECT id, charge_id, amount, status, idempotency_key FROM refunds ORDER BY id ASC")
    approvals = _query_db("chat", "SELECT id, summary, amount, status FROM approvals ORDER BY id DESC")
    
    html = [HEADER, "<div class='grid'>"]
    
    # Helpdesk summary
    html.append("<div class='card'><h3>🎫 Open Helpdesk Tickets</h3><table><tr><th>ID</th><th>Subject</th><th>Status</th></tr>")
    for t in tickets[:6]:
        color = "green" if t["status"] in ("resolved", "closed") else "yellow" if t["status"] == "waiting_customer" else "blue"
        html.append(f"<tr><td>#{t['id']}</td><td>{t['subject']}</td><td><span class='badge badge-{color}'>{t['status']}</span></td></tr>")
    html.append("</table></div>")
    
    # Payments summary
    html.append("<div class='card'><h3>💳 Recent Refunds</h3><table><tr><th>ID</th><th>Charge</th><th>Amount</th><th>Status</th></tr>")
    if refunds:
        for r in refunds:
            html.append(f"<tr><td>#{r['id']}</td><td>Charge #{r['charge_id']}</td><td>₹{r['amount']}</td><td><span class='badge badge-green'>{r['status']}</span></td></tr>")
    else:
        html.append("<tr><td colspan='4'>No refunds issued yet</td></tr>")
    html.append("</table></div>")
    
    # Approvals summary
    html.append("<div class='card'><h3>🛡️ Approvals</h3><table><tr><th>ID</th><th>Summary</th><th>Amount</th><th>Status</th></tr>")
    if approvals:
        for a in approvals:
            c = "green" if a["status"] == "approved" else "red" if a["status"] == "denied" else "yellow"
            html.append(f"<tr><td>#{a['id']}</td><td>{a['summary']}</td><td>₹{a.get('amount') or 0}</td><td><span class='badge badge-{c}'>{a['status']}</span></td></tr>")
    else:
        html.append("<tr><td colspan='4'>No approvals requested</td></tr>")
    html.append("</table></div>")

    html.append("</div>", FOOTER)
    return "".join(html)


@app.get("/shop", response_class=HTMLResponse)
def view_shop():
    orders = _query_db("shop", "SELECT * FROM orders ORDER BY id ASC")
    customers = _query_db("shop", "SELECT * FROM customers ORDER BY id ASC")
    shipments = _query_db("shop", "SELECT * FROM shipments ORDER BY order_id ASC")
    
    html = [HEADER, "<h2>Shop System (SQLite: shop.db)</h2>", "<div class='card'><h3>Orders</h3><table><tr><th>Order ID</th><th>Customer ID</th><th>Status</th><th>Total</th><th>Items</th></tr>"]
    for o in orders:
        html.append(f"<tr><td><b>#{o['id']}</b></td><td>Cust #{o['customer_id']}</td><td><span class='badge badge-blue'>{o['status']}</span></td><td>₹{o['total_amount']}</td><td><code>{o['items_json']}</code></td></tr>")
    html.append("</table></div>")
    
    html.append("<div class='card'><h3>Shipments</h3><table><tr><th>Order ID</th><th>Carrier</th><th>Tracking</th><th>Status</th><th>Delivered At</th></tr>")
    for s in shipments:
        html.append(f"<tr><td>#{s['order_id']}</td><td>{s['carrier']}</td><td>{s['tracking_number']}</td><td><span class='badge badge-green'>{s['status']}</span></td><td>{s['delivered_at'] or 'In Transit'}</td></tr>")
    html.append("</table></div>")
    
    html.append(FOOTER)
    return "".join(html)


@app.get("/payments", response_class=HTMLResponse)
def view_payments():
    charges = _query_db("payments", "SELECT * FROM charges ORDER BY id ASC")
    refunds = _query_db("payments", "SELECT * FROM refunds ORDER BY id ASC")
    
    html = [HEADER, "<h2>Payments System (SQLite: payments.db)</h2>"]
    html.append("<div class='card'><h3>Charges</h3><table><tr><th>ID</th><th>Order ID</th><th>Amount</th><th>Status</th><th>Customer</th><th>Created</th></tr>")
    for c in charges:
        badge = "green" if c["status"] == "succeeded" else "yellow" if c["status"] == "partially_refunded" else "blue"
        html.append(f"<tr><td>#{c['id']}</td><td>Order #{c['order_id']}</td><td>₹{c['amount']}</td><td><span class='badge badge-{badge}'>{c['status']}</span></td><td>{c['customer_email']}</td><td>{c['created_at']}</td></tr>")
    html.append("</table></div>")
    
    html.append("<div class='card'><h3>Refunds (Idempotency Key Protected)</h3><table><tr><th>ID</th><th>Charge ID</th><th>Amount</th><th>Status</th><th>Idempotency Key</th><th>Created</th></tr>")
    for r in refunds:
        html.append(f"<tr><td>#{r['id']}</td><td>Charge #{r['charge_id']}</td><td>₹{r['amount']}</td><td><span class='badge badge-green'>{r['status']}</span></td><td><code>{r['idempotency_key']}</code></td><td>{r['created_at']}</td></tr>")
    html.append("</table></div>")
    
    html.append(FOOTER)
    return "".join(html)


@app.get("/helpdesk", response_class=HTMLResponse)
def view_helpdesk():
    tickets = _query_db("helpdesk", "SELECT * FROM tickets ORDER BY id ASC")
    replies = _query_db("helpdesk", "SELECT * FROM ticket_replies ORDER BY id DESC")
    
    html = [HEADER, "<h2>Helpdesk System (SQLite: helpdesk.db)</h2>"]
    html.append("<div class='card'><h3>Tickets</h3><table><tr><th>ID</th><th>Subject</th><th>Email</th><th>Status</th><th>Tags</th></tr>")
    for t in tickets:
        badge = "green" if t["status"] in ("resolved", "closed") else "yellow" if t["status"] in ("waiting_customer", "escalated") else "blue"
        html.append(f"<tr><td>#{t['id']}</td><td>{t['subject']}</td><td>{t['customer_email']}</td><td><span class='badge badge-{badge}'>{t['status']}</span></td><td>{t.get('tags') or ''}</td></tr>")
    html.append("</table></div>")
    
    html.append("<div class='card'><h3>Recent Replies & Internal Notes</h3><table><tr><th>Ticket</th><th>Author</th><th>Type</th><th>Body</th><th>Created</th></tr>")
    for r in replies[:15]:
        note_type = "Internal Note" if r.get("is_internal") else "Public Reply"
        badge = "yellow" if r.get("is_internal") else "blue"
        html.append(f"<tr><td>#{r['ticket_id']}</td><td>{r['author']}</td><td><span class='badge badge-{badge}'>{note_type}</span></td><td>{r['body'][:120]}</td><td>{r['created_at']}</td></tr>")
    html.append("</table></div>")
    
    html.append(FOOTER)
    return "".join(html)


@app.get("/chat", response_class=HTMLResponse)
def view_chat():
    channels = _query_db("chat", "SELECT * FROM channels")
    messages = _query_db("chat", "SELECT m.*, c.name as channel_name FROM messages m JOIN channels c ON m.channel_id = c.id ORDER BY m.id DESC")
    approvals = _query_db("chat", "SELECT * FROM approvals ORDER BY id DESC")
    
    html = [HEADER, "<h2>Chat System (SQLite: chat.db)</h2>"]
    html.append("<div class='card'><h3>Approvals Channel (#approvals)</h3><table><tr><th>ID</th><th>Summary</th><th>Amount</th><th>Status</th><th>Decided By</th></tr>")
    for a in approvals:
        badge = "green" if a["status"] == "approved" else "red" if a["status"] == "denied" else "yellow"
        html.append(f"<tr><td>#{a['id']}</td><td>{a['summary']}</td><td>₹{a.get('amount') or 0}</td><td><span class='badge badge-{badge}'>{a['status']}</span></td><td>{a.get('decided_by') or 'Pending'}</td></tr>")
    html.append("</table></div>")
    
    html.append("<div class='card'><h3>Recent Messages</h3><table><tr><th>Channel</th><th>Author</th><th>Message</th><th>Time</th></tr>")
    for m in messages[:15]:
        html.append(f"<tr><td>#{m['channel_name']}</td><td><b>{m['author']}</b></td><td>{m['body']}</td><td>{m['created_at']}</td></tr>")
    html.append("</table></div>")
    
    html.append(FOOTER)
    return "".join(html)


@app.get("/inventory", response_class=HTMLResponse)
def view_inventory():
    products = _query_db("inventory", "SELECT * FROM products ORDER BY id ASC")
    replacements = _query_db("inventory", "SELECT * FROM replacement_orders ORDER BY id ASC")
    
    html = [HEADER, "<h2>Inventory System (SQLite: inventory.db)</h2>"]
    html.append("<div class='card'><h3>Products</h3><table><tr><th>ID</th><th>Name</th><th>SKU</th><th>Price</th><th>Stock</th><th>Category</th></tr>")
    for p in products:
        badge = "green" if p["stock_quantity"] > 0 else "red"
        html.append(f"<tr><td>#{p['id']}</td><td>{p['name']}</td><td><code>{p['sku']}</code></td><td>₹{p['price']}</td><td><span class='badge badge-{badge}'>{p['stock_quantity']}</span></td><td>{p['category']}</td></tr>")
    html.append("</table></div>")
    
    html.append("<div class='card'><h3>Replacement Orders</h3><table><tr><th>ID</th><th>Original Order</th><th>Product ID</th><th>Status</th><th>Created</th></tr>")
    if replacements:
        for r in replacements:
            html.append(f"<tr><td>#{r['id']}</td><td>Order #{r['original_order_id']}</td><td>Prod #{r['product_id']}</td><td><span class='badge badge-green'>{r['status']}</span></td><td>{r['created_at']}</td></tr>")
    else:
        html.append("<tr><td colspan='5'>No replacement orders yet</td></tr>")
    html.append("</table></div>")
    
    html.append(FOOTER)
    return "".join(html)


@app.get("/runs", response_class=HTMLResponse)
def view_runs():
    html = [HEADER, "<h2>Agent Runs</h2>"]
    runs = []
    if RUNS_DIR.exists():
        for rdir in sorted(RUNS_DIR.iterdir(), reverse=True):
            if rdir.is_dir() and (rdir / "state.json").exists():
                try:
                    state_data = json.loads((rdir / "state.json").read_text())
                    runs.append(state_data)
                except Exception:
                    pass
    
    html.append("<div class='card'><h3>Run History</h3><table><tr><th>Run ID</th><th>Ticket</th><th>Status</th><th>Steps</th><th>Actions</th><th>Updated</th></tr>")
    if runs:
        for r in runs:
            badge = "green" if r["status"] == "resolved" else "yellow" if r["status"] in ("waiting_customer", "waiting_human") else "red"
            html.append(f"<tr><td><code>{r['run_id']}</code></td><td>Ticket #{r['ticket_id']}</td><td><span class='badge badge-{badge}'>{r['status']}</span></td><td>{r.get('step_count', 0)}</td><td>{len(r.get('actions', []))}</td><td>{r.get('updated_at', '')}</td></tr>")
    else:
        html.append("<tr><td colspan='6'>No runs executed yet</td></tr>")
    html.append("</table></div>")
    
    html.append(FOOTER)
    return "".join(html)


def main():
    uvicorn.run("viewers.app:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()
