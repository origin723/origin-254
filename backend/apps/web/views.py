"""Server-rendered web app for buyers, suppliers, logistics partners and admins.

Every state change goes through the same service functions as the REST API.
"""
import logging
from decimal import Decimal
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse, Http404
from apps.core.files import open_stored
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.models import (BuyerProfile, LogisticsProfile, Role, SupplierProfile, User, VerificationDocument,
                                  VerificationStatus)
from apps.accounts.services import initial_verification_fields, profile_model_for_role, set_verification
from apps.catalog.models import MAX_PHOTOS_PER_LISTING, InventoryItem, InventoryPhoto
from apps.catalog.services import add_photos, can_view_photo
from apps.comms.models import Conversation
from apps.comms.services import post_message, start_conversation
from apps.core.analytics import platform_metrics
from apps.core.audit import log_action
from apps.core.exceptions import WorkflowError
from apps.core.models import AuditLog, MatchingConfig
from apps.orders import services as order_services
from apps.orders.api import visible_orders, visible_sales
from apps.orders.models import Delivery, Dispute, OrderLine
from apps.payments import services as pay_services
from apps.payments.models import Payment, Payout
from apps.reviews.models import Review
from apps.reviews.services import create_review
from apps.sourcing import services as sourcing
from apps.sourcing.api import visible_requests
from apps.sourcing.market import market_report
from apps.sourcing.matching import ROAD_FACTOR, nearest_first_plan, nearest_suppliers, offer_new_stock
from apps.sourcing.models import Match, Quotation, SourcingRequest

from . import forms

logger = logging.getLogger("origin254.web")


def role_required(*checks):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapper(request, *args, **kwargs):
            if not any(getattr(request.user, c) for c in checks):
                raise PermissionDenied
            return view(request, *args, **kwargs)
        return wrapper
    return decorator


buyer_required = role_required("is_buyer", "is_platform_admin")
supplier_required = role_required("is_supplier")
admin_required = role_required("is_platform_admin")


def _error_text(exc):
    if isinstance(exc, ValidationError):
        if hasattr(exc, "error_dict"):
            return "; ".join(f"{k.replace('_', ' ')}: {' '.join(v)}" if k != "__all__" else " ".join(v)
                             for k, v in exc.message_dict.items())
        return " ".join(exc.messages)
    return exc.message


def run_action(request, fn, success, *args, **kwargs):
    """Run a service call, flashing success or the business-rule error."""
    try:
        result = fn(*args, **kwargs)
    except (WorkflowError, ValidationError) as exc:
        messages.error(request, _error_text(exc))
        return None
    if success:
        messages.success(request, success)
    return result


def back(request, fallback="web:dashboard"):
    return redirect(request.POST.get("next") or request.META.get("HTTP_REFERER") or fallback)


# --- Public ------------------------------------------------------------------------

def landing(request):
    return render(request, "landing.html")


def register(request):
    form = forms.RegisterForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        with transaction.atomic():
            user = User(username=d["username"], email=d["email"], first_name=d["first_name"],
                        last_name=d["last_name"], phone=d["phone"], role=d["role"])
            user.set_password(d["password"])
            user.save()
            profile = profile_model_for_role(d["role"]).objects.create(
                user=user, business_name=d["business_name"], location=d["location"], **initial_verification_fields())
            log_action(user, "account.registered", user, role=user.role, status=profile.verification_status)
        login(request, user)
        if profile.is_approved:
            messages.success(request, "Welcome to Origin 254! Your account is active and ready to use.")
            return redirect("web:dashboard")
        messages.success(request, "Welcome to Origin 254! Upload your documents so we can verify your account.")
        return redirect("web:profile")
    return render(request, "web/register.html", {"form": form,
                                                 "needs_verification": settings.REQUIRE_ACCOUNT_VERIFICATION})


# --- Dashboard --------------------------------------------------------------------------

@login_required
def dashboard(request):
    user = request.user
    ctx = {"profile": user.profile}
    if user.is_platform_admin:
        return redirect("web:admin_dashboard")
    if user.is_buyer:
        reqs = SourcingRequest.objects.filter(buyer=user)
        ctx.update(active_requests=reqs.exclude(status__in=["completed", "cancelled"])[:5],
                   orders=visible_orders(user)[:5],
                   stats={"open": reqs.exclude(status__in=["completed", "cancelled"]).count(),
                          "quotes": Quotation.objects.filter(sourcing_request__buyer=user,
                                                             status__in=Quotation.OPEN_STATUSES).count(),
                          "orders": visible_orders(user).exclude(status__in=["completed", "cancelled"]).count(),
                          "completed": visible_orders(user).filter(status="completed").count()})
        payable = [o for o in visible_orders(user).filter(status__in=pay_services.PAYABLE_ORDER_STATUSES)
                   if pay_services.amount_due(o) > 0]
        ctx["to_pay"] = {"count": len(payable), "amount": sum((pay_services.amount_due(o) for o in payable), 0)}
    elif user.is_supplier:
        profile = user.supplier_profile
        ctx.update(listings=profile.inventory.filter(is_active=True).select_related("product", "variety")
                   .prefetch_related("photos"), photo_form=forms.PhotoUploadForm(),
                   max_photos=MAX_PHOTOS_PER_LISTING, min_photos=settings.MIN_PHOTOS_PER_LISTING)
        ctx.update(opportunities=Match.objects.filter(supplier=profile, status__in=[
            Match.Status.POTENTIAL, Match.Status.CONTACTED, Match.Status.AVAILABILITY_CONFIRMED]).select_related(
            "sourcing_request__product")[:5],
            lines=OrderLine.objects.filter(supplier=profile).exclude(status__in=["completed", "cancelled"])
            .select_related("order")[:5],
            stale=profile.inventory.filter(is_active=True).fresh().count() != profile.inventory.filter(
                is_active=True).count(),
            stats={"listings": profile.inventory.filter(is_active=True).count(),
                   "opportunities": Match.objects.filter(supplier=profile, status__in=[
                       Match.Status.POTENTIAL, Match.Status.CONTACTED]).count(),
                   "quotes": profile.quotations.filter(status__in=Quotation.OPEN_STATUSES).count(),
                   "completed": profile.completed_orders})
        my_payouts = Payout.objects.filter(supplier=profile)
        ctx["earnings"] = {"received": sum((p.amount for p in my_payouts.filter(status="paid")), 0),
                           "awaiting": sum((p.amount for p in my_payouts.exclude(status="paid")), 0),
                           "pct": pay_services.commission_percent()}
    elif user.is_logistics:
        ctx.update(deliveries=Delivery.objects.filter(logistics_partner=user).exclude(
            status__in=["delivered"]).select_related("order_line__order", "pickup_location", "dropoff_location"))
    return render(request, "web/dashboard.html", ctx)


