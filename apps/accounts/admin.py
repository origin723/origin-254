from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import BuyerProfile, LogisticsProfile, SupplierProfile, User, VerificationDocument, VerificationStatus
from .services import set_verification


class DocumentInline(admin.TabularInline):
    model = VerificationDocument
    fk_name = "owner"
    extra = 0
    readonly_fields = ("doc_type", "original_name", "status", "created_at")
    fields = readonly_fields
    can_delete = False


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    inlines = [DocumentInline]
    list_display = ("username", "email", "role", "phone", "is_active", "date_joined")
    list_filter = ("role", "is_active", "is_staff")
    fieldsets = BaseUserAdmin.fieldsets + (("Origin 254", {"fields": ("role", "phone", "sms_opt_in",
                                                                        "whatsapp_opt_in")}),)
    add_fieldsets = BaseUserAdmin.add_fieldsets + (("Origin 254", {"fields": ("email", "role", "phone")}),)


def _verification_action(status, label):
    def action(modeladmin, request, queryset):
        for profile in queryset:
            set_verification(profile, request.user, status)
        messages.success(request, f"{queryset.count()} profile(s) marked {status}.")
    action.short_description = label
    action.__name__ = f"mark_{status}"
    return action


class VerifiableAdmin(admin.ModelAdmin):
    list_display = ("business_name", "user", "location", "verification_status", "created_at")
    list_filter = ("verification_status", "location__county")
    search_fields = ("business_name", "user__username", "user__email", "registration_number")
    readonly_fields = ("verified_at", "verified_by", "verification_status")
    actions = [
        _verification_action(VerificationStatus.APPROVED, "Approve selected"),
        _verification_action(VerificationStatus.REJECTED, "Reject selected"),
        _verification_action(VerificationStatus.SUSPENDED, "Suspend selected"),
    ]


@admin.register(SupplierProfile)
class SupplierAdmin(VerifiableAdmin):
    list_display = VerifiableAdmin.list_display + ("reliability_score", "completed_orders", "average_rating")
    readonly_fields = VerifiableAdmin.readonly_fields + (
        "reliability_score", "completed_orders", "average_rating", "on_time_rate", "inspection_pass_rate")


@admin.register(BuyerProfile)
class BuyerAdmin(VerifiableAdmin):
    pass


@admin.register(LogisticsProfile)
class LogisticsAdmin(VerifiableAdmin):
    pass


@admin.register(VerificationDocument)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ("owner", "doc_type", "original_name", "status", "created_at")
    list_filter = ("doc_type", "status")
    readonly_fields = ("owner", "file", "original_name", "reviewed_by")
