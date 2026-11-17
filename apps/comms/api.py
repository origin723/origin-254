from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import Conversation, Notification
from .serializers import ConversationSerializer, MessageSerializer, NotificationSerializer
from .services import post_message


class ConversationViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Buyer ↔ supplier messaging. Start a thread via /sourcing-requests/{id}/contact/."""

    serializer_class = ConversationSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Conversation.objects.none()
        return Conversation.objects.filter(participants=self.request.user).prefetch_related("participants")

    @extend_schema(responses=MessageSerializer(many=True))
    @action(detail=True, methods=["get"])
    def messages(self, request, pk=None):
        convo = self.get_object()
        msgs = convo.messages.select_related("sender")
        for m in msgs.exclude(read_by=request.user):
            m.read_by.add(request.user)
        return Response(MessageSerializer(msgs, many=True).data)

    @extend_schema(request=MessageSerializer, responses=MessageSerializer)
    @action(detail=True, methods=["post"])
    def send(self, request, pk=None):
        msg = post_message(request.user, self.get_object(), request.data.get("body", ""))
        return Response(MessageSerializer(msg).data, status=status.HTTP_201_CREATED)


class NotificationViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    serializer_class = NotificationSerializer
    filterset_fields = ("kind",)

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Notification.objects.none()
        qs = self.request.user.notifications.all()
        if self.request.query_params.get("unread") in {"1", "true"}:
            qs = qs.filter(read_at__isnull=True)
        return qs

    @action(detail=True, methods=["post"])
    def read(self, request, pk=None):
        n = self.get_queryset().get(pk=pk)
        n.read_at = n.read_at or timezone.now()
        n.save(update_fields=["read_at"])
        return Response(self.get_serializer(n).data)

    @action(detail=False, methods=["post"])
    def read_all(self, request):
        count = self.get_queryset().filter(read_at__isnull=True).update(read_at=timezone.now())
        return Response({"marked_read": count})
