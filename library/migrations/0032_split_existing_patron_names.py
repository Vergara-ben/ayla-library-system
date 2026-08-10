"""Split names already on file into their parts.

Everything the desk matcher does depends on the parts being there, so rows
created before the split are read once with the same parser used for a walk-in
typed at the desk. It is a guess on names like "Pedro delos Reyes" — correct
here, but a librarian can fix any it gets wrong on the patron record.
"""

from django.db import migrations

from library.names import parse_name


def split_names(apps, schema_editor):
    Patron = apps.get_model('library', 'Patron')
    for patron in Patron.objects.all():
        if patron.first_name or patron.last_name:
            continue
        first, middle, last = parse_name(patron.fullname)
        patron.first_name = first
        patron.middle_name = middle
        patron.last_name = last
        patron.save(update_fields=['first_name', 'middle_name', 'last_name'])


def unsplit(apps, schema_editor):
    """The single string was never dropped, so there is nothing to restore."""


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0031_patron_first_name_patron_last_name_and_more'),
    ]

    operations = [
        migrations.RunPython(split_names, unsplit),
    ]
