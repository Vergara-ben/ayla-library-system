"""Desk mode: the Log Management page, handed to the patron.

Ayla has one computer. It is the staff workstation *and* the screen a patron
touches on the way in, so attendance capture cannot sit inside a live
administrator session — a visitor standing at an armed portal is the problem
this module exists to remove.

Desk mode is a lock, not a separate screen. A staff member arms it and the
browser is confined to Log Management: every other portal page redirects back
there until someone types their account password. The patron types their visit
straight into the table, or clicks Scan and holds up their library card.

Nothing here creates a borrowing account. A walk-in is logged as a visitor;
turning that into a membership needs a librarian to check a physical ID, and
nobody can attest to their own.
"""

from datetime import datetime, timedelta

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect
from django.utils import timezone

from .auth_utils import check_password, hash_password
from .models import DeskSettings, Patron, PatronLog, User


DESK_SESSION_KEY = 'desk_mode'

# Free-text purposes produce "study", "Study", "studying" and "asdf" in equal
# measure, none of which can be counted. A short list can be.
PURPOSE_CHOICES = [
    'Study',
    'Research',
    'Borrow or return a book',
    'Reading',
    'Internet use',
    'Meeting',
    'Other',
]

# The one page desk mode leaves reachable, per role.
ADMIN_LOG_PATH = '/admin-portal/log-management/'
STAFF_LOG_PATH = '/library-staff/logs/'


def _normalise_name(value):
    """Letters only, lower case — so one person is one person.

    "Juan Dela Cruz", "Juan dela Cruz" and "juan delacruz" are the same visitor
    typing on different days. Comparing raw strings would file them as three
    people and scatter their history across three rows.
    """
    return ''.join(ch for ch in (value or '').lower() if ch.isalpha())


def _digits(value):
    return ''.join(ch for ch in (value or '') if ch.isdigit())


def _find_patron(name, contact):
    """Work out who is standing at the desk.

    Returns (patron, error). The mobile number is the strongest signal, so it
    is tried first: someone who spells their name differently this week is
    still the same person if the number matches, which is what stops the
    visitor list filling up with near-duplicates.
    """
    typed = _normalise_name(name)
    digits = _digits(contact)
    is_email = '@' in contact

    people = list(Patron.objects.exclude(account_status__in=('Suspended', 'Inactive'))
                  .only('patron_id', 'fullname', 'email', 'contact_number',
                        'account_status', 'patron_type'))

    by_name = [p for p in people if _normalise_name(p.fullname) == typed]

    by_contact = []
    if is_email:
        by_contact = [p for p in people if (p.email or '').lower() == contact.lower()]
    elif len(digits) >= 4:
        by_contact = [p for p in people
                      if _digits(p.contact_number) and _digits(p.contact_number)[-4:] == digits[-4:]]

    if contact and by_contact:
        both = [p for p in by_contact if p in by_name]
        if len(both) == 1:
            return both[0], None
        if not by_name and len(by_contact) == 1:
            # Same number, name spelled differently: the same person, not a new one.
            return by_contact[0], None
        if len(both) > 1:
            return None, ('More than one record matches. Please see the librarian.')

    if contact and by_name and not by_contact:
        if len(by_name) > 1:
            return None, ('That email or number does not match anyone by that name. '
                          'Please see the librarian.')
        return by_name[0], None

    if len(by_name) == 1:
        return by_name[0], None
    if len(by_name) > 1:
        # Never say who the others are: a stranger at the desk has no business
        # learning which people share a name here.
        return None, ('More than one person uses that name. Add your email or '
                      'mobile number so we know which is you.')
    return None, None      # nobody by that name — a first-time visitor


def _last_visit_details(patron):
    """What this person put down last time, so they need not type it again."""
    previous = (PatronLog.objects.filter(patron=patron)
                .exclude(school__isnull=True, purpose_of_visit__isnull=True)
                .order_by('-entry_time').first())
    if previous is None:
        return '', ''
    return (previous.school or ''), (previous.purpose_of_visit or '')


def desk_is_armed(request):
    return bool(request.session.get(DESK_SESSION_KEY))


