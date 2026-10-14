"""Every book gets its copy number: 1, 2, 3 within its title and author, in the order the copies were added."""

from django.db import migrations


def number(apps, schema_editor):
    Book = apps.get_model('library', 'Book')
    counters = {}
    for book in Book.objects.order_by('book_id').only('book_id', 'title', 'author', 'copy_number'):
        key = ((book.title or '').strip().lower(), (book.author or '').strip().lower())
        counters[key] = counters.get(key, 0) + 1
        if book.copy_number != counters[key]:
            Book.objects.filter(pk=book.pk).update(copy_number=counters[key])


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0094_book_copy_number'),
    ]

    operations = [
        migrations.RunPython(number, migrations.RunPython.noop),
    ]
