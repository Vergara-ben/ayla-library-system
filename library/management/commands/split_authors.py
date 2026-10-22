"""Split every book's author line into names, once, keeping a copy of the lines as they were."""

import csv
from datetime import date
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from library import authors
from library.models import Book, SystemLog


class Command(BaseCommand):
    help = 'Split author lines into names; the ones it is unsure of are listed under Check authors.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Show what would change, and change nothing.')
        parser.add_argument('--backup-dir', default=str(Path(settings.BASE_DIR).parent / 'backups'))

    def handle(self, *args, **options):
        books = list(authors.unsplit().order_by('book_id'))
        if not books:
            self.stdout.write('Every book already has its names.')
            return
        if not options['dry_run']:
            folder = Path(options['backup_dir'])
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / ('authors-before-split-%s.csv' % date.today().isoformat())
            with open(path, 'w', newline='', encoding='utf-8') as f:
                out = csv.writer(f)
                out.writerow(['book_id', 'title', 'author', 'copy_number', 'label_printed_at'])
                for b in books:
                    out.writerow([b.book_id, b.title, b.author, b.copy_number or '', b.label_printed_at or ''])
            self.stdout.write('Backup: %s' % path)

        # First names and surnames learnt from the lines that read plainly, for the ones that do not.
        known = authors.known_names()
        for text in {b.author for b in books}:
            reading = authors.split(text)
            if reading['sure']:
                for c in reading['credits']:
                    if c['kind'] == 'Person':
                        authors.learn(known, c['first'], c['last'])

        numbers = {b.pk: b.copy_number for b in books}
        with transaction.atomic():
            # Labelled copies first, so a copy whose number is already printed keeps it.
            for book in sorted(books, key=lambda b: (b.label_printed_at is None, b.pk)):
                entry, problem = authors.from_text(book.author, known)
                if problem:
                    entry = authors.make_entry([], True, sure=False, original=book.author)
                authors.attach(book, entry)
            after = dict(Book.all_objects.filter(pk__in=numbers).values_list('pk', 'copy_number'))
            moved = [(pk, numbers[pk], after[pk]) for pk in numbers if numbers[pk] != after[pk]]
            unsure = Book.all_objects.filter(pk__in=numbers, authors_unsure=True)
            changed = Book.all_objects.filter(pk__in=numbers).exclude(author_original='').count()
            self.stdout.write('%d book(s) split; %d author line(s) now first name first; %d to check (%d line(s)).'
                              % (len(books), changed, unsure.count(),
                                 unsure.values('author_original', 'author').distinct().count()))
            for pk, old, new in moved:
                self.stdout.write('  Book %d: Copy %s is now Copy %s (another copy of the title has its number).'
                                  % (pk, old, new))
            if options['dry_run']:
                transaction.set_rollback(True)
                self.stdout.write('Dry run: nothing was changed.')
            else:
                SystemLog.objects.create(
                    actor_role='System', admin_name='System', action='Update', entity_type='Book',
                    detail='Split %d author line(s) into names; %d book(s) to check under Check authors.'
                           % (len(books), unsure.count()))