@login_required
def profile(request):
    doc_form = forms.DocumentForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and doc_form.is_valid():
        doc = doc_form.save(commit=False)
        doc.owner = request.user
        doc.original_name = request.FILES["file"].name[:255]
        doc.save()
        log_action(request.user, "document.uploaded", doc, doc_type=doc.doc_type)
        messages.success(request, "Document uploaded. An administrator will review it.")
        return redirect("web:profile")
    reviews = Review.objects.filter(reviewee=request.user).select_related("reviewer")[:10]
    return render(request, "web/profile.html", {"profile": request.user.profile, "doc_form": doc_form,
                                                "documents": request.user.documents.all(), "reviews": reviews})


@login_required
def document_download(request, pk):
    doc = get_object_or_404(VerificationDocument, pk=pk)
    if not (request.user.is_platform_admin or doc.owner_id == request.user.pk):
        raise Http404
    if request.user.is_platform_admin and doc.owner_id != request.user.pk:
        log_action(request.user, "document.viewed", doc)
    return FileResponse(open_stored(doc.file), as_attachment=True, filename=doc.original_name or "document")


# --- Buyer: sourcing requests ------------------------------------------------------------

@buyer_required
def request_list(request):
    reqs = visible_requests(request.user)
    status = request.GET.get("status")
    if status:
        reqs = reqs.filter(status=status)
    return render(request, "web/request_list.html", {"requests": reqs, "statuses": SourcingRequest.Status.choices,
                                                     "current": status})


def _matching_summary(req, match):
    """Explain the matching outcome in plain words, including why listings were left out."""
    n = len(match.candidates)
    if n and match.fully_covered:
        return messages.INFO, (f"{req.reference}: {n} supplier match{'es' if n != 1 else ''} found and the full "
                               f"{req.quantity:g} {req.unit} can be covered.")
    reasons = {}
    for reason in match.excluded.values():
        reason = reason if isinstance(reason, str) else "; ".join(reason)
        key = "score below threshold" if reason.startswith("score ") else reason
        reasons[key] = reasons.get(key, 0) + 1
    why = "; ".join(f"{count} listing{'s' if count != 1 else ''}: {reason}" for reason, count in
                    sorted(reasons.items(), key=lambda x: -x[1]))
    if not n:
        text = f"{req.reference} is saved, but no supplier can meet it yet."
        text += f" Left out: {why}." if why else (f" No approved supplier currently lists {req.product.name} "
                                                 f"in stock with photos.")
        return messages.WARNING, text + " Try a later deadline, a lower grade or another unit, or re-run " \
                                        "matching later as suppliers add stock."
    return messages.WARNING, (f"{req.reference}: {n} supplier match{'es' if n != 1 else ''}, but only "
                              f"{match.coverage:.0%} of the {req.quantity:g} {req.unit} is covered."
                              + (f" Left out: {why}." if why else ""))


@buyer_required
def request_new(request):
    if not request.user.is_buyer:
        messages.info(request, "Sourcing requests are posted by buyer (processor) accounts. "
                               "As an administrator you can review them here.")
        return redirect("web:admin_requests")
    initial = {}
    if request.GET.get("delivery_location", "").isdigit():
        initial["delivery_location"] = request.GET["delivery_location"]
    if request.GET.get("product"):
        initial["product_name"] = request.GET["product"][:120]
    else:
        profile = getattr(request.user, "buyer_profile", None)
        if profile and profile.location_id:
            initial["delivery_location"] = profile.location_id
    form = forms.SourcingRequestForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        result = None
        try:
            with transaction.atomic():
                result = sourcing.create_sourcing_request(request.user, **form.request_data(request.user))
        except (WorkflowError, ValidationError) as exc:
            messages.error(request, _error_text(exc))
        if result:
            messages.success(request, "Sourcing request submitted.")
            req, match = result
            messages.add_message(request, *_matching_summary(req, match))
            return redirect("web:request_detail", pk=req.pk)
    return render(request, "web/request_form.html", {"form": form})


