from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.accounts.models import User
from apps.comms.serializers import ConversationSerializer
from apps.comms.services import start_conversation
from apps.core.permissions import IsBuyer, IsSupplier

from . import services
from .models import Match, Quotation, SourcingRequest
from .serializers import (ContactSupplierSerializer, CounterSerializer, MatchingSummarySerializer,
                          MatchResponseSerializer, MatchSerializer, NotifySuppliersSerializer,
                          OfferComparisonSerializer, QuotationSerializer, ReasonSerializer, ReviseSerializer,
                          SourcingRequestSerializer)


def visible_requests(user):
    qs = SourcingRequest.objects.select_related("product", "variety", "delivery_location", "buyer")
    if user.is_platform_admin:
        return qs
    if user.is_buyer:
        return qs.filter(buyer=user)
    if user.is_supplier:
        return qs.filter(Q(matches__supplier__user=user) | Q(quotations__supplier__user=user)).distinct()
    return qs.none()


class SourcingRequestViewSet(mixins.CreateModelMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin,
                             viewsets.GenericViewSet):
    serializer_class = SourcingRequestSerializer
    filterset_fields = ("status", "product", "delivery_location__county")
    search_fields = ("reference", "product__name", "intended_application")
    ordering_fields = ("created_at", "delivery_deadline", "quantity")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return SourcingRequest.objects.none()
        return visible_requests(self.request.user)

    def get_permissions(self):
        if self.action == "create":
            return [IsBuyer()]
        return super().get_permissions()

    @extend_schema(responses={201: SourcingRequestSerializer})
    def create(self, request, *args, **kwargs):
        """Submit a sourcing request. Matching runs immediately."""
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        with transaction.atomic():
            obj, result = services.create_sourcing_request(request.user, **ser.resolved_data(request.user))
        data = self.get_serializer(obj).data
        data["matching"] = MatchingSummarySerializer(result).data
        return Response(data, status=status.HTTP_201_CREATED)

    @extend_schema(request=None, responses=MatchingSummarySerializer)
    @action(detail=True, methods=["post"])
    def rematch(self, request, pk=None):
        """Re-run the matching engine (e.g. after new inventory is listed)."""
        result = services.rerun_matching(request.user, self.get_object())
        return Response(MatchingSummarySerializer(result).data)

    @extend_schema(responses=MatchSerializer(many=True))
    @action(detail=True, methods=["get"])
    def matches(self, request, pk=None):
        """Ranked supplier matches with score breakdown and suggested allocation."""
        req = self.get_object()
        qs = req.matches.select_related("supplier__user", "inventory_item__location", "inventory_item__variety")
        if request.user.is_supplier:
            qs = qs.filter(supplier__user=request.user)
        return Response(MatchSerializer(qs.exclude(status=Match.Status.EXPIRED), many=True).data)

    @extend_schema(request=NotifySuppliersSerializer, responses=MatchSerializer(many=True))
    @action(detail=True, methods=["post"], url_path="notify-suppliers")
    def notify_suppliers(self, request, pk=None):
        """Ask matched suppliers to confirm availability (all potential matches if no ids given)."""
        ser = NotifySuppliersSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        matches = services.notify_matched_suppliers(request.user, self.get_object(),
                                                    ser.validated_data.get("match_ids"),
                                                    ser.validated_data.get("message", ""))
        return Response(MatchSerializer(matches, many=True).data)

    @extend_schema(responses=OfferComparisonSerializer)
    @action(detail=True, methods=["get"])
    def offers(self, request, pk=None):
        """Compare supplier quotations by landed cost, with the cheapest plan covering the quantity."""
        req = self.get_object()
        if not (request.user.is_platform_admin or req.buyer_id == request.user.pk):
            return Response(status=status.HTTP_403_FORBIDDEN)
        return Response(OfferComparisonSerializer(services.compare_offers(req)).data)

    @extend_schema(request=ContactSupplierSerializer, responses=ConversationSerializer)
    @action(detail=True, methods=["post"])
    def contact(self, request, pk=None):
        """Open (or reuse) a conversation with a matched supplier about this request."""
        req = self.get_object()
        ser = ContactSupplierSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        other = get_object_or_404(User, pk=ser.validated_data["supplier_user_id"])
        convo = start_conversation(request.user, other, f"{req.reference} — {req.product.name}",
                                   sourcing_request=req, first_message=ser.validated_data["message"])
        return Response(ConversationSerializer(convo, context={"request": request}).data,
                        status=status.HTTP_201_CREATED)

    @extend_schema(request=ReasonSerializer, responses=SourcingRequestSerializer)
    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        ser = ReasonSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        req = services.cancel_sourcing_request(request.user, self.get_object(), ser.validated_data.get("reason", ""))
        return Response(self.get_serializer(req).data)