def desk_log_path(request):
    if request.session.get('admin_role') == 'Staff':
        return STAFF_LOG_PATH
    return ADMIN_LOG_PATH


# ─── the lock ─────────────────────────────────────────────────────────────

def arm_desk_mode(request):
    """Hand the machine to the patron. Requires a signed-in portal user."""
    if request.method != 'POST':
        return redirect(desk_log_path(request))
    if 'admin_id' not in request.session:
        return redirect('/admin-portal/login/')

    request.session[DESK_SESSION_KEY] = True
    request.session.modified = True
    return redirect(desk_log_path(request))


def unlock_desk_mode(request):
    """Take the machine back with the account password.

    Checked against the account whose session this is — the person on shift.
    There is no separate desk PIN to set, forget, or leave written on a sticky
    note beside the monitor.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST is allowed.'})

    user = User.objects.filter(admin_id=request.session.get('admin_id')).first()
    password = request.POST.get('password') or ''
    if user is None:
        request.session.flush()
        return JsonResponse({'success': False, 'redirect': '/admin-portal/login/',
                             'error': 'This session has expired. Please sign in again.'})
    if not password or not check_password(password, user.password_hash):
        return JsonResponse({'success': False, 'error': 'That password is not correct.'})

    request.session.pop(DESK_SESSION_KEY, None)
    request.session.modified = True
    destination = ('/library-staff/dashboard/'
                   if request.session.get('admin_role') == 'Staff'
                   else '/admin-portal/dashboard/')
    return JsonResponse({'success': True, 'redirect': destination})


# ─── visits that nobody closed ────────────────────────────────────────────

def close_stale_visits():
    """Close visits left open on days that have already ended.

    People leave without signing out. Left alone, `currently_inside` counts
    every one of them for ever and the number only climbs, so a month of real
    use would report a crowd in an empty room. Each stale visit is stamped with
    that day's closing time and flagged `auto_closed`, so a guessed exit stays
    legible as a guess.

    Runs on page load rather than from a scheduler: the library has one
    computer and nobody to maintain a cron job.
    """
    today = timezone.localdate()
    settings_row = DeskSettings.load()
    stale = PatronLog.objects.filter(exit_time__isnull=True, entry_time__date__lt=today)

    closed = 0
    for log in stale:
        day = timezone.localtime(log.entry_time).date()
        assumed = timezone.make_aware(datetime.combine(day, settings_row.closing_for(day)))
        # Someone who arrived after closing gets a nominal minute, never an
        # exit that precedes their entry.
        if assumed <= log.entry_time:
            assumed = log.entry_time + timedelta(minutes=1)
        log.exit_time = assumed
        log.auto_closed = True
        log.save(update_fields=['exit_time', 'auto_closed'])
        closed += 1
    return closed


# ─── signing in and out ───────────────────────────────────────────────────

def _open_visit_for(patron):
    return (PatronLog.objects
            .filter(patron=patron, exit_time__isnull=True)
            .order_by('-entry_time')
            .first())


def _sign_in(patron, purpose='', school=''):
    """Record an arrival, and only ever an arrival.

    Typing a name used to toggle, which meant someone who went to lunch without
    signing out was marked as *leaving* when they came back — after which the
    system believed they were outside while they sat in the reading room. A
    typed name now means "I have just arrived" and nothing else; departures go
    through the Sign out button on the visitor's own row, where there is no
    ambiguity to resolve.
    """
    open_visit = _open_visit_for(patron)
    if open_visit is not None:
        return {'success': True, 'action': 'already_in', 'name': patron.fullname,
                'message': (patron.fullname.split(' ')[0] + ', you are already signed in '
                            'since ' + timezone.localtime(open_visit.entry_time)
                            .strftime('%I:%M %p') + '. Press Sign out on your row when '
                            'you leave.')}

    # Blank details are filled from their last visit rather than asked for
    # again — most people come for the same reason from the same school.
    if not school or not purpose:
        remembered_school, remembered_purpose = _last_visit_details(patron)
        school = school or remembered_school
        purpose = purpose or remembered_purpose

    log = PatronLog.objects.create(
        patron=patron,
        purpose_of_visit=purpose or None,
        school=school or None,
        entry_time=timezone.now(),
    )
    return {'success': True, 'action': 'entry', 'name': patron.fullname,
            'message': ('Welcome, ' + patron.fullname.split(' ')[0] + ' — signed in at '
                        + timezone.localtime(log.entry_time).strftime('%I:%M %p') + '.')}


def _toggle_visit(patron, purpose='', school=''):
    """Arrive or leave, whichever the patron is not doing.

    Only the card scanner uses this: a card holder holds the same card up on
    the way in and on the way out, so the reader cannot know which it is and
    the open visit has to decide.
    """
    open_visit = (PatronLog.objects
                  .filter(patron=patron, exit_time__isnull=True)
                  .order_by('-entry_time')
                  .first())

    if open_visit is not None:
        # A second scan seconds after the first is a stutter, not a departure.
        if timezone.now() - open_visit.entry_time < timedelta(seconds=30):
            return {
                'success': True, 'action': 'already_in',
                'name': patron.fullname,
                'message': (patron.fullname.split(' ')[0] + ', you are already signed in '
                            + 'since ' + timezone.localtime(open_visit.entry_time)
                            .strftime('%I:%M %p') + '.'),
            }
        open_visit.exit_time = timezone.now()
        open_visit.save(update_fields=['exit_time'])
        minutes = int((open_visit.exit_time - open_visit.entry_time).total_seconds() // 60)
        stay = (f'{minutes // 60}h {minutes % 60}m' if minutes >= 60 else f'{minutes}m')
        return {'success': True, 'action': 'exit', 'name': patron.fullname,
                'message': ('Goodbye, ' + patron.fullname.split(' ')[0]
                            + ' — you were here for ' + stay + '.')}

    log = PatronLog.objects.create(
        patron=patron,
        purpose_of_visit=purpose or None,
        school=school or None,
        entry_time=timezone.now(),
    )
    return {'success': True, 'action': 'entry', 'name': patron.fullname,
            'message': ('Welcome, ' + patron.fullname.split(' ')[0] + ' — signed in at '
                        + timezone.localtime(log.entry_time).strftime('%I:%M %p') + '.')}


def _require_armed_or_staff(request):
    """Usable both by a patron in desk mode and by staff working the desk."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST is allowed.'})
    if 'admin_id' not in request.session:
        return JsonResponse({'success': False, 'error': 'This screen is not signed in.'})
    return None


