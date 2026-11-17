from django.contrib import admin

from .models import Match, Quotation, SourcingRequest


class MatchInline(admin.TabularInline):
    model = Match
    extra = 0
    fields = ("rank", "supplier", "score", "distance_km", "available_quantity", "suggested_quantity", "status")
    readonly_fields = fields
    can_delete = False


class QuotationInline(admin.TabularInline):
    model = Quotation
    extra = 0
    fields = ("reference", "supplier", "quantity", "price_per_unit", "delivery_cost", "status")
    readonly_fields = fields
    can_delete = False


@admin.register(SourcingRequest)
class SourcingRequestAdmin(admin.ModelAdmin):
    list_display = ("reference", "buyer", "product", "quantity", "unit", "delivery_deadline", "status", "created_at")
    list_filter = ("status", "product", "delivery_location__county")
    search_fields = ("reference", "buyer__username", "intended_application")
    readonly_fields = ("reference", "last_matched_at")
    inlines = [MatchInline, QuotationInline]


@admin.register(Match)
class MatchAdmin(admin.ModelAdmin):
    list_display = ("sourcing_request", "rank", "supplier", "score", "distance_km", "suggested_quantity", "status")
    list_filter = ("status",)
    search_fields = ("sourcing_request__reference", "supplier__business_name")
    readonly_fields = [f.name for f in Match._meta.fields]


@admin.register(Quotation)
class QuotationAdmin(admin.ModelAdmin):
    list_display = ("reference", "sourcing_request", "supplier", "quantity", "price_per_unit", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("reference", "sourcing_request__reference", "supplier__business_name")
    readonly_fields = ("reference", "history")
