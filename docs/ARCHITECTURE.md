# Technical architecture

## Overview

```
 Browser (web app)          Mobile / partner apps
        │ session + CSRF            │ JWT (Bearer)
        ▼                           ▼
 ┌──────────────────────────────────────────────┐
 │ Django                                       │
 │  apps/web  (views + templates)  apps/*/api.py (DRF viewsets)
 │        └──────────────┬──────────────┘       │
 │                 service layer                │  ← all business rules live here
 │   sourcing/services  sourcing/matching       │
 │   orders/services    reviews/services        │
 │   accounts/services  comms/services, notify  │
 │                       │                      │
 │                Django ORM models             │
 └───────────────────────┼──────────────────────┘
                         ▼
        PostgreSQL (+ PostGIS geography index)     private file storage
                                                   (documents, inspection reports, PODs)
        Notification channels: in-app · email (SMTP) · SMS (Africa's Talking / Twilio) · WhatsApp (Twilio)
```

The project is a **modular monolith**: one deployable Django project split into apps along domain lines. The web UI and the REST API are thin layers over a shared **service layer** (`services.py` in each app, plus `sourcing/matching.py`). Every state change goes through a service function, which checks the permission, the current state, the business rules, the audit log entry and the notifications. Both interfaces therefore behave the same way, and the logic can be tested without HTTP.

The boundaries are drawn so that pieces can later be split into separate services without a redesign. Matching (`sourcing/matching.py`), notifications (`comms/notify.py`) and analytics (`core/analytics.py`) have narrow interfaces.

## Modules (spec section 4)

| Spec module | Where |
|---|---|
| A. Authentication & user management | `accounts` (custom `User` with `role`), SimpleJWT, Django sessions |
| B/C. Producer & buyer registration and verification | `accounts.models.*Profile`, `accounts.services.set_verification`, `VerificationDocument` |
| D. Product catalogue | `catalog.Category / Product / ProductVariety` |
| E. Supplier inventory | `catalog.InventoryItem` (freshness via `last_confirmed_at`) |
| F. Sourcing requests | `sourcing.SourcingRequest`, `sourcing.services.create_sourcing_request` |
| G. Intelligent matching | `sourcing/matching.py`, `core.MatchingConfig` |
| H. Quotation & negotiation | `sourcing.Quotation` (counter / revise / accept, with history) |
| I. Buyer–supplier communication | `comms.Conversation / Message` |
| J. Order & procurement | `orders.Order` (one per procurement) and `OrderLine` (one per supplier) |
| K. Quality assurance | `orders.Inspection` (pre-dispatch and on-receipt) |
| L. Logistics | `orders.Delivery` (assignment, tracking log, proof of delivery) |
| M. Notifications | `comms.Notification`, `comms/notify.py` |
| N. Reviews & performance | `reviews.Review`, `reviews.services.recompute_supplier_metrics` |
| O. Admin dashboard | `/app/admin/*` (web), `/api/v1/admin/*` (API), Django admin for raw data |
| P. Reporting & analytics | `core/analytics.py` → `/app/admin/` and `/api/v1/admin/analytics/` |

## Roles and permissions

`User.role` is one of `farmer` (**supplier**), `processor` (**buyer**), `logistics` or `admin`. Superusers are treated as admins. Each non-admin user has one role profile (`SupplierProfile`, `BuyerProfile` or `LogisticsProfile`), and each profile carries a verification status: `pending`, `approved`, `rejected` or `suspended`. By default new profiles are **approved at registration** (`REQUIRE_ACCOUNT_VERIFICATION=0`); admins can still reject or suspend them, and setting it to `1` restores the admin approval step.

