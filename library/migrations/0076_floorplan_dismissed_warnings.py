from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0072_transaction_archive'),
    ]

    operations = [
        migrations.AddField(
            model_name='floorplan',
            name='dismissed_warnings',
            field=models.JSONField(blank=True, default=list),
        ),
    ]
