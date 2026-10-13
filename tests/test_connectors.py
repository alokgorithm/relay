"""Unit and integration tests for Relay MCP connectors."""

import pytest

from connectors.chat import seed as chat_seed
from connectors.chat import server as chat_server
from connectors.helpdesk import seed as helpdesk_seed
from connectors.helpdesk import server as helpdesk_server
from connectors.inventory import seed as inventory_seed
from connectors.inventory import server as inventory_server
from connectors.payments import seed as payments_seed
from connectors.payments import server as payments_server
from connectors.shop import seed as shop_seed
from connectors.shop import server as shop_server


def setup_module():
    """Seed all databases before testing."""
    shop_seed.init_db(True)
    shop_seed.seed_data()
    payments_seed.init_db(True)
    payments_seed.seed_data()
    helpdesk_seed.init_db(True)
    helpdesk_seed.seed_data()
    chat_seed.init_db(True)
    chat_seed.seed_data()
    inventory_seed.init_db(True)
    inventory_seed.seed_data()


def test_shop_connector_tools():
    # Test get_order
    order = shop_server.get_order(1042)
    assert order["found"] is True
    assert order["id"] == 1042
    assert order["total_amount"] == 3200
    assert order["customer_email"] == "ananya@example.com"
    assert len(order["items"]) == 1

    # Test search_orders
    res = shop_server.search_orders("ananya@example.com")
    assert res["count"] >= 2
    assert any(o["id"] == 1042 for o in res["orders"])

    # Test get_customer
    cust = shop_server.get_customer(1)
    assert cust["found"] is True
    assert cust["name"] == "Ananya Sharma"

    # Test get_customer_by_email
    cust_email = shop_server.get_customer_by_email("rohan@example.com")
    assert cust_email["found"] is True
    assert cust_email["id"] == 2

    # Test get_shipment
    shipment = shop_server.get_shipment(1042)
    assert shipment["found"] is True
    assert shipment["carrier"] == "BlueDart"
    assert shipment["status"] == "delivered"
    assert shipment["days_since_delivery"] is not None


def test_payments_connector_and_idempotency():
    # Test get_charge_by_order
    charge = payments_server.get_charge_by_order(1042)
    assert charge["id"] is not None
    assert charge["amount"] == 3200
    assert charge["status"] == "succeeded"

    charge_id = charge["id"]

    # Test create_refund with idempotency key
    key = "test_ticket_1:step_1:create_refund"
    refund1 = payments_server.create_refund(
        charge_id=charge_id,
        amount=3200,
        reason="Damaged item",
        idempotency_key=key,
    )
    assert refund1["status"] == "succeeded"
    assert refund1["amount"] == 3200
    assert refund1["idempotency_key"] == key

    # CRITICAL TEST: Exact same idempotency key must NOT double-refund
    refund2 = payments_server.create_refund(
        charge_id=charge_id,
        amount=3200,
        reason="Damaged item duplicate call",
        idempotency_key=key,
    )
    assert refund2["id"] == refund1["id"]
    assert refund2["idempotency_key"] == key

    # Verify refunds list contains exactly ONE refund
    all_refunds = payments_server.list_refunds(charge_id)
    assert len(all_refunds) == 1


def test_helpdesk_connector():
    # Test list_open_tickets
    tickets = helpdesk_server.list_open_tickets()
    assert len(tickets) >= 10

    # Test get_ticket
    ticket = helpdesk_server.get_ticket(1)
    assert ticket["id"] == 1
    assert "1042" in ticket["subject"]

    # Test add_reply
    reply = helpdesk_server.add_reply(ticket_id=1, body="We are processing your refund", author="agent")
    assert reply["body"] == "We are processing your refund"

    # Test add_internal_note
    note = helpdesk_server.add_internal_note(ticket_id=1, body="Identity verified. Refund issued.")
    assert note["is_internal"] == 1

    # Test set_status and set_tags
    updated = helpdesk_server.set_status(ticket_id=1, status="resolved")
    assert updated["status"] == "resolved"

    tagged = helpdesk_server.set_tags(ticket_id=1, tags="refund-issued,damaged")
    assert "refund-issued" in tagged["tags"]


def test_chat_connector_and_approvals():
    # Test post_message
    msg = chat_server.post_message(channel_name="support-ops", body="Test announcement")
    assert msg["body"] == "Test announcement"

    # Test request_approval
    appr = chat_server.request_approval(
        channel_name="approvals",
        summary="High value refund for order #1043",
        amount=18000,
    )
    appr_id = appr["id"]
    assert appr["status"] == "pending"

    # Test get_approval
    status = chat_server.get_approval(appr_id)
    assert status["status"] == "pending"

    # Test decide_approval
    decided = chat_server.decide_approval(approval_id=appr_id, decision="approved", decided_by="manager_alice")
    assert decided["status"] == "approved"
    assert decided["decided_by"] == "manager_alice"


def test_inventory_connector():
    # Test get_product
    prod = inventory_server.get_product(1)
    assert prod["id"] == 1
    assert prod["stock_quantity"] > 0
    initial_stock = prod["stock_quantity"]

    # Test create_replacement_order decrements stock
    repl = inventory_server.create_replacement_order(original_order_id=1042, product_id=1)
    assert repl["status"] == "pending"
    assert repl["product_id"] == 1

    # Verify stock decremented
    updated_prod = inventory_server.get_product(1)
    assert updated_prod["stock_quantity"] == initial_stock - 1

    # Test out of stock rejection (Bluetooth Speaker SPK-001 has 0 stock)
    out_prod = inventory_server.get_product(7)
    assert out_prod["stock_quantity"] == 0
    with pytest.raises(ValueError, match="out of stock"):
        inventory_server.create_replacement_order(original_order_id=1048, product_id=7)
