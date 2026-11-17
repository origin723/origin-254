from rest_framework import serializers

from apps.accounts.models import User
from apps.core.serializers import LocationSerializer
from apps.sourcing.models import Quotation

from .models import Delivery, Dispute, Inspection, Order, OrderEvent, OrderLine, SaleRecord


class InspectionSerializer(serializers.ModelSerializer):
    inspector_name = serializers.CharField(source="inspector.display_name", read_only=True)

    class Meta:
        model = Inspection
        fields = ("id", "order_line", "stage", "inspector", "inspector_name", "inspected_at", "result",
                  "grade_observed", "measurements", "accepted_quantity", "rejected_quantity", "notes", "report")
        read_only_fields = ("inspector", "inspected_at")
        extra_kwargs = {"report": {"required": False}}


class DeliverySerializer(serializers.ModelSerializer):
    pickup_location = LocationSerializer(read_only=True)
    dropoff_location = LocationSerializer(read_only=True)
    logistics_partner_name = serializers.CharField(source="logistics_partner.display_name", read_only=True,
                                                   default=None)
    order_reference = serializers.CharField(source="order_line.order.reference", read_only=True)
    supplier_name = serializers.CharField(source="order_line.supplier.business_name", read_only=True)
    quantity = serializers.DecimalField(source="order_line.quantity", max_digits=12, decimal_places=2,
                                        read_only=True)

    class Meta:
        model = Delivery
        fields = ("id", "order_line", "order_reference", "supplier_name", "quantity", "logistics_partner",
                  "logistics_partner_name", "pickup_location", "dropoff_location", "status", "scheduled_date",
                  "vehicle_reg", "driver_name", "driver_phone", "picked_up_at", "delivered_at", "received_quantity",
                  "received_by", "tracking_log")
        read_only_fields = fields


class OrderLineSerializer(serializers.ModelSerializer):
    supplier_name = serializers.CharField(source="supplier.business_name", read_only=True)
    quotation_reference = serializers.CharField(source="quotation.reference", read_only=True)
    total = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    delivery = DeliverySerializer(read_only=True)
    inspections = InspectionSerializer(many=True, read_only=True)

    class Meta:
        model = OrderLine
        fields = ("id", "supplier", "supplier_name", "quotation", "quotation_reference", "quantity", "unit",
                  "price_per_unit", "delivery_cost", "total", "quality_grade", "quality_specifications",
                  "agreed_delivery_date", "status", "status_display", "reconfirmed_at", "delivered_quantity",
                  "completed_at", "delivery", "inspections")
        read_only_fields = fields


class OrderEventSerializer(serializers.ModelSerializer):
    actor = serializers.CharField(source="actor.display_name", default=None)

    class Meta:
        model = OrderEvent
        fields = ("id", "event", "note", "actor", "order_line", "created_at")


class OrderSerializer(serializers.ModelSerializer):
    lines = OrderLineSerializer(many=True, read_only=True)
    events = OrderEventSerializer(many=True, read_only=True)
    request_reference = serializers.CharField(source="sourcing_request.reference", read_only=True)
    product = serializers.CharField(source="sourcing_request.product.name", read_only=True)
    delivery_location = LocationSerializer(read_only=True)
    total = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    total_quantity = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = Order
        fields = ("id", "reference", "sourcing_request", "request_reference", "product", "buyer", "status",
                  "status_display", "delivery_location", "notes", "total", "total_quantity", "lines", "events",
                  "created_at", "completed_at")
        read_only_fields = fields


class CreateOrderSerializer(serializers.Serializer):
    sourcing_request = serializers.IntegerField()
    quotations = serializers.PrimaryKeyRelatedField(queryset=Quotation.objects.all(), many=True)
    notes = serializers.CharField(required=False, allow_blank=True)


class ReconfirmSerializer(serializers.Serializer):
    available = serializers.BooleanField()
    note = serializers.CharField(required=False, allow_blank=True)


class AssignLogisticsSerializer(serializers.Serializer):
    logistics_partner = serializers.PrimaryKeyRelatedField(queryset=User.objects.filter(role="logistics"))
    scheduled_date = serializers.DateField(required=False)


class DeliveryUpdateSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=Delivery.Status.choices)
    note = serializers.CharField(required=False, allow_blank=True)
    received_quantity = serializers.DecimalField(max_digits=12, decimal_places=2, required=False)
    received_by = serializers.CharField(required=False, allow_blank=True)
    vehicle_reg = serializers.CharField(required=False, allow_blank=True)
    driver_name = serializers.CharField(required=False, allow_blank=True)
    driver_phone = serializers.CharField(required=False, allow_blank=True)
    proof_of_delivery = serializers.FileField(required=False)


class DisputeSerializer(serializers.ModelSerializer):
    raised_by_name = serializers.CharField(source="raised_by.display_name", read_only=True)
    order_reference = serializers.CharField(source="order.reference", read_only=True)

    class Meta:
        model = Dispute
        fields = ("id", "order", "order_reference", "order_line", "raised_by", "raised_by_name", "reason",
                  "status", "resolution", "resolved_by", "resolved_at", "created_at")
        read_only_fields = ("raised_by", "status", "resolution", "resolved_by", "resolved_at", "created_at")


class ResolveDisputeSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=[Dispute.Status.UNDER_REVIEW, Dispute.Status.RESOLVED,
                                              Dispute.Status.REJECTED])
    resolution = serializers.CharField()


class SaleRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = SaleRecord
        fields = ("id", "order", "order_reference", "order_line", "buyer", "buyer_name", "supplier", "supplier_name",
                  "inventory_item", "product_name", "variety_name", "quality_grade", "quantity", "unit",
                  "price_per_unit", "delivery_cost", "total", "delivered_to", "completed_at")
        read_only_fields = fields
