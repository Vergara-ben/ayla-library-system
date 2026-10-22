"""What at the circulation desk needs someone now: loans past their due date, and extension requests."""

import time

from django.utils import timezone

from .models import Book, DueDateExtension, Transaction

_last_flagged = 0.0


def overdue_loans():
    """Out past the due date, from the day after it, whether or not the daily run has flagged them."""
    return (Transaction.objects.filter(transaction_type='Borrow', return_date__isnull=True,
                                       due_date__lt=timezone.localdate())
            .exclude(marked_lost=True))


def waiting_extensions():
    return DueDateExtension.objects.filter(status='Pending')


def flag_overdue_now():
    """Mark loans overdue as soon as their date passes, instead of at the next daily run."""
    global _last_flagged
    if time.monotonic() - _last_flagged < 60:
        return
    _last_flagged = time.monotonic()
    late = overdue_loans().filter(overdue_flag=False)
    book_ids = list(late.values_list('book_id', flat=True))
    if book_ids:
        late.update(overdue_flag=True)
        Book.objects.filter(book_id__in=book_ids, status='Borrowed').update(status='Overdue')


def attention():
    """{'overdue': n, 'extensions': m, 'total': n + m}"""
    flag_overdue_now()
    overdue, extensions = overdue_loans().count(), waiting_extensions().count()
    return {'overdue': overdue, 'extensions': extensions, 'total': overdue + extensions}