@login_required
def request_detail(request, pk):
    req = get_object_or_404(visible_requests(request.user), pk=pk)
    if request.user.is_supplier:
        match = req.matches.filter(supplier__user=request.user).exclude(status="expired").first()
        if match:
            return redirect("web:opportunity_detail", pk=match.pk)
        raise Http404
    sort = "nearest" if request.GET.get("sort") == "nearest" else "best"
    matches = req.matches.exclude(status=Match.Status.EXPIRED).select_related(
        "supplier__user", "inventory_item__location", "inventory_item__variety").prefetch_related(
        "inventory_item__photos").order_by("distance_km" if sort == "nearest" else "rank")
    nearest_id = min(matches, key=lambda m: m.distance_km).pk if matches else None
    comparison = sourcing.compare_offers(req)
    accepted_not_ordered = req.quotations.filter(status=Quotation.Status.ACCEPTED, order_line__isnull=True)
    planned = sum(m.suggested_quantity for m in matches)
    return render(request, "web/request_detail.html", {
        "req": req, "matches": matches, "comparison": comparison, "planned": planned,
        "all_quotes": req.quotations.select_related("supplier").order_by("-created_at"),
        "accepted_not_ordered": accepted_not_ordered, "orders": req.orders.all(),
        "counter_form": forms.CounterForm(), "sort": sort, "nearest_id": nearest_id,
        "nearest_plan": nearest_first_plan(req, list(matches)), "road_factor": ROAD_FACTOR})


@buyer_required
def nearest_suppliers_view(request):
    """Buyer tool: nearest approved suppliers with current stock of a product."""
    initial = {}
    profile = getattr(request.user, "buyer_profile", None)
    if profile:
        initial["location"] = profile.location
    form = forms.NearestSuppliersForm(request.GET or None, initial=initial)
    results = None
    if request.GET and form.is_valid():
        d = form.cleaned_data
        results = [] if d["product"] is None else nearest_suppliers(
            d["product"], d["location"], min_quantity=d.get("min_quantity"), radius_km=d.get("radius_km"))
    return render(request, "web/nearest_suppliers.html", {"form": form, "results": results})


@buyer_required
@require_POST
def request_action(request, pk, action):
    req = get_object_or_404(SourcingRequest, pk=pk, buyer=request.user)
    if action == "rematch":
        res = run_action(request, sourcing.rerun_matching, "Matching re-run.", request.user, req)
        if res:
            messages.info(request, f"{len(res.candidates)} match(es); coverage {res.coverage:.0%}.")
    elif action == "notify":
        ids = request.POST.getlist("match_ids")
        res = run_action(request, sourcing.notify_matched_suppliers, "Suppliers notified.", request.user, req,
                         ids or None, request.POST.get("message", ""))
        if res == []:
            messages.info(request, "No new suppliers to notify.")
    elif action == "contact":
        other = get_object_or_404(User, pk=request.POST.get("supplier_user_id"))
        convo = run_action(request, start_conversation, "Message sent.", request.user, other,
                           f"{req.reference} — {req.product.name}", sourcing_request=req,
                           first_message=request.POST.get("message", ""))
        if convo:
            return redirect("web:conversation", pk=convo.pk)
    elif action == "cancel":
        run_action(request, sourcing.cancel_sourcing_request, "Request cancelled.", request.user, req,
                   request.POST.get("reason", ""))
    elif action == "order":
        quotes = req.quotations.filter(pk__in=request.POST.getlist("quotation_ids"))
        order = run_action(request, order_services.create_order, "Order placed. Suppliers will reconfirm.",
                           request.user, req, quotes, request.POST.get("notes", ""))
        if order:
            return redirect("web:order_detail", pk=order.pk)
    return redirect("web:request_detail", pk=pk)


@buyer_required
@require_POST
def quotation_buyer_action(request, pk, action):
    quote = get_object_or_404(Quotation, pk=pk, sourcing_request__buyer=request.user)
    if action == "accept":
        run_action(request, sourcing.accept_quotation, f"{quote.reference} accepted.", request.user, quote)
    elif action == "reject":
        run_action(request, sourcing.reject_quotation, f"{quote.reference} rejected.", request.user, quote,
                   request.POST.get("reason", ""))
    elif action == "counter":
        form = forms.CounterForm(request.POST)
        if form.is_valid():
            d = form.cleaned_data
            run_action(request, sourcing.counter_quotation, "Counter-offer sent.", request.user, quote,
                       d.get("price_per_unit"), d.get("quantity"), d.get("note", ""))
        else:
            messages.error(request, "Enter a valid price or quantity.")
    return redirect("web:request_detail", pk=quote.sourcing_request_id)


# --- Supplier ----------------------------------------------------------------------------

