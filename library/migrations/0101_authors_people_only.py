import re

from django.db import migrations

# Reference works an editorial body stands behind are by "Various Authors"; other books are "Unknown".
REFERENCE_GENRES = {'Encyclopedias', 'Dictionaries', 'General Reference', 'Cookbooks & Food'}
EDITORIAL = re.compile(r"\b(editors?|editorial|staff|kitchen|board|encyclopa?edia|dictionar(y|ies)|grolier"
                       r"|collier|britannica|merriam|time-life|reader's digest)\b", re.I)


def _join(names, others):
    if others:
        return ', '.join(names) + ' et al.'
    if len(names) > 1:
        return ', '.join(names[:-1]) + ' & ' + names[-1]
    return names[0]


def people_only(apps, schema_editor):
    """Authors only: publishers and offices leave, and so do illustrators, editors and translators."""
    Book = apps.get_model('library', 'Book')
    BookAuthor = apps.get_model('library', 'BookAuthor')
    Author = apps.get_model('library', 'Author')

    # A book named only by a publisher or an office.
    for book in Book.objects.filter(credits__author__kind='Organization').distinct():
        bodies = list(BookAuthor.objects.filter(book=book, author__kind='Organization')
                      .values_list('author__name', flat=True))
        BookAuthor.objects.filter(book=book, author__kind='Organization').delete()
        if BookAuthor.objects.filter(book=book).exists():
            continue
        various = (book.authors_and_others or book.genre in REFERENCE_GENRES
                   or any(EDITORIAL.search(n) for n in bodies))
        book.author_original = book.author_original or book.author
        book.author = 'Various Authors' if various else 'Unknown'
        book.author_unknown = True
        book.authors_and_others = various
        book.authors_unsure = False
        book.save(update_fields=['author', 'author_original', 'author_unknown', 'authors_and_others',
                                 'authors_unsure'])

    # A book naming only its editor or illustrator: that person is its author.
    for book in Book.objects.filter(credits__isnull=False).exclude(credits__role='Author').distinct():
        credits = list(BookAuthor.objects.filter(book=book).select_related('author').order_by('position'))
        keep = [c for c in credits if c.role == credits[0].role]
        BookAuthor.objects.filter(pk__in=[c.pk for c in keep]).update(role='Author')
        book.author_original = book.author_original or book.author
        book.author = _join([c.author.name for c in keep], book.authors_and_others)[:255]
        book.save(update_fields=['author', 'author_original'])

    BookAuthor.objects.exclude(role='Author').delete()
    Author.objects.filter(kind='Organization').delete()
    Author.objects.filter(credits__isnull=True).delete()

    # Copies now under one title and author line share its numbers: an unlabelled repeat takes the next free one.
    groups = {}
    for book in Book.objects.exclude(copy_number__isnull=True).order_by('pk'):
        groups.setdefault(((book.title or '').strip().lower(), (book.author or '').strip().lower()), []).append(book)
    for copies in groups.values():
        taken, top = set(), max(b.copy_number for b in copies)
        for book in sorted(copies, key=lambda b: (b.label_printed_at is None, b.pk)):
            if book.copy_number in taken and book.label_printed_at is None:
                top += 1
                book.copy_number = top
                book.save(update_fields=['copy_number'])
            taken.add(book.copy_number)


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0100_author_names'),
    ]

    operations = [
        migrations.RunPython(people_only, migrations.RunPython.noop),
    ]
