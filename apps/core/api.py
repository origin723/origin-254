from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import generics, viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from .analytics import platform_metrics
from .audit import log_action
from .models import AuditLog, MatchingConfig
from .permissions import IsPlatformAdmin
from .serializers import AuditLogSerializer, MatchingConfigSerializer


class AnalyticsView(APIView):
    """Platform KPIs: users, sourcing, matching performance, orders and delivery."""

    permission_classes = [IsPlatformAdmin]

    @extend_schema(parameters=[OpenApiParameter("days", int, description="Limit to the last N days")],
                   responses=dict)
    def get(self, request):
        days = request.query_params.get("days")
        return Response(platform_metrics(int(days) if days and days.isdigit() else None))


class AuditLogViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsPlatformAdmin]
    queryset = AuditLog.objects.select_related("actor")
    serializer_class = AuditLogSerializer
    filterset_fields = ("action", "target_type", "target_id", "actor")
    search_fields = ("action", "target_repr")


class MatchingConfigView(generics.RetrieveUpdateAPIView):
    """System configuration for the matching engine's weights and thresholds."""

    permission_classes = [IsPlatformAdmin]
    serializer_class = MatchingConfigSerializer

    def get_object(self):
        return MatchingConfig.get()

    def perform_update(self, serializer):
        obj = serializer.save(updated_by=self.request.user)
        log_action(self.request.user, "config.matching_updated", obj,
                   changes={k: str(v) for k, v in serializer.validated_data.items()})
