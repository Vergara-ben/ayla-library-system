"""Template context shared across the portal templates."""

from .models import Book, Conversation, Patron, Transaction, User


def staff_modules(request):
    """Expose the signed-in Staff account's granted modules to every template."""
    admin_id = request.session.get('admin_id')
    if not admin_id:
        return {}
    user = User.objects.filter(admin_id=admin_id).first()
    if user is None:
        return {}
    # Unread enquiry count for the portal pages.
    return {
        'allowed_modules': user.module_keys,
        # The header's profile menu shows who is signed in on every page.
        'portal_user': user,
        'unanswered_messages': Conversation.objects.filter(status='Open').count(),
        # Reshelving count for the sidebar.
        'reshelving_count': Book.objects.filter(status='For Reshelving').count(),
        # Badged on the Transactions nav item.
        'pending_transactions_count': Transaction.objects.filter(
            transaction_type='Borrow', return_date__isnull=True).count(),
    }


def patron_session(request):
    """Whether a patron is signed in, for the patron portal's templates."""
    patron_id = request.session.get('patron_id')
    if not patron_id:
        return {'patron_signed_in': False, 'signed_in_patron': None}
    patron = Patron.objects.filter(patron_id=patron_id).first()
    return {'patron_signed_in': patron is not None, 'signed_in_patron': patron}
