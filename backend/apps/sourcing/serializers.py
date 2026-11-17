from django.utils import timezone
from rest_framework import serializers

from apps.catalog.models import InventoryItem
from apps.core.models import Location
from apps.core.serializers import LocationSerializer

from .models import Match, Quotation, SourcingRequest


class SourcingRequestSerializer(serializers.ModelSerializer):
    """Send either `product` (id) or `product_name` (any typed name); likewise `variety` or `variety_name`."""

    product_name = serializers.CharField(required=False, max_length=120)
    variety_name = serializers.CharField(required=False, allow_blank=True, allow_null=True, max_length=80)
    buyer_name = serializers.CharField(source="buyer.display_name", read_only=True)
    delivery_location = LocationSerializer(read_only=True)
    delivery_location_id = serializers.PrimaryKeyRelatedField(queryset=Location.objects.all(),
                                                              source="delivery_location", write_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    match_count = serializers.IntegerField(source="matches.count", read_only=True)
    quotation_count = serializers.IntegerField(source="quotations.count", read_only=True)

    class Meta:
        model = SourcingRequest
        fields = ("id", "reference", "buyer", "buyer_name", "product", "product_name", "variety", "variety_name",
                  "quantity", "unit", "delivery_location", "delivery_location_id", "delivery_deadline",
                  "min_quality_grade", "quality_specifications", "quality_notes", "intended_application",
                  "packaging_requirements", "target_budget", "delivery_arrangement", "status", "status_display",
                  "match_count", "quotation_count", "last_matched_at", "created_at")
        read_only_fields = ("reference", "buyer", "status", "last_matched_at", "created_at")
        extra_kwargs = {"product": {"required": False}}

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["product_name"] = instance.product.name
        data["variety_name"] = instance.variety.name if instance.variety_id else None
        return data

    def to_internal_value(self, data):
        # Report a missing product together with any other field errors.
        missing = not data.get("product") and not str(data.get("product_name") or "").strip()
        try:
            return super().to_internal_value(data)
        except serializers.ValidationError as exc:
            if missing and isinstance(exc.detail, dict):
                exc.detail.setdefault("product", [serializers.ErrorDetail(
                    "Give the product: `product` (id) or `product_name`.", code="required")])
            raise

    def validate(self, attrs):
        from apps.catalog.services import clean_name, find_product
        name = clean_name(attrs.get("product_name"))
        if not attrs.get("product") and len(name) < 2:
            raise serializers.ValidationError({"product": "Give the product: `product` (id) or `product_name`."})
        if not attrs.get("product"):
            existing = find_product(name)
            if existing and not existing.is_active:
                raise serializers.ValidationError({"product_name": f"{existing.name} is not currently traded."})
        variety = attrs.get("variety")
        if variety and attrs.get("product") and variety.product_id != attrs["product"].pk:
            raise serializers.ValidationError({"variety": "This variety does not belong to the selected product."})
        return attrs

    def resolved_data(self, user):
        """validated_data with typed names turned into catalogue entries."""
        from apps.catalog.services import resolve_product, resolve_variety
        data = dict(self.validated_data)
        product_name, variety_name = data.pop("product_name", ""), data.pop("variety_name", "")
        if not data.get("product"):
            data["product"], _ = resolve_product(product_name, user)
        if not data.get("variety") and variety_name:
            data["variety"] = resolve_variety(data["product"], variety_name)
        return data

    def validate_delivery_deadline(self, value):
        if value < timezone.localdate():
            raise serializers.ValidationError("The delivery deadline cannot be in the past.")
        return value

    def validate_quality_specifications(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError('Provide an object, e.g. {"min_size_mm": 70}.')
        return value


class MatchSerializer(serializers.ModelSerializer):
    supplier_name = serializers.CharField(source="supplier.business_name", read_only=True)
    supplier_user_id = serializers.IntegerField(source="supplier.user_id", read_only=True)
    supplier_type = serializers.CharField(source="supplier.user.get_role_display", read_only=True)
    supplier_rating = serializers.DecimalField(source="supplier.average_rating", max_digits=3, decimal_places=2,
                                               read_only=True)
    location = LocationSerializer(source="inventory_item.location", read_only=True)
    quality_grade = serializers.CharField(source="inventory_item.quality_grade", read_only=True)
    variety = serializers.CharField(source="inventory_item.variety", read_only=True, default=None)
    indicative_price = serializers.DecimalField(source="inventory_item.price_per_unit", max_digits=12,
                                                decimal_places=2, read_only=True)
    request_reference = serializers.CharField(source="sourcing_request.reference", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = Match
        fields = ("id", "sourcing_request", "request_reference", "rank", "score", "score_breakdown", "status",
                  "status_display", "supplier", "supplier_user_id", "supplier_name", "supplier_type",
                  "supplier_rating", "inventory_item", "variety", "quality_grade", "indicative_price", "location",
                  "distance_km", "available_quantity", "suggested_quantity", "confirmed_quantity", "supplier_note",
                  "notified_at", "responded_at")
        read_only_fields = fields


class MatchingSummarySerializer(serializers.Serializer):
    requested_quantity = serializers.FloatField()
    planned_quantity = serializers.FloatField()
    coverage = serializers.FloatField()
    fully_covered = serializers.BooleanField()
    suppliers_needed = serializers.IntegerField()
    excluded_count = serializers.SerializerMethodField()

    def get_excluded_count(self, obj) -> int:
        return len(obj.excluded)


class MatchResponseSerializer(serializers.Serializer):
    available = serializers.BooleanField()
    quantity = serializers.DecimalField(max_digits=12, decimal_places=2, required=False)
    note = serializers.CharField(required=False, allow_blank=True)


class NotifySuppliersSerializer(serializers.Serializer):
    match_ids = serializers.ListField(child=serializers.IntegerField(), required=False)
    message = serializers.CharField(required=False, allow_blank=True)


class ContactSupplierSerializer(serializers.Serializer):
    supplier_user_id = serializers.IntegerField()
    message = serializers.CharField()


class QuotationSerializer(serializers.ModelSerializer):
    supplier_name = serializers.CharField(source="supplier.business_name", read_only=True)
    request_reference = serializers.CharField(source="sourcing_request.reference", read_only=True)
    total = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    goods_total = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    inventory_item = serializers.PrimaryKeyRelatedField(queryset=InventoryItem.objects.all())

    class Meta:
        model = Quotation
        fields = ("id", "reference", "sourcing_request", "request_reference", "match", "supplier", "supplier_name",
                  "inventory_item", "quantity", "price_per_unit", "delivery_cost", "includes_delivery",
                  "quality_grade", "earliest_delivery_date", "valid_until", "notes", "status", "status_display",
                  "revision", "counter_price_per_unit", "counter_quantity", "counter_note", "goods_total", "total",
                  "history", "decided_at", "created_at")
        read_only_fields = ("reference", "match", "supplier", "quality_grade", "status", "revision",
                            "counter_price_per_unit", "counter_quantity", "counter_note", "history", "decided_at",
                            "created_at")


class CounterSerializer(serializers.Serializer):
    price_per_unit = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, min_value=0)
    quantity = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, min_value=0)
    note = serializers.CharField(required=False, allow_blank=True)


class ReviseSerializer(CounterSerializer):
    delivery_cost = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, min_value=0)
    accept_counter = serializers.BooleanField(default=False)


class ReasonSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True)


class OfferRowSerializer(serializers.Serializer):
    quotation = QuotationSerializer()
    landed_unit_cost = serializers.DecimalField(max_digits=12, decimal_places=2)
    match_score = serializers.DecimalField(max_digits=5, decimal_places=4, allow_null=True)
    distance_km = serializers.DecimalField(max_digits=8, decimal_places=1, allow_null=True)
    reliability = serializers.DecimalField(max_digits=4, decimal_places=3, allow_null=True)
    average_rating = serializers.DecimalField(max_digits=3, decimal_places=2, allow_null=True)


class PlanRowSerializer(serializers.Serializer):
    quotation = serializers.CharField(source="quotation.reference")
    supplier = serializers.CharField(source="quotation.supplier.business_name")
    quantity = serializers.DecimalField(max_digits=12, decimal_places=2)


class OfferComparisonSerializer(serializers.Serializer):
    offers = OfferRowSerializer(many=True)
    cheapest_plan = PlanRowSerializer(many=True)
    cheapest_plan_quantity = serializers.DecimalField(max_digits=12, decimal_places=2)
    cheapest_plan_cost = serializers.DecimalField(max_digits=14, decimal_places=2)
    within_budget = serializers.BooleanField(allow_null=True)