| Capability | Supplier | Buyer | Logistics | Admin |
|---|:-:|:-:|:-:|:-:|
| Register (self-service; admins can't self-register) | ✓ | ✓ | ✓ | — |
| Manage own inventory | ✓ | | | view all |
| Create sourcing requests | | ✓ (verified) | | view all |
| See a request | only if matched or quoted | own | | all |
| Respond to matches, submit or revise quotations | ✓ (approved) | | | |
| Counter, accept or reject quotations; place orders | | own requests | | |
| Reconfirm supply on an order line | own lines | | | |
| Record inspections | | own orders | | ✓ (QA) |
| Assign logistics | | own orders | | ✓ |
| Update delivery status | if supplier delivers | if buyer collects | assigned jobs | ✓ |
| Confirm completion | | ✓ | | ✓ |
| Review a counterparty | after completion | after completion | | |
| Verify accounts, resolve disputes, configure matching, view the audit log | | | | ✓ |

Where these checks happen:
- **API permission classes** in `core/permissions.py` (`IsBuyer`, `IsSupplier`, `IsPlatformAdmin` and others) handle coarse role checks.
- **Scoped querysets** keep data isolated: every viewset filters its queryset to rows the caller may see, so a request for another tenant's object returns **404**, not 403.
- **Service functions** handle object-level and state-level checks. A violation raises `WorkflowError`, returned as **409**.

## Procurement workflow

```
Registration → Verification → Inventory listing → Buyer request → Supplier matching
  → Supplier notification → Negotiation → Supply confirmation → Quality inspection
  → Delivery → Completion → Feedback
```

**Payments.** Once every supplier has reconfirmed, the buyer pays the order total by M-Pesa (STK Push, `apps/payments`). The money is held by Origin 254. When the buyer confirms receipt of a supplier's lot, a payout for that line (its total minus `PLATFORM_FEE_PERCENT`) is released: sent by M-Pesa B2C, or paid by an admin and recorded with its transaction code. An open dispute puts the payout on hold until an admin releases it. Lots that are cancelled or fail inspection after payment show as a refund due. Callbacks are idempotent, amount-checked and authenticated by a secret URL token.

When the buyer confirms receipt of a line (Completion), a `SaleRecord` is saved with a copy of the product, variety, grade, delivered quantity, price and parties. Buyers see it under **Purchase history**, suppliers under **Sales history** (`/app/history/`, `GET /api/v1/history/`). Because the details are copied, history stays correct after the listing is edited, sold out or removed.

### State machines

**SourcingRequest:** `open → matched → negotiating → supply_confirmed → in_fulfilment → completed`. A request can be `cancelled` at any point before it has orders.

**Match** (the three levels the spec distinguishes):
`potential` (found by the engine) → `contacted` (supplier notified) → **`availability_confirmed`** (supplier confirmed stock) → **`committed`** (stock reserved on an order line). The other states are `declined`, and `expired` (no longer qualifies on re-match).

**Quotation:** `submitted ⇄ countered` (buyer proposes; supplier revises or accepts the counter, which bumps `revision`) `→ accepted | rejected | withdrawn | expired`. Every step is appended to `history`.

**OrderLine** (one per supplier):
`awaiting_reconfirmation → confirmed → inspection_passed → in_transit → delivered → completed`. A line can instead end in `cancelled` (the supplier declined, or the order was cancelled) or `inspection_failed`.

**Order** status is *derived* from its lines by `orders.services.refresh_order_status`:
- `disputed` if any dispute is open
- `cancelled` if no line is active
- `pending_confirmation` while any line awaits reconfirmation
- `completed` once all active lines are completed
- `delivered` when all are delivered or completed
- `in_progress` once any line passes inspection
- `confirmed` otherwise

The parent request's status follows the order's.

**Delivery:** `pending → scheduled → picked_up → in_transit → delivered`. `failed` is allowed from any active state, and a failed delivery can be rescheduled.

### Business rules (spec section 11) and where they are enforced

| Rule | Enforcement |
|---|---|
| Only approved suppliers appear in recommendations | `matching.find_candidates` filters `verification_status=approved`; `submit_quotation` requires approval |
| Inventory must be regularly updated | Listings not confirmed within `INVENTORY_STALE_DAYS` (14) are excluded from matching. Any edit, availability response or "still accurate" click refreshes `last_confirmed_at` |
| Availability reconfirmed before order confirmation | Order lines start `awaiting_reconfirmation`. `reconfirm_line` re-checks stock under `SELECT … FOR UPDATE` and decrements it |
| Products are accompanied by at least two pictures | `MIN_PHOTOS_PER_LISTING` (2). The stock form refuses to save with fewer; photos can't be deleted below the minimum; `InventoryItem.objects.with_required_photos()` hides incomplete listings from matching, nearest-supplier search and quoting |
| Buyers can compare offers | `sourcing.services.compare_offers`: landed cost per unit and the cheapest combination covering the quantity |
| Multiple suppliers per request | The matching allocation plan; one `Order` holds many `OrderLine`s; accepted quantity is capped at the requested quantity |
| Quality requirements recorded and traceable | Request specs are snapshotted onto each `OrderLine.quality_specifications`; `Inspection` keeps measurements, results and reports |
| Delivery and completion documented | `Delivery.tracking_log`, received quantity and recipient, proof-of-delivery file, and an `OrderEvent` timeline |
| Ratings only on completed transactions | `reviews.services.create_review` requires a `completed` line; one review per party per line |
| Sensitive admin actions logged | `core.audit.log_action` records verification decisions, config changes, document access, orders, inspections, disputes and backups. `AuditLog` rows cannot be edited or deleted |

## Matching algorithm (`apps/sourcing/matching.py`)

**1. Hard filters.** These run in SQL and decide which listings are eligible at all:
- same product
- supplier `approved` and active
- listing active, quantity > 0, not expired, fresh (≤ 14 days since confirmation)
- `available_from ≤ deadline`
- within `max_distance_km` of the delivery point (PostGIS `ST_DWithin`, or a bounding box plus haversine)
- not the buyer's own stock

**2. Per-candidate checks.** Exclude any listing whose quality grade is below the requirement (or one grade below, if `allow_one_grade_below` is on), or whose estimated arrival misses the deadline. Arrival is `max(today, available_from) + ceil(distance / transport_km_per_day)`.

**3. Scoring.** Each criterion is scored from 0 to 1:

| Criterion | Score |
|---|---|
| product | exact or unspecified variety = 1.0 · listing has no variety = 0.8 · different variety = 0.6 |
| quantity | `min(1, available / requested)` (units converted to kg) |
| quality | 1.0 if the grade meets the requirement (0.4 if one grade below). Then −0.25 per numeric spec missed (e.g. `min_size_mm`, `max_moisture_pct`; API only) and −0.1 per spec the supplier didn't declare |
| proximity | `1 − distance / max_distance_km` |
| delivery | 1.0 if the delivery arrangement can be met, else 0.3 (e.g. "supplier delivers" but the supplier's radius is too small) |
| reliability | the supplier's `reliability_score`, or `new_supplier_reliability` (0.6) when there is no history |

`score = Σ weightᵢ × criterionᵢ`. The weights are stored in `MatchingConfig` and normalised to sum to 1; the defaults are 0.15 / 0.20 / 0.20 / 0.20 / 0.10 / 0.15. Candidates below `min_score` (0.35) are dropped, and the rest are sorted by score, then distance, and capped at `max_matches`.

**4. Multi-supplier allocation.** The planner works greedily in rank order, taking `min(remaining, available)` from each candidate until the quantity is covered. Each match stores its `suggested_quantity`, and the result reports `coverage`, `fully_covered` and `suppliers_needed`.

**5. Persistence and re-matching.** Re-running matching re-scores everything:
- New matches are added.
- Untouched `potential` matches that no longer qualify are deleted.
- Engaged matches (`contacted` or `availability_confirmed`) that no longer qualify become `expired`.
- Committed and declined matches are kept.

**Reliability score.** Recomputed after each completion or review:

```
raw = 0.4 × (avg rating / 5) + 0.3 × on-time rate + 0.3 × inspection pass rate
reliability = (n × raw + 3 × default) / (n + 3)
```

`n` is the number of completed transactions. Missing components fall back to the default. The second line is Bayesian shrinkage, so one good or bad order doesn't swing a supplier's ranking.

Admins tune all parameters at `/app/admin/config/` or `PATCH /api/v1/admin/matching-config/`, and each change is audit-logged. The score breakdown is stored on every match and shown to buyers ("Why this match") and admins, so rankings are explainable.

## Geography

`core.Location` stores WGS84 latitude and longitude. Migration `core/0002_postgis` runs `CREATE EXTENSION postgis` and builds a GiST index on the derived geography **only if PostGIS is installed on the server**. `core/geo.py` then uses `ST_DWithin` for radius searches. On plain PostgreSQL (as on the development Mac) it falls back to a bounding-box prefilter plus haversine distance, which gives identical results. The Docker setup uses the `postgis/postgis` image.

## Notifications

`comms.notify.notify(user, title, body, kind, link)` always stores an in-app `Notification`. After the database transaction commits, it also sends over each channel enabled in `NOTIFICATION_CHANNELS` that the user can receive:

| Channel | Condition | Provider |
|---|---|---|
| email | user has an email address | Django `EMAIL_BACKEND` |
| sms | phone number and `sms_opt_in` | `SMS_PROVIDER`: `console`, `africastalking`, `twilio` |
| whatsapp | phone number and `whatsapp_opt_in` | `WHATSAPP_PROVIDER`: `console`, `twilio` |

A provider failure is logged and never rolls back the business transaction. The channels actually used are recorded in `channels_sent`. For high volume, move `_deliver` onto a task queue (Celery or RQ); the function boundary is already in place.

## Security

- Passwords use Django's PBKDF2 hashing and validators (minimum length, common and numeric checks). JWT access tokens last 30 minutes, refresh tokens 7 days with rotation.
- Role-based permissions, plus per-object scoping and state checks in services (see above).
- Input validation happens in DRF serializers and model `clean()`, with business-rule validation in services. Errors come back in one consistent envelope: `{"error": {"code", "message", "details"}}`.
- **Secure document storage:** uploads go to `PRIVATE_MEDIA_ROOT`, outside any served directory, under random UUID filenames, with a whitelist of extensions and sizes. They are served only through permission-checked views, and admin access to another user's document is audit-logged.
- With `DEBUG=0`: secure cookies, HSTS, SSL redirect, proxy SSL header, `nosniff`. Throttling: 60/min anonymous, 600/min authenticated.
- The audit log is append-only at model level.
- Concurrency: stock reservation uses row locks; references derive from primary keys (`SR-2026-000042`), so they are unique without locking.

## Extensibility

- **New products, regions, units:** these are data (catalogue and locations), not code. Unit conversion lives in `catalog.UNIT_TO_KG`.
- **New matching criteria:** add a scorer function and a weight field. The breakdown, UI and analytics pick it up automatically.
- **New channels or providers:** add a sender in `comms/notify.py`.
- **Payments, escrow, credit scoring, IoT cold-chain:** these can hang off `Order` and `OrderLine` and the event timeline without schema rewrites.
- **Separate frontends** (a mobile app, a React SPA) can use the documented REST API and JWT today. CORS is configurable.