def desk_sign(request):
    """The row a patron typed straight into the table.

    Always an arrival. Name is the only thing always asked for; an email or
    mobile number picks the right person when several share a name, and
    recognises a returning visitor even if they spell their name differently
    this time. School and purpose are inherited from their last visit when left
    blank, so a regular only types their name.
    """
    blocked = _require_armed_or_staff(request)
    if blocked:
        return blocked

    name = ' '.join((request.POST.get('name') or '').split())
    contact = (request.POST.get('contact') or '').strip()
    patron_type = (request.POST.get('patron_type') or 'Student').strip()
    school = (request.POST.get('school') or '').strip()
    purpose = (request.POST.get('purpose') or '').strip()
    if purpose == 'Other':
        purpose = (request.POST.get('purpose_other') or '').strip() or 'Other'

    if len(_normalise_name(name)) < 2:
        return JsonResponse({'success': False, 'error': 'Type your full name first.'})
    if patron_type not in dict(Patron.PATRON_TYPE_CHOICES):
        patron_type = 'Student'

    patron, error = _find_patron(name, contact)
    if error:
        return JsonResponse({'success': False, 'error': error})

    if patron is not None:
        result = _sign_in(patron, purpose, school)
        result['is_visitor'] = patron.account_status == 'Visitor'
        return JsonResponse(result)

    # Nobody by that name: a first-time visitor, logged without an account.
    # Anyone may walk into a public library and read; membership is only needed
    # to take a book home, and that needs a librarian to check an ID.
    visitor = Patron.objects.create(
        fullname=name,
        email=contact if '@' in contact else None,
        contact_number=None if '@' in contact else (contact or None),
        patron_type=patron_type,
        account_status='Visitor',
        password_hash=hash_password(None),   # unusable: visitors do not sign in
        qr_code=None,                        # and cannot borrow
        registration_channel='On-site',
        otp_verified=False,
    )
    result = _sign_in(visitor, purpose, school)
    result['is_visitor'] = True
    result['created_visitor'] = True
    result['message'] = ('Welcome, ' + name.split(' ')[0]
                         + ' — signed in as a visitor. See the librarian with a valid ID '
                           'if you would like to become a member.')
    return JsonResponse(result)


