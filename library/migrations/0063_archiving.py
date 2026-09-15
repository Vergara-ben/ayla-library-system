from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0062_floorplan_locked'),
    ]

    operations = [
        operation
        for name in ('announcement', 'book', 'donation', 'floorplan', 'patron', 'patronlog')
        for operation in (
            migrations.AddField(
                model_name=name,
                name='archived_at',
                field=models.DateTimeField(blank=True, db_index=True, null=True),
            ),
            migrations.AddField(
                model_name=name,
                name='archived_by',
                field=models.CharField(blank=True, default='', max_length=255),
            ),
            migrations.AddField(
                model_name=name,
                name='archive_reason',
                field=models.CharField(blank=True, default='', max_length=500),
            ),
        )
    ]
