from django.db.models import Q
from django.http import FileResponse, Http404
from apps.core.files import open_stored
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, parsers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from apps.core.permissions import IsBuyer, IsPlatformAdmin
from apps.sourcing.models import SourcingRequest

from . import services
from .models import Delivery, Dispute, Inspection, Order, OrderLine, SaleRecord
from .serializers import (AssignLogisticsSerializer, CreateOrderSerializer, DeliverySerializer,
                          DeliveryUpdateSerializer, DisputeSerializer, InspectionSerializer, OrderLineSerializer,
                          OrderSerializer, ReconfirmSerializer, ResolveDisputeSerializer, SaleRecordSerializer)


def visible_orders(user):
    qs = Order.objects.select_related("sourcing_request__product", "delivery_location").prefetch_related(
        "lines__supplier", "lines__delivery", "lines__inspections", "events__actor")
    if user.is_platform_admin:
        return qs
    if user.is_buyer:
        return qs.filter(buyer=user)
    if user.is_supplier:
        return qs.filter(lines__supplier__user=user).distinct()
    if user.is_logistics:
        return qs.filter(lines__delivery__logistics_partner=user).distinct()
    return qs.none()


def visible_sales(user):
    """Purchase history for buyers, sales history for suppliers, everything for admins."""
    qs = SaleRecord.objects.select_related("inventory_item", "supplier", "buyer")
    if user.is_platform_admin:
        return qs
    if user.is_buyer:
        return qs.filter(buyer=user)
    if user.is_supplier:
        return qs.filter(supplier__user=user)
    return qs.none()


class SaleHistoryViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Completed purchases. Buyers see what they bought, suppliers what they sold."""
    serializer_class = SaleRecordSerializer
    filterset_fields = ("product_name", "supplier", "buyer")
    search_fields = ("product_name", "variety_name", "order_reference", "supplier_name", "buyer_name")
    ordering_fields = ("completed_at", "total")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return SaleRecord.objects.none()
        return visible_sales(self.request.user)


class OrderViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    serializer_class = OrderSerializer
    filterset_fields = ("status", "sourcing_request")
    ordering_fields = ("created_at",)

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Order.objects.none()
        return visible_orders(self.request.user)

    @extend_schema(request=CreateOrderSerializer, responses={201: OrderSerializer})
    def create(self, request):
        """Place an order from accepted quotations (one or several suppliers)."""
        if not IsBuyer().has_permission(request, self):
            raise PermissionDenied(IsBuyer.message)
        ser = CreateOrderSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        req = get_object_or_404(SourcingRequest, pk=ser.validated_data["sourcing_request"], buyer=request.user)
        order = services.create_order(request.user, req, ser.validated_data["quotations"],
                                      ser.validated_data.get("notes", ""))
        return Response(OrderSerializer(order).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=None)
    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        order = services.cancel_order(request.user, self.get_object(), request.data.get("reason", ""))
        return Response(OrderSerializer(order).data)

    @extend_schema(request=DisputeSerializer, responses=DisputeSerializer)
    @action(detail=True, methods=["post"])
    def dispute(self, request, pk=None):
        order = self.get_object()
        line = None
        if request.data.get("order_line"):
            line = get_object_or_404(OrderLine, pk=request.data["order_line"], order=order)
        d = services.raise_dispute(request.user, order, request.data.get("reason", ""), line)
        return Response(DisputeSerializer(d).data, status=status.HTTP_201_CREATED)


class OrderLineViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Individual supply agreements (one per supplier on an order)."""

    serializer_class = OrderLineSerializer
    filterset_fields = ("status", "order")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return OrderLine.objects.none()
        user = self.request.user
        qs = OrderLine.objects.select_related("order", "supplier", "quotation", "delivery").prefetch_related(
            "inspections")
        if user.is_platform_admin:
            return qs
        return qs.filter(Q(order__buyer=user) | Q(supplier__user=user) | Q(delivery__logistics_partner=user))

    @extend_schema(request=ReconfirmSerializer)
    @action(detail=True, methods=["post"])
    def reconfirm(self, request, pk=None):
        """Supplier reconfirms availability; stock is reserved."""
        ser = ReconfirmSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        line = services.reconfirm_line(request.user, self.get_object(), ser.validated_data["available"],
                                       ser.validated_data.get("note", ""))
        return Response(self.get_serializer(line).data)

    @extend_schema(request=InspectionSerializer, responses=InspectionSerializer)
    @action(detail=True, methods=["post"], parser_classes=[parsers.JSONParser, parsers.MultiPartParser])
    def inspect(self, request, pk=None):
        """Record a quality inspection (pre-dispatch or on receipt)."""
        line = self.get_object()
        data = request.data.copy()
        data["order_line"] = line.pk
        ser = InspectionSerializer(data=data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        insp = services.record_inspection(
            request.user, line, d.get("stage", Inspection.Stage.PRE_DISPATCH), d["result"],
            d.get("grade_observed", ""), d.get("measurements"), d.get("accepted_quantity"),
            d.get("rejected_quantity"), d.get("notes", ""), d.get("report"))
        return Response(InspectionSerializer(insp).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=None)
    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        """Buyer confirms receipt and closes this supply agreement."""
        line = services.complete_line(request.user, self.get_object(), request.data.get("note", ""))
        return Response(self.get_serializer(line).data)


class DeliveryViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    serializer_class = DeliverySerializer
    filterset_fields = ("status",)

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Delivery.objects.none()
        user = self.request.user
        qs = Delivery.objects.select_related("order_line__order", "order_line__supplier", "pickup_location",
                                             "dropoff_location", "logistics_partner")
        if user.is_platform_admin:
            return qs
        return qs.filter(Q(logistics_partner=user) | Q(order_line__order__buyer=user) |
                         Q(order_line__supplier__user=user))

    @extend_schema(request=AssignLogisticsSerializer)
    @action(detail=True, methods=["post"])
    def assign(self, request, pk=None):
        ser = AssignLogisticsSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = services.assign_logistics(request.user, self.get_object(), ser.validated_data["logistics_partner"],
                                      ser.validated_data.get("scheduled_date"))
        return Response(self.get_serializer(d).data)

    @extend_schema(request=DeliveryUpdateSerializer)
    @action(detail=True, methods=["post"], parser_classes=[parsers.JSONParser, parsers.MultiPartParser])
    def update_status(self, request, pk=None):
        ser = DeliveryUpdateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = services.update_delivery(request.user, self.get_object(), **ser.validated_data)
        return Response(self.get_serializer(d).data)

    @action(detail=True, methods=["get"], url_path="proof")
    def proof(self, request, pk=None):
        d = self.get_object()
        if not d.proof_of_delivery:
            raise Http404
        return FileResponse(open_stored(d.proof_of_delivery), as_attachment=True)


class InspectionViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = InspectionSerializer
    filterset_fields = ("result", "stage", "order_line")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Inspection.objects.none()
        user = self.request.user
        qs = Inspection.objects.select_related("order_line__order", "inspector")
        if user.is_platform_admin:
            return qs
        return qs.filter(Q(order_line__order__buyer=user) | Q(order_line__supplier__user=user))

    @action(detail=True, methods=["get"])
    def report(self, request, pk=None):
        insp = self.get_object()
        if not insp.report:
            raise Http404
        return FileResponse(open_stored(insp.report), as_attachment=True)


class DisputeViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = DisputeSerializer
    filterset_fields = ("status",)

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Dispute.objects.none()
        user = self.request.user
        qs = Dispute.objects.select_related("order", "raised_by")
        return qs if user.is_platform_admin else qs.filter(Q(raised_by=user) | Q(order__buyer=user) |
                                                           Q(order__lines__supplier__user=user)).distinct()

    @extend_schema(request=ResolveDisputeSerializer)
    @action(detail=True, methods=["post"], permission_classes=[IsPlatformAdmin])
    def resolve(self, request, pk=None):
        ser = ResolveDisputeSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = services.resolve_dispute(request.user, self.get_object(), ser.validated_data["resolution"],
                                     ser.validated_data["status"])
        return Response(self.get_serializer(d).data)
