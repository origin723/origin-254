from django.contrib import admin

from .models import Category, InventoryItem, InventoryPhoto, Product, ProductVariety


class VarietyInline(admin.TabularInline):
    model = ProductVariety
    extra = 1


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "default_unit", "perishable", "is_active")
    list_filter = ("category", "is_active", "perishable")
    search_fields = ("name",)
    inlines = [VarietyInline]


class PhotoInline(admin.TabularInline):
    model = InventoryPhoto
    extra = 0
    fields = ("image", "caption", "created_at")
    readonly_fields = ("created_at",)


@admin.register(InventoryItem)
class InventoryAdmin(admin.ModelAdmin):
    inlines = [PhotoInline]
    list_display = ("product", "variety", "supplier", "quantity_available", "unit", "quality_grade",
                    "location", "last_confirmed_at", "is_active")
    list_filter = ("product", "quality_grade", "is_active", "location__county")
    search_fields = ("supplier__business_name", "product__name")
    autocomplete_fields = ("supplier",)