def desk_sign_out(request):
    """Sign out by pressing the button on your own row.

    Making someone retype their name and number to leave is asking them to
    identify themselves twice for one visit — and the second time they are
    staring at a table that already has their name in it. Their open visit is
    the only thing that needs naming, so the row names it.
    """
    blocked = _require_armed_or_staff(request)
    if blocked:
        return blocked

    log = PatronLog.objects.filter(log_id=request.POST.get('log_id')).first()
    if log is None:
        return JsonResponse({'success': False, 'error': 'That visit is no longer on file.'})
    if log.exit_time is not None:
        return JsonResponse({'success': False,
                             'error': log.patron.fullname.split(' ')[0]
                                      + ' has already been signed out.'})

    log.exit_time = timezone.now()
    log.save(update_fields=['exit_time'])
    minutes = int((log.exit_time - log.entry_time).total_seconds() // 60)
    stay = (f'{minutes // 60}h {minutes % 60}m' if minutes >= 60 else f'{minutes}m')
    return JsonResponse({'success': True, 'action': 'exit', 'name': log.patron.fullname,
                         'message': ('Goodbye, ' + log.patron.fullname.split(' ')[0]
                                     + ' — you were here for ' + stay + '.')})


def desk_scan(request):
    """A library card was held up to the scanner. The QR is unique."""
    blocked = _require_armed_or_staff(request)
    if blocked:
        return blocked

    code = (request.POST.get('code') or '').strip()
    if not code:
        return JsonResponse({'success': False, 'error': 'Nothing was scanned.'})

    patron = Patron.objects.filter(qr_code=code).first()
    if patron is None:
        return JsonResponse({'success': False,
                             'error': 'That card was not recognised. Please see the librarian.'})
    if patron.account_status in ('Suspended', 'Inactive'):
        return JsonResponse({'success': False,
                             'error': 'This account is not active. Please see the librarian.'})

    result = _toggle_visit(patron, (request.POST.get('purpose') or '').strip())
    result['is_visitor'] = patron.account_status == 'Visitor'
    return JsonResponse(result)


# ─── settings ─────────────────────────────────────────────────────────────

def desk_settings(request):
    """Administrator page: closing time, and the state of the desk."""
    from .auth_utils import admin_only_required   # local: avoids a cycle at import
    from django.shortcuts import render

    @admin_only_required
    def _view(request):
        row = DeskSettings.load()
        if request.method == 'POST':
            closing = (request.POST.get('closing_time') or '').strip()
            weekend = (request.POST.get('weekend_closing_time') or '').strip()
            if closing:
                try:
                    row.closing_time = datetime.strptime(closing, '%H:%M').time()
                except ValueError:
                    messages.error(request, 'Closing time must look like 17:00.')
                    return redirect('desk_settings')
            # Blank means the weekend closes at the same time as a weekday.
            if weekend:
                try:
                    row.weekend_closing_time = datetime.strptime(weekend, '%H:%M').time()
                except ValueError:
                    messages.error(request, 'Weekend closing time must look like 12:00.')
                    return redirect('desk_settings')
            else:
                row.weekend_closing_time = None
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
        now = timezone.now()
        for log in PatronLog.objects.filter(exit_time__isnull=True):
            log.exit_time = now
            log.auto_closed = True
            log.save(update_fields=['exit_time', 'auto_closed'])
            closed += 1
        messages.success(request, f'Closed {closed} open visit(s).')
        return redirect(request.POST.get('next') or 'desk_settings')

    return _view(request)
