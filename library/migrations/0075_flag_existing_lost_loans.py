from django.db import migrations


def flag_lost(apps, schema_editor):
    """Open loans already marked lost, from before the flag existed."""
    Transaction = apps.get_model('library', 'Transaction')
    Transaction.objects.filter(transaction_type='Borrow', return_date__isnull=True,
                               book__status='Lost').update(marked_lost=True)


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0074_transaction_marked_lost'),
    ]

    operations = [
        migrations.RunPython(flag_lost, migrations.RunPython.noop),
    ]
