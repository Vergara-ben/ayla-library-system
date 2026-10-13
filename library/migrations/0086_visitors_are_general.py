from django.db import migrations


def visitors_are_general(apps, schema_editor):
    """A walk-in visitor is a general visitor, whatever the kiosk once let them pick."""
    Patron = apps.get_model('library', 'Patron')
    Patron.objects.filter(account_status='Visitor').exclude(
        patron_type='General Visitor').update(patron_type='General Visitor')


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0085_condition_scale'),
    ]

    operations = [
        migrations.RunPython(visitors_are_general, migrations.RunPython.noop),
    ]
