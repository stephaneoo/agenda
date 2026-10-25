from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ingest', '0002_review_refused_warning'),
    ]

    operations = [
        migrations.AddField(
            model_name='syncrun',
            name='too_many_dates',
            field=models.PositiveIntegerField(db_default=0, default=0),
        ),
    ]
