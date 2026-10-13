"""Exercise the read-only viewer against temporary databases and run artifacts."""

import json
from html import escape
from html.parser import HTMLParser
from importlib import import_module

import pytest
from fastapi.testclient import TestClient

import viewers.app as viewer
from connectors.common import db

SYSTEMS = ("shop", "payments", "helpdesk", "chat", "inventory")
ROUTES = {
    "/": ("Open Helpdesk Tickets", "Damaged item - Order #1042"),
    "/shop": ("Shop System", "Cust #1"),
    "/payments": ("Payments System", "Order #1042"),
    "/helpdesk": ("Helpdesk System", "Viewer public reply"),
    "/chat": ("Chat System", "Viewer chat message"),
    "/inventory": ("Inventory System", "Order #1042"),
    "/runs": ("Run History", "viewer-resolved"),
}


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)

    def handle_data(self, data):
        self.text.append(data)


def snapshot():
    return {
        (name, path.relative_to(root)): path.read_bytes()
        for name, root in (("data", viewer.DATA_DIR), ("runs", viewer.RUNS_DIR))
        for path in root.rglob("*")
        if path.is_file()
    }


def execute(system, query, params=()):
    conn = db.get_connection(system)
    try:
        with conn:
            conn.execute(query, params)
    finally:
        conn.close()


def write_run(directory, **overrides):
    directory.mkdir()
    state = {
        "run_id": directory.name,
        "ticket_id": "1",
        "status": "resolved",
        "step_count": 3,
        "actions": [{"status": "succeeded"}, {"status": "failed"}],
        "updated_at": "2026-10-12T12:00:00+00:00",
        **overrides,
    }
    (directory / "state.json").write_text(json.dumps(state), encoding="utf-8")


