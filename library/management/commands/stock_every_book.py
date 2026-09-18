"""Give every book record its one inventory copy, e.g. after loading an older export."""

import importlib

from django.apps import apps
from django.core.management.base import BaseCommand
from django.db import transaction

from library.models import Book, InventoryRecord


class Command(BaseCommand):
    help = 'Count books that have no inventory copy into stock, and split copies that share a book.'

    def handle(self, *args, **options):
        migration = importlib.import_module('library.migrations.0066_every_book_one_copy')
        books_before = Book._base_manager.count()
        copies_before = InventoryRecord.objects.count()
        with transaction.atomic():
            migration.give_every_book_one_copy(apps, None)
        self.stdout.write(self.style.SUCCESS(
            'Added %d inventory copy(ies) and %d book record(s).'
            % (InventoryRecord.objects.count() - copies_before,
               Book._base_manager.count() - books_before)))
