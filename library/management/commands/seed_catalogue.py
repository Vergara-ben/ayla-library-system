"""Fill the structural gaps that leave the catalogue unusable.

Shelves existed and were placed on the floor plan, but ten of eleven had no
levels at all, so there was nowhere to put a book: 76 of 79 titles sat
unplaced, which means Inventory shows nothing, a stock audit has nothing to
count, and "guide me to this book" has no shelf to guide anyone to.

This creates shelf levels, files the unplaced books onto them by genre, gives
every copy an inventory record, and writes some donation and stock-audit
history so those pages have something to show.

    python manage.py seed_catalogue                 4 levels a shelf
    python manage.py seed_catalogue --levels 5      taller shelving
    python manage.py seed_catalogue --donations 20  more accessioning history
    python manage.py seed_catalogue --clear         undo all of it

Everything written is listed in seeded_catalogue.json. Clearing unplaces the
books this filed but never deletes a book: Book.shelf_level is SET_NULL, so a
level can go away without taking the catalogue with it.
"""

import json
import random
import uuid
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction as db_transaction
from django.utils import timezone

from library.models import (Book, Donation, InventoryRecord, Shelf, ShelfLevel,
                            StockAudit, Transaction, User)

LEDGER = 'seeded_catalogue.json'

DONORS = [
    'Barangay Council', 'Mrs. Aurora Villanueva', 'Rotary Club of Pasig',
    'Anonymous', 'Sto. Niño Parish', 'Alumni Association',
    'Dr. Ramon Salazar', 'Sagip Aklat Foundation',
]
SUPPLIERS = ['National Book Store', 'Rex Book Store', 'Anvil Publishing',
             'Central Books', 'Fully Booked']


