from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("shop", "0002_price_nullable")]
    operations = [
        # contract-of: shop.0002_price_nullable
        migrations.RemoveField(model_name="product", name="price"),
    ]
