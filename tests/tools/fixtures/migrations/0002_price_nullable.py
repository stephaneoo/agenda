from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("shop", "0001_initial")]
    operations = [
        migrations.AlterField(model_name="product", name="price", field=models.IntegerField(null=True)),
    ]
