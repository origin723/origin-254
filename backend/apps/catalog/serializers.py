from django.utils import timezone
from rest_framework import serializers

from apps.core.models import Location
from apps.core.serializers import LocationSerializer

from .models import Category, InventoryItem, InventoryPhoto, Product, ProductVariety


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ("id", "name", "slug", "description")
        read_only_fields = ("slug",)


class VarietySerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductVariety
        fields = ("id", "product", "name")


class ProductSerializer(serializers.ModelSerializer):
    varieties = VarietySerializer(many=True, read_only=True)
    category_name = serializers.CharField(source="category.name", read_only=True)

    class Meta:
        model = Product
        fields = ("id", "name", "slug", "category", "category_name", "default_unit", "perishable",
                  "shelf_life_days", "quality_parameters", "is_active", "varieties")
        read_only_fields = ("slug",)


class InventoryPhotoSerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField()

    class Meta:
        model = InventoryPhoto
        fields = ("id", "caption", "url", "created_at")

    def get_url(self, obj) -> str:
        return f"/app/photos/{obj.pk}/"


class InventorySerializer(serializers.ModelSerializer):
    photos = InventoryPhotoSerializer(many=True, read_only=True)
    photos_missing = serializers.IntegerField(read_only=True,
                                              help_text="Photos still needed before buyers can see this listing")
    product_name = serializers.CharField(source="product.name", read_only=True)
    variety_name = serializers.CharField(source="variety.name", read_only=True, default=None)
    supplier_name = serializers.CharField(source="supplier.business_name", read_only=True)
    location = LocationSerializer(read_only=True)
    location_id = serializers.PrimaryKeyRelatedField(queryset=Location.objects.all(), source="location",
                                                     write_only=True, required=False)
    is_stale = serializers.BooleanField(read_only=True)

    class Meta:
        model = InventoryItem
        fields = ("id", "supplier", "supplier_name", "product", "product_name", "variety", "variety_name",
                  "quantity_available", "unit", "quality_grade", "quality_attributes", "price_per_unit",
                  "location", "location_id", "harvest_date", "available_from", "available_until", "can_deliver",
                  "packaging", "is_active", "last_confirmed_at", "is_stale", "photos", "photos_missing", "created_at")
        read_only_fields = ("supplier", "last_confirmed_at", "created_at")

    def validate(self, attrs):
        product = attrs.get("product") or getattr(self.instance, "product", None)
        variety = attrs.get("variety")
        if variety and product and variety.product_id != product.pk:
            raise serializers.ValidationError({"variety": "This variety does not belong to the selected product."})
        start = attrs.get("available_from") or getattr(self.instance, "available_from", None)
        end = attrs.get("available_until") or getattr(self.instance, "available_until", None)
        if start and end and end < start:
            raise serializers.ValidationError({"available_until": "Must be after the available-from date."})
        harvest = attrs.get("harvest_date")
        if harvest and harvest > timezone.localdate() + timezone.timedelta(days=365):
            raise serializers.ValidationError({"harvest_date": "Harvest date is too far in the future."})
        if product is not None and not product.is_active:
            raise serializers.ValidationError({"product": "This product is not active."})
        return attrs
