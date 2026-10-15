"""On each board, a title's copies stand together: a later copy moves to just after its title's first."""

from django.db import migrations
from django.db.models import F


def group(apps, schema_editor):
    Book = apps.get_model('library', 'Book')
    ShelfLevel = apps.get_model('library', 'ShelfLevel')

    def key(book):
        return ((book.title or '').strip().lower(), (book.author or '').strip().lower())

    for level in ShelfLevel.objects.all():
        books = list(Book.objects.filter(shelf_level=level)
                     .order_by(F('shelf_slot').asc(nulls_last=True), 'title', 'book_id'))
        order = []
        for book in books:
            last = max((i for i, b in enumerate(order) if key(b) == key(book)), default=None)
            order.insert(last + 1, book) if last is not None else order.append(book)
        if [b.pk for b in order] == [b.pk for b in books]:
            continue
        for position, book in enumerate(order, start=1):
            if book.shelf_slot != position:
                Book.objects.filter(pk=book.pk).update(shelf_slot=position)


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0096_copy_number_out_of_titles'),
    ]

    operations = [
        migrations.RunPython(group, migrations.RunPython.noop),
    ]
