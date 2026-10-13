"""584 one-off genre descriptions become the 45 standard genres; each book keeps its old wording as its subject."""

from django.db import migrations


def tidy_genres(apps, schema_editor):
    from library.genres import standard_genre
    Book = apps.get_model('library', 'Book')
    SystemLog = apps.get_model('library', 'SystemLog')
    before = Book.objects.exclude(genre__isnull=True).exclude(genre='').values('genre').distinct().count()
    moved = 0
    for old in list(Book.objects.exclude(genre__isnull=True).exclude(genre='')
                    .values_list('genre', flat=True).distinct()):
        new = standard_genre(old)
        if not new or new == old:
            continue
        moved += Book.objects.filter(genre=old, subject__isnull=True).update(genre=new, subject=old)
        moved += Book.objects.filter(genre=old).update(genre=new)
    if moved:
        after = Book.objects.exclude(genre__isnull=True).exclude(genre='').values('genre').distinct().count()
        SystemLog.objects.create(actor_role='System', admin_name='System', action='Update', entity_type='Book',
                                 detail='Tidied genres: %d became %d; %d books keep their old wording as the '
                                        'subject' % (before, after, moved))


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0088_book_subject'),
    ]

    operations = [
        migrations.RunPython(tidy_genres, migrations.RunPython.noop),
    ]
