from django.shortcuts import get_object_or_404
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.response import Response

from apps.orders.models import OrderLine

from .models import Review
from .services import create_review


class ReviewSerializer(serializers.ModelSerializer):
    reviewer_name = serializers.CharField(source="reviewer.display_name", read_only=True)
    reviewee_name = serializers.CharField(source="reviewee.display_name", read_only=True)

    class Meta:
        model = Review
        fields = ("id", "order_line", "reviewer", "reviewer_name", "reviewee", "reviewee_name", "rating",
                  "quality_rating", "timeliness_rating", "communication_rating", "comment", "created_at")
        read_only_fields = ("reviewer", "reviewee", "created_at")


class ReviewViewSet(mixins.CreateModelMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin,
                    viewsets.GenericViewSet):
    """Reviews are public to signed-in users and can only be left on completed transactions."""

    serializer_class = ReviewSerializer
    queryset = Review.objects.select_related("reviewer", "reviewee")
    filterset_fields = ("reviewee", "rating")

    def create(self, request, *args, **kwargs):
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        line = get_object_or_404(OrderLine, pk=d["order_line"].pk)
        review = create_review(request.user, line, d["rating"], d.get("comment", ""), d.get("quality_rating"),
                               d.get("timeliness_rating"), d.get("communication_rating"))
        return Response(self.get_serializer(review).data, status=status.HTTP_201_CREATED)
