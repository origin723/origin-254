from django.utils import timezone
from django.core.exceptions import ValidationError
from drf_spectacular.utils import extend_schema
from rest_framework import parsers, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from apps.core.models import Location
from apps.core.permissions import IsPlatformAdmin
from apps.core.serializers import LocationSerializer

from .models import Category, InventoryItem, Product, ProductVariety
from .serializers import (CategorySerializer, InventoryPhotoSerializer, InventorySerializer, ProductSerializer,
                          VarietySerializer)
from .services import add_photos


class ReadAllWriteAdmin(permissions.BasePermission):
    def has_permission(self, request, view):
        if request.method in permissions.SAFE_METHODS:
            return request.user.is_authenticated
        return IsPlatformAdmin().has_permission(request, view)


class CategoryViewSet(viewsets.ModelViewSet):
    queryset = Category.objects.all()
    serializer_class = CategorySerializer
    permission_classes = [ReadAllWriteAdmin]
    search_fields = ("name",)


class ProductViewSet(viewsets.ModelViewSet):
    queryset = Product.objects.select_related("category").prefetch_related("varieties")
    serializer_class = ProductSerializer
    permission_classes = [ReadAllWriteAdmin]
    filterset_fields = ("category", "is_active", "perishable")
    search_fields = ("name", "varieties__name")


class VarietyViewSet(viewsets.ModelViewSet):
    queryset = ProductVariety.objects.select_related("product")
    serializer_class = VarietySerializer
    permission_classes = [ReadAllWriteAdmin]
    filterset_fields = ("product",)


class LocationViewSet(viewsets.ModelViewSet):
    """Farms, stores and delivery points. Any signed-in user may add one; only admins edit."""

    queryset = Location.objects.all()
    serializer_class = LocationSerializer
    filterset_fields = ("county",)
    search_fields = ("name", "county", "sub_county")

    def get_permissions(self):
        if self.action in {"update", "partial_update", "destroy"}:
            return [IsPlatformAdmin()]
        return super().get_permissions()


class InventoryViewSet(viewsets.ModelViewSet):
    """Suppliers manage their own stock. Admins see everything."""

    serializer_class = InventorySerializer
    filterset_fields = ("product", "quality_grade", "is_active", "location__county")
    ordering_fields = ("last_confirmed_at", "quantity_available", "price_per_unit")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return InventoryItem.objects.none()
        qs = InventoryItem.objects.select_related("product", "variety", "supplier", "location")
        user = self.request.user
        if user.is_platform_admin:
            return qs
        if user.is_supplier:
            return qs.filter(supplier__user=user)
        return qs.none()

    def _profile(self):
        user = self.request.user
        if not user.is_supplier or not hasattr(user, "supplier_profile"):
            raise PermissionDenied("Only suppliers can list inventory.")
        return user.supplier_profile

    def perform_create(self, serializer):
        profile = self._profile()
        serializer.save(supplier=profile, location=serializer.validated_data.get("location", profile.location),
                        last_confirmed_at=timezone.now())

    def perform_update(self, serializer):
        self._profile()
        serializer.save(last_confirmed_at=timezone.now())

    def perform_destroy(self, instance):
        # Keep history for orders/quotations; just deactivate.
        instance.is_active = False
        instance.save(update_fields=["is_active", "updated_at"])

    @action(detail=True, methods=["post"])
    def confirm(self, request, pk=None):
        """Reconfirm that this stock listing is still accurate (keeps it in matching)."""
        item = self.get_object()
        self._profile()
        item.last_confirmed_at = timezone.now()
        item.save(update_fields=["last_confirmed_at", "updated_at"])
        return Response(self.get_serializer(item).data)

    @extend_schema(request={"multipart/form-data": {"type": "object", "properties": {
        "photos": {"type": "array", "items": {"type": "string", "format": "binary"}},
        "caption": {"type": "string"}}}}, responses=InventoryPhotoSerializer(many=True))
    @action(detail=True, methods=["post"], parser_classes=[parsers.MultiPartParser, parsers.FormParser])
    def photos(self, request, pk=None):
        """Upload product photos (JPG/PNG/WebP, up to 5 MB each, 6 per listing)."""
        item = self.get_object()
        self._profile()
        files = request.FILES.getlist("photos")
        if not files:
            raise ValidationError({"photos": "Attach at least one photo."})
        added = add_photos(request.user, item, files, request.data.get("caption", ""))
        return Response(InventoryPhotoSerializer(added, many=True).data, status=status.HTTP_201_CREATED)
