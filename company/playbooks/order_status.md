# Playbook: Order Status Inquiry ("Where is my order?")

## Overview
This playbook handles customer inquiries about order status, delivery estimates, tracking links, and shipment progress. Shop lookups are read-only; helpdesk replies and ticket updates are permitted, but financial transactions are not.

## Connectors Needed
- `shop`: Look up orders, line items, and fulfillment/shipment tracking details.
- `helpdesk`: Retrieve ticket context, reply to the customer, and add internal notes/resolve tickets.

## Applicable Scenarios
- Customer asks "Where is my order?", "Has my package shipped?", "What is my tracking number?", or requests status updates.

## Policy Rules to Enforce
1. **Customer Identity Verification**: Verify the email on the ticket matches the order record.
2. **Safe Read-Only Operations**: No side-effecting financial transactions or cancellations are performed in this flow.
3. **Accuracy**: Provide up-to-date tracking information, carrier name, tracking URL, and estimated delivery dates.

## Step-by-Step Procedure

### 1. Extract Order & Customer Details
- Extract order number and customer email from the ticket.
- If order ID is not provided in ticket, look up recent orders by customer email using `shop.search_orders(customer_email=...)`.
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
- Send a helpful, polite reply via `helpdesk.add_reply(ticket_id, body)` containing:
  - Current fulfillment status
  - Carrier name and tracking number / tracking link
  - Expected delivery date / timeframe
- Add internal note via `helpdesk.add_internal_note(ticket_id, body)` noting order status looked up.
- When verified tracking information answers the inquiry, call `helpdesk.set_status(ticket_id, status="resolved")`, then `finish`. If clarification is needed, ask a question and set `waiting_customer`; do not claim completion while the ticket is still open.
- Batch independent order/shipment reads for a known order ID. Do not invent delivery estimates that are absent from the tool results.