@supplier_required
def inventory(request):
    profile = request.user.supplier_profile
    edit = None
    if request.GET.get("edit"):
        edit = get_object_or_404(InventoryItem, pk=request.GET["edit"], supplier=profile)
    initial = {"location": profile.location, "can_deliver": profile.can_deliver}
    if request.GET.get("product") and not edit:
        initial["product_name"] = request.GET["product"][:120]
    form = forms.InventoryForm(request.POST or None, request.FILES or None, instance=edit, user=request.user,
                               initial=initial)
    if request.method == "POST" and form.is_valid():
        photos = form.cleaned_data.get("photos") or []
        with transaction.atomic():
            item = form.save(commit=False)
            item.supplier = profile
            item.last_confirmed_at = timezone.now()
            item.save()
            if photos:
                add_photos(request.user, item, photos)
        messages.success(request, f"{item.product}{f' ({item.variety.name})' if item.variety else ''} "
                                  "saved and confirmed as current.")
        if form.product_created:
            messages.info(request, f"“{item.product}” is new to Origin 254. Our team will review it; buyers can "
                                   "request it right away.")
        _announce_offers(request, item)
        return redirect("web:inventory")
    items = (profile.inventory.select_related("product", "variety", "location").prefetch_related("photos")
             .order_by("-is_active", "product"))
    return render(request, "web/inventory.html", {"items": items, "form": form, "edit": edit,
                                                  "verified": profile.is_approved,
                                                  "photo_form": forms.PhotoUploadForm(),
                                                  "max_photos": MAX_PHOTOS_PER_LISTING,
                                                  "min_photos": settings.MIN_PHOTOS_PER_LISTING})


def _announce_offers(request, item):
    """Match new or updated stock to buyers already waiting for it, and tell the supplier."""
    new = offer_new_stock(item)
    if new:
        messages.success(request, f"Good news: {len(new)} buyer request{'s' if len(new) != 1 else ''} for "
                                  f"{item.product} matched your stock. See Opportunities.")


@supplier_required
def market(request):
    """Farmer tool: is there a market for this product? Open buyer demand, recent prices, competition."""
    q = request.GET.get("product", "").strip()[:120]
    report = market_report(q, request.user.supplier_profile) if len(q) >= 2 else None
    return render(request, "web/market.html", {"q": q, "report": report})


@supplier_required
@require_POST
def inventory_photo_upload(request, pk):
    item = get_object_or_404(InventoryItem, pk=pk, supplier__user=request.user)
    form = forms.PhotoUploadForm(request.POST, request.FILES)
    if form.is_valid():
        added = run_action(request, add_photos, "Photos uploaded.", request.user, item,
                           form.cleaned_data["photos"], form.cleaned_data["caption"])
        if added:
            messages.success(request, f"{len(added)} photo(s) added to {item.product}.")
            _announce_offers(request, item)
    else:
        messages.error(request, " ".join(e for errs in form.errors.values() for e in errs))
    return back(request, "web:inventory")


@supplier_required
@require_POST
def inventory_photo_delete(request, pk):
    photo = get_object_or_404(InventoryPhoto, pk=pk, inventory_item__supplier__user=request.user)
    item = photo.inventory_item
    storage, name = photo.image.storage, photo.image.name
    photo.delete()  # remove the record first: a leftover file is harmless, a dangling record is not
    if name:
        try:
            storage.delete(name)
        except Exception as exc:  # missing file or storage unavailable: still removed from the listing
            logger.warning("Photo %s removed but its file %r could not be deleted: %s", pk, name, exc)
    missing = item.photos_missing
    if item.is_active and missing:
        messages.warning(request, f"Photo removed. This listing is hidden from buyers until you add "
                                  f"{missing} more photo{'s' if missing > 1 else ''}.")
    else:
        messages.success(request, "Photo removed.")
    return back(request, "web:inventory")


@login_required
def photo_view(request, pk):
    photo = get_object_or_404(InventoryPhoto.objects.select_related("inventory_item__supplier"), pk=pk)
    if not can_view_photo(request.user, photo):
        raise Http404
    ext = photo.image.name.rsplit(".", 1)[-1].lower()
    content_type = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}[ext]
    response = FileResponse(open_stored(photo.image), content_type=content_type)
    response["Cache-Control"] = "private, max-age=3600"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@supplier_required
@require_POST
def inventory_action(request, pk, action):
    item = get_object_or_404(InventoryItem, pk=pk, supplier__user=request.user)
    if action == "confirm":
        item.last_confirmed_at = timezone.now()
        item.is_active = True
        item.save(update_fields=["last_confirmed_at", "is_active", "updated_at"])
        messages.success(request, f"{item.product} stock confirmed as current.")
        _announce_offers(request, item)
    elif action == "deactivate":
        item.is_active = False
        item.save(update_fields=["is_active", "updated_at"])
        messages.success(request, "Listing hidden from matching.")
    return redirect("web:inventory")


@supplier_required
def opportunities(request):
    matches = Match.objects.filter(supplier__user=request.user).exclude(status=Match.Status.EXPIRED).select_related(
        "sourcing_request__product", "sourcing_request__delivery_location", "inventory_item").order_by(
        "-created_at")
    return render(request, "web/opportunities.html", {"matches": matches})


@supplier_required
def opportunity_detail(request, pk):
    match = get_object_or_404(Match.objects.select_related("sourcing_request__product", "inventory_item"),
                              pk=pk, supplier__user=request.user)
    req = match.sourcing_request
    profile = request.user.supplier_profile
    quote_form = forms.QuotationForm(request.POST or None, supplier=profile, request_obj=req,
                                     initial={"inventory_item": match.inventory_item,
                                              "quantity": match.confirmed_quantity or match.suggested_quantity
                                              or match.available_quantity,
                                              "price_per_unit": match.inventory_item.price_per_unit})
    if request.method == "POST" and request.POST.get("form") == "quote" and quote_form.is_valid():
        d = quote_form.cleaned_data
        q = run_action(request, sourcing.submit_quotation, "Quotation submitted.", request.user, req,
                       d["inventory_item"], d["quantity"], d["price_per_unit"], d["earliest_delivery_date"],
                       d["valid_until"], d["delivery_cost"], d["includes_delivery"], d["notes"])
        if q:
            return redirect("web:quotation_detail", pk=q.pk)
    quotes = req.quotations.filter(supplier=profile)
    convo = Conversation.objects.filter(sourcing_request=req, participants=request.user).first()
    return render(request, "web/opportunity_detail.html", {"match": match, "req": req, "quote_form": quote_form,
                                                           "quotes": quotes, "convo": convo})