@pytest.fixture
def client(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    runs_dir = tmp_path / "runs"
    data_dir.mkdir()
    runs_dir.mkdir()
    # The viewer does not use connectors.common.db.DATA_DIR or RELAY_DATA_DIR.
    monkeypatch.setattr(viewer, "DATA_DIR", data_dir)
    monkeypatch.setattr(viewer, "RUNS_DIR", runs_dir)
    monkeypatch.setattr(db, "DATA_DIR", data_dir)
    monkeypatch.setenv("RELAY_DATA_DIR", str(data_dir))
    with TestClient(viewer.app) as test_client:
        yield test_client


@pytest.fixture
def systems(client, request):
    mode = request.param
    if mode != "missing":
        for name in SYSTEMS:
            seed = import_module(f"connectors.{name}.seed")
            seed.init_db()
            if mode == "seeded":
                seed.seed_data()
    if mode == "seeded":
        for internal, body in ((0, "Viewer public reply"), (1, "Viewer internal note")):
            execute(
                "helpdesk",
                "INSERT INTO ticket_replies (ticket_id, author, body, is_internal) VALUES (1, 'agent', ?, ?)",
                (body, internal),
            )
        for status in ("approved", "denied", "pending"):
            execute(
                "chat",
                "INSERT INTO approvals (channel_id, requestor, summary, amount, status) VALUES (1, 'agent', ?, 3200, ?)",
                (f"Viewer approval {status}", status),
            )
        execute(
            "chat",
            "INSERT INTO messages (channel_id, author, body) VALUES (1, 'agent', 'Viewer chat message')",
        )
        execute(
            "inventory",
            "INSERT INTO replacement_orders (original_order_id, product_id) VALUES (1042, 1)",
        )
        for status in ("resolved", "waiting_human", "failed"):
            write_run(viewer.RUNS_DIR / f"viewer-{status}", status=status)
    return mode


@pytest.mark.parametrize("systems", ["missing", "empty", "seeded"], indirect=True)
@pytest.mark.parametrize("route", ROUTES)
def test_html_routes(client, systems, route):
    before = snapshot()
    response = client.get(route)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.text.startswith("\n<!DOCTYPE html>")
    assert response.text.rstrip().endswith("</body>\n</html>")
    heading, seeded_text = ROUTES[route]
    assert heading in response.text
    for nav_route in ROUTES:
        assert f'<a href="{nav_route}">' in response.text
    if systems == "seeded":
        assert seeded_text in response.text
        if route == "/helpdesk":
            assert "Viewer internal note" in response.text
            assert "Internal Note" in response.text
            assert "Public Reply" in response.text
        elif route == "/chat":
            for status, badge in (("approved", "green"), ("denied", "red"), ("pending", "yellow")):
                assert f"Viewer approval {status}" in response.text
                assert f"class='badge badge-{badge}'>{status}</span>" in response.text
        elif route == "/runs":
            for status in ("resolved", "waiting_human", "failed"):
                assert f"<code>viewer-{status}</code>" in response.text
            assert "<td>3</td><td>2</td>" in response.text
    else:
        assert seeded_text not in response.text
        parser = PageParser()
        parser.feed(response.text)
        notices = {
            "/": ("No refunds issued yet", "No approvals requested"),
            "/inventory": ("No replacement orders yet",),
            "/runs": ("No runs executed yet",),
        }.get(route, ())
        assert parser.tags.count("td") == len(notices)
        assert parser.tags.count("tr") == parser.tags.count("table") + len(notices)
        for notice in notices:
            assert notice in response.text
    assert snapshot() == before


def test_route_coverage():
    assert {getattr(route, "path", None) for route in viewer.app.routes} == set(ROUTES) | {
        "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc",
    }


@pytest.mark.parametrize("route", ["/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"])
def test_framework_routes(client, route):
    before = snapshot()
    response = client.get(route)
    assert response.status_code == 200
    if route == "/openapi.json":
        assert set(response.json()["paths"]) == set(ROUTES)
    else:
        assert response.headers["content-type"].startswith("text/html")
    assert snapshot() == before


@pytest.mark.parametrize("systems", ["seeded"], indirect=True)
@pytest.mark.parametrize("route", ROUTES)
def test_stored_text_is_escaped(client, systems, route):
    payload = '<img src=x onerror="alert(1)"><script>alert(2)</script> & "quoted" \'text\''
    if route in ("/", "/helpdesk"):
        execute("helpdesk", "UPDATE tickets SET subject = ?, customer_email = ?, tags = ? WHERE id = 1", (payload,) * 3)
        execute(
            "helpdesk",
            "UPDATE ticket_replies SET body = ? WHERE body IN ('Viewer public reply', 'Viewer internal note')",
            (payload,),
        )
        if route == "/":
            execute("chat", "UPDATE approvals SET summary = ?", (payload,))
        expected_count = 4 if route == "/" else 5
    elif route == "/shop":
        execute("shop", "UPDATE orders SET items_json = ? WHERE id = 1042", (payload,))
        execute("shop", "UPDATE shipments SET carrier = ?, tracking_number = ? WHERE order_id = 1042", (payload,) * 2)
        expected_count = 3
    elif route == "/payments":
        execute("payments", "UPDATE charges SET customer_email = ? WHERE id = 1", (payload,))
        execute("payments", "UPDATE refunds SET idempotency_key = ?", (payload,))
        expected_count = 2
    elif route == "/chat":
        execute("chat", "UPDATE approvals SET summary = ?, decided_by = ?", (payload,) * 2)
        execute("chat", "UPDATE messages SET author = ?, body = ?", (payload,) * 2)
        execute("chat", "UPDATE channels SET name = ? WHERE id = 1", (payload,))
        expected_count = 12
    elif route == "/inventory":
        execute("inventory", "UPDATE products SET name = ?, sku = ?, category = ? WHERE id = 1", (payload,) * 3)
        expected_count = 3
    else:
        write_run(viewer.RUNS_DIR / "untrusted", run_id=payload, ticket_id=payload, status=payload, updated_at=payload)
        expected_count = 4
    before = snapshot()
    response = client.get(route)
    assert response.status_code == 200
    assert payload not in response.text
    assert response.text.count(escape(payload)) == expected_count
    parser = PageParser()
    parser.feed(response.text)
    assert "script" not in parser.tags
    assert "img" not in parser.tags
    assert sum(text.count(payload) for text in parser.text) == expected_count
    assert "<table><tr><th>" in response.text
    assert "<span class='badge badge-" in response.text
    assert snapshot() == before


@pytest.mark.parametrize("systems", ["seeded"], indirect=True)
def test_reply_truncation_happens_before_escaping(client, systems):
    body = "A" * 115 + "<script>alert(1)</script>"
    execute(
        "helpdesk",
        "UPDATE ticket_replies SET body = ? WHERE body = 'Viewer public reply'",
        (body,),
    )
    response = client.get("/helpdesk")
    assert response.status_code == 200
    assert f"<td>{escape(body[:120])}</td>" in response.text
    assert "alert(1)" not in response.text
    assert "<script>" not in response.text


def test_runs_ignore_invalid_and_incomplete_artifacts(client):
    write_run(viewer.RUNS_DIR / "2026-10-11-old")
    write_run(viewer.RUNS_DIR / "2026-10-12-new")
    corrupt = viewer.RUNS_DIR / "corrupt"
    corrupt.mkdir()
    (corrupt / "state.json").write_text("{invalid", encoding="utf-8")
    invalid_utf8 = viewer.RUNS_DIR / "invalid-utf8"
    invalid_utf8.mkdir()
    (invalid_utf8 / "state.json").write_bytes(b"\xff")
    (viewer.RUNS_DIR / "incomplete").mkdir()
    (viewer.RUNS_DIR / "not-a-run.txt").write_text("ignored", encoding="utf-8")
    before = snapshot()
    response = client.get("/runs")
    assert response.status_code == 200
    assert response.text.index("2026-10-12-new") < response.text.index("2026-10-11-old")
    assert "corrupt" not in response.text
    assert "invalid-utf8" not in response.text
    assert "incomplete" not in response.text
    assert snapshot() == before


def test_runs_without_directory(client):
    viewer.RUNS_DIR.rmdir()
    response = client.get("/runs")
    assert response.status_code == 200
    assert "No runs executed yet" in response.text
    assert not viewer.RUNS_DIR.exists()
