import os

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

TYPES = {
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.png': 'image/png',
    '.pdf': 'application/pdf',
}


def copy_files_in(apps, schema_editor):
    """Move uploaded IDs from media/credentials/ into the database."""
    Patron = apps.get_model('library', 'Patron')
    PatronCredential = apps.get_model('library', 'PatronCredential')
    base = os.path.join(settings.MEDIA_ROOT, 'credentials')
    patrons = Patron.objects.exclude(credential_document__isnull=True).exclude(credential_document='')
    for patron in patrons:
        name = patron.credential_document.replace('\\', '/').rsplit('/', 1)[-1]
        path = os.path.join(base, name)
        if not os.path.isfile(path) or PatronCredential.objects.filter(patron=patron).exists():
            continue
        with open(path, 'rb') as fh:
            data = fh.read()
        PatronCredential.objects.create(
            patron=patron, name=name, data=data,
            content_type=TYPES.get(os.path.splitext(name)[1].lower(), 'application/octet-stream'),
        )


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0060_backfill_card_numbers'),
    ]

    operations = [
        migrations.CreateModel(
            name='PatronCredential',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=255, unique=True)),
                ('content_type', models.CharField(max_length=100)),
                ('data', models.BinaryField()),
                ('uploaded_at', models.DateTimeField(auto_now_add=True)),
                ('patron', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='credential_file', to='library.patron')),
            ],
            options={
                'db_table': 'PatronCredential',
            },
        ),
        migrations.RunPython(copy_files_in, migrations.RunPython.noop),
    ]
