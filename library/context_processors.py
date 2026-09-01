"""Template context shared across the portal templates."""

from .models import Book, Conversation, Patron, Transaction, User


def staff_modules(request):
    """Expose the signed-in Staff account's granted modules to every template.

    Lets templates/library_staff/_sidebar.html hide the modules an account was
    not granted without every staff view having to pass them in. Admins get all
    keys, so admin templates are unaffected.
    """
    admin_id = request.session.get('admin_id')
    if not admin_id:
        return {}
    user = User.objects.filter(admin_id=admin_id).first()
    if user is None:
        return {}
    # Surfaced on every portal page: an enquiry nobody has answered should be
    # visible from wherever the librarian happens to be working, not only from
    # the Messages page they have no reason to open.
    return {
        'allowed_modules': user.module_keys,
        # The header's profile menu shows who is signed in on every page. The
        # account row is already loaded above for the module check, so exposing
        # it costs nothing and saves 25 templates each passing it in.
        'portal_user': user,
        'unanswered_messages': Conversation.objects.filter(status='Open').count(),
        # Badged in the sidebar: a returned book sitting in a trolley is
        # invisible work, and invisible work does not get done.
        'reshelving_count': Book.objects.filter(status='For Reshelving').count(),
        # Badged on the Transactions nav item. This same query was copy-pasted
        # into ten separate views, so ten places had to remember to pass it and
        # any change to what "pending" means had to be made ten times.
        'pending_transactions_count': Transaction.objects.filter(
            transaction_type='Borrow', return_date__isnull=True).count(),
    }


def patron_session(request):
    """Whether a patron is signed in, for the patron portal's templates.

    The catalogue, book records, map and announcements are open to visitors
    who have not registered, so every one of those templates has to be able to
    tell the two apart -- to keep the Account tab in place while making it ask
    for a sign-in rather than opening a page that has nothing in it.
    """
    patron_id = request.session.get('patron_id')
    if not patron_id:
        return {'patron_signed_in': False, 'signed_in_patron': None}
    patron = Patron.objects.filter(patron_id=patron_id).first()
    return {'patron_signed_in': patron is not None, 'signed_in_patron': patron}
