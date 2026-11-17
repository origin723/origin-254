# Origin 254

> **Purpose of this file.** A precise, self-contained description of the Origin 254 codebase: what it does, how it is built, every rule it enforces and where each piece lives. It is written so that a developer **or an AI coding agent** can understand and safely change the project without reading every file first. Facts here were taken from the code; if code and README ever disagree, the code wins: update this file.

---

## 1. What Origin 254 is

Origin 254 is a **multi-sided agricultural sourcing and value-addition marketplace for Kenya** ("254" is Kenya's dialling code). It connects:

| Side | Role value | Who they are | What they do on the platform |
|---|---|---|---|
| Supply | `farmer` | Farmers / producers | List stock (with photos), check market demand, answer buyer requests, quote, reconfirm, deliver, get paid |
| Demand | `processor` | Food entrepreneurs / processors (the "buyer" or "manufacturer") | Post sourcing requests, compare matched suppliers, negotiate, place orders, pay by M-Pesa, confirm receipt, rate |
| Logistics | `logistics` | Delivery partners | Receive delivery jobs, update pickup / transit / delivered status |
| Platform | `admin` (or Django superuser) | Origin 254 staff | Verify accounts, resolve disputes, tune matching, manage payouts, see analytics and the audit log |

**Core loop:** a buyer posts a sourcing request → the matching engine ranks verified farmers with current stock (splitting big orders across several farmers) → suppliers quote → the buyer negotiates and places one order → farmers reconfirm (stock is reserved) → buyer pays by M-Pesa (held by Origin 254) → quality inspection → delivery → buyer confirms receipt → the farmer is paid (minus a **10% platform commission**) → both sides rate each other → the sale is saved in history.

**MVP demonstration scenario (built in):** a Nairobi jam maker (`mama_jam`, "Mama Jam Foods Ltd") needs **1,500 kg of grade-1 mangoes** within 7 days on a **KES 80,000** budget. The system fills it from three farmers: 800 kg (Makueni) + 600 kg (Machakos) + 100 kg (Kandara, Murang'a) for **KES 78,200**, of which KES 7,820 is commission.

**Removed / not present (do not re-add without being asked):** "Agricultural cooperative" and "Produce aggregator" roles (merged into `farmer`); sugar (°Brix), defects (%) and ripeness fields (replaced by free-text "Any other details / requirements / observations").

---

## 2. Technology stack

| Layer | Choice |
|---|---|
| Language / runtime | Python 3.13 |
| Web framework | Django 5.2 (server-rendered templates for the web app) |
| API | Django REST Framework 3.18, drf-spectacular (OpenAPI 3), django-filter, django-cors-headers |
| Auth | Django sessions (web) + SimpleJWT (API): access token 30 min, refresh 7 days, rotating |
| Database | PostgreSQL 16 (database name `origin254`). PostGIS is **optional**: migration `core/0002_postgis` enables it if available; otherwise distances use a haversine fallback (`apps/core/geo.py`) |
| Static files | WhiteNoise; `collectstatic` → `staticfiles/` |
| Server | `runserver` for development; gunicorn in Docker / production |
| Payments | Safaricom M-Pesa Daraja API (STK Push for buyers, B2C for payouts) with a `simulated` test mode |
| SMS / WhatsApp | Pluggable providers: `console` (log only), Africa's Talking, Twilio |
| Front end | Plain HTML/CSS (`static/css/app.css`), no JS framework. Inter font. Bright "leaf green + mango orange" theme, light mode only |

Dependencies are pinned in `requirements.txt`. The virtual environment is `.venv/` (an older `env/` virtualenv may exist and must **not** be used).

---

## 3. Running it locally (macOS)

```bash
cd "Origin 254"
brew services start postgresql@16          # PostgreSQL must be running
createdb origin254                          # first time only
python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env                        # then edit values (see section 13)
.venv/bin/python manage.py migrate
.venv/bin/python manage.py load_kenya_locations   # 47 counties + 290 constituencies
.venv/bin/python manage.py createsuperuser        # optional: your own admin
.venv/bin/python manage.py runserver              # http://127.0.0.1:8000
```

Activate with `source .venv/bin/activate` (run `deactivate` first if another env such as `(env)` is active, otherwise `ModuleNotFoundError: No module named 'django'`).

| URL | What |
|---|---|
| `http://127.0.0.1:8000/` | Public landing page (`templates/landing.html`, illustrated hero `static/img/hero-agritech.svg`) |
| `/login/`, `/register/` | Sign in / create account (no verification step by default) |
| `/app/` | Web app; the dashboard adapts to the user's role |
| `/api/v1/` | REST API |
| `/api/docs/` · `/api/redoc/` · `/api/schema/` | Swagger UI · ReDoc · raw OpenAPI |
| `/django-admin/` | Django back office (catalogue, raw data) |
| `/health/` | Returns `{"status": "ok"}` |
| `/payments/mpesa/...` | Safaricom callbacks (see section 9) |

`index.html` in the project root is a **standalone placeholder page**, not served by Django. Its Log in / Register buttons always link to the app at the URL in `<meta name="origin254-app-url" content="http://127.0.0.1:8000">` (change it when deployed). Opening it with VS Code Live Server is fine.

### Demo data

```bash
.venv/bin/python manage.py seed_demo            # idempotent demo users, catalogue, stock with 2 placeholder photos each
.venv/bin/python manage.py mango_demo           # runs the full 1,500 kg mango scenario end to end
.venv/bin/python manage.py mango_demo --stop-after quotes   # stages: request, matching, contact, quotes, agreement, inspection, delivery, complete
```

> **Warning:** `mango_demo` and `seed_demo --reset` **delete all transactional data** (sourcing requests, matches, quotations, orders, payments, payouts, sale history, conversations, notifications, reviews), including data created by real users. Never run them on a database with real activity.

All demo accounts use the password **`Origin254!demo`**:

| Username | Role | Business / notes |
|---|---|---|
| `mama_jam` | processor | Mama Jam Foods Ltd, Nairobi Industrial Area (the demo buyer) |
| `new_juice_co` | processor | Second buyer |
| `makueni_coop` | farmer | Makueni Mango Growers: 800 kg Apple mango, grade 1, KES 45/kg |
| `machakos_agg` | farmer | Machakos Fresh Produce Farm: 600 kg Ngowe, grade 1, KES 50/kg |
| `muranga_farm` | farmer | Kandara Orchards (J. Wanjiru): 300 kg Apple, premium, KES 60/kg |
| `kilifi_coop` | farmer | Malindi Coast Mango Farm: far away, does not deliver |
| `kisumu_agg` | farmer | Lake Region Fruit Farm: far away |
| `embu_farm` | farmer | Embu Highland Farm: grade 2 only (excluded from grade-1 requests) |
| `kitui_farm` | farmer | Kitui Dryland Fruits: seeded as *pending verification* |
| `meru_farm` | farmer | Meru Sunrise Farm: stock unconfirmed for 30 days (stale, excluded) |
| `swift_logistics` | logistics | Swift Agri Logistics |
| `admin`, `qa_officer` | admin | `admin` is also a Django superuser |

Usernames ending `_coop` / `_agg` are historical; those accounts are ordinary farmers.

---

## 4. Code layout

```
origin254/            settings.py (env-driven), urls.py (router + routes), wsgi.py
apps/core/            Location, AuditLog, MatchingConfig; geo (PostGIS / haversine), analytics, error envelope,
                      permissions, references, demo images; management commands
apps/accounts/        User + Role, Supplier/Buyer/Logistics profiles, verification, private documents
apps/catalog/         Category, Product, ProductVariety, InventoryItem, InventoryPhoto; typed-name resolution
apps/sourcing/        SourcingRequest, Match, Quotation; matching engine, nearest suppliers, market report
apps/orders/          Order, OrderLine, Inspection, Delivery, OrderEvent, Dispute, SaleRecord
apps/payments/        Payment, Payout; M-Pesa / simulated providers; escrow + commission logic; callbacks
apps/comms/           Conversation, Message, Notification; multi-channel notify()
apps/reviews/         Review; supplier performance metrics (reliability score)
apps/web/             server-rendered views, forms, template tags (badge, kes, qty, pct, label …)
templates/            landing.html, web/*.html (one per page), partials _field, _form, _thumbs, _photo_upload
static/               css/app.css, img/hero-agritech.svg
tests/                automated tests (see section 15)
docs/                 ARCHITECTURE.md, DATABASE.md, API.md, DEPLOYMENT.md, openapi.yaml
Dockerfile, docker-compose.yml   production image (gunicorn) and Compose with postgis/postgis:16-3.4
```

### Architectural conventions (follow these when changing code)

1. **Service layer.** All state changes go through functions in `apps/*/services.py` (and `apps/sourcing/matching.py`). Both the REST API (`api.py`) and the web views (`apps/web/views.py`) call the *same* service functions. Never put business rules only in a view or serializer.
2. **Errors.** Business-rule violations raise `apps.core.exceptions.WorkflowError(message, code)`. The API turns it into **HTTP 409** with the envelope `{"error": {"code", "message", "details"}}`; validation errors → 400 `validation_error`; permissions → 403. Web views catch it through `run_action()` and show it as a flash message.
3. **Audit.** Sensitive actions call `apps.core.audit.log_action(actor, action, target, **details)`. `AuditLog` is append-only.
4. **Notifications.** Use `apps.comms.notify.notify(user, title, body, kind, link)` / `notify_admins(...)`. External sends happen on transaction commit and never break the business transaction.
5. **References.** Human-readable ids are derived from the primary key after save: `SR-YYYY-NNNNNN` (sourcing request), `QT-` (quotation), `PO-` (order), `PAY-` (payment), `PAYOUT-` (payout), via `apps.core.refs.assign_reference`.
6. **Money** is `Decimal`, rounded to 2 dp (`quantize(Decimal("0.01"))`). M-Pesa amounts are whole shillings (rounded up).
7. **Private files** (verification documents, inspection reports, proof of delivery, product photos) live in `PRIVATE_MEDIA_ROOT` (`private_media/`), never under a public URL; they are streamed by permission-checked views. Uploads are checked by extension **and** magic bytes.
8. **Migrations:** every model change needs a migration (`makemigrations`), including help-text / choices changes.

---

## 5. Domain model (main tables)

Enums are shown as `value` (label).

### Accounts (`apps/accounts`)
- **User** (custom `AbstractUser`): `role` ∈ `farmer` (Farmer / producer), `processor` (Food entrepreneur / processor), `logistics` (Logistics & delivery partner), `admin` (Platform administrator); unique `email`; `phone` (validated `+?[0-9]{9,15}`); `sms_opt_in`, `whatsapp_opt_in`. Helpers: `is_supplier` (role farmer), `is_buyer` (processor), `is_logistics`, `is_platform_admin` (admin role or superuser), `is_verified`, `display_name`, `profile`.
- **SupplierProfile / BuyerProfile / LogisticsProfile** (one per non-admin user, `VerifiableProfile` base): `business_name`, `location` (FK Location), `verification_status` ∈ `pending`, `approved`, `rejected`, `suspended`; supplier extras: `can_deliver`, `delivery_radius_km`, `member_count` (unused), `certifications`, and computed performance fields `reliability_score`, `completed_orders`, `average_rating`, `on_time_rate`, `inspection_pass_rate`.
- **VerificationDocument**: private uploaded file per user (registration certificate, ID, etc.).

### Core (`apps/core`)
- **Location**: `name`, `county`, `sub_county`, `latitude`, `longitude` (+ PostGIS point when enabled). Loaded from `apps/core/data/kenya_locations.json`: **47 counties and 290 constituency centroids**. Dropdowns group locations by county.
- **AuditLog**: actor, action, target type/id/repr, details JSON, IP, time.
- **MatchingConfig** (singleton, pk=1): matching weights and thresholds (section 7).

### Catalogue (`apps/catalog`)
- **Category → Product → ProductVariety.** Products typed by users that don't exist yet are created under the category **"Other (supplier-added)"** and admins are notified.
- **InventoryItem** (a supplier's stock listing): `product`, `variety`, `quantity_available`, `unit` ∈ `kg`, `tonne`, `crate`, `bag_90kg` (90 kg bags), `litre`, `piece`; `quality_grade` ∈ `premium` (Premium / export grade), `grade_1`, `grade_2`, `processing` (rank 4 / 3 / 2 / 1); `price_per_unit` (KES, indicative); `location`; `harvest_date`, `available_from`, `available_until`; `can_deliver`; `packaging`; `quality_attributes` JSON (the form stores `{"other_details": "<free text>"}`); `is_active`; `last_confirmed_at`.
- **InventoryPhoto**: JPG/PNG/WebP, max **5 MB**, max **6 per listing**, private storage.
- Unit conversion to kg: kg ×1, tonne ×1000, bag_90kg ×90, litre ×1. **Crates and pieces are not convertible**: they only match the same unit (`units_compatible`).

### Sourcing (`apps/sourcing`)
- **SourcingRequest** (`SR-`): `buyer`, `product`, `variety` (optional), `quantity` (≥ 0.01), `unit`, `delivery_location`, `delivery_deadline` (not in the past), `min_quality_grade` (default grade_1), `quality_specifications` JSON (API only, e.g. `{"min_size_mm": 70}`), `quality_notes` (form label "Any other requirements"), `intended_application`, `packaging_requirements`, `target_budget` (optional, ≥ KES 1), `delivery_arrangement` ∈ `supplier_delivers`, `buyer_collects`, `platform_logistics`; `status` ∈ `open`, `matched`, `negotiating`, `supply_confirmed`, `in_fulfilment`, `completed`, `cancelled`.
- **Match**: request × inventory item, `rank`, `score` (0–1), `score_breakdown` JSON (components, weights, notes), `distance_km`, `available_quantity` and `suggested_quantity` (in the request's unit), `status` ∈ `potential`, `contacted`, `availability_confirmed`, `declined`, `committed`, `expired`.
- **Quotation** (`QT-`): supplier's offer from a listing: `quantity`, `price_per_unit`, `delivery_cost`, `includes_delivery`, `earliest_delivery_date`, `valid_until`, counter fields; `status` ∈ `submitted`, `countered`, `accepted`, `rejected`, `withdrawn`, `expired`.

### Orders (`apps/orders`)
- **Order** (`PO-`): one per accepted set of quotations, may span several suppliers; `status` ∈ `pending_confirmation`, `confirmed`, `in_progress`, `delivered`, `completed`, `cancelled`, `disputed` (derived from its lines by `refresh_order_status`). `total` = sum of active line totals.
- **OrderLine** (the supply agreement with one supplier): `quantity`, `unit`, `price_per_unit`, `delivery_cost`, `quality_grade`, `quality_specifications` snapshot, `agreed_delivery_date`, `delivered_quantity`; `status` ∈ `awaiting_reconfirmation`, `confirmed`, `inspection_passed`, `inspection_failed`, `in_transit`, `delivered`, `completed`, `cancelled`. `total = quantity × price + delivery_cost`. Inactive statuses: `cancelled`, `inspection_failed`.
- **Inspection**: `stage` ∈ `pre_dispatch`, `on_receipt`; `result` ∈ `passed`, `conditional`, `failed`; `grade_observed`, `measurements` JSON, `accepted_quantity`, `notes` ("Any other observations"), private `report` file.
- **Delivery** (one per line): `status` ∈ `pending`, `scheduled`, `picked_up`, `in_transit`, `delivered`, `failed`; optional `logistics_partner`, vehicle, driver, received quantity/by, private proof of delivery.
- **OrderEvent**: timeline of everything that happened to the order.
- **Dispute**: `status` ∈ `open`, `under_review`, `resolved`, `rejected`.
- **SaleRecord**: permanent purchase/sales history row written when the buyer confirms receipt of a line; copies product name, variety, grade, delivered quantity, unit, price, delivery cost, total, buyer/supplier names, delivery place, completion time (links use `SET NULL` so history survives deletions).

### Payments (`apps/payments`)
- **Payment** (`PAY-`): buyer → platform. `order`, `payer`, `amount`, `currency` KES, `method` mpesa, `provider` (`simulated` / `mpesa` / `manual` for refunds), `phone` (2547XXXXXXXX), `status` ∈ `pending`, `succeeded`, `failed`, `cancelled`, `refunded`; `checkout_request_id`, `receipt_number` (M-Pesa code, unique when set), `result_code`, raw callback. A recorded **refund is a negative succeeded Payment**.
- **Payout** (`PAYOUT-`): platform → supplier, one per OrderLine. `gross_amount` (line total), `fee` (commission), `amount` (= gross − fee, what the seller receives), `phone`, `status` ∈ `on_hold`, `ready`, `processing`, `paid`, `failed`; `method` ∈ `mpesa_b2c`, `manual`, `simulated`; `receipt_number`, `paid_by`, `paid_at`.

### Comms & reviews
- **Conversation / Message**: buyer ↔ supplier chat, optionally tied to a sourcing request.
- **Notification**: in-app record, `kind` ∈ account, match, availability, quotation, order, inspection, delivery, message, review, dispute, system; also sent by email / SMS / WhatsApp per `NOTIFICATION_CHANNELS` and user opt-ins.
- **Review**: 1–5 stars (+ optional quality and timeliness ratings) by buyer or supplier on a **completed** line, one per reviewer per line.

---

## 6. Business rules (enforced in services)

### Accounts
- Registration roles: farmer, processor, logistics (never admin). New profiles are **approved immediately** unless `REQUIRE_ACCOUNT_VERIFICATION=1`, in which case they start `pending` and admins approve them. Admins can reject or suspend at any time; unapproved users cannot transact.

### Inventory (supplier stock)
- Supplier **types** product and variety names (no pick-lists). Names are matched case-insensitively, ignoring extra spaces and simple plurals ("mangoes" → Mango); unknown products are created and admins notified. Inactive products are refused.
- A listing needs **at least `MIN_PHOTOS_PER_LISTING` (2) photos** to be visible to buyers, matched, found in nearest-supplier search or quoted from. Photo deletion that would leave an active listing below the minimum is blocked.
- Stock not reconfirmed within **`INVENTORY_STALE_DAYS` (14) days** is stale and excluded from matching.
- Saving, confirming or adding photos to stock calls **`offer_new_stock(item)`**: the listing is appended (never re-ranking or expiring others) as a new match to every open request it qualifies for, and the buyer is notified.

### Sourcing requests
- Only buyers (processors) create requests; admins are redirected to the admin request list. The buyer **types** the product (and optional variety); unknown names become new catalogue products.
- Deadline cannot be in the past; budget, if given, ≥ KES 1; variety must belong to the product.
- Matching runs immediately on creation; the buyer sees how many suppliers matched, the coverage %, and **why listings were left out** when coverage is short.
- Buyers can notify matched suppliers (status `contacted`), message them, re-run matching, compare offers, counter, accept or reject quotations, and cancel (not while there are active orders).

### Quotations
- A supplier can only quote if matched to the request, from a listing of the same product, with enough photos, a compatible unit, and grade not more than one below the request. Quantity ≤ listed stock and ≤ requested quantity; earliest delivery ≤ deadline; validity not in the past; one open quote per listing per request.
- Counter-offers: buyer proposes price and/or quantity; supplier accepts (revises) or the buyer waits; accepting while a counter is pending is refused.

### Orders and fulfilment
- `create_order` turns accepted quotations into one Order with one OrderLine per supplier.
- **Reconfirmation:** each supplier reconfirms availability; stock is then **reserved** (deducted, unit-converted). Declining cancels the line.
- **Inspection before pickup:** a line must pass pre-dispatch inspection before its delivery can be `picked_up`.
- Delivery transitions: pending → scheduled|failed; scheduled → picked_up|failed; picked_up → in_transit|delivered|failed; in_transit → delivered|failed; failed → scheduled. `delivered` requires received quantity and receiver name.
- Optional: with `REQUIRE_PAYMENT_BEFORE_DISPATCH=1`, pickup is blocked until the order is fully paid.
- Only the buyer (or admin) completes a delivered line → writes a **SaleRecord**, recomputes supplier metrics, and releases the **Payout** (if the order is paid).
- Orders can be cancelled only before goods are in transit; afterwards a dispute must be raised. An open dispute sets the order to `disputed`.
- Reviews only after completion, by the buyer or that line's supplier, once each.
- **Supplier reliability** = weighted blend of average rating (40%), on-time delivery rate (30%) and inspection pass rate (30%), shrunk toward the new-supplier default (0.6) with a prior weight of 3 completed transactions.

### Payments and commission (escrow)
1. Buyer can pay once the order is `confirmed` or later (all suppliers reconfirmed). Amount due = order total − net paid, rounded **up** to whole KES; M-Pesa limit 250,000 per transaction (larger orders pay in instalments).
2. `start_payment` normalises the phone (07…/7…/+254… → 2547…), refuses a second prompt within 2 minutes, creates a pending Payment and sends an STK Push. In `simulated` mode it succeeds instantly with a fake receipt.
3. Result arrives by Safaricom callback (`handle_stk_callback`) or the buyer's "Check payment status" (`refresh_payment`). Callbacks are idempotent; the paid amount must match exactly or the payment fails and admins are alerted. Result code 1032 = cancelled by user.
4. Money is **held by Origin 254**. When the buyer completes a line and the order is paid, `release_payout` creates a Payout: **fee = `PLATFORM_FEE_PERCENT` (10%) × line total; seller receives the rest (90%)**. Example: KES 40,000 line → KES 4,000 commission, KES 36,000 to the farmer. Lines completed before payment are released when payment succeeds.
5. Open dispute → payout `on_hold` until an admin releases it. `PAYOUT_PROVIDER=manual` (default): an admin pays and records the transaction code; `simulated` / `mpesa` send automatically (B2C).
6. Money paid for lots later cancelled or failed appears as **refund due**; admins record refunds (negative Payment).
7. Commission summary (`commission_summary()`): earned = fees on paid payouts; pending = fees on unpaid payouts; held = received − paid out − earned.

### Photo visibility (`can_view_photo`)
Owner and admins always; any buyer for active listings of approved suppliers; buyers who were matched, quoted or bought keep access after a listing is hidden; the logistics partner carrying that produce; nobody else (other suppliers cannot see competitors' photos).

---

## 7. Matching engine (`apps/sourcing/matching.py`)

**Hard filters** (a listing is a candidate only if all hold): same product; supplier `approved` and user active; listing active with quantity > 0; fresh (`INVENTORY_STALE_DAYS`); ≥ `MIN_PHOTOS_PER_LISTING` photos; within `max_distance_km` of the delivery point; `available_from` ≤ deadline; not the buyer's own stock; **unit compatible**; quality not below the requested grade (one grade below allowed only if `allow_one_grade_below`, penalised to 0.4); can arrive before the deadline (transit days = ceil(distance / `transport_km_per_day`), counted from max(today, available_from)).

**Scores (each 0–1) and default weights** (`MatchingConfig`, editable at `/app/admin/config/`):

| Component | Weight | Rule |
|---|---|---|
| product | 0.15 | exact variety 1.0 · request has no variety 1.0 · listing has no variety 0.8 · other variety 0.6 |
| quantity | 0.20 | min(1, listing kg / requested kg) |
| quality | 0.20 | grade meets requirement 1.0 (one below 0.4); −0.25 per numeric `min_*`/`max_*` spec missed, −0.1 per spec not declared |
| proximity | 0.20 | 1 − distance / max_distance_km |
| delivery | 0.10 | 1.0 if the arrangement is satisfiable, 0.3 if the supplier won't deliver that far under `supplier_delivers` |
| reliability | 0.15 | supplier reliability score, or `new_supplier_reliability` (0.6) when new |

`score = Σ weight × component`. Candidates below `min_score` (0.35) are dropped; the rest are sorted by score (then distance) and capped at `max_matches` (20). Other defaults: `max_distance_km` 600, `transport_km_per_day` 400.

**Allocation:** greedy in rank order until the requested quantity is covered (`suggested_quantity`), so large orders are split across several farmers. A "nearest first" plan (`nearest_first_plan`) is also offered on the request page (`?sort=nearest`).

**Re-matching** (`run_matching`) is safe to repeat: matches suppliers already engaged with are kept and re-scored; untouched potential matches that no longer qualify are deleted; contacted / confirmed ones that no longer qualify become `expired`.

**Distances:** straight-line (PostGIS or haversine). Estimated road distance = straight × **1.3** (`ROAD_FACTOR`); travel time at **50 km/h** (`AVG_SPEED_KMH`).

**Nearest suppliers** (`nearest_suppliers`): approved suppliers with fresh, photographed stock of a typed product, sorted by distance, with optional minimum stock and radius.

**Market report** (`apps/sourcing/market.py`, `market_report`): for a typed product, the farmer sees open requests (status open/matched/negotiating, deadline not passed, not their own), total demand in kg (+ non-convertible units), number of buyers, distance from the farmer, budget per unit, whether they are matched, average/min/max price per kg from sale history in the last **90 days**, and the number of competing suppliers. Demand level: `strong` (≥ 3 requests or ≥ 2 buyers), `some`, `past` (no open requests but recent sales) or `none`. Buyer names are hidden until matched.

---

## 8. Web app pages (`apps/web/urls.py`)

| Path | Who | Page |
|---|---|---|
| `/app/` | all | Role dashboard. Buyer: Payments button with "to pay" badge, Nearest suppliers, New request, stats, active requests, recent orders. Farmer: My payments card, **"Is there a market for my product?"** search, stats, opportunities, supply agreements, photo upload per listing (warnings when < 2 photos). Logistics: active delivery jobs. Admins are redirected to `/app/admin/` |
| `/app/profile/` | all | Profile, verification documents, reviews received |
| `/app/requests/`, `/app/requests/new/`, `/app/requests/<id>/` | buyer | List; create (typed product/variety, grouped location dropdown, ISO date picker, pre-fill via `?product=&delivery_location=`); detail with ranked matches, coverage, quotes, offer comparison |
| `/app/suppliers/nearest/` | buyer | Nearest suppliers search |
| `/app/payments/` | buyer / farmer | Buyer: **Total expenses** (total, produce, delivery, paid), "Still to pay" and the main **Pay with M-Pesa** button (`/app/payments/pay/`), per-order pay buttons, payment history. Farmer: payouts with sale value, 10% commission and amount received, plus upcoming earnings |
| `/app/history/` | buyer / farmer / admin | Purchase history / Sales history / Transaction history (SaleRecords) with search |
| `/app/inventory/` | farmer | Add/edit stock (typed names, "Any other details", ≥ 2 photos), confirm, hide; pre-fill via `?product=` |
| `/app/market/` | farmer | Market search (section 7) |
| `/app/opportunities/`, `/app/quotations/` | farmer | Matches to respond to; own quotations |
| `/app/orders/`, `/app/orders/<id>/` | parties | Order detail: payment card (pay, check status, history, refund due), per-line reconfirm, inspection, logistics, delivery updates, completion, reviews, payout status, timeline, disputes |
| `/app/deliveries/` | logistics / farmer | Delivery jobs |
| `/app/messages/`, `/app/notifications/` | all | Chat and notifications |
| `/app/admin/` | admin | Overview: commission earned/pending, buyer payments received, paid out, plus platform metrics; button **Payments & commission** |
| `/app/admin/payments/` | admin | Payouts to make (send via M-Pesa, record as paid, release hold), refunds due, buyer payments, paid payouts, commission summary |
| `/app/admin/verification/`, `/requests/`, `/disputes/`, `/config/`, `/audit/` | admin | Verification queue, all requests, disputes, matching weights, audit log |
| `/app/photos/<id>/` | see section 6 | Permission-checked product photo |

---

## 9. REST API (`/api/v1/`)

Authentication: `POST /api/v1/auth/token/` (`username`, `password`) → JWT `access` / `refresh`; send `Authorization: Bearer <access>`. Session auth also works. Pagination 25 per page. Throttling: anonymous 60/min, authenticated 600/min. Full spec: `docs/openapi.yaml` (regenerate with `manage.py spectacular --file docs/openapi.yaml`). Worked example: `docs/API.md`.

| Area | Endpoints |
|---|---|
| Auth | `auth/register/`, `auth/token/`, `auth/token/refresh/`, `auth/me/`, `auth/me/profile/` |
| Directory | `suppliers/` (approved suppliers), `suppliers/nearest/?product=&location=` |
| Catalogue | `categories/`, `products/`, `varieties/`, `locations/`, `inventory/` (+ `inventory/{id}/photos/` upload) |
| Documents | `documents/` (+ `{id}/download/`) |
| Sourcing | `sourcing-requests/` (create with `product` id **or** `product_name` text; `variety` or `variety_name`; `delivery_location_id`; response includes a `matching` summary); per request: `rematch/`, `matches/`, `notify-suppliers/`, `offers/` (comparison), `contact/`, `cancel/`. `matches/{id}/respond/` (supplier availability). `quotations/` (create = submit) with `counter/`, `revise/`, `accept/`, `reject/`, `withdraw/` |
| Orders | `orders/` (+ `cancel/`, `dispute/`), `order-lines/{id}/` `reconfirm/`, `inspect/`, `complete/`; `deliveries/{id}/` `assign/`, `update_status/`, `proof/`; `inspections/{id}/report/`; `disputes/{id}/resolve/` (admin); `history/` |
| Payments | `POST payments/` `{order, phone}` (send M-Pesa prompt), `POST payments/{id}/refresh/`, `GET payments/` (buyer own, admin all); `GET payouts/` (supplier own, admin all); admin `POST payouts/{id}/send/`, `…/record/` `{receipt_number, note}`, `…/release/` |
| Comms / reviews | `conversations/`, `notifications/`, `reviews/` |
| Admin | `admin/users/` (activate, deactivate), `admin/suppliers/`, `admin/buyers/`, `admin/logistics/` (verify), `admin/audit-logs/`, `admin/analytics/`, `admin/matching-config/` |

Safaricom callbacks (no login; authenticated by the secret token in the URL, compared in constant time; respond `{"ResultCode": 0, "ResultDesc": "Accepted"}`):
`POST /payments/mpesa/stk/<MPESA_CALLBACK_TOKEN>/`, `/payments/mpesa/b2c-result/<token>/`, `/payments/mpesa/b2c-timeout/<token>/`.

---

## 10. Notifications

Every notification is stored in-app and additionally sent over `NOTIFICATION_CHANNELS` (subset of `in_app,email,sms,whatsapp`) the user can receive. SMS/WhatsApp providers: `console` (default, logs only), `africastalking`, `twilio`. Typical triggers: new match/opportunity, availability responses, quotations and counters, order placed/reconfirmed, inspection results, delivery updates, payment received/failed, payout paid, new product typed by a user (to admins), disputes, reviews.

---

## 11. Security and privacy

- Role-based permissions on every view and API endpoint; data isolation (buyers see only their requests/orders; suppliers only what they were matched to; buyer identities hidden in market search until matched).
- Private media served only through permission checks; upload type checked by magic bytes; size limits.
- Audit log for sensitive actions (verification, payments, payouts, admin document views, etc.).
- Production (`DJANGO_DEBUG=0`): secure session and CSRF cookies, HTTPS redirect (`DJANGO_SECURE_SSL_REDIRECT`, default on), HSTS 30 days, `nosniff`, proxy SSL header. Never commit `.env` (it is in `.gitignore`).
- M-Pesa callbacks: secret URL token, idempotent handling, amount verification, unique receipt numbers.

---

## 12. Front-end design

`static/css/app.css` defines colour tokens on `:root` (light theme only, `color-scheme: light`): leaf green `--brand #2f9e4f` / `--brand-dark #1f7a3a`, mango `--accent #ff9a1f`, sun `--sun #ffd23f`, sky `--sky #3aa7e8`, soft mint background `--bg #f4faf1`. Stat cards have coloured top stripes; money actions use the orange `.pay-btn` style. Templates use the `o254` tag library: `badge` (status pill), `tone`, `kes` (KES 1,234), `qty`, `pct`, `bar`, `isodate`, `label` (snake_case → Words). Date inputs use ISO `YYYY-MM-DD` (`apps.web.forms.DateInput`). Optional fields show a faint "if applicable" placeholder. After CSS changes run `collectstatic` (or hard-reload the browser with Cmd + Shift + R).

---

## 13. Configuration (`.env`, read by `origin254/settings.py`)

| Variable | Default | Meaning |
|---|---|---|
| `DJANGO_SECRET_KEY` | dev key | **Must** be set in production |
| `DJANGO_DEBUG` | `0` | `1` for development |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1` | Comma list |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | empty | e.g. `https://origin254.example.com` |
| `DJANGO_SECURE_SSL_REDIRECT` | `1` (production only) | HTTPS redirect |
| `DATABASE_URL` | `postgres://localhost:5432/origin254` | PostgreSQL connection |
| `PRIVATE_MEDIA_ROOT` | `private_media/` | Private uploads folder |
| `CORS_ALLOWED_ORIGINS` | empty | For external front ends |
| `LOG_LEVEL` | `INFO` | `origin254.*` loggers |
| `NOTIFICATION_CHANNELS` | `in_app,email` | `in_app,email,sms,whatsapp` |
| `SMS_PROVIDER`, `WHATSAPP_PROVIDER` | `console` | `africastalking` / `twilio` |
| `AFRICASTALKING_USERNAME`, `AFRICASTALKING_API_KEY`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_SMS_FROM`, `TWILIO_WHATSAPP_FROM` | empty | Provider credentials |
| `EMAIL_BACKEND`, `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS`, `DEFAULT_FROM_EMAIL` | console backend | Email |
| `REQUIRE_ACCOUNT_VERIFICATION` | `0` | `1` = new accounts wait for admin approval |
| `MIN_PHOTOS_PER_LISTING` | `2` | Photos needed before a listing is visible |
| `INVENTORY_STALE_DAYS` | `14` | Days before unconfirmed stock is hidden |
| `PAYMENT_PROVIDER` | `simulated` | `simulated` (test mode) or `mpesa` |
| `PAYOUT_PROVIDER` | `manual` | `manual`, `simulated` or `mpesa` (B2C) |
| `PLATFORM_FEE_PERCENT` | `10` | Origin 254 commission on each sale |
| `REQUIRE_PAYMENT_BEFORE_DISPATCH` | `0` | `1` blocks pickup until paid |
| `MPESA_ENVIRONMENT` | `sandbox` | or `production` |
| `MPESA_CONSUMER_KEY`, `MPESA_CONSUMER_SECRET`, `MPESA_SHORTCODE`, `MPESA_PASSKEY` | empty | Daraja app credentials (sandbox shortcode 174379) |
| `MPESA_TRANSACTION_TYPE`, `MPESA_TILL_OR_PAYBILL` | `CustomerPayBillOnline`, empty | Use `CustomerBuyGoodsOnline` + till number for a till |
| `MPESA_CALLBACK_BASE_URL`, `MPESA_CALLBACK_TOKEN` | empty | Public HTTPS base URL and secret token for callbacks |
| `MPESA_B2C_SHORTCODE`, `MPESA_B2C_INITIATOR`, `MPESA_B2C_SECURITY_CREDENTIAL` | empty | Only for automatic payouts |
| `BACKUP_RETENTION_DAYS` | `14` | For `backup_db` |

Tests use a temporary private-media folder automatically.

---

## 14. Management commands (`apps/core/management/commands/`)

| Command | Does |
|---|---|
| `load_kenya_locations` | Loads/updates the 47 counties and 290 constituencies (idempotent) |
| `seed_demo [--reset]` | Demo catalogue, users, stock and 2 placeholder photos per listing; `--reset` wipes transactional data first |
| `mango_demo [--stop-after STAGE]` | Resets and runs the MVP scenario through the real services, narrating each step (includes a simulated M-Pesa payment) |
| `backup_db` | `pg_dump` backup into `backups/`, pruning older than `BACKUP_RETENTION_DAYS` |

---

## 15. Tests

```bash
.venv/bin/python manage.py test tests        # 132 tests, PostgreSQL test database
```

| File | Covers |
|---|---|
| `test_matching.py` | Filters, ranking, multi-supplier allocation, weights, re-matching, geo |
| `test_api_workflow.py` | Full mango scenario over the REST API as each role; business rules; permissions; audit; auth |
| `test_request_form.py` | New request page: typed products, validation, units, budget, messages, admin redirect, API `product_name` |
| `test_inventory_form.py` | Typed product/variety stock form, photo minimum |
| `test_photos.py` | Upload, limits, deletion rule, visibility rules, nearest-supplier photos |
| `test_nearest.py` | Nearest suppliers page and API |
| `test_market.py` | Market search and `offer_new_stock` |
| `test_history.py` | Sale history records and pages |
| `test_payments.py` | Phone formats, simulated flow, escrow, 10% commission, disputes/holds, refunds, dispatch rule, M-Pesa callbacks (success, cancel, mismatch, bad token), admin/buyer/farmer pages, Pay button |
| `test_locations.py` | Kenya location data |
| `factories.py` | `seed()`, `user()`, `api()`, `mango_request()`, `quote()` helpers |

Run the whole suite after every change; keep it green.

---

## 16. Deployment status

- **Not yet deployed.** No hosting account or git remote is configured; the git repository exists but has no commits. `Dockerfile` (gunicorn, runs migrations on start) and `docker-compose.yml` (PostGIS 16-3.4) are provided but untested here (Docker is not installed on the development Mac). See `docs/DEPLOYMENT.md`.
- Going live with real money requires Safaricom Daraja credentials, `PAYMENT_PROVIDER=mpesa`, a public HTTPS URL for callbacks, and (for automatic payouts) B2C approval.

---

## 17. Known limitations and ideas not yet built

- Road distance is an estimate (straight line × 1.3); no routing service.
- Locations stop at constituency level (no wards).
- Product photos are served at full size (no resizing / thumbnails).
- No card payments (only M-Pesa); no mobile app or USSD.
- `member_count` field and the "cooperative certificate" document type are leftovers from the removed cooperative role.
- The development database may contain real user accounts besides the demo ones; the demo reset commands delete their transactional data.

---

## 18. Guidance for AI agents working on this repo

1. Read section 4's conventions first; put rules in `services.py` and call them from both `api.py` and `apps/web/views.py`.
2. Raise `WorkflowError` for rule violations; never return ad-hoc error formats.
3. Add or update tests in `tests/` for every behaviour change and run `manage.py test tests`.
4. After model changes run `makemigrations` + `migrate`; after API changes regenerate `docs/openapi.yaml`.
5. Keep docs in sync: this README, `docs/ARCHITECTURE.md`, `docs/DATABASE.md`, `docs/API.md`, `docs/DEPLOYMENT.md`, `.env.example`.
6. Do not run `mango_demo` / `seed_demo --reset` on a database with real users. Do not commit `.env`, `private_media/` or `backups/`.
7. Keep the UI in the existing light theme and template structure; reuse `o254` template tags and the `_field.html` / `_form.html` partials.
8. Money: `Decimal`, 2 dp; commission via `apps.payments.services.split()` / `PLATFORM_FEE_PERCENT`, never hard-coded.
9. Product and variety are **typed by users** everywhere in the web UI (no dropdown lists); resolve names with `apps.catalog.services.find_product` / `resolve_product` / `resolve_variety`.
