# Playbook: Order Status Inquiry ("Where is my order?")

## Overview
This playbook handles customer inquiries inquiring about order status, delivery estimates, tracking links, and shipment progress. It is a strictly read-only flow.

## Connectors Needed
- `shop`: Look up orders, line items, and fulfillment/shipment tracking details.
- `helpdesk`: Retrieve ticket context, reply to the customer, and add internal notes/resolve tickets.

## Applicable Scenarios
- Customer asks "Where is my order?", "Has my package shipped?", "What is my tracking number?", or requests status updates.

## Policy Rules to Enforce
1. **Customer Identity Verification**: Verify the email or phone number in the ticket matches the order record.
2. **Safe Read-Only Operations**: No side-effecting financial transactions or cancellations are performed in this flow.
3. **Accuracy**: Provide up-to-date tracking information, carrier name, tracking URL, and estimated delivery dates.

## Step-by-Step Procedure

### 1. Extract Order & Customer Details
- Extract order number and customer email from the ticket.
- If order ID is not provided in ticket, look up recent orders by customer email using `shop.list_orders(customer_email=...)`.
- If no matching order or multiple active orders are ambiguous, reply to customer requesting order confirmation and set status to `waiting_customer`.

### 2. Retrieve Order & Shipment Information
- Call `shop.get_order(order_id)`.
- Call `shop.get_shipment(order_id)` or examine order fulfillment records.
- Note key facts:
  - Order status (e.g., `processing`, `shipped`, `delivered`)
  - Carrier name
  - Tracking number / tracking URL
  - Estimated delivery date or last transit checkpoint

### 3. Verification
- Verify that tracking and order details belong to the authenticated customer.

### 4. Respond to Customer
- Send a helpful, polite reply via `helpdesk.add_reply(ticket_id, message)` containing:
  - Current fulfillment status
  - Carrier name and tracking number / tracking link
  - Expected delivery date / timeframe
- Add internal note via `helpdesk.add_internal_note(ticket_id, note)` noting order status looked up.
- If delivered or tracking provided, update ticket status to `resolved` or leave open if awaiting delivery.
