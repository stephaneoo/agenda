from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("shop", "0001_initial")]
    operations = [
        # A Python-side default only: the column stays NOT NULL, so old code can't survive its removal.
        migrations.AlterField(model_name="product", name="price", field=models.IntegerField(default=0)),
    ]
