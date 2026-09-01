"""Flag overdue borrows and email patrons their overdue reminders.

Run on a schedule (e.g. a daily PythonAnywhere scheduled task):

    python manage.py send_overdue_notifications

Use --dry-run to preview without sending email or changing data.
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from library.models import Transaction, BorrowingRule
from library.emails import bulk_connection, overdue_email


class Command(BaseCommand):
    help = "Flag overdue borrows and email patrons their overdue reminders."

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help="Preview overdue items without sending email or changing data.",
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        today = timezone.localdate()
        rule = BorrowingRule.current()

        overdue_txns = (Transaction.objects
                        .select_related('patron', 'book')
                        .filter(transaction_type='Borrow',
                                return_date__isnull=True,
                                due_date__lt=today))

        by_patron = {}
        flagged = 0
        for tx in overdue_txns:
            flagged += 1
            if not dry_run:
                tx.overdue_flag = True
                # Accrued penalty to date (finalised on actual return).
                tx.fine_amount = rule.compute_fine(tx.due_date, today)
                tx.save(update_fields=['overdue_flag', 'fine_amount'])
                if tx.book and tx.book.status != 'Overdue':
                    tx.book.status = 'Overdue'
                    tx.book.save(update_fields=['status'])
            if tx.patron:
                by_patron.setdefault(tx.patron, []).append(tx)

        sent = failed = 0
        # One SMTP connection for the whole run rather than one per patron.
        connection = None if dry_run else bulk_connection()
        try:
            for patron, txns in by_patron.items():
                if dry_run:
                    self.stdout.write(
                        f"[dry-run] {patron.email or '(no email)'}: {len(txns)} overdue item(s)")
                    continue
                if patron.email and overdue_email(patron, txns, connection=connection):
                    sent += 1
                else:
                    failed += 1
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

        if not dry_run and (flagged or sent or failed):
            # A dry run changes nothing, so it leaves no trail; a real run marks
            # books overdue and emails patrons without any person behind it,
            # which is exactly what the System role is for.
            from library.audit import log_system_action
            log_system_action('Notify', 'Transaction', None,
                              f'Overdue sweep: {flagged} item(s) flagged, '
                              f'{sent} patron(s) notified, {failed} failed')

        self.stdout.write(self.style.SUCCESS(
            f"Overdue items: {flagged} | patrons notified: {sent} | "
            f"failed: {failed} | dry-run: {dry_run}"))
