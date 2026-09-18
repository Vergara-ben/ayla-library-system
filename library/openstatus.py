"""Library open or closed: switched on the dashboard, closed by itself at closing time."""

from datetime import datetime

from django.http import JsonResponse
from django.utils import timezone

from .audit import log_admin_action, log_system_action
from .auth_utils import admin_login_required
from .models import LibraryStatus, User


def _clock(value):
    # Formatted by hand so it works on Windows.
    return value.strftime('%I:%M %p').lstrip('0')


CLOSING_LABEL = _clock(LibraryStatus.CLOSING_TIME)


def _day(value):
    return '%s %d' % (value.strftime('%b'), value.day)


def _closing_on(day):
    return timezone.make_aware(datetime.combine(day, LibraryStatus.CLOSING_TIME))


def past_closing(now=None):
    now = now or timezone.now()
    return now >= _closing_on(timezone.localdate(now))


def current_status():
    """The status, closed first if it was left open past closing time."""
    status, _ = LibraryStatus.objects.select_related('changed_by').get_or_create(pk=1)
    if not status.is_open:
        return status

    deadline = _closing_on(timezone.localdate(status.changed_at))
    if timezone.now() < deadline:
        return status

    # Matching changed_at keeps a newer manual change from being overwritten.
    closed = (LibraryStatus.objects
              .filter(pk=status.pk, is_open=True, changed_at=status.changed_at)
              .update(is_open=False, changed_at=deadline, changed_by=None, auto_closed=True))
    if closed:
        log_system_action('Auto-close', 'Library Status', status.pk,
                          'Closed the library at %s on %s because nobody switched it off'
                          % (CLOSING_LABEL, _day(deadline)))
    status.refresh_from_db()
    return status


def _note(status, now):
    changed = timezone.localtime(status.changed_at)
    when = _clock(changed) if changed.date() == timezone.localdate(now) else _day(changed)
    who = ' by %s' % status.changed_by.fullname if status.changed_by else ''

    if status.is_open:
        return 'Opened %s%s. Closes automatically at %s.' % (when, who, CLOSING_LABEL)
    if past_closing(now):
        return 'Closing time (%s) has passed. It can be opened again tomorrow.' % CLOSING_LABEL
    if status.auto_closed:
        return 'Closed automatically at %s on %s.' % (CLOSING_LABEL, _day(changed))
    if status.changed_by:
        return 'Closed %s%s.' % (when, who)
    return 'Switch on when the library opens.'


def status_context(status=None):
    """What the dashboard switch and the patron badge need."""
    status = status or current_status()
    now = timezone.now()
    until_closing = (_closing_on(timezone.localdate(now)) - now).total_seconds()
    return {
        'is_open': status.is_open,
        'can_open': until_closing > 0,
        'closing_label': CLOSING_LABEL,
        'note': _note(status, now),
        # Lets an open dashboard update itself at closing time.
        'seconds_to_closing': int(until_closing) + 1 if until_closing > 0 else None,
    }


@admin_login_required
def library_status(request):
    """GET reads the status, POST open=1 or open=0 sets it."""
    if request.method == 'GET':
        return JsonResponse({'success': True, **status_context()})
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only GET or POST is allowed.'}, status=405)

    want_open = request.POST.get('open') == '1'
    status = current_status()

    if want_open and past_closing():
        return JsonResponse({'success': False, **status_context(status),
                             'error': 'It is past %s, so the library stays closed until '
                                      'tomorrow.' % CLOSING_LABEL})

    if status.is_open != want_open:
        status.is_open = want_open
        status.changed_at = timezone.now()
        status.changed_by = User.objects.filter(admin_id=request.session.get('admin_id')).first()
        status.auto_closed = False
        status.save()
        log_admin_action(request, 'Update', 'Library Status', status.pk,
                         'Opened the library' if want_open else 'Closed the library')

    return JsonResponse({'success': True, **status_context(status)})