@supplier_required
@require_POST
def opportunity_respond(request, pk):
    match = get_object_or_404(Match, pk=pk, supplier__user=request.user)
    available = request.POST.get("available") == "yes"
    qty = request.POST.get("quantity") or None
    run_action(request, sourcing.respond_to_match, "Response sent to the buyer.", request.user, match, available,
               qty, request.POST.get("note", ""))
    return redirect("web:opportunity_detail", pk=pk)


@supplier_required
@require_POST
def opportunity_message(request, pk):
    match = get_object_or_404(Match, pk=pk, supplier__user=request.user)
    req = match.sourcing_request
    convo = run_action(request, start_conversation, "Message sent.", request.user, req.buyer,
                       f"{req.reference} — {req.product.name}", sourcing_request=req,
                       first_message=request.POST.get("message", ""))
    return redirect("web:conversation", pk=convo.pk) if convo else redirect("web:opportunity_detail", pk=pk)


@login_required
def quotation_list(request):
    user = request.user
    if user.is_supplier:
        quotes = Quotation.objects.filter(supplier__user=user)
    elif user.is_buyer:
        quotes = Quotation.objects.filter(sourcing_request__buyer=user)
    elif user.is_platform_admin:
        quotes = Quotation.objects.all()
    else:
        raise PermissionDenied
    return render(request, "web/quotation_list.html",
                  {"quotes": quotes.select_related("sourcing_request__product", "supplier")})


@login_required
def quotation_detail(request, pk):
    q = get_object_or_404(Quotation.objects.select_related("sourcing_request__product", "supplier__user"), pk=pk)
    user = request.user
    if not (user.is_platform_admin or q.supplier.user_id == user.pk or q.sourcing_request.buyer_id == user.pk):
        raise Http404
    if user.is_buyer:
        return redirect("web:request_detail", pk=q.sourcing_request_id)
    return render(request, "web/quotation_detail.html", {"q": q, "counter_form": forms.CounterForm()})


@supplier_required
@require_POST
def quotation_supplier_action(request, pk, action):
    q = get_object_or_404(Quotation, pk=pk, supplier__user=request.user)
    if action == "accept_counter":
        run_action(request, sourcing.revise_quotation, "Counter-offer accepted; revised quotation sent.",
                   request.user, q, accept_counter=True)
    elif action == "revise":
        form = forms.CounterForm(request.POST)
        if form.is_valid():
            d = form.cleaned_data
            run_action(request, sourcing.revise_quotation, "Revised quotation sent.", request.user, q,
                       d.get("price_per_unit"), d.get("quantity"), None, d.get("note", ""))
    elif action == "withdraw":
        run_action(request, sourcing.withdraw_quotation, "Quotation withdrawn.", request.user, q)
    return redirect("web:quotation_detail", pk=pk)


# --- Orders (all roles) ------------------------------------------------------------------

@login_required
def order_list(request):
    orders = visible_orders(request.user)
    return render(request, "web/order_list.html", {"orders": orders})


@login_required
def history(request):
    """Everything bought (buyers) or sold (suppliers) once the buyer confirmed receipt."""
    records = visible_sales(request.user).prefetch_related("inventory_item__photos")
    q = request.GET.get("q", "").strip()
    if q:
        records = records.filter(Q(product_name__icontains=q) | Q(variety_name__icontains=q)
                                 | Q(order_reference__icontains=q) | Q(supplier_name__icontains=q)
                                 | Q(buyer_name__icontains=q))
    records = list(records)
    by_product = {}
    for r in records:
        key = (r.product_name, r.unit)
        by_product[key] = by_product.get(key, 0) + r.quantity
    u = request.user
    title = "Sales history" if u.is_supplier else "Purchase history" if u.is_buyer else "Transaction history"
    return render(request, "web/history.html", {
        "records": records, "q": q, "title": title, "total_value": sum((r.total for r in records), 0),
        "by_product": sorted(((p, unit, n) for (p, unit), n in by_product.items()), key=lambda x: -x[2]),
        "counterparty": "Buyer" if u.is_supplier else "Supplier" if u.is_buyer else None})


