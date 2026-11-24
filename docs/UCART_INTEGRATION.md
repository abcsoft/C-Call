# Ucart Ecommerce Integration Contract (C-Call V2)

## 1. Authentication
- Header: `X-API-Key: cc_live_<token>`
- Required Scope: `orders:write` for order intake/recall, `orders:read` for status queries.

## 2. Create Order Confirmation Call
`POST /api/v1/orders` (alias `/api/v1/order-call`)

```json
{
  "order_id": "UCART-10042",
  "phone": "+8801711223344",
  "customer_name": "Rafiqul Islam",
  "amount": 1450.00,
  "product_name": "Cotton Panjabi",
  "delivery_charge": 60,
  "grand_total": 1510,
  "site_name": "Ucart Store BD",
  "payment_method": "COD",
  "consent": true,
  "consent_source": "ucart_checkout",
  "webhook_url": "https://merchant-ucart.example.com/api/ccall/webhook"
}
```
- **Idempotency**: Duplicate `order_id` for the same tenant returns the existing order without queuing a duplicate call.

## 3. COD IVR Actions & Status Mapping
- **Press `1`**: `CONFIRMED` -> webhook `event: "order.confirmed"`
- **Press `2`**: `CANCELLED` -> webhook `event: "order.cancelled"`
- **Press `3`** (or `0`): Human assistance requested (`metadata.human_agent_requested = true`) -> webhook `event: "order.agent_requested"`
- **No Answer / Busy**: `NO_ANSWER` (eligible for bounded retry) -> `order.no_answer`

## 4. Webhook Signature Verification
Verify `X-C-Call-Timestamp` and `X-C-Call-Signature: sha256=<hex>` using HMAC-SHA256 over `f"{timestamp}.{canonical_json}"` with the merchant's `whsec_...` secret.
