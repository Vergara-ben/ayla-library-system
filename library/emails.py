"""Email notifications for the AYLA library system.

Covers the manuscript's automated alerts: overdue reminders and
announcement emails. All sends are best-effort and never raise to the
caller; the configured backend is real SMTP when credentials are set,
otherwise the console backend (see settings).
"""

from django.conf import settings
from django.core.mail import send_mail


def _library_name():
    return getattr(settings, 'LIBRARY_NAME', 'Ayla Public Library')


def send_email(subject, message, recipient):
    """Send a single plain-text email. Returns True on success, False otherwise."""
    if not recipient:
        return False
    try:
        sent = send_mail(
            subject,
            message,
            getattr(settings, 'DEFAULT_FROM_EMAIL', None),
            [recipient],
            fail_silently=False,
        )
        return bool(sent)
    except Exception:
        return False


def overdue_email(patron, transactions):
    """Email a patron a reminder listing their overdue book(s)."""
    lib = _library_name()
    lines = [
        f"Dear {patron.fullname},",
        "",
        "Our records show that the following borrowed item(s) are overdue:",
        "",
    ]
    for tx in transactions:
        due = tx.due_date.strftime('%B %d, %Y') if tx.due_date else 'N/A'
        title = tx.book.title if tx.book else 'Unknown title'
        lines.append(f"  - {title} (due {due})")
    lines += [
        "",
        "Please return them at your earliest convenience to avoid further penalties.",
        "",
        "Thank you,",
        lib,
    ]
    return send_email(f"[{lib}] Overdue Book Reminder", "\n".join(lines), patron.email)


def borrow_confirmation_email(patron, books, due_date):
    """Email a patron confirming the book(s) they just borrowed and the due date."""
    lib = _library_name()
    due = due_date.strftime('%B %d, %Y') if due_date else 'N/A'
    lines = [
        f"Dear {patron.fullname},",
        "",
        "You have successfully borrowed the following item(s):",
        "",
    ]
    for book in books:
        title = book.title if hasattr(book, 'title') else str(book)
        lines.append(f"  - {title}")
    lines += [
        "",
        f"Please return them on or before {due} to avoid penalties.",
        "",
        "Thank you,",
        lib,
    ]
    return send_email(f"[{lib}] Borrowing Confirmation", "\n".join(lines), patron.email)


def return_receipt_email(patron, books, had_overdue=False):
    """Email a patron a receipt confirming the book(s) they just returned."""
    lib = _library_name()
    lines = [
        f"Dear {patron.fullname},",
        "",
        "We have received the following returned item(s):",
        "",
    ]
    for book in books:
        title = book.title if hasattr(book, 'title') else str(book)
        lines.append(f"  - {title}")
    lines.append("")
    if had_overdue:
        lines.append("Note: one or more of these items were returned past the due date.")
        lines.append("")
    lines += [
        "Thank you for returning your book(s).",
        "",
        lib,
    ]
    return send_email(f"[{lib}] Return Receipt", "\n".join(lines), patron.email)


def announcement_email(patron, announcement):
    """Email a patron a single library announcement."""
    lib = _library_name()
    body = (
        f"Dear {patron.fullname},\n\n"
        f"{announcement.title}\n\n"
        f"{announcement.message}\n\n"
        f"— {lib}"
    )
    return send_email(f"[{lib}] {announcement.title}", body, patron.email)
