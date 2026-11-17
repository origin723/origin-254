from django.contrib import admin

from .models import Payment, Payout


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("reference", "order", "payer", "amount", "status", "receipt_number", "provider", "created_at")
    list_filter = ("status", "provider")
    search_fields = ("reference", "order__reference", "receipt_number", "phone", "checkout_request_id")
    readonly_fields = [f.name for f in Payment._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Payout)
class PayoutAdmin(admin.ModelAdmin):
    list_display = ("reference", "supplier", "amount", "fee", "status", "method", "receipt_number", "paid_at")
    list_filter = ("status", "method")
    search_fields = ("reference", "supplier__business_name", "receipt_number", "order_line__order__reference")
    readonly_fields = [f.name for f in Payout._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
