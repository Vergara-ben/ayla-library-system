"""Fill the Transactions table with a plausible history for demonstration.

The page, the dashboard counters and the reports all read from Transactions,
so with an empty table there is nothing to look at and nothing to screenshot.
This writes a spread of borrows, returns, overdues and in-library reading
across the books and patrons that already exist.

    python manage.py seed_transactions                 40 transactions, 8 weeks
    python manage.py seed_transactions --count 120     more history
    python manage.py seed_transactions --weeks 16      spread further back
    python manage.py seed_transactions --clear         remove what this created
    python manage.py seed_transactions --clear --all   remove every transaction

Rows created here are recorded in seeded_transactions.json so --clear can tell
demonstration data apart from anything recorded for real. Without that file it
refuses to guess, which is the only safe answer once real work has been done.
"""

import json
import random
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction as db_transaction
from django.utils import timezone

from library.models import Book, BorrowingRule, Patron, Transaction, User

LEDGER = 'seeded_transactions.json'

# How a loan turned out, and how often. Weighted so the table reads like a
# working library rather than a uniform sample: most loans come back, a few
# come back late, and a handful are still out.
OUTCOMES = (
    ['returned_on_time'] * 55
    + ['returned_late'] * 18
    + ['still_out'] * 17
    + ['still_out_overdue'] * 10
)


