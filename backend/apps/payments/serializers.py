from rest_framework import serializers

from .models import Payment, Payout


class PaymentSerializer(serializers.ModelSerializer):
    order_reference = serializers.CharField(source="order.reference", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = Payment
        fields = ("id", "reference", "order", "order_reference", "amount", "currency", "method", "provider", "phone",
                  "status", "status_display", "receipt_number", "result_description", "paid_at", "created_at")
        read_only_fields = fields


class StartPaymentSerializer(serializers.Serializer):
    order = serializers.IntegerField()
    phone = serializers.CharField(max_length=20, help_text="M-Pesa number, e.g. 0712345678")


class PayoutSerializer(serializers.ModelSerializer):
    order_reference = serializers.CharField(source="order_line.order.reference", read_only=True)
    supplier_name = serializers.CharField(source="supplier.business_name", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = Payout
        fields = ("id", "reference", "order_line", "order_reference", "supplier", "supplier_name", "gross_amount",
                  "fee", "amount", "phone", "status", "status_display", "method", "receipt_number",
                  "result_description", "paid_at", "created_at")
        read_only_fields = fields


class RecordPayoutSerializer(serializers.Serializer):
    receipt_number = serializers.CharField(max_length=40)
    note = serializers.CharField(max_length=255, required=False, allow_blank=True)
