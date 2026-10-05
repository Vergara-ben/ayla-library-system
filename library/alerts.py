"""What is waiting on the library's people, for the pop-up alerts on every portal page."""

from django.db.models import Q
from django.http import JsonResponse
from django.urls import reverse

from .models import Conversation, DueDateExtension, Patron, Transaction, User


def _may(user, role, module):
    """Administrators may act on patrons; everything else needs the module granted."""
    if module == 'patrons' and role != 'Staff':
        return True
    return user.has_module(module)


def _portal_url(role, admin_name, staff_name):
    return reverse(staff_name if role == 'Staff' else admin_name)


def portal_alerts(request):
    """Counts and the newest item of each kind this account can act on."""
    user = User.objects.filter(admin_id=request.session.get('admin_id'),
                               account_status='Active').first()
    if user is None:
        return JsonResponse({'success': False}, status=403)
    role = user.role
    items = []

    if _may(user, role, 'patrons'):
        # Desk sign-ups, and online ones whose email is confirmed; not those sent back to fix.
        waiting = (Patron.objects.filter(account_status='Pending', fix_token__isnull=True)
                   .filter(Q(otp_verified=True) | Q(registration_channel='On-site')))
        newest_new = waiting.order_by('-patron_id').first()
        newest_back = waiting.filter(resubmitted_at__isnull=False).order_by('-resubmitted_at').first()
        # A resubmission is news even though the applicant's id is old; name whichever came last.
        newest = newest_new
        if newest_back and (newest_new is None
                            or newest_back.resubmitted_at.date() >= newest_new.registration_date):
            newest = newest_back
        items.append({
            'key': 'registrations', 'count': waiting.count(),
            'latest': newest_new.patron_id if newest_new else 0,
            'stamp': newest_back.resubmitted_at.isoformat() if newest_back else '',
            'text': (('%s resubmitted their registration' if newest.resubmitted_at
                      else '%s registered online and is waiting for approval') % newest.fullname)
                    if newest else '',
            'title': 'New registration', 'icon': 'fa-user-plus',
            'url': _portal_url(role, 'admin_manage_patron', 'staff_manage_patron'),
        })

    if _may(user, role, 'transactions'):
        requests = (DueDateExtension.objects.filter(status='Pending')
                    .select_related('transaction__book', 'transaction__patron')
                    .order_by('-extension_id'))
        newest = requests.first()
        items.append({
            'key': 'extensions', 'count': requests.count(),
            'latest': newest.extension_id if newest else 0,
            'text': ('%s asked for more time with "%s"' % (
                newest.transaction.patron.fullname if newest.transaction.patron else 'A patron',
                newest.transaction.book.title)) if newest else '',
            'title': 'Extension request', 'icon': 'fa-hourglass-half',
            'url': _portal_url(role, 'admin_transaction', 'staff_transaction'),
        })
        overdue = (Transaction.objects.filter(transaction_type='Borrow', return_date__isnull=True,
                                              overdue_flag=True)
                   .select_related('book', 'patron').order_by('-transaction_id'))
        newest = overdue.first()
        count = overdue.count()
        items.append({
            'key': 'overdue', 'count': count, 'latest': newest.transaction_id if newest else 0,
            'text': ('%d loan%s overdue, the newest "%s"' % (count, '' if count == 1 else 's',
                     newest.book.title)) if newest else '',
            'title': 'Overdue loans', 'icon': 'fa-triangle-exclamation',
            'url': _portal_url(role, 'admin_transaction', 'staff_transaction') + '?status=overdue',
        })

    if _may(user, role, 'chat'):
        open_threads = (Conversation.objects.filter(status='Open')
                        .select_related('patron').order_by('-last_message_at'))
        newest = open_threads.first()
        items.append({
            'key': 'messages', 'count': open_threads.count(),
            'latest': newest.conversation_id if newest else 0,
            'stamp': newest.last_message_at.isoformat() if newest and newest.last_message_at else '',
            'text': ('%s sent the library a message' % newest.patron.fullname) if newest else '',
            'title': 'New message', 'icon': 'fa-comments',
            'url': reverse('admin_messages') + ('?p=%d' % newest.patron_id if newest else ''),
        })

    return JsonResponse({'success': True, 'items': items})