class Command(BaseCommand):
    help = 'Create demonstration borrow/return history in the Transactions table.'

    def add_arguments(self, parser):
        parser.add_argument('--count', type=int, default=40,
                            help='how many transactions to create (default 40)')
        parser.add_argument('--weeks', type=int, default=8,
                            help='how far back to spread them (default 8)')
        parser.add_argument('--seed', type=int, default=None,
                            help='fix the random seed so a run can be repeated')
        parser.add_argument('--clear', action='store_true',
                            help='remove transactions this command created')
        parser.add_argument('--all', action='store_true',
                            help='with --clear, remove every transaction instead')

    # ── helpers ──────────────────────────────────────────────────────────
    @property
    def ledger_path(self):
        return settings.BASE_DIR / LEDGER

    def read_ledger(self):
        try:
            with open(self.ledger_path, encoding='utf-8') as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def write_ledger(self, ids):
        with open(self.ledger_path, 'w', encoding='utf-8') as fh:
            json.dump({'transaction_ids': ids,
                       'created': timezone.now().isoformat()}, fh, indent=1)

    # ── entry point ──────────────────────────────────────────────────────
    def handle(self, *args, **options):
        if options['clear']:
            return self.clear(options['all'])
        return self.seed(options)

    def clear(self, everything):
        if everything:
            count = Transaction.objects.count()
            Transaction.objects.all().delete()
            Book.objects.exclude(status__in=('Lost', 'Donated')).update(status='Available')
            self.write_ledger([])
            self.stdout.write(self.style.WARNING(
                'Deleted all %d transaction(s) and reset book statuses.' % count))
            return

        ledger = self.read_ledger()
        if ledger is None:
            self.stdout.write(self.style.ERROR(
                'No %s found, so there is no way to tell demonstration rows from real '
                'ones.\nRun with --clear --all to delete every transaction, if that is '
                'really what you want.' % LEDGER))
            return

        ids = ledger.get('transaction_ids') or []
        removed = Transaction.objects.filter(transaction_id__in=ids)
        # Freeing the books first, or the copies stay marked out for ever.
        book_ids = list(removed.values_list('book_id', flat=True))
        count = removed.count()
        removed.delete()
        Book.objects.filter(book_id__in=book_ids).exclude(
            status__in=('Lost', 'Donated')).update(status='Available')
        self.write_ledger([])
        self.stdout.write(self.style.SUCCESS(
            'Removed %d seeded transaction(s) and freed %d book(s).' % (count, len(set(book_ids)))))

    def seed(self, options):
        rng = random.Random(options['seed'])
        rule = BorrowingRule.current()
        today = timezone.localdate()

        patrons = list(Patron.objects.all())
        books = list(Book.objects.exclude(status__in=('Lost', 'Donated')))
        staff = list(User.objects.filter(account_status='Active'))

        if not patrons:
            self.stdout.write(self.style.ERROR('No patrons exist — register one first.'))
            return
        if not books:
            self.stdout.write(self.style.ERROR('No books exist — add the catalogue first.'))
            return

        # A one-day loan with no fine makes every overdue read as ₱0.00, which
        # looks like a broken calculation rather than a policy. Say so plainly
        # rather than producing a table that invites the question at a defence.
        if rule.fine_per_day == 0:
            self.stdout.write(self.style.WARNING(
                'Borrowing Rules has fine_per_day = 0.00, so overdue rows will show a '
                '₱0.00 fine.\nSet a fine on the Borrowing Rules page if the penalty '
                'should be visible.'))
        if rule.loan_period_days <= 1:
            self.stdout.write(self.style.WARNING(
                'Loan period is %d day(s); due dates will sit almost on top of borrow '
                'dates.' % rule.loan_period_days))

        count = max(1, options['count'])
        window = max(1, options['weeks']) * 7

        created = []
        # A copy can only be out once at a time, and a patron may not exceed the
        # borrowing limit, so open loans are tracked as they are handed out.
        open_books = set()
        open_per_patron = {p.patron_id: 0 for p in patrons}
        book_status = {}

        with db_transaction.atomic():
            for _ in range(count):
                outcome = rng.choice(OUTCOMES)
                book = rng.choice(books)
                patron = rng.choice(patrons)

                borrowed_on = today - timedelta(days=rng.randint(1, window))
                due = borrowed_on + timedelta(days=rule.loan_period_days)

                still_out = outcome.startswith('still_out')
                if still_out:
                    at_limit = open_per_patron[patron.patron_id] >= rule.max_books_per_patron
                    if book.book_id in open_books or at_limit:
                        # Cannot leave this one out; record it as returned instead
                        # of skipping, so --count still means what it says.
                        outcome = 'returned_on_time'
                        still_out = False

                if outcome == 'returned_on_time':
                    span = max(0, rule.loan_period_days)
                    returned_on = borrowed_on + timedelta(days=rng.randint(0, span) if span else 0)
                    returned_on = min(returned_on, today)
                elif outcome == 'returned_late':
                    returned_on = min(due + timedelta(days=rng.randint(1, 10)), today)
                    if returned_on <= due:
                        returned_on = min(due + timedelta(days=1), today)
                else:
                    returned_on = None

                if outcome == 'still_out_overdue':
                    # Force it genuinely past due, whatever the loan period is.
                    borrowed_on = today - timedelta(days=rule.loan_period_days
                                                    + rng.randint(1, 14))
                    due = borrowed_on + timedelta(days=rule.loan_period_days)

                overdue = bool(due and ((returned_on and returned_on > due)
                                        or (returned_on is None and today > due)))
                fine = rule.compute_fine(due, returned_on or today) if overdue else 0

                tx = Transaction.objects.create(
                    patron=patron,
                    book=book,
                    processed_by=rng.choice(staff) if staff else None,
                    transaction_type='Borrow',
                    due_date=due,
                    return_date=returned_on,
                    overdue_flag=overdue,
                    fine_amount=fine,
                )
                # transaction_date is auto_now_add, so it ignores anything passed
                # to create(). A second write is the only way to backdate it.
                Transaction.objects.filter(pk=tx.pk).update(transaction_date=borrowed_on)
                created.append(tx.transaction_id)

                if still_out:
                    open_books.add(book.book_id)
                    open_per_patron[patron.patron_id] += 1
                    book_status[book.book_id] = 'Overdue' if overdue else 'Borrowed'
                else:
                    book_status.setdefault(book.book_id, 'Available')

            # A few in-library reads, which carry no patron and no due date.
            for _ in range(max(1, count // 8)):
                book = rng.choice(books)
                if book.book_id in open_books:
                    continue
                read_on = today - timedelta(days=rng.randint(1, window))
                tx = Transaction.objects.create(
                    patron=None, book=book,
                    processed_by=rng.choice(staff) if staff else None,
                    transaction_type='In-Library Reading',
                )
                Transaction.objects.filter(pk=tx.pk).update(transaction_date=read_on)
                created.append(tx.transaction_id)

            for book_id, status in book_status.items():
                Book.objects.filter(book_id=book_id).update(status=status)

        self.write_ledger(created)

        out = Transaction.objects.filter(transaction_type='Borrow', return_date__isnull=True)
        self.stdout.write(self.style.SUCCESS('\nCreated %d transaction(s).' % len(created)))
        self.stdout.write('  still on loan   %d' % out.count())
        self.stdout.write('  overdue         %d' % Transaction.objects.filter(overdue_flag=True).count())
        self.stdout.write('  returned        %d' % Transaction.objects.filter(
            return_date__isnull=False).count())
        self.stdout.write('  in-library      %d' % Transaction.objects.filter(
            transaction_type='In-Library Reading').count())
        self.stdout.write('  dated between   %s and %s'
                          % (today - timedelta(days=window), today))
        self.stdout.write('\nRun "python manage.py seed_transactions --clear" to remove these.')
