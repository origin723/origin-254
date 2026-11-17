from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.permissions import IsBuyer, IsPlatformAdmin
from apps.orders.api import visible_orders

from . import services
from .models import Payment, Payout
from .serializers import PaymentSerializer, PayoutSerializer, RecordPayoutSerializer, StartPaymentSerializer


class PaymentViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Buyer payments by M-Pesa. Buyers see their own; admins see all."""
    serializer_class = PaymentSerializer
    filterset_fields = ("status", "order")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Payment.objects.none()
        qs = Payment.objects.select_related("order")
        user = self.request.user
        if user.is_platform_admin:
            return qs
        return qs.filter(order__buyer=user)

    def get_permissions(self):
        if self.action == "create":
            return [IsBuyer()]
        return super().get_permissions()

    @extend_schema(request=StartPaymentSerializer, responses={201: PaymentSerializer})
    def create(self, request, *args, **kwargs):
        """Send an M-Pesa payment prompt to the buyer's phone for the order's outstanding amount."""
        ser = StartPaymentSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        order = get_object_or_404(visible_orders(request.user), pk=ser.validated_data["order"])
        payment = services.start_payment(request.user, order, ser.validated_data["phone"])
        return Response(PaymentSerializer(payment).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=None, responses=PaymentSerializer)
    @action(detail=True, methods=["post"])
    def refresh(self, request, pk=None):
        """Ask M-Pesa for the result of a pending payment."""
        payment = services.refresh_payment(request.user, self.get_object())
        return Response(PaymentSerializer(payment).data)


class PayoutViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Supplier payouts. Suppliers see their own; admins see and act on all."""
    serializer_class = PayoutSerializer
    filterset_fields = ("status",)

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Payout.objects.none()
        qs = Payout.objects.select_related("order_line__order", "supplier")
        user = self.request.user
        if user.is_platform_admin:
            return qs
        if user.is_supplier:
            return qs.filter(supplier__user=user)
        return qs.none()

    @extend_schema(request=None, responses=PayoutSerializer)
    @action(detail=True, methods=["post"], permission_classes=[IsPlatformAdmin])
    def send(self, request, pk=None):
        """Send the payout to the supplier with M-Pesa B2C."""
        return Response(PayoutSerializer(services.send_payout(request.user, self.get_object())).data)

    @extend_schema(request=RecordPayoutSerializer, responses=PayoutSerializer)
    @action(detail=True, methods=["post"], permission_classes=[IsPlatformAdmin])
    def record(self, request, pk=None):
        """Record a payout made outside the system (transaction code required)."""
        ser = RecordPayoutSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        payout = services.record_manual_payout(request.user, self.get_object(), ser.validated_data["receipt_number"],
                                               ser.validated_data.get("note", ""))
        return Response(PayoutSerializer(payout).data)

    @extend_schema(request=None, responses=PayoutSerializer)
    @action(detail=True, methods=["post"], permission_classes=[IsPlatformAdmin])
    def release(self, request, pk=None):
        """Release a payout that was held because of a dispute."""
        return Response(PayoutSerializer(services.release_hold(request.user, self.get_object())).data)