@login_required
def order_detail(request, pk):
    order = get_object_or_404(visible_orders(request.user), pk=pk)
    user = request.user
    lines = order.lines.select_related("supplier__user", "delivery__logistics_partner", "delivery__pickup_location",
                                       "quotation").prefetch_related("inspections", "reviews")
    for line in lines:
        line.can_update_delivery = hasattr(line, "delivery") and order_services.can_update_delivery(user,
                                                                                                    line.delivery)
        line.my_review = next((r for r in line.reviews.all() if r.reviewer_id == user.pk), None)
        line.is_mine = line.supplier.user_id == user.pk
    is_buyer = order.buyer_id == user.pk
    payments = order.payments.all()
    pay = {"state": pay_services.payment_state(order), "paid": pay_services.amount_paid(order),
           "due": pay_services.amount_due(order), "refund_due": pay_services.refund_due(order),
           "payable": order.status in pay_services.PAYABLE_ORDER_STATUSES,
           "pending": next((p for p in payments if p.status == Payment.Status.PENDING), None),
           "payments": payments, "provider": settings.PAYMENT_PROVIDER,
           "phone": user.phone if is_buyer else ""}
    payouts = {p.order_line_id: p for p in Payout.objects.filter(order_line__order=order)}
    for line in lines:
        line.payout_record = payouts.get(line.pk)
    return render(request, "web/order_detail.html", {"pay": pay,
        "order": order, "lines": lines, "is_buyer": is_buyer, "can_inspect": is_buyer or user.is_platform_admin,
        "logistics_partners": User.objects.filter(role=Role.LOGISTICS, is_active=True,
                                                  logistics_profile__verification_status="approved"),
        "inspection_form": forms.InspectionForm(), "delivery_form": forms.DeliveryUpdateForm(),
        "review_form": forms.ReviewForm(), "events": order.events.select_related("actor", "order_line__supplier"),
        "disputes": order.disputes.all()})


@login_required
@require_POST
def order_line_action(request, pk, action):
    line = get_object_or_404(OrderLine.objects.select_related("order"), pk=pk)
    order = line.order
    if not order_services.can_view_order(request.user, order):
        raise Http404
    u = request.user
    if action == "reconfirm":
        run_action(request, order_services.reconfirm_line, "Availability reconfirmed — stock reserved.", u, line,
                   request.POST.get("available") == "yes", request.POST.get("note", ""))
    elif action == "inspect":
        form = forms.InspectionForm(request.POST, request.FILES)
        if form.is_valid():
            d = form.cleaned_data
            run_action(request, order_services.record_inspection, "Inspection recorded.", u, line, d["stage"],
                       d["result"], d["grade_observed"], {}, d.get("accepted_quantity"), None,
                       d["notes"], d.get("report"))
        else:
            messages.error(request, "Check the inspection form.")
    elif action == "assign":
        partner = get_object_or_404(User, pk=request.POST.get("logistics_partner"))
        run_action(request, order_services.assign_logistics, "Logistics partner assigned.", u, line.delivery,
                   partner, request.POST.get("scheduled_date") or None)
    elif action == "delivery":
        form = forms.DeliveryUpdateForm(request.POST, request.FILES)
        if form.is_valid():
            d = {k: v for k, v in form.cleaned_data.items() if v not in (None, "")}
            status = d.pop("status")
            run_action(request, order_services.update_delivery, "Delivery updated.", u, line.delivery, status, **d)
        else:
            messages.error(request, "Check the delivery form.")
    elif action == "complete":
        run_action(request, order_services.complete_line, "Receipt confirmed — transaction completed.", u, line,
                   request.POST.get("note", ""))
    elif action == "review":
        form = forms.ReviewForm(request.POST)
        if form.is_valid():
            d = form.cleaned_data
            run_action(request, create_review, "Thank you for your feedback.", u, line, d["rating"], d["comment"],
                       d.get("quality_rating"), d.get("timeliness_rating"))
    return back(request)


@login_required
@require_POST
def order_action(request, pk, action):
    order = get_object_or_404(visible_orders(request.user), pk=pk)
    if action == "cancel":
        run_action(request, order_services.cancel_order, "Order cancelled.", request.user, order,
                   request.POST.get("reason", ""))
    elif action == "dispute":
        line = order.lines.filter(pk=request.POST.get("order_line")).first()
        run_action(request, order_services.raise_dispute, "Dispute raised; our team will be in touch.",
                   request.user, order, request.POST.get("reason", ""), line)
    elif action == "pay":
        payment = run_action(request, pay_services.start_payment, "", request.user, order,
                             request.POST.get("phone", ""))
        if payment is not None:
            _payment_message(request, payment)
    elif action == "pay-refresh":
        payment = get_object_or_404(Payment, pk=request.POST.get("payment"), order=order)
        payment = run_action(request, pay_services.refresh_payment, "", request.user, payment)
        if payment is not None:
            _payment_message(request, payment)
    elif action == "refund" and request.user.is_platform_admin:
        run_action(request, pay_services.mark_refunded, "Refund recorded.", request.user, order,
                   request.POST.get("receipt_number", ""), request.POST.get("amount") or 0)
    if request.POST.get("next") == "payments":
        return redirect("web:payments")
    return redirect("web:order_detail", pk=pk)


def _payment_message(request, payment):
    if payment.status == Payment.Status.SUCCEEDED:
        messages.success(request, f"Paid KES {payment.amount:,.0f}. M-Pesa code {payment.receipt_number}.")
    elif payment.status == Payment.Status.PENDING:
        messages.info(request, f"Check your phone ({payment.phone}) and enter your M-Pesa PIN to pay "
                               f"KES {payment.amount:,.0f}. Then tap “Check payment status”.")
    else:
        messages.error(request, f"Payment {payment.get_status_display().lower()}: {payment.result_description}")


