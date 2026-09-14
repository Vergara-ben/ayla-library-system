"""Give every existing member the card number the desk will ask them for.

Members registered before this field existed have none, and the sign-in screen
identifies people by card number or email. Without a backfill every patron
already on file would have to be re-registered to be recognised.

Visitors are skipped on purpose: they hold no card. If one of them later
becomes a member, saving the record mints the number then.
"""

from django.db import migrations


def mint(apps, schema_editor):
    from library.cardnumbers import generate

    Patron = apps.get_model('library', 'Patron')
    taken = set(Patron.objects.exclude(card_number__isnull=True)
                .values_list('card_number', flat=True))

    for patron in Patron.objects.filter(card_number__isnull=True).exclude(
            account_status='Visitor').iterator():
        number = generate(exists=lambda candidate: candidate in taken)
        taken.add(number)
        patron.card_number = number
        patron.save(update_fields=['card_number'])


def clear(apps, schema_editor):
    Patron = apps.get_model('library', 'Patron')
    Patron.objects.update(card_number=None)


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0059_patron_card_number'),
    ]

    operations = [
        migrations.RunPython(mint, clear),
    ]
