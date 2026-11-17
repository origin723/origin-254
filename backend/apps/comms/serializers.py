from rest_framework import serializers

from .models import Conversation, Message, Notification


class MessageSerializer(serializers.ModelSerializer):
    sender_name = serializers.CharField(source="sender.display_name", read_only=True)

    class Meta:
        model = Message
        fields = ("id", "conversation", "sender", "sender_name", "body", "created_at")
        read_only_fields = ("conversation", "sender", "created_at")


class ConversationSerializer(serializers.ModelSerializer):
    participants = serializers.SerializerMethodField()
    last_message = serializers.SerializerMethodField()
    unread = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = ("id", "subject", "sourcing_request", "order", "participants", "last_message", "unread",
                  "updated_at")

    def get_participants(self, obj) -> list:
        return [{"id": u.pk, "name": u.display_name, "role": u.role} for u in obj.participants.all()]

    def get_last_message(self, obj) -> dict | None:
        m = obj.messages.order_by("-created_at").first()
        return MessageSerializer(m).data if m else None

    def get_unread(self, obj) -> int:
        request = self.context.get("request")
        if not request:
            return 0
        return obj.messages.exclude(read_by=request.user).count()


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = ("id", "kind", "title", "body", "link", "channels_sent", "read_at", "created_at")
        read_only_fields = fields
