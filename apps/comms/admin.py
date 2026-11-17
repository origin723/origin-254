from django.contrib import admin

from .models import Conversation, Message, Notification


class MessageInline(admin.TabularInline):
    model = Message
    extra = 0
    fields = ("created_at", "sender", "body")
    readonly_fields = fields


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ("subject", "sourcing_request", "order", "updated_at")
    inlines = [MessageInline]


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("user", "kind", "title", "channels_sent", "read_at", "created_at")
    list_filter = ("kind",)
    search_fields = ("user__username", "title")