class Command(BaseCommand):
    help = 'Create shelf levels, place books, and fill inventory/donation/audit history.'

    def add_arguments(self, parser):
        parser.add_argument('--levels', type=int, default=4,
                            help='levels each shelf should have (default 4)')
        parser.add_argument('--donations', type=int, default=12,
                            help='donation records to create (default 12)')
        parser.add_argument('--audits', type=int, default=5,
                            help='stock audits to create (default 5)')
        parser.add_argument('--seed', type=int, default=None)
        parser.add_argument('--clear', action='store_true',
                            help='undo everything this command created')

    @property
    def ledger_path(self):
        return settings.BASE_DIR / LEDGER

    def read_ledger(self):
        try:
            with open(self.ledger_path, encoding='utf-8') as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def write_ledger(self, data):
        data['created'] = timezone.now().isoformat()
        with open(self.ledger_path, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, indent=1)

    def handle(self, *args, **options):
        if options['clear']:
            return self.clear()
        return self.seed(options)

    def clear(self):
        ledger = self.read_ledger()
        if ledger is None:
            self.stdout.write(self.style.ERROR(
                'No %s found, so there is no way to tell what was seeded from what was '
                'set up by hand. Nothing removed.' % LEDGER))
            return

        with db_transaction.atomic():
            audits = StockAudit.objects.filter(audit_id__in=ledger.get('audit_ids') or [])
            n_audits = audits.count(); audits.delete()

            donations = Donation.objects.filter(donation_id__in=ledger.get('donation_ids') or [])
            n_donations = donations.count(); donations.delete()

            records = InventoryRecord.objects.filter(
                inventory_id__in=ledger.get('inventory_ids') or [])
            n_records = records.count(); records.delete()

            # Unplace before the levels go, so the books are explicitly released
            # rather than relying on the cascade to do it quietly.
            placed = ledger.get('placed_book_ids') or []
            Book.objects.filter(book_id__in=placed).update(shelf_level=None)

            for level_id, old in (ledger.get('category_restores') or {}).items():
                ShelfLevel.objects.filter(shelf_level_id=int(level_id)).update(category=old)

            levels = ShelfLevel.objects.filter(
                shelf_level_id__in=ledger.get('shelf_level_ids') or [])
            n_levels = levels.count(); levels.delete()

        self.write_ledger({'shelf_level_ids': [], 'inventory_ids': [], 'donation_ids': [],
                           'audit_ids': [], 'placed_book_ids': [], 'category_restores': {}})
        self.stdout.write(self.style.SUCCESS(
            'Removed %d level(s), unplaced %d book(s), deleted %d inventory record(s), '
            '%d donation(s), %d audit(s).'
            % (n_levels, len(placed), n_records, n_donations, n_audits)))

    def seed(self, options):
        rng = random.Random(options['seed'])
        today = timezone.localdate()

        shelves = list(Shelf.objects.order_by('name'))
        if not shelves:
            self.stdout.write(self.style.ERROR('No shelves exist — build the floor plan first.'))
            return

        genres = sorted({b.genre for b in Book.objects.all() if b.genre})
        if not genres:
            genres = ['General Collection']

        ledger = {'shelf_level_ids': [], 'inventory_ids': [], 'donation_ids': [],
                  'audit_ids': [], 'placed_book_ids': [], 'category_restores': {}}

        with db_transaction.atomic():
            # ── Levels ──────────────────────────────────────────────────
            want = max(1, options['levels'])
            for shelf in shelves:
                existing = set(ShelfLevel.objects.filter(shelf=shelf)
                               .values_list('level_number', flat=True))
                for number in range(1, want + 1):
                    if number in existing:
                        continue
                    level = ShelfLevel.objects.create(
                        shelf=shelf, level_number=number, category='', is_active=True)
                    ledger['shelf_level_ids'].append(level.shelf_level_id)

            # A level with no category cannot be signposted, and the whole point
            # of the hierarchy is that a patron is told where to go.
            all_levels = list(ShelfLevel.objects.select_related('shelf')
                              .order_by('shelf__name', 'level_number'))
            for index, level in enumerate(all_levels):
                if level.category:
                    continue
                if level.shelf_level_id not in ledger['shelf_level_ids']:
                    ledger['category_restores'][str(level.shelf_level_id)] = level.category
                level.category = genres[index % len(genres)]
                level.save(update_fields=['category'])

            by_genre = {}
            for level in ShelfLevel.objects.all():
                by_genre.setdefault(level.category, []).append(level)
            fallback = list(ShelfLevel.objects.all())

            # ── Put the books somewhere ─────────────────────────────────
            unplaced = list(Book.objects.filter(shelf_level__isnull=True))
            for book in unplaced:
                choices = by_genre.get(book.genre) or fallback
                level = rng.choice(choices)
                Book.objects.filter(book_id=book.book_id).update(shelf_level=level)
                ledger['placed_book_ids'].append(book.book_id)

            # ── One inventory record per copy ───────────────────────────
            need_records = list(Book.objects.filter(inventory_records__isnull=True))
            donation_pool = []
            for book in need_records:
                is_donation = rng.random() < 0.28
                condition = rng.choices(['Good', 'Good', 'Good', 'Damaged'], k=1)[0]
                record = InventoryRecord.objects.create(
                    book=book,
                    source='Donation' if is_donation else 'Purchase',
                    supplier=None if is_donation else rng.choice(SUPPLIERS),
                    po_number=None if is_donation else 'PO-%04d' % rng.randint(1000, 9999),
                    donor_name=rng.choice(DONORS) if is_donation else None,
                    donated_date=(today - timedelta(days=rng.randint(20, 400))
                                  if is_donation else None),
                    processing_stage='Shelved' if is_donation else None,
                    condition=condition,
                    status='In Stock',
                    qr_label=str(uuid.uuid4()),
                )
                ledger['inventory_ids'].append(record.inventory_id)
                if is_donation:
                    donation_pool.append((book, record))

            # ── Accessioning history ────────────────────────────────────
            rng.shuffle(donation_pool)
            for book, record in donation_pool[:max(0, options['donations'])]:
                donation = Donation.objects.create(
                    book=book,
                    donor_name=record.donor_name or rng.choice(DONORS),
                    date_donated=record.donated_date or (today - timedelta(days=rng.randint(20, 400))),
                    status=rng.choices(['Shelved', 'Shelved', 'Processing', 'Received'], k=1)[0],
                )
                ledger['donation_ids'].append(donation.donation_id)
                # The copy and its accessioning row must agree, or the Donations
                # page and Inventory tell two different stories about one book.
                InventoryRecord.objects.filter(pk=record.pk).update(
                    donation=donation, processing_stage=donation.status)

            # ── Stock-take history ──────────────────────────────────────
            staff = list(User.objects.filter(account_status='Active'))
            audited = rng.sample(shelves, min(len(shelves), max(0, options['audits'])))
            for shelf in audited:
                expected = Book.objects.filter(shelf_level__shelf=shelf).count()
                on_loan = Transaction.objects.filter(
                    book__shelf_level__shelf=shelf, transaction_type='Borrow',
                    return_date__isnull=True).count()
                missing = rng.randint(0, 2) if expected > 6 else 0
                found = max(0, expected - on_loan - missing)
                audit = StockAudit.objects.create(
                    shelf=shelf,
                    shelf_name=shelf.name,
                    audited_by=rng.choice(staff) if staff else None,
                    audited_at=timezone.now() - timedelta(days=rng.randint(1, 60)),
                    expected_count=expected,
                    scanned_count=found,
                    found_count=found,
                    on_loan_count=on_loan,
                    missing_count=missing,
                    recovered_count=rng.randint(0, 1) if missing else 0,
                    unexpected_count=rng.randint(0, 1),
                    notes=('Routine shelf check.' if not missing
                           else 'Gaps found; copies flagged Missing pending a second look.'),
                )
                ledger['audit_ids'].append(audit.audit_id)

        self.write_ledger(ledger)

        self.stdout.write(self.style.SUCCESS('\nCatalogue filled in.'))
        self.stdout.write('  shelf levels created   %d' % len(ledger['shelf_level_ids']))
        self.stdout.write('  books placed           %d' % len(ledger['placed_book_ids']))
        self.stdout.write('  inventory records      %d' % len(ledger['inventory_ids']))
        self.stdout.write('  donations              %d' % len(ledger['donation_ids']))
        self.stdout.write('  stock audits           %d' % len(ledger['audit_ids']))
        self.stdout.write('')
        self.stdout.write('  books still unplaced   %d'
                          % Book.objects.filter(shelf_level__isnull=True).count())
        self.stdout.write('  books with no record   %d'
                          % Book.objects.filter(inventory_records__isnull=True).count())
        self.stdout.write('\nRun "python manage.py seed_catalogue --clear" to undo.')