@login_required
def payments(request):
    """Payments page for buyers (what to pay) and suppliers (what they earn). Admins use admin_payments."""
    user = request.user
    if user.is_platform_admin:
        return redirect("web:admin_payments")
    pct = pay_services.commission_percent()
    if user.is_buyer:
        orders = list(visible_orders(user).exclude(status="cancelled"))
        rows = []
        for o in orders:
            rows.append({"order": o, "state": pay_services.payment_state(o), "paid": pay_services.amount_paid(o),
                         "due": pay_services.amount_due(o), "payable": o.status in pay_services.PAYABLE_ORDER_STATUSES,
                         "pending": o.payments.filter(status=Payment.Status.PENDING).first()})
        produce = delivery = Decimal(0)
        for o in orders:
            for line in o.lines.all():
                if line.status in OrderLine.INACTIVE_STATUSES:
                    continue
                produce += (line.quantity * line.price_per_unit).quantize(Decimal("0.01"))
                delivery += line.delivery_cost
        payable_rows = [r for r in rows if r["due"] and r["payable"] and not r["pending"]]
        return render(request, "web/payments.html", {
            "expenses": {"produce": produce, "delivery": delivery, "total": produce + delivery,
                         "refunds": sum((pay_services.refund_due(o) for o in orders), Decimal(0))},
            "payable_rows": payable_rows,
            "rows": rows, "history": Payment.objects.filter(order__buyer=user).select_related("order")[:50],
            "total_due": sum((r["due"] for r in rows if r["payable"]), 0),
            "total_paid": sum((r["paid"] for r in rows), 0), "phone": user.phone,
            "provider": settings.PAYMENT_PROVIDER})
    if user.is_supplier:
        profile = user.supplier_profile
        payouts = list(Payout.objects.filter(supplier=profile).select_related("order_line__order"))
        upcoming = []
        for line in (OrderLine.objects.filter(supplier=profile, payout__isnull=True)
                     .exclude(status__in=OrderLine.INACTIVE_STATUSES).select_related("order")):
            fee, net = pay_services.split(line.total)
            upcoming.append({"line": line, "fee": fee, "net": net,
                             "buyer_paid": pay_services.is_paid(line.order)})
        return render(request, "web/payments.html", {
            "payouts": payouts, "upcoming": upcoming, "pct": pct,
            "received": sum((p.amount for p in payouts if p.status == Payout.Status.PAID), 0),
            "awaiting": sum((p.amount for p in payouts if p.status != Payout.Status.PAID), 0)
                        + sum((u["net"] for u in upcoming), 0),
            "commission": sum((p.fee for p in payouts), 0)})
    raise PermissionDenied


@buyer_required
@require_POST
def payments_pay(request):
    """The main Pay button on the Payments page: pay the chosen order by M-Pesa."""
    order = visible_orders(request.user).filter(pk=request.POST.get("order") or 0, buyer=request.user).first()
    if order is None:
        messages.error(request, "Choose an order to pay.")
        return redirect("web:payments")
    payment = run_action(request, pay_services.start_payment, "", request.user, order, request.POST.get("phone", ""))
    if payment is not None:
        _payment_message(request, payment)
    return redirect("web:payments")


@admin_required
def admin_payments(request):
    if request.method == "POST":
        payout = get_object_or_404(Payout, pk=request.POST.get("payout"))
        action = request.POST.get("action")
        if action == "send":
            run_action(request, pay_services.send_payout, "Payout sent.", request.user, payout)
        elif action == "record":
            run_action(request, pay_services.record_manual_payout, "Payout recorded as paid.", request.user, payout,
                       request.POST.get("receipt_number", ""), request.POST.get("note", ""))
        elif action == "release":
            run_action(request, pay_services.release_hold, "Payout released.", request.user, payout)
        return redirect("web:admin_payments")
    payouts = Payout.objects.select_related("supplier", "order_line__order")
    payments = Payment.objects.select_related("order", "payer")[:100]
    from apps.orders.models import Order
    refunds = [(o, pay_services.refund_due(o)) for o in Order.objects.filter(payments__status="succeeded").distinct()]
    held = sum((p.amount for p in Payment.objects.filter(status="succeeded")), 0) - sum(
        (p.gross_amount for p in Payout.objects.filter(status="paid")), 0)
    return render(request, "web/admin_payments.html", {
        "to_pay": payouts.filter(status__in=["ready", "failed", "on_hold", "processing"]),
        "paid": payouts.filter(status="paid")[:50], "payments": payments,
        "refunds": [(o, amt) for o, amt in refunds if amt], "held": held, "c": pay_services.commission_summary(),
        "payout_provider": settings.PAYOUT_PROVIDER, "payment_provider": settings.PAYMENT_PROVIDER})


@role_required("is_logistics", "is_supplier", "is_platform_admin")
def deliveries(request):
    qs = Delivery.objects.select_related("order_line__order", "order_line__supplier", "pickup_location",
                                         "dropoff_location")
    if not request.user.is_platform_admin:
        qs = qs.filter(Q(logistics_partner=request.user) | Q(order_line__supplier__user=request.user,
                                                              logistics_partner__isnull=True))
    return render(request, "web/deliveries.html", {"deliveries": qs, "form": forms.DeliveryUpdateForm()})


# --- Messages & notifications ------------------------------------------------------------

@login_required
def conversations(request):
    convos = Conversation.objects.filter(participants=request.user).prefetch_related("participants", "messages")
    return render(request, "web/conversations.html", {"convos": convos})


