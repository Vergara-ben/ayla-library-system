"""Backfill identity QR codes for patrons that existed before the
approval workflow was introduced. They were registered under the old
instant-activation flow, so they are treated as already verified."""

from uuid import uuid4

from django.db import migrations


def backfill_qr(apps, schema_editor):
    Patron = apps.get_model('library', 'Patron')
    for patron in Patron.objects.filter(qr_code__isnull=True):
        patron.qr_code = str(uuid4())
        patron.otp_verified = True
        patron.save(update_fields=['qr_code', 'otp_verified'])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0011_patron_credential_document_patron_otp_code_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill_qr, noop),
    ]
