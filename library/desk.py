"""The front-desk attendance screen.

Ayla has one computer. It is the staff workstation *and* the screen a patron
touches on the way in, so attendance capture cannot live inside the portal:
a visitor standing at an armed administrator session is the problem this
module exists to remove.

Desk mode is therefore a lock rather than a page. A staff member arms it, the
portal becomes unreachable in that browser, and only this screen answers until
someone types the desk PIN. Capture is public and unprivileged; reviewing the
logs stays in the portal where it belongs.

Nothing here creates a borrowing account. A walk-in either logs a visit as a
visitor or leaves their details for a librarian to turn into a membership after
checking a physical ID — a patron cannot attest to their own identity, which is
the whole point of the on-site check.
"""

from datetime import datetime, timedelta
from uuid import uuid4

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone

from .auth_utils import check_password, hash_password
from .models import DeskSettings, Patron, PatronLog, User


# ─── the lock ─────────────────────────────────────────────────────────────

DESK_SESSION_KEY = 'desk_mode'


def desk_is_armed(request):
    return bool(request.session.get(DESK_SESSION_KEY))


def arm_desk_mode(request):
    """Hand the machine to the public. Requires a signed-in portal user."""
    if request.method != 'POST':
        return redirect('/admin-portal/dashboard/')
    if 'admin_id' not in request.session:
        return redirect('/admin-portal/login/')

    settings_row = DeskSettings.load()
    if not settings_row.pin_is_set:
        # Arming without a PIN would strand the machine on the kiosk screen
        # with no way back, so this is refused rather than worked around.
        messages.error(request, 'Set a desk PIN first — without one there is no way '
                                'to unlock the computer once desk mode starts.')
        return redirect('/admin-portal/desk-settings/')

    request.session[DESK_SESSION_KEY] = True
    request.session.modified = True
    return redirect('desk_attendance')


def unlock_desk_mode(request):
    """Take the machine back. The PIN is short because this happens all day."""
    if request.method != 'POST':
        return redirect('desk_attendance')

    pin = (request.POST.get('pin') or '').strip()
    settings_row = DeskSettings.load()
    if not settings_row.pin_is_set or not check_password(pin, settings_row.pin_hash):
        return JsonResponse({'success': False, 'error': 'That PIN is not correct.'})

    request.session.pop(DESK_SESSION_KEY, None)
    request.session.modified = True
    destination = ('/library-staff/dashboard/'
                   if request.session.get('admin_role') == 'Staff'
                   else '/admin-portal/dashboard/')
    return JsonResponse({'success': True, 'redirect': destination})


# ─── visits that nobody closed ────────────────────────────────────────────

def close_stale_visits():
    """Close visits left open on days that have already ended.

    People leave without logging out. Left alone, `currently_inside` counts
    every one of them for ever and the number only climbs, so a month of real
    use would report a crowd in an empty room. Each stale visit is stamped with
    that day's closing time and flagged `auto_closed`, so a guessed exit stays
    legible as a guess.

    Runs on page load rather than from a scheduler: the library has one
    computer and nobody to maintain a cron job.
    """
    today = timezone.localdate()
    closing = DeskSettings.load().closing_time
    stale = PatronLog.objects.filter(exit_time__isnull=True, entry_time__date__lt=today)

    closed = 0
    for log in stale:
        day = timezone.localtime(log.entry_time).date()
        assumed = timezone.make_aware(datetime.combine(day, closing))
        # Someone who arrived after closing gets a nominal minute, never an
        # exit that precedes their entry.
        if assumed <= log.entry_time:
            assumed = log.entry_time + timedelta(minutes=1)
        log.exit_time = assumed
        log.auto_closed = True
        log.save(update_fields=['exit_time', 'auto_closed'])
        closed += 1
    return closed


# ─── the screen ───────────────────────────────────────────────────────────

def desk_attendance(request):
    """The public attendance screen.

    Only answers in a browser where a staff member armed desk mode, so opening
    this URL from a phone on the library wi-fi gets the idle notice and nothing
    else. The session cookie is the token.
    """
    if not desk_is_armed(request):
        return render(request, 'desk/attendance.html', {'armed': False})

    close_stale_visits()
    return render(request, 'desk/attendance.html', {
        'armed': True,
        'inside_now': PatronLog.objects.filter(exit_time__isnull=True).count(),
        'purpose_choices': ['Study', 'Research', 'Borrow a book', 'Reading',
                            'Internet use', 'Other'],
    })


# ─── what the screen calls ────────────────────────────────────────────────

