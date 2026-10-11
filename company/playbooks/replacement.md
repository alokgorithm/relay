# Playbook: Item Replacement

## Overview
This playbook handles requests where a customer needs a replacement item (e.g., defective unit, incorrect item received, or damaged goods where the customer prefers a replacement over a refund). This playbook leverages the dynamically plugged `inventory` connector.

## Connectors Needed
- `shop`: Look up order details, line item SKUs, and customer shipping address.
- `inventory`: Check stock availability for SKUs, reserve stock, and check replenishment dates.
- `helpdesk`: Reply to customer, record internal actions, update ticket status.
- `chat`: Notify support operations channel or request supervisor approval for out-of-policy exceptions.

## Applicable Scenarios
- Customer receives wrong product or damaged item and desires a replacement instead of a refund.
- Item replacement authorized under warranty or return policy.

## Policy Rules to Enforce
1. **Eligibility Window**: Replacement request must be submitted within 30 days of delivery.
2. **Identity Verification**: Ticket email must match customer record on the original order.
3. **Inventory Availability**: A replacement order can only be created if replacement stock is verified available (`stock_quantity > 0`).
4. **Order Limit**: Only 1 replacement or refund permitted per order line item.

## Step-by-Step Procedure

### 1. Extract Details & Verify
- Look up original order via `shop.get_order(order_id)` and customer via `shop.get_customer(customer_id)`.
- Confirm email matches between ticket and order.
- Identify the specific item SKU that requires replacement.

### 2. Check Stock Availability
- Call `inventory.check_stock(sku=item_sku)`.
- Check if `available_quantity >= requested_quantity`.
- If out of stock:
  - Check `inventory.get_restock_date(sku=item_sku)`.
  - Offer customer the option to wait for restock or switch to the `damaged_item_refund` playbook.
  - Inform customer and pause for response (`waiting_customer`).

### 3. Create Replacement Order
- If in stock, create replacement shipment/order record using `shop.create_order` or `inventory.reserve_stock(sku=item_sku, quantity=...)`.
- Record action in action log with idempotency key (`{ticket_id}:{step_id}:create_replacement`).
- Use the verified shipping address from the customer profile or original order.

### 4. Verification
- Verify that replacement order is created in `shop` and inventory was properly reserved/deducted.

### 5. Respond and Update Helpdesk
- Reply to customer via `helpdesk.add_reply(ticket_id, message)` with new replacement order number and expected dispatch timeline.
- Add internal note via `helpdesk.add_internal_note(ticket_id, note)` with details of SKU replaced and inventory reservation ID.
- Post summary to `#support-ops` channel via `chat.post_message`.
- Update ticket status to `resolved` or `pending` shipment.
