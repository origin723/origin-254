from decimal import Decimal

from django.db import migrations


def backfill(apps, schema_editor):
    OrderLine = apps.get_model("orders", "OrderLine")
    SaleRecord = apps.get_model("orders", "SaleRecord")
    BuyerProfile = apps.get_model("accounts", "BuyerProfile")
    lines = OrderLine.objects.filter(status="completed", sale_record__isnull=True).select_related(
        "order__buyer", "order__delivery_location", "supplier", "inventory_item__product", "inventory_item__variety")
    for line in lines:
        order, item, loc = line.order, line.inventory_item, line.order.delivery_location
        bp = BuyerProfile.objects.filter(user=order.buyer).first()
        qty = line.delivered_quantity if line.delivered_quantity is not None else line.quantity
        SaleRecord.objects.create(
            order_line=line, order=order, order_reference=order.reference, buyer=order.buyer,
            buyer_name=(bp.business_name if bp else "") or order.buyer.username,
            supplier=line.supplier, supplier_name=line.supplier.business_name, inventory_item=item,
            product_name=item.product.name, variety_name=item.variety.name if item.variety_id else "",
            quality_grade=line.quality_grade, quantity=qty, unit=line.unit, price_per_unit=line.price_per_unit,
            delivery_cost=line.delivery_cost,
            total=(qty * line.price_per_unit + line.delivery_cost).quantize(Decimal("0.01")),
            delivered_to=f"{loc.name}, {loc.county}", completed_at=line.completed_at or line.updated_at)


class Migration(migrations.Migration):
    dependencies = [("orders", "0003_sale_history"), ("accounts", "0004_role_choices")]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