class MatchViewSet(viewsets.ReadOnlyModelViewSet):
    """Suppliers: your sourcing opportunities. Buyers: matches on your requests."""

    serializer_class = MatchSerializer
    filterset_fields = ("status", "sourcing_request")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Match.objects.none()
        user = self.request.user
        qs = Match.objects.select_related("sourcing_request", "supplier__user", "inventory_item__location",
                                          "inventory_item__variety")
        if user.is_platform_admin:
            return qs
        if user.is_supplier:
            return qs.filter(supplier__user=user).exclude(status=Match.Status.EXPIRED)
        if user.is_buyer:
            return qs.filter(sourcing_request__buyer=user)
        return qs.none()

    @extend_schema(request=MatchResponseSerializer, responses=MatchSerializer)
    @action(detail=True, methods=["post"], permission_classes=[IsSupplier])
    def respond(self, request, pk=None):
        """Supplier confirms availability (with quantity) or declines."""
        ser = MatchResponseSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        match = services.respond_to_match(request.user, self.get_object(), ser.validated_data["available"],
                                          ser.validated_data.get("quantity"), ser.validated_data.get("note", ""))
        return Response(MatchSerializer(match).data)


class QuotationViewSet(mixins.CreateModelMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin,
                       viewsets.GenericViewSet):
    serializer_class = QuotationSerializer
    filterset_fields = ("status", "sourcing_request", "supplier")
    ordering_fields = ("created_at", "price_per_unit")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Quotation.objects.none()
        user = self.request.user
        qs = Quotation.objects.select_related("supplier", "sourcing_request", "inventory_item")
        if user.is_platform_admin:
            return qs
        if user.is_supplier:
            return qs.filter(supplier__user=user)
        if user.is_buyer:
            return qs.filter(sourcing_request__buyer=user)
        return qs.none()

    def get_permissions(self):
        if self.action in {"create", "revise", "withdraw"}:
            return [IsSupplier()]
        if self.action in {"counter", "accept", "reject"}:
            return [IsBuyer()]
        return super().get_permissions()

    def create(self, request, *args, **kwargs):
        """Supplier submits a quotation against a sourcing request they can see."""
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        req = get_object_or_404(SourcingRequest, pk=data["sourcing_request"].pk)
        quote = services.submit_quotation(
            request.user, req, data["inventory_item"], data["quantity"], data["price_per_unit"],
            data["earliest_delivery_date"], data["valid_until"], data.get("delivery_cost", 0),
            data.get("includes_delivery", False), data.get("notes", ""))
        return Response(self.get_serializer(quote).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=CounterSerializer)
    @action(detail=True, methods=["post"])
    def counter(self, request, pk=None):
        ser = CounterSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        q = services.counter_quotation(request.user, self.get_object(), ser.validated_data.get("price_per_unit"),
                                       ser.validated_data.get("quantity"), ser.validated_data.get("note", ""))
        return Response(self.get_serializer(q).data)

    @extend_schema(request=ReviseSerializer)
    @action(detail=True, methods=["post"])
    def revise(self, request, pk=None):
        ser = ReviseSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        q = services.revise_quotation(request.user, self.get_object(), d.get("price_per_unit"), d.get("quantity"),
                                      d.get("delivery_cost"), d.get("note", ""), d.get("accept_counter", False))
        return Response(self.get_serializer(q).data)

    @extend_schema(request=None)
    @action(detail=True, methods=["post"])
    def accept(self, request, pk=None):
        return Response(self.get_serializer(services.accept_quotation(request.user, self.get_object())).data)

    @extend_schema(request=ReasonSerializer)
    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        reason = request.data.get("reason", "")
        return Response(self.get_serializer(services.reject_quotation(request.user, self.get_object(), reason)).data)

    @extend_schema(request=None)
    @action(detail=True, methods=["post"])
    def withdraw(self, request, pk=None):
        return Response(self.get_serializer(services.withdraw_quotation(request.user, self.get_object())).data)
