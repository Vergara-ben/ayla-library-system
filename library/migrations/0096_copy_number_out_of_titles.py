"""'A World of Ideas (Copy 2)' becomes 'A World of Ideas', copy 2: the number has its own field now."""

import re

from django.db import migrations

COPY_IN_TITLE = re.compile(r'\s*\(\s*copy\s*(\d+)\s*\)\s*$', re.I)


def move(apps, schema_editor):
    Book = apps.get_model('library', 'Book')
    marked = [b for b in Book.objects.only('book_id', 'title', 'author') if COPY_IN_TITLE.search(b.title or '')]
    groups = {}
    for book in marked:
        base = COPY_IN_TITLE.sub('', book.title).strip()
        groups.setdefault((base.lower(), (book.author or '').strip().lower()), base)
    for (base_lower, author_lower), base in groups.items():
        same = [b for b in Book.objects.order_by('book_id')
                if (COPY_IN_TITLE.sub('', b.title or '').strip().lower(), (b.author or '').strip().lower())
                == (base_lower, author_lower)]
        # The number in the title first, then the rest in the order they were added.
        def wanted(b):
            m = COPY_IN_TITLE.search(b.title or '')
            return (int(m.group(1)) if m else 10 ** 6, b.book_id)
        for n, book in enumerate(sorted(same, key=wanted), start=1):
            Book.objects.filter(pk=book.pk).update(title=base, copy_number=n)


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0095_number_existing_copies'),
    ]

    operations = [
        migrations.RunPython(move, migrations.RunPython.noop),
    ]
