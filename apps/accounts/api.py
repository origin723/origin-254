from django.http import FileResponse
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.exceptions import ValidationError
from rest_framework import generics, mixins, parsers, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.audit import log_action
from apps.core.permissions import IsPlatformAdmin

from .models import BuyerProfile, LogisticsProfile, SupplierProfile, User, VerificationDocument, VerificationStatus
from .serializers import (BuyerProfileSerializer, DocumentSerializer, LogisticsProfileSerializer,
                          PublicSupplierSerializer, RegisterSerializer, SupplierProfileSerializer, UserSerializer,
                          VerificationDecisionSerializer, profile_serializer_for)
from .services import set_verification


class RegisterView(generics.CreateAPIView):
    """Register a farmer, processor or logistics partner."""

    serializer_class = RegisterSerializer
    permission_classes = [permissions.AllowAny]

    def create(self, request, *args, **kwargs):
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        user = ser.save()
        log_action(user, "account.registered", user, role=user.role)
        return Response(UserSerializer(user).data, status=status.HTTP_201_CREATED)


class MeView(generics.RetrieveUpdateAPIView):
    serializer_class = UserSerializer

    def get_object(self):
        return self.request.user


class MyProfileView(generics.RetrieveUpdateAPIView):
    """The caller's role profile (supplier, buyer or logistics)."""

    def get_serializer_class(self):
        if getattr(self, "swagger_fake_view", False):
            return SupplierProfileSerializer
        return profile_serializer_for(self.request.user) or SupplierProfileSerializer

    def get_object(self):
        profile = self.request.user.profile
        if profile is None:
            from rest_framework.exceptions import NotFound
            raise NotFound("This account has no role profile.")
        return profile


class DocumentViewSet(mixins.CreateModelMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin,
                      viewsets.GenericViewSet):
    """Verification documents. Stored privately; downloadable only by the owner and administrators."""

    serializer_class = DocumentSerializer
    parser_classes = [parsers.MultiPartParser, parsers.FormParser]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return VerificationDocument.objects.none()
        qs = VerificationDocument.objects.select_related("owner")
        return qs if self.request.user.is_platform_admin else qs.filter(owner=self.request.user)

    def perform_create(self, serializer):
        f = serializer.validated_data["file"]
        doc = serializer.save(owner=self.request.user, original_name=f.name[:255])
        log_action(self.request.user, "document.uploaded", doc, doc_type=doc.doc_type)

    @extend_schema(responses={(200, "application/octet-stream"): bytes})
    @action(detail=True, methods=["get"])
    def download(self, request, pk=None):
        doc = self.get_object()
        if request.user.is_platform_admin and doc.owner_id != request.user.pk:
            log_action(request.user, "document.viewed", doc)
        return FileResponse(doc.file.open("rb"), as_attachment=True, filename=doc.original_name or "document")


class SupplierDirectoryViewSet(viewsets.ReadOnlyModelViewSet):
    """Approved suppliers (admins see all, with ?verification_status= filter)."""

    serializer_class = PublicSupplierSerializer
    filterset_fields = {"location__county": ["exact"], "user__role": ["exact"], "verification_status": ["exact"],
                        "inventory__product": ["exact"]}
    search_fields = ("business_name", "location__name", "location__county")
    ordering_fields = ("reliability_score", "average_rating", "completed_orders", "business_name")

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return SupplierProfile.objects.none()
        qs = SupplierProfile.objects.select_related("user", "location").distinct().order_by("business_name")
        if not self.request.user.is_platform_admin:
            qs = qs.filter(verification_status=VerificationStatus.APPROVED, user__is_active=True)
        return qs


    @extend_schema(parameters=[
        OpenApiParameter("product", int, required=True), OpenApiParameter("location", int, required=True,
                                                                          description="core.Location id"),
        OpenApiParameter("min_quantity", float), OpenApiParameter("radius_km", int),
        OpenApiParameter("limit", int, description="default 15")], responses=dict)
    @action(detail=False, methods=["get"])
    def nearest(self, request):
        """Nearest approved suppliers with current stock of a product, closest first."""
        from apps.catalog.models import Product
        from apps.core.models import Location
        from apps.sourcing.matching import nearest_suppliers

        params = request.query_params
        try:
            product = Product.objects.get(pk=int(params["product"]), is_active=True)
            location = Location.objects.get(pk=int(params["location"]))
            min_q = float(params["min_quantity"]) if params.get("min_quantity") else None
            radius = int(params["radius_km"]) if params.get("radius_km") else None
            limit = min(int(params.get("limit", 15)), 50)
        except (KeyError, ValueError, Product.DoesNotExist, Location.DoesNotExist):
            raise ValidationError({"detail": "Provide valid `product` and `location` ids."})
        results = nearest_suppliers(product, location, min_quantity=min_q, radius_km=radius, limit=limit)
        return Response([{
            "rank": i, "supplier": PublicSupplierSerializer(r.supplier).data, "inventory_item": r.item.pk,
            "variety": r.item.variety.name if r.item.variety else None, "quantity_available": r.item.quantity_available,
            "unit": r.item.unit, "quality_grade": r.item.quality_grade, "price_per_unit": r.item.price_per_unit,
            "distance_km": round(r.distance_km, 1), "est_road_km": r.road_km, "est_travel_hours": r.travel_hours,
            "location": r.item.location.name,
        } for i, r in enumerate(results, start=1)])


class _AdminProfileViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsPlatformAdmin]
    filterset_fields = ("verification_status", "location__county")
    search_fields = ("business_name", "user__username", "user__email", "registration_number")

    @extend_schema(request=VerificationDecisionSerializer)
    @action(detail=True, methods=["post"])
    def verify(self, request, pk=None):
        """Approve, reject or suspend this profile. Logged in the audit trail."""
        ser = VerificationDecisionSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        profile = set_verification(self.get_object(), request.user, ser.validated_data["status"],
                                   ser.validated_data.get("notes", ""))
        return Response(self.get_serializer(profile).data)


class AdminSupplierViewSet(_AdminProfileViewSet):
    queryset = SupplierProfile.objects.select_related("user", "location").order_by("-created_at")
    serializer_class = SupplierProfileSerializer


class AdminBuyerViewSet(_AdminProfileViewSet):
    queryset = BuyerProfile.objects.select_related("user", "location").order_by("-created_at")
    serializer_class = BuyerProfileSerializer


class AdminLogisticsViewSet(_AdminProfileViewSet):
    queryset = LogisticsProfile.objects.select_related("user", "location").order_by("-created_at")
    serializer_class = LogisticsProfileSerializer


class AdminUserViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsPlatformAdmin]
    queryset = User.objects.order_by("-date_joined")
    serializer_class = UserSerializer
    filterset_fields = ("role", "is_active")
    search_fields = ("username", "email", "phone")

    @action(detail=True, methods=["post"])
    def deactivate(self, request, pk=None):
        user = get_object_or_404(User, pk=pk)
        user.is_active = False
        user.save(update_fields=["is_active"])
        log_action(request.user, "account.deactivated", user)
        return Response(UserSerializer(user).data)

    @action(detail=True, methods=["post"])
    def activate(self, request, pk=None):
        user = get_object_or_404(User, pk=pk)
        user.is_active = True
        user.save(update_fields=["is_active"])
        log_action(request.user, "account.activated", user)
        return Response(UserSerializer(user).data)
