# REST API

- Base URL: `/api/v1/`
- Interactive docs: `/api/docs/` (Swagger UI) and `/api/redoc/`
- Machine-readable spec: [`openapi.yaml`](openapi.yaml). Regenerate it with `python manage.py spectacular --file docs/openapi.yaml`

## Authentication

```bash
# Obtain a token pair
curl -s -X POST localhost:8000/api/v1/auth/token/ -H 'Content-Type: application/json' \
     -d '{"username":"mama_jam","password":"Origin254!demo"}'
# → {"access": "...", "refresh": "..."}

# Use it
curl -s localhost:8000/api/v1/auth/me/ -H "Authorization: Bearer $ACCESS"

# Refresh (refresh tokens rotate)
curl -s -X POST localhost:8000/api/v1/auth/token/refresh/ -d '{"refresh":"..."}' -H 'Content-Type: application/json'
```

Browser sessions (the web app) also work against the API, with CSRF protection.

## Errors

Every error uses the same envelope:

```json
{"error": {"code": "validation_error", "message": "Invalid input.", "details": {"quantity": ["Ensure this value is greater than or equal to 0.01."]}}}
```

| Status | Meaning |
|---|---|
| 400 | Validation failed (`details` holds field errors) |
| 401 | Not authenticated |
| 403 | Your role can't use this endpoint |
| 404 | Not found, **or not visible to you** (data is scoped per user) |
| 409 | Business rule or state conflict. `code` is e.g. `not_verified`, `insufficient_stock`, `quantity_exceeded`, `inspection_required`, `not_matched`, `permission_denied` |
| 429 | Rate limited |

List endpoints are paginated (`?page=`, 25 per page) and support `?search=`, `?ordering=` and field filters, as listed in the docs.

## Endpoint map

| Area | Endpoints |
|---|---|
| Auth | `POST auth/register/` · `POST auth/token/` · `POST auth/token/refresh/` · `GET/PATCH auth/me/` · `GET/PATCH auth/me/profile/` |
| Verification documents | `GET/POST documents/` (multipart) · `GET documents/{id}/download/` |
| Catalogue | `categories/` · `products/` · `varieties/` (read for everyone, write for admins) · `locations/` |
| Inventory (suppliers) | `GET/POST inventory/` · `PATCH/DELETE inventory/{id}/` · `POST inventory/{id}/confirm/` |
| Supplier directory | `GET suppliers/` (approved only, unless admin) |
| Sourcing requests | `GET/POST sourcing-requests/` · `GET …/{id}/matches/` · `POST …/{id}/rematch/` · `POST …/{id}/notify-suppliers/` · `GET …/{id}/offers/` · `POST …/{id}/contact/` · `POST …/{id}/cancel/` |
| Matches (suppliers) | `GET matches/` · `POST matches/{id}/respond/` |
| Quotations | `GET/POST quotations/` · `POST quotations/{id}/counter/` · `accept/` · `reject/` (buyer) · `revise/` · `withdraw/` (supplier) |
| Orders | `GET/POST orders/` · `POST orders/{id}/cancel/` · `POST orders/{id}/dispute/` |
| Order lines | `GET order-lines/` · `POST order-lines/{id}/reconfirm/` · `inspect/` · `complete/` |
| Deliveries | `GET deliveries/` · `POST deliveries/{id}/assign/` · `POST deliveries/{id}/update_status/` · `GET deliveries/{id}/proof/` |
| Inspections / disputes | `GET inspections/` · `GET inspections/{id}/report/` · `GET disputes/` · `POST disputes/{id}/resolve/` (admin) |
| History | `GET history/` (buyers: purchases; suppliers: sales; admins: all; `?search=`, `?ordering=-completed_at`) |
| Payments | `POST payments/` `{order, phone}` sends an M-Pesa prompt · `POST payments/{id}/refresh/` · `GET payments/` (buyer: own; admin: all) |
| Payouts | `GET payouts/` (supplier: own; admin: all) · admin: `POST payouts/{id}/send/` (M-Pesa B2C) · `POST payouts/{id}/record/` `{receipt_number}` · `POST payouts/{id}/release/` |
| Messaging | `GET conversations/` · `GET conversations/{id}/messages/` · `POST conversations/{id}/send/` |
| Notifications | `GET notifications/?unread=1` · `POST notifications/{id}/read/` · `POST notifications/read_all/` |
| Reviews | `GET/POST reviews/` (completed transactions only) |
| Admin | `GET admin/analytics/?days=30` · `GET/PATCH admin/matching-config/` · `GET admin/audit-logs/` · `admin/users/` (`activate`, `deactivate`) · `admin/suppliers/`, `admin/buyers/`, `admin/logistics/` (`POST …/{id}/verify/`) |
| Ops | `GET /health/` |