@login_required
def conversation(request, pk):
    convo = get_object_or_404(Conversation, pk=pk, participants=request.user)
    if request.method == "POST":
        run_action(request, post_message, "Sent.", request.user, convo, request.POST.get("body", ""))
        return redirect("web:conversation", pk=pk)
    msgs = convo.messages.select_related("sender")
    for m in msgs.exclude(read_by=request.user):
        m.read_by.add(request.user)
    return render(request, "web/conversation.html", {"convo": convo, "msgs": msgs})


@login_required
def notifications(request):
    qs = request.user.notifications.all()[:100]
    if request.method == "POST":
        request.user.notifications.filter(read_at__isnull=True).update(read_at=timezone.now())
        return redirect("web:notifications")
    return render(request, "web/notifications.html", {"notifications": qs})


@login_required
def notification_open(request, pk):
    n = get_object_or_404(request.user.notifications, pk=pk)
    n.read_at = n.read_at or timezone.now()
    n.save(update_fields=["read_at"])
    return redirect(n.link or "web:notifications")


# --- Admin dashboard -------------------------------------------------------------------------

@admin_required
def admin_dashboard(request):
    days = request.GET.get("days")
    metrics = platform_metrics(int(days) if days and days.isdigit() else None)
    recent_requests = SourcingRequest.objects.select_related("product", "buyer")[:8]
    return render(request, "web/admin_dashboard.html", {"m": metrics, "recent_requests": recent_requests,
                                                        "days": days, "c": pay_services.commission_summary(),
                                                        "payouts_due": Payout.objects.filter(
                                                            status__in=["ready", "failed", "on_hold"]).count()})


@admin_required
def admin_verification(request):
    status = request.GET.get("status", VerificationStatus.PENDING)
    groups = []
    for label, model in (("Suppliers", SupplierProfile), ("Buyers", BuyerProfile),
                         ("Logistics partners", LogisticsProfile)):
        qs = model.objects.select_related("user", "location").prefetch_related("user__documents")
        if status != "all":
            qs = qs.filter(verification_status=status)
        groups.append((label, model._meta.model_name, qs))
    return render(request, "web/admin_verification.html", {"groups": groups, "status": status,
                                                           "statuses": VerificationStatus.choices})


@admin_required
@require_POST
def admin_verify(request, kind, pk):
    model = {"supplierprofile": SupplierProfile, "buyerprofile": BuyerProfile,
             "logisticsprofile": LogisticsProfile}.get(kind)
    if model is None:
        raise Http404
    profile = get_object_or_404(model, pk=pk)
    status = request.POST.get("status")
    run_action(request, set_verification, f"{profile} marked {status}.", profile, request.user, status,
               request.POST.get("notes", ""))
    return back(request, "web:admin_verification")


@admin_required
def admin_requests(request):
    reqs = SourcingRequest.objects.select_related("product", "buyer", "delivery_location").prefetch_related(
        "matches", "quotations")
    status = request.GET.get("status")
    if status:
        reqs = reqs.filter(status=status)
    return render(request, "web/admin_requests.html", {"requests": reqs, "statuses": SourcingRequest.Status.choices,
                                                       "current": status})


@admin_required
def admin_request_detail(request, pk):
    req = get_object_or_404(SourcingRequest, pk=pk)
    if request.method == "POST":
        res = run_action(request, sourcing.rerun_matching, "Matching re-run.", request.user, req)
        if res:
            messages.info(request, f"{len(res.candidates)} matches, coverage {res.coverage:.0%}, "
                                   f"{len(res.excluded)} listings excluded.")
        return redirect("web:admin_request_detail", pk=pk)
    matches = req.matches.select_related("supplier", "inventory_item__location").order_by("rank")
    return render(request, "web/admin_request_detail.html", {"req": req, "matches": matches,
                                                             "comparison": sourcing.compare_offers(req)})


@admin_required
def admin_disputes(request):
    disputes = Dispute.objects.select_related("order", "raised_by", "order_line__supplier")
    return render(request, "web/admin_disputes.html", {"disputes": disputes})


@admin_required
@require_POST
def admin_dispute_resolve(request, pk):
    dispute = get_object_or_404(Dispute, pk=pk)
    run_action(request, order_services.resolve_dispute, "Dispute updated.", request.user, dispute,
               request.POST.get("resolution", ""), request.POST.get("status", Dispute.Status.RESOLVED))
    return redirect("web:admin_disputes")


@admin_required
def admin_dispute_detail(request, pk):
    return redirect("web:order_detail", pk=get_object_or_404(Dispute, pk=pk).order_id)


@admin_required
def admin_config(request):
    config = MatchingConfig.get()
    form = forms.MatchingConfigForm(request.POST or None, instance=config)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.updated_by = request.user
        obj.save()
        log_action(request.user, "config.matching_updated", obj, changed=form.changed_data)
        messages.success(request, "Matching configuration saved.")
        return redirect("web:admin_config")
    return render(request, "web/admin_config.html", {"form": form, "weights": config.weights()})


@admin_required
def admin_audit(request):
    logs = AuditLog.objects.select_related("actor")
    q = request.GET.get("q")
    if q:
        logs = logs.filter(Q(action__icontains=q) | Q(target_repr__icontains=q) | Q(actor__username__icontains=q))
    return render(request, "web/admin_audit.html", {"logs": logs[:300], "q": q or ""})
