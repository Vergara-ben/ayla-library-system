"""Borrowing eligibility checks."""

from django.db.models import Q
from django.utils import timezone

from .models import Transaction


def check_patron_eligibility(patron):
    """Return (eligible: bool, violations: list[str]) for the 3-point check."""
    violations = []
    today = timezone.localdate()

    # 1. Account status must be Active.
    if patron.account_status != 'Active':
        violations.append(f"Account is {patron.account_status.lower()}")

    active_borrows = Transaction.objects.filter(
        patron=patron,
        transaction_type='Borrow',
        return_date__isnull=True,
    )

    # 2. No overdue items (past due, already flagged, or book marked Overdue).
    has_overdue = active_borrows.filter(
        Q(due_date__lt=today) | Q(overdue_flag=True) | Q(book__status='Overdue')
    ).exists()
    if has_overdue:
        violations.append("Has overdue item(s)")

    # 3. No outstanding lost-book penalty (an unreturned borrow whose book is Lost).
    has_lost = active_borrows.filter(book__status='Lost').exists()
    if has_lost:
        violations.append("Has outstanding lost-book penalty")

    return (len(violations) == 0, violations)
