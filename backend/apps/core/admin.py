from django.contrib import admin

from .audit import log_action
from .models import AuditLog, Location, MatchingConfig


@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    list_display = ("name", "county", "sub_county", "latitude", "longitude")
    list_filter = ("county",)
    search_fields = ("name", "county", "sub_county")


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "actor", "action", "target_repr", "ip_address")
    list_filter = ("action", "target_type")
    search_fields = ("action", "target_repr", "actor__username")
    readonly_fields = [f.name for f in AuditLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(MatchingConfig)
class MatchingConfigAdmin(admin.ModelAdmin):
    readonly_fields = ("updated_at", "updated_by")

    def has_add_permission(self, request):
        return not MatchingConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)
        log_action(request.user, "config.matching_updated", obj, changed=form.changed_data)
