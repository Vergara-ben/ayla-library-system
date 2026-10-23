from django.db import migrations


def no_beyond_repair(apps, schema_editor):
    """Beyond Repair is no longer a condition: such a copy is Damaged until it is withdrawn."""
    Book = apps.get_model('library', 'Book')
    InventoryRecord = apps.get_model('library', 'InventoryRecord')
    Book.objects.filter(condition='Beyond Repair').update(condition='Damaged')
    Book.objects.filter(status='Beyond Repair').update(status='Under Repair')
    InventoryRecord.objects.filter(condition='Beyond Repair').update(condition='Damaged')


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0102_authors_people_only_fields'),
    ]

    operations = [
        migrations.RunPython(no_beyond_repair, migrations.RunPython.noop),
    ]
