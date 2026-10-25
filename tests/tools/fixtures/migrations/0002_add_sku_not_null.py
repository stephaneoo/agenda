from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("shop", "0001_initial")]
    operations = [
        migrations.AddField(
            model_name="product",
            name="sku",
            field=models.CharField(max_length=20, default="none"),
            preserve_default=False,
        ),
    ]