## Worked example: the mango request

```bash
B="Authorization: Bearer $BUYER_TOKEN"; S="Authorization: Bearer $SUPPLIER_TOKEN"; J='Content-Type: application/json'

# 1. Buyer creates a request (type any product in "product_name", or send a "product" id). Matching runs immediately.
curl -s -X POST localhost:8000/api/v1/sourcing-requests/ -H "$B" -H "$J" -d '{
  "product_name": "Mango", "quantity": "1500", "unit": "kg", "delivery_location_id": 1,
  "delivery_deadline": "2026-11-24", "min_quality_grade": "grade_1",
  "quality_specifications": {},
  "intended_application": "Mango jam production", "packaging_requirements": "20 kg crates",
  "delivery_arrangement": "supplier_delivers", "target_budget": "80000"}'
# → {"id": 1, "reference": "SR-2026-000001", ..., "matching": {"coverage": 1.0, "suppliers_needed": 3, ...}}

# 2. Ranked matches with score breakdown and suggested split
curl -s localhost:8000/api/v1/sourcing-requests/1/matches/ -H "$B"

# 3. Notify them
curl -s -X POST localhost:8000/api/v1/sourcing-requests/1/notify-suppliers/ -H "$B" -H "$J" -d '{}'

# 4. Supplier confirms availability and quotes
curl -s -X POST localhost:8000/api/v1/matches/1/respond/ -H "$S" -H "$J" -d '{"available": true, "quantity": "800"}'
curl -s -X POST localhost:8000/api/v1/quotations/ -H "$S" -H "$J" -d '{
  "sourcing_request": 1, "inventory_item": 1, "quantity": "800", "price_per_unit": "45",
  "delivery_cost": "4000", "includes_delivery": true,
  "earliest_delivery_date": "2026-11-20", "valid_until": "2026-11-22"}'

# 5. Buyer compares offers, negotiates, accepts, orders
curl -s localhost:8000/api/v1/sourcing-requests/1/offers/ -H "$B"
curl -s -X POST localhost:8000/api/v1/quotations/2/counter/ -H "$B" -H "$J" -d '{"price_per_unit": "47"}'
curl -s -X POST localhost:8000/api/v1/quotations/1/accept/ -H "$B"
curl -s -X POST localhost:8000/api/v1/orders/ -H "$B" -H "$J" -d '{"sourcing_request": 1, "quotations": [1, 2, 3]}'

# 6. Each supplier reconfirms (stock is reserved)
curl -s -X POST localhost:8000/api/v1/order-lines/1/reconfirm/ -H "$S" -H "$J" -d '{"available": true}'

# 7. QA inspection, then delivery updates, then completion and review
curl -s -X POST localhost:8000/api/v1/order-lines/1/inspect/ -H "$QA" -H "$J" -d '{"stage":"pre_dispatch","result":"passed","notes":"Ripe, firm fruit"}'
curl -s -X POST localhost:8000/api/v1/deliveries/1/update_status/ -H "$S" -H "$J" -d '{"status":"scheduled"}'
curl -s -X POST localhost:8000/api/v1/deliveries/1/update_status/ -H "$S" -H "$J" -d '{"status":"picked_up"}'
curl -s -X POST localhost:8000/api/v1/deliveries/1/update_status/ -H "$S" -H "$J" -d '{"status":"delivered","received_quantity":"800","received_by":"Stores"}'
curl -s -X POST localhost:8000/api/v1/order-lines/1/complete/ -H "$B"
curl -s -X POST localhost:8000/api/v1/reviews/ -H "$B" -H "$J" -d '{"order_line": 1, "rating": 5, "comment": "Great fruit"}'
```

`tests/test_api_workflow.py::MangoScenarioAPITest` runs exactly this flow, including the negative cases. It is the best executable reference for the API.
