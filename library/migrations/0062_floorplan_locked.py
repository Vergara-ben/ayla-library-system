from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0061_patroncredential'),
    ]

    operations = [
        migrations.AddField(
            model_name=name,
            name='locked',
            field=models.BooleanField(default=False),
        )
        for name in ('blebeacon', 'door', 'obstacle', 'room', 'shelf', 'stairway', 'waypoint')
    ]
