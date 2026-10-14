"""A person who gave books before the name was split gets first, middle and surname from the name on file."""

from django.db import migrations


def split(apps, schema_editor):
    from library.names import parse_name
    Donor = apps.get_model('library', 'Donor')
    for donor in Donor.objects.filter(donor_type='Individual', first_name__isnull=True):
        first, middle, last = parse_name(donor.name)
        if first and last:
            donor.first_name, donor.middle_name, donor.last_name = first, middle or None, last
            donor.save(update_fields=['first_name', 'middle_name', 'last_name'])


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0090_donor_name_parts'),
    ]

    operations = [
        migrations.RunPython(split, migrations.RunPython.noop),
    ]
