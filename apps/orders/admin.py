from django.contrib import admin

from .models import Delivery, Dispute, Inspection, Order, OrderEvent, OrderLine, SaleRecord


class LineInline(admin.TabularInline):
    model = OrderLine
    extra = 0
    fields = ("supplier", "quantity", "unit", "price_per_unit", "delivery_cost", "status", "agreed_delivery_date")
    readonly_fields = fields
    can_delete = False


class EventInline(admin.TabularInline):
    model = OrderEvent
    extra = 0
    fields = ("created_at", "actor", "event", "order_line", "note")
    readonly_fields = fields
    can_delete = False


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("reference", "sourcing_request", "buyer", "status", "created_at", "completed_at")
    list_filter = ("status",)
    search_fields = ("reference", "sourcing_request__reference", "buyer__username")
    readonly_fields = ("reference", "completed_at")
    inlines = [LineInline, EventInline]


@admin.register(OrderLine)
class OrderLineAdmin(admin.ModelAdmin):
    list_display = ("order", "supplier", "quantity", "unit", "price_per_unit", "status", "agreed_delivery_date")
    list_filter = ("status",)
    search_fields = ("order__reference", "supplier__business_name")


@admin.register(Inspection)
class InspectionAdmin(admin.ModelAdmin):
    list_display = ("order_line", "stage", "result", "inspector", "inspected_at")
    list_filter = ("stage", "result")


@admin.register(Delivery)
class DeliveryAdmin(admin.ModelAdmin):
    list_display = ("order_line", "logistics_partner", "status", "scheduled_date", "delivered_at")
    list_filter = ("status",)


@admin.register(Dispute)
class DisputeAdmin(admin.ModelAdmin):
    list_display = ("order", "raised_by", "status", "created_at", "resolved_at")
    list_filter = ("status",)


@admin.register(SaleRecord)
class SaleRecordAdmin(admin.ModelAdmin):
    list_display = ("completed_at", "order_reference", "product_name", "variety_name", "quantity", "unit", "total",
                    "supplier_name", "buyer_name")
    list_filter = ("product_name",)
    search_fields = ("order_reference", "product_name", "supplier_name", "buyer_name")
    date_hierarchy = "completed_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