def _require_armed(request):
    if not desk_is_armed(request):
        return JsonResponse({'success': False, 'error': 'This screen is not in desk mode.'})
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST is allowed.'})
    return None


def _toggle_visit(patron, purpose='', school=''):
    """One action for arriving and leaving: whichever the patron is not doing.

    Asking someone at the door to choose between two buttons invites the wrong
    one; an open visit can only be ended and a closed one can only be started,
    so the system already knows which this is.
    """
    open_visit = (PatronLog.objects
                  .filter(patron=patron, exit_time__isnull=True)
                  .order_by('-entry_time')
                  .first())

    if open_visit is not None:
        # A second scan seconds after the first is a stutter, not a departure.
        age = timezone.now() - open_visit.entry_time
        if age < timedelta(seconds=30):
            return {
                'success': True, 'action': 'already_in',
                'name': patron.fullname.split(' ')[0],
                'since': timezone.localtime(open_visit.entry_time).strftime('%I:%M %p'),
            }
        open_visit.exit_time = timezone.now()
        open_visit.save(update_fields=['exit_time'])
        minutes = int((open_visit.exit_time - open_visit.entry_time).total_seconds() // 60)
        stay = (f'{minutes // 60}h {minutes % 60}m' if minutes >= 60 else f'{minutes}m')
        return {'success': True, 'action': 'exit',
                'name': patron.fullname.split(' ')[0], 'stay': stay}

    log = PatronLog.objects.create(
        patron=patron,
        purpose_of_visit=purpose or None,
        school=school or None,
        entry_time=timezone.now(),
    )
    return {'success': True, 'action': 'entry',
            'name': patron.fullname.split(' ')[0],
            'is_visitor': patron.account_status == 'Visitor',
            'at': timezone.localtime(log.entry_time).strftime('%I:%M %p')}


def desk_scan(request):
    """A library card was scanned. The QR is unique, so there is nothing to ask."""
    blocked = _require_armed(request)
    if blocked:
        return blocked

    code = (request.POST.get('code') or '').strip()
    if not code:
        return JsonResponse({'success': False, 'error': 'Nothing was scanned.'})

    patron = Patron.objects.filter(qr_code=code).first()
    if patron is None:
        return JsonResponse({'success': False, 'error': 'That card was not recognised. '
                                                        'Please see the librarian.'})
    if patron.account_status in ('Suspended', 'Inactive'):
        return JsonResponse({'success': False, 'error': 'This account is not active. '
                                                        'Please see the librarian.'})
    return JsonResponse(_toggle_visit(patron, request.POST.get('purpose', '')))


def desk_lookup(request):
    """Find someone who did not bring their card.

    Deliberately never returns names. Showing a stranger a list of people who
    use this library would leak who they are and let anyone log in as the one
    they liked the look of, so the last four digits of the contact number do
    the disambiguating instead of the patron's own choice.
    """
    blocked = _require_armed(request)
    if blocked:
        return blocked

    name = (request.POST.get('name') or '').strip()
    last4 = (request.POST.get('last4') or '').strip()
    if len(name) < 2:
        return JsonResponse({'success': False, 'error': 'Please type your full name.'})

    matches = Patron.objects.filter(fullname__iexact=name).exclude(
        account_status__in=('Suspended', 'Inactive'))
    if not matches.exists():
        matches = Patron.objects.filter(fullname__icontains=name).exclude(
            account_status__in=('Suspended', 'Inactive'))

    count = matches.count()
    if count == 0:
        return JsonResponse({'success': True, 'status': 'not_found'})

    if not last4:
        # One match or ten, the question is the same, so the reply gives away
        # nothing about how many people share the name.
        return JsonResponse({'success': True, 'status': 'need_pin'})

    if not (last4.isdigit() and len(last4) == 4):
        return JsonResponse({'success': True, 'status': 'bad_pin'})

    for patron in matches:
        digits = ''.join(ch for ch in (patron.contact_number or '') if ch.isdigit())
        if digits and digits[-4:] == last4:
            result = _toggle_visit(patron, request.POST.get('purpose', ''))
            result['status'] = 'logged'
            return JsonResponse(result)

    return JsonResponse({'success': True, 'status': 'no_match'})


def desk_visitor(request):
    """Log a visit for someone who is not a member and does not want to be.

    Anyone may walk into a public library and read; membership is only needed
    to take a book home. A returning visitor is matched on name plus the last
    four digits so their history stays on one row instead of scattering.
    """
    blocked = _require_armed(request)
    if blocked:
        return blocked

    name = (request.POST.get('name') or '').strip()
    contact = (request.POST.get('contact_number') or '').strip()
    purpose = (request.POST.get('purpose') or '').strip()
    school = (request.POST.get('school') or '').strip()
    if len(name) < 2:
        return JsonResponse({'success': False, 'error': 'Please type your full name.'})

    digits = ''.join(ch for ch in contact if ch.isdigit())
    existing = None
    if digits:
        for candidate in Patron.objects.filter(fullname__iexact=name,
                                               account_status='Visitor'):
            known = ''.join(ch for ch in (candidate.contact_number or '') if ch.isdigit())
            if known and known[-4:] == digits[-4:]:
                existing = candidate
                break

    if existing is None:
        existing = Patron.objects.create(
            fullname=name,
            email=None,                       # not asked for at the door
            contact_number=contact or None,
            patron_type='General Visitor',
            account_status='Visitor',
            password_hash=hash_password(None),   # unusable: visitors do not sign in
            qr_code=None,                        # and cannot borrow
            registration_channel='On-site',
            otp_verified=False,
        )

    result = _toggle_visit(existing, purpose, school)
    result['status'] = 'logged'
    return JsonResponse(result)


def desk_registration_request(request):
    """Hand a would-be member to the librarian without losing their typing.

    On-site registration requires a staff member to confirm they have seen a
    physical ID, and nobody can attest to their own. So the details entered
    here become a pending registration for the desk to finish, and the visit is
    logged either way — they came in regardless of whether they end up joining.
    """
    blocked = _require_armed(request)
    if blocked:
        return blocked

    name = (request.POST.get('name') or '').strip()
    contact = (request.POST.get('contact_number') or '').strip()
    email = (request.POST.get('email') or '').strip()
    purpose = (request.POST.get('purpose') or '').strip()
    if len(name) < 2:
        return JsonResponse({'success': False, 'error': 'Please type your full name.'})
    if email and Patron.objects.filter(email__iexact=email).exists():
        return JsonResponse({'success': False,
                             'error': 'That email is already registered. '
                                      'Please see the librarian.'})

    applicant = Patron.objects.create(
        fullname=name,
        email=email or None,
        contact_number=contact or None,
        patron_type='General Visitor',
        account_status='Pending',
        password_hash=hash_password(None),
        qr_code=None,
        registration_channel='On-site',
        otp_verified=False,
    )
    _toggle_visit(applicant, purpose)
    return JsonResponse({'success': True, 'status': 'handed_off',
                         'name': applicant.fullname.split(' ')[0]})


# ─── settings ─────────────────────────────────────────────────────────────

def desk_settings(request):
    """Administrator page: the desk PIN and the library's closing time."""
    from .auth_utils import admin_only_required   # local: avoids a cycle at import

    @admin_only_required
    def _view(request):
        row = DeskSettings.load()
        if request.method == 'POST':
            pin = (request.POST.get('pin') or '').strip()
            confirm = (request.POST.get('pin_confirm') or '').strip()
            closing = (request.POST.get('closing_time') or '').strip()

            if pin or confirm:
                if not (pin.isdigit() and 4 <= len(pin) <= 8):
                    messages.error(request, 'The desk PIN must be 4 to 8 digits.')
                    return redirect('desk_settings')
                if pin != confirm:
                    messages.error(request, 'The two PINs do not match.')
                    return redirect('desk_settings')
                row.pin_hash = hash_password(pin)

            if closing:
                try:
                    row.closing_time = datetime.strptime(closing, '%H:%M').time()
                except ValueError:
                    messages.error(request, 'Closing time must look like 17:00.')
                    return redirect('desk_settings')

            row.updated_by = User.objects.filter(
                admin_id=request.session.get('admin_id')).first()
            row.save()
            messages.success(request, 'Desk settings saved.')
            return redirect('desk_settings')

        return render(request, 'admin/desksettings.html', {
            'desk': row,
            'open_visits': PatronLog.objects.filter(exit_time__isnull=True).count(),
        })

    return _view(request)


def close_open_visits_now(request):
    """Manual sweep, for when staff know the room is empty."""
    from .auth_utils import admin_login_required

    @admin_login_required
    def _view(request):
        if request.method != 'POST':
            return redirect('desk_settings')
        closed = close_stale_visits()
        today_open = PatronLog.objects.filter(exit_time__isnull=True)
        now = timezone.now()
        for log in today_open:
            log.exit_time = now
            log.auto_closed = True
            log.save(update_fields=['exit_time', 'auto_closed'])
            closed += 1
        messages.success(request, f'Closed {closed} open visit(s).')
        return redirect(request.POST.get('next') or 'desk_settings')

    return _view(request)
