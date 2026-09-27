"""Desk mode: the Log Management page, handed to the patron."""

import copy
import json
import logging
from datetime import datetime, time, timedelta
from urllib.parse import urlencode

from django.contrib import messages
from django.http import JsonResponse, QueryDict
from django.shortcuts import redirect
from django.utils import timezone

from .auth_utils import (
    check_password,
    clear_login_failures,
    hash_password,
    login_locked_message,
    new_otp,
    otp_matches,
    record_login_failure,
    waste_password_time,
)
from .audit import log_system_action
from .emails import account_action_otp_email
from .models import Book, FloorPlan, Patron, PatronLog, Shelf, ShelfLevel, User
from .names import name_matches, parse_name, tokenise

logger = logging.getLogger(__name__)


DESK_SESSION_KEY = 'desk_mode'

# The emailed way out, kept in the session: it belongs to this screen only.
UNLOCK_CODE_KEY = 'desk_unlock_code'
UNLOCK_CODE_EXPIRY = timedelta(minutes=10)
UNLOCK_CODE_MAX_ATTEMPTS = 5

# Time before a repeat sign-in counts as a new visit.
RETURN_THRESHOLD = timedelta(minutes=15)

# How long after signing in the desk screen keeps showing that patron their own row.
DESK_VIEWER_WINDOW = timedelta(minutes=3)
DESK_VIEWER_KEY = 'desk_viewer'
DESK_VIEWER_AT = 'desk_viewer_at'

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


def _digits(value):
    return ''.join(ch for ch in (value or '') if ch.isdigit())


def _find_patron(name, contact):
    """Work out who is standing at the desk."""
    digits = _digits(contact)
    is_email = '@' in contact

    people = list(Patron.objects.exclude(account_status__in=('Suspended', 'Inactive'))
                  .only('patron_id', 'fullname', 'first_name', 'middle_name',
                        'last_name', 'email', 'contact_number',
                        'account_status', 'patron_type'))

    # Match a typed name in any common order or format.
    by_name = [p for p in people
               if name_matches(name, p.first_name, p.middle_name, p.last_name)]

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
        # Don't reveal other patrons with the same name.
        return None, ('More than one person uses that name. Add your email or '
                      'mobile number so we know which is you.')
    return None, None      # no one by that name, so a first-time visitor


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


# The lock

def arm_desk_mode(request):
    """Hand the machine to the patron. Requires a signed-in portal user."""
    if request.method != 'POST':
        return redirect(desk_log_path(request))
    if 'admin_id' not in request.session:
        return redirect('/admin-portal/login/')

    request.session[DESK_SESSION_KEY] = True
    request.session.modified = True
    return redirect(desk_log_path(request))


WRONG_ACCOUNT = 'That email and password do not match an active account.'


def _mask_address(email):
    """Show enough of an address to recognise it, not enough to harvest it."""
    name, _, domain = (email or '').partition('@')
    if not domain:
        return ''
    head = name[:2] if len(name) > 2 else name[:1]
    return head + '*' * max(len(name) - len(head), 1) + '@' + domain


def _emailed_code_ok(request, supplied):
    """Check a code emailed to the signed-in account, counting the attempt."""
    held = request.session.get(UNLOCK_CODE_KEY) or {}
    code = held.get('code')
    expires = held.get('expires')
    if not code or not expires or timezone.now().timestamp() > expires:
        return False, 'That code has expired. Send a new one.'
    if held.get('attempts', 0) >= UNLOCK_CODE_MAX_ATTEMPTS:
        return False, 'Too many incorrect codes. Send a new one.'
    if not otp_matches(supplied, code):
        held['attempts'] = held.get('attempts', 0) + 1
        request.session[UNLOCK_CODE_KEY] = held
        request.session.modified = True
        left = UNLOCK_CODE_MAX_ATTEMPTS - held['attempts']
        return False, 'That code is not correct. %d attempt(s) left.' % left
    request.session.pop(UNLOCK_CODE_KEY, None)
    request.session.modified = True
    return True, None


def unlock_desk_mode(request):
    """Take the machine back: the signed-in account, another one, or an emailed code."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST is allowed.'})

    signed_in = User.objects.filter(admin_id=request.session.get('admin_id')).first()
    if signed_in is None:
        request.session.flush()
        return JsonResponse({'success': False, 'redirect': '/admin-portal/login/',
                             'error': 'This session has expired. Please sign in again.'})

    password = request.POST.get('password') or ''
    code = (request.POST.get('code') or '').strip()
    email = (request.POST.get('email') or '').strip()

    # A colleague can free the machine with their own account.
    other = None
    if email and email.lower() != (signed_in.email or '').lower():
        other = User.objects.filter(email__iexact=email, account_status='Active').first()
        if other is None:
            # Same wording and cost either way, so this cannot be used to find accounts.
            waste_password_time()
            return JsonResponse({'success': False, 'error': WRONG_ACCOUNT})

    user = other or signed_in
    scope, identity = 'desk', str(user.admin_id)
    locked = login_locked_message(scope, identity)
    if locked:
        return JsonResponse({'success': False, 'error': locked})

    if code:
        # The code went to the signed-in account, so it only frees that one.
        ok, problem = _emailed_code_ok(request, code)
        if not ok:
            return JsonResponse({'success': False, 'error': problem})
        user, other = signed_in, None
    elif not password or not check_password(password, user.password_hash):
        remaining = record_login_failure(scope, identity)
        log_system_action('Unlock failed', 'Auth', user.admin_id,
                          'Failed desk-mode unlock for %s' % user.fullname)
        error = WRONG_ACCOUNT if other is not None else 'That password is not correct.'
        if remaining is not None and 0 < remaining <= 2:
            error += ' %d attempt(s) left before this is locked.' % remaining
        return JsonResponse({'success': False, 'error': error})

    clear_login_failures(scope, identity)
    request.session.pop(UNLOCK_CODE_KEY, None)
    request.session.pop(DESK_SESSION_KEY, None)
    request.session.modified = True

    if other is not None:
        # Someone else freed the machine. End the session rather than hand them
        # the armed account's portal.
        log_system_action('Unlock', 'Auth', other.admin_id,
                          '%s ended desk mode opened by %s' % (other.fullname, signed_in.fullname))
        request.session.flush()
        return JsonResponse({'success': True, 'redirect': '/admin-portal/login/',
                             'signed_out': True})

    destination = ('/library-staff/dashboard/'
                   if request.session.get('admin_role') == 'Staff'
                   else '/admin-portal/dashboard/')
    return JsonResponse({'success': True, 'redirect': destination})


# Finding a book, for whoever is standing at the desk

DESK_FIND_PAGE = 20
# What the kiosk may narrow the list by.
DESK_FIND_FILTERS = ('genre', 'material', 'room', 'shelf', 'level')


def _shelf_location(book):
    """Room, shelf, level and floor for one copy, as far as it is known."""
    level = getattr(book, 'shelf_level', None)
    shelf = getattr(level, 'shelf', None) if level else None
    if shelf is None:
        return {'shelved': False, 'where': 'Not yet shelved', 'floor': '', 'room': ''}
    room = getattr(shelf, 'room', None)
    plan = getattr(room, 'floor_plan', None) if room else None
    return {
        'shelved': True,
        'where': '%s · %s' % (shelf.name, level.label),
        'room': room.name if room else '',
        'floor': plan.floor_label if plan else '',
    }


def _desk_find_options():
    """The filter choices that would find something on the shelves."""
    from .views import CATALOGUE_HIDDEN_STATUSES

    visible = Book.objects.exclude(status__in=CATALOGUE_HIDDEN_STATUSES)
    genres = sorted({g for g in visible.values_list('genre', flat=True).distinct() if g})
    in_use = set(visible.values_list('material_type', flat=True).distinct())
    materials = [[k, v] for k, v in Book.MATERIAL_TYPE_CHOICES if k in in_use]

    level_ids = (visible.filter(shelf_level__isnull=False)
                 .values_list('shelf_level_id', flat=True).distinct())
    levels = (ShelfLevel.objects.filter(pk__in=list(level_ids), shelf__is_active=True)
              .select_related('shelf', 'shelf__room', 'shelf__room__floor_plan')
              .order_by('shelf__room__name', 'shelf__name', 'level_number', 'column_number'))
    rooms, shelves, out_levels = {}, {}, []
    for lv in levels:
        shelf, room = lv.shelf, lv.shelf.room
        if room is not None and room.room_id not in rooms:
            plan = room.floor_plan
            rooms[room.room_id] = {'id': room.room_id, 'name': room.name,
                                   'floor': plan.floor_label if plan else ''}
        if shelf.shelf_id not in shelves:
            shelves[shelf.shelf_id] = {'id': shelf.shelf_id, 'name': shelf.name,
                                       'room': shelf.room_id}
        out_levels.append({'id': lv.shelf_level_id, 'name': lv.label, 'shelf': shelf.shelf_id})
    return {'genres': genres, 'materials': materials, 'rooms': list(rooms.values()),
            'shelves': list(shelves.values()), 'levels': out_levels}


def desk_find_book(request):
    """Catalogue list for the kiosk: every book at first, narrowed by search and filters.

    Public catalogue data only, no patron details.
    """
    # Imported here: views imports this module, so it cannot be imported at the top.
    from .views import _catalogue_query, _catalogue_rows

    term = (request.GET.get('search') or '').strip()
    params = {'search': term, 'sort': 'title'}
    for key in DESK_FIND_FILTERS:
        params[key] = (request.GET.get(key) or '').strip()
    raw_page = (request.GET.get('page') or '1').strip()
    page = int(raw_page) if raw_page.isdigit() and int(raw_page) > 0 else 1

    query = _catalogue_query(params)
    total = query['titles'].count()
    start = (page - 1) * DESK_FIND_PAGE
    rows = _catalogue_rows(list(query['titles'][start:start + DESK_FIND_PAGE]))

    results = []
    for row in rows:
        book = row.get('book')
        if book is None:
            continue
        place = _shelf_location(book)
        results.append({
            'title': row['title'],
            'author': row['author'] or '',
            'year': row['year'] or '',
            'genre': row['genre_name'] or '',
            'copies': row['copies'],
            'available': row['available'],
            'status': book.get_status_display(),
            'shelf_id': getattr(getattr(book.shelf_level, 'shelf', None), 'shelf_id', None),
            'shelved': place['shelved'],
            'where': place['where'],
            'room': place['room'],
            'floor': place['floor'],
        })

    data = {
        'success': True,
        'results': results,
        'count': total,
        'page': page,
        'has_more': start + len(rows) < total,
        'term': term,
    }
    if request.GET.get('options'):
        data['options'] = _desk_find_options()
    return JsonResponse(data)


def desk_floor():
    """The floor whose plan has the desk marked on it."""
    return (FloorPlan.objects
            .filter(is_active=True, desk_x__isnull=False, desk_y__isnull=False)
            .order_by('floor_number', 'floor_plan_id')
            .first())


def _sub_request(request, params):
    """The same request, asking a different question of another view."""
    sub = copy.copy(request)
    sub.GET = QueryDict(urlencode(params))
    return sub


def desk_way_to_book(request):
    """Draw the way from the desk computer to a shelf, on the desk's own floor."""
    # Imported here: views imports this module.
    from .views import get_navigation_route, get_patron_map_data

    raw = (request.GET.get('shelf_id') or '').strip()
    if not raw.isdigit():
        return JsonResponse({'success': False, 'error': 'Which shelf?'})

    start = desk_floor()
    if start is None:
        return JsonResponse({
            'success': False,
            'no_desk': True,
            'error': 'The desk has not been marked on the floor plan yet.'})

    shelf = Shelf.objects.filter(shelf_id=int(raw)).first()
    if shelf is None or shelf.map_x is None:
        return JsonResponse({'success': False,
                             'error': 'That shelf is not placed on the floor plan.'})

    route = json.loads(get_navigation_route(_sub_request(request, {
        'start_x': start.desk_x,
        'start_y': start.desk_y,
        'from_floor': start.floor_plan_id,
        'target_shelf_id': shelf.shelf_id,
    })).content)

    # The plan drawn is always the one the patron is standing on: the desk's.
    plan = json.loads(get_patron_map_data(_sub_request(request, {
        'floor': start.floor_plan_id,
    })).content)

    return JsonResponse({
        'success': bool(route.get('success')),
        'error': route.get('error', ''),
        'desk': {'x': start.desk_x, 'y': start.desk_y,
                 'floor_plan_id': start.floor_plan_id,
                 'floor': start.floor_label},
        'route': route.get('route') or [],
        'instruction': route.get('instruction') or '',
        'cross_floor': bool(route.get('cross_floor')),
        'target_floor': route.get('target_floor') or {},
        'target_shelf': route.get('target_shelf') or {},
        'plan': plan if plan.get('success') else {},
    })


def send_desk_unlock_code(request):
    """Email a one-time code to the account that armed the desk."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST is allowed.'})

    user = User.objects.filter(admin_id=request.session.get('admin_id')).first()
    if user is None:
        request.session.flush()
        return JsonResponse({'success': False, 'redirect': '/admin-portal/login/',
                             'error': 'This session has expired. Please sign in again.'})
    if not user.email:
        return JsonResponse({'success': False,
                             'error': 'This account has no email address on file.'})

    code = new_otp()
    request.session[UNLOCK_CODE_KEY] = {
        'code': code,
        'expires': (timezone.now() + UNLOCK_CODE_EXPIRY).timestamp(),
        'attempts': 0,
    }
    request.session.modified = True

    if not account_action_otp_email(user.email, user.fullname, code, 'unlock desk mode on'):
        request.session.pop(UNLOCK_CODE_KEY, None)
        request.session.modified = True
        return JsonResponse({'success': False,
                             'error': 'We could not send the code. Use a password instead.'})

    log_system_action('Unlock code', 'Auth', user.admin_id,
                      'Desk-mode unlock code emailed to %s' % user.fullname)
    return JsonResponse({'success': True, 'sent_to': _mask_address(user.email)})


# Visits that nobody closed

def close_stale_visits():
    """Close visits left open on days that have already ended."""
    today = timezone.localdate()
    stale = PatronLog.objects.filter(exit_time__isnull=True, entry_time__date__lt=today)

    closed = 0
    for log in stale:
        day = timezone.localtime(log.entry_time).date()
        assumed = timezone.make_aware(datetime.combine(day, time(23, 59)))
        # Exit time is never before entry time.
        if assumed <= log.entry_time:
            assumed = log.entry_time + timedelta(minutes=1)
        log.exit_time = assumed
        log.auto_closed = True
        log.save(update_fields=['exit_time', 'auto_closed'])
        closed += 1
    if closed:
        # Logged as a system action.
        log_system_action('Auto-close', 'PatronLog', None,
                          f'Closed {closed} visit(s) left open on a previous day')
    return closed


# Signing in and out

def _open_visit_for(patron):
    return (PatronLog.objects
            .filter(patron=patron, exit_time__isnull=True)
            .order_by('-entry_time')
            .first())


def _sign_in(patron, purpose='', school=''):
    """Record an arrival, and only ever an arrival."""
    returned_from = None
    open_visit = _open_visit_for(patron)
    if open_visit is not None:
        age = timezone.now() - open_visit.entry_time
        if age < RETURN_THRESHOLD:
            # Typed twice in the same breath: one arrival, not two.
            return {'success': True, 'action': 'already_in', 'name': patron.fullname,
                    'message': (patron.fullname.split(' ')[0] + ', you are already signed in '
                                'since ' + timezone.localtime(open_visit.entry_time)
                                .strftime('%I:%M %p') + '. Press Sign out on your row when '
                                'you leave.')}
        # Visits left open because the patron did not sign out.
        open_visit.exit_time = timezone.now()
        open_visit.auto_closed = True
        open_visit.save(update_fields=['exit_time', 'auto_closed'])
        returned_from = timezone.localtime(open_visit.entry_time).strftime('%I:%M %p')

    # Fill in missing school and purpose from the patron record or last visit.
    if not school:
        school = (patron.school or '').strip()
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
    at = timezone.localtime(log.entry_time).strftime('%I:%M %p')
    if returned_from:
        message = ('Welcome back, ' + patron.fullname.split(' ')[0] + ' — new visit started at '
                   + at + '. Your earlier visit from ' + returned_from
                   + ' has been closed.')
    else:
        message = 'Welcome, ' + patron.fullname.split(' ')[0] + ' — signed in at ' + at + '.'
    return {'success': True, 'action': 'entry', 'name': patron.fullname,
            'returned': bool(returned_from), 'message': message}


def _toggle_visit(patron, purpose='', school=''):
    """Arrive or leave, whichever the patron is not doing."""
    open_visit = (PatronLog.objects
                  .filter(patron=patron, exit_time__isnull=True)
                  .order_by('-entry_time')
                  .first())

    if open_visit is not None:
        # Ignore a repeat scan soon after signing in.
        if timezone.now() - open_visit.entry_time < RETURN_THRESHOLD:
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


def remember_desk_viewer(request, patron):
    """Note who just signed in, so the desk can show them their own row."""
    try:
        request.session[DESK_VIEWER_KEY] = patron.patron_id
        request.session[DESK_VIEWER_AT] = timezone.now().isoformat()
    except Exception:
        # Not worth failing the sign-in over: the desk simply shows the unfiltered table.
        logger.exception('Could not record the desk viewer')


def forget_desk_viewer(request):
    for key in (DESK_VIEWER_KEY, DESK_VIEWER_AT):
        try:
            request.session.pop(key, None)
        except Exception:
            logger.exception('Could not clear desk viewer key %r', key)


def desk_viewer_id(request):
    """The patron the desk screen is currently showing, if any."""
    patron_id = request.session.get(DESK_VIEWER_KEY)
    if not patron_id:
        return None
    stamp = request.session.get(DESK_VIEWER_AT)
    if not stamp:
        return None
    try:
        seen = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    if timezone.is_naive(seen):
        seen = timezone.make_aware(seen)
    if timezone.now() - seen > DESK_VIEWER_WINDOW:
        forget_desk_viewer(request)
        return None
    return patron_id


def _require_armed_or_staff(request):
    """Usable both by a patron in desk mode and by staff working the desk."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST is allowed.'})
    if 'admin_id' not in request.session:
        return JsonResponse({'success': False, 'error': 'This screen is not signed in.'})
    return None


def desk_sign(request):
    """The row a patron typed straight into the table."""
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

    if len(''.join(tokenise(name))) < 2:
        return JsonResponse({'success': False, 'error': 'Type your full name first.'})
    if patron_type not in dict(Patron.PATRON_TYPE_CHOICES):
        patron_type = 'Student'

    patron, error = _find_patron(name, contact)
    if error:
        return JsonResponse({'success': False, 'error': error})

    if patron is not None:
        result = _sign_in(patron, purpose, school)
        result['is_visitor'] = patron.account_status == 'Visitor'
        remember_desk_viewer(request, patron)
        return JsonResponse(result)

    # Nobody by that name: a first-time visitor, logged without an account.
    first, middle, last = parse_name(name)
    visitor = Patron.objects.create(
        fullname=name,
        first_name=first,
        middle_name=middle,
        last_name=last,
        email=contact if '@' in contact else None,
        contact_number=None if '@' in contact else (contact or None),
        patron_type=patron_type,
        # Typed once here, so their next visit does not ask again.
        school=school or None,
        account_status='Visitor',
        password_hash=hash_password(None),   # unusable: visitors do not sign in
        qr_code=None,                        # and cannot borrow
        registration_channel='On-site',
        otp_verified=False,
    )
    result = _sign_in(visitor, purpose, school)
    result['is_visitor'] = True
    result['created_visitor'] = True
    remember_desk_viewer(request, visitor)
    result['message'] = ('Welcome, ' + name.split(' ')[0]
                         + ' — signed in as a visitor. See the librarian with a valid ID '
                           'if you would like to become a member.')
    return JsonResponse(result)


def desk_sign_out(request):
    """Sign out by pressing the button on your own row."""
    blocked = _require_armed_or_staff(request)
    if blocked:
        return blocked

    raw_id = (request.POST.get('log_id') or '').strip()
    log = PatronLog.objects.filter(log_id=int(raw_id)).first() if raw_id.isdigit() else None
    if log is None:
        return JsonResponse({'success': False, 'error': 'That visit is no longer on file.'})
    if log.exit_time is not None:
        return JsonResponse({'success': False,
                             'error': log.patron.fullname.split(' ')[0]
                                      + ' has already been signed out.'})

    log.exit_time = timezone.now()
    log.save(update_fields=['exit_time'])
    forget_desk_viewer(request)   # they have left; their row leaves the screen with them
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
    if result.get('action') == 'exit':
        forget_desk_viewer(request)      # gone: nothing of theirs stays on screen
    else:
        remember_desk_viewer(request, patron)
    return JsonResponse(result)


def close_open_visits_now(request):
    """Manual sweep, for when staff can see the room is empty."""
    from .auth_utils import admin_login_required

    @admin_login_required
    def _view(request):
        if request.method != 'POST':
            return redirect(desk_log_path(request))
        closed = close_stale_visits()
        now = timezone.now()
        for log in PatronLog.objects.filter(exit_time__isnull=True):
            log.exit_time = now
            log.auto_closed = True
            log.save(update_fields=['exit_time', 'auto_closed'])
            closed += 1
        messages.success(request, f'Closed {closed} open visit(s), marked as assumed exits.')
        return redirect(desk_log_path(request))

    return _view(request)


# Kiosk.

# How long the kiosk holds on to who just identified themselves.
DESK_PENDING_KEY = 'desk_pending'
DESK_PENDING_AT = 'desk_pending_at'
DESK_PENDING_WINDOW = timedelta(minutes=2)


def _remember_pending(request, patron):
    request.session[DESK_PENDING_KEY] = patron.patron_id
    request.session[DESK_PENDING_AT] = timezone.now().isoformat()
    request.session.modified = True


def _pending_patron(request):
    """The patron who identified themselves recently, from the session."""
    patron_id = request.session.get(DESK_PENDING_KEY)
    stamp = request.session.get(DESK_PENDING_AT)
    if not patron_id or not stamp:
        return None
    try:
        seen = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    if timezone.is_naive(seen):
        seen = timezone.make_aware(seen)
    if timezone.now() - seen > DESK_PENDING_WINDOW:
        forget_pending(request)
        return None
    return Patron.objects.filter(patron_id=patron_id).first()


def forget_pending(request):
    for key in (DESK_PENDING_KEY, DESK_PENDING_AT):
        try:
            request.session.pop(key, None)
        except Exception:
            logger.exception('Could not clear the kiosk identity key %r', key)


def _first_name(patron):
    return (patron.first_name or patron.fullname or '').split(' ')[0] or 'there'


def _stay_length(started, ended):
    minutes = int((ended - started).total_seconds() // 60)
    if minutes >= 60:
        return '%dh %dm' % (minutes // 60, minutes % 60)
    return '%dm' % minutes


def _account_problem(patron):
    """Why this person cannot sign themselves in, in words they can act on."""
    if patron.account_status == 'Suspended':
        return ('This membership is on hold. Please see the librarian at the '
                'counter — they can sign you in.')
    if patron.account_status == 'Inactive':
        return ('This membership is closed. Please see the librarian if you '
                'would like to use it again.')
    return None


def _identify(value):
    """Find the member behind a card number, an email, or a scanned QR."""
    from .cardnumbers import is_valid, normalise

    value = (value or '').strip()
    if not value:
        return None, 'Type your email or card number first.'

    if '@' in value:
        patron = Patron.objects.filter(email__iexact=value).first()
        if patron is None:
            return None, ('No member is registered with that email. Check the '
                          'spelling, or sign in as a visitor instead.')
        return patron, None

    # Card numbers may be typed with a dash or the AYLA prefix.
    typed = value.upper()
    if typed.startswith('AYLA'):
        typed = typed[4:].lstrip('- ')
    digits = normalise(typed)
    if digits and not any(ch.isalpha() for ch in typed):
        if len(digits) != 7:
            return None, 'A card number is 7 digits, like 7482051.'
        if not is_valid(digits):
            # The check digit did its job.
            return None, ('That card number is not quite right. Please check the '
                          'digits on your card and try again.')
        patron = Patron.objects.filter(card_number=digits).first()
        if patron is None:
            return None, 'That card number is not on file. Please see the librarian.'
        return patron, None

    patron = Patron.objects.filter(qr_code=value).first()
    if patron is None:
        return None, 'That card was not recognised. Please see the librarian.'
    return patron, None


def desk_identify(request):
    """Step one: who is standing here."""
    blocked = _require_armed_or_staff(request)
    if blocked:
        return blocked

    patron, error = _identify(request.POST.get('value') or request.POST.get('code'))
    if error:
        return JsonResponse({'success': False, 'error': error})

    problem = _account_problem(patron)
    if problem:
        return JsonResponse({'success': False, 'error': problem})

    open_visit = _open_visit_for(patron)
    _remember_pending(request, patron)

    inside_since = ''
    if open_visit is not None:
        inside_since = timezone.localtime(open_visit.entry_time).strftime('%I:%M %p')

    return JsonResponse({
        'success': True,
        'name': patron.fullname,
        'first_name': _first_name(patron),
        'card': patron.card_display,
        'inside': open_visit is not None,
        'inside_since': inside_since,
        # Which button to emphasise.
        'suggest': 'exit' if open_visit is not None else 'entry',
        'is_visitor': patron.account_status == 'Visitor',
        'purpose': _last_visit_details(patron)[1],
    })


def desk_visit(request):
    """Step two: entry or exit, for whoever identified themselves a moment ago."""
    blocked = _require_armed_or_staff(request)
    if blocked:
        return blocked

    patron = _pending_patron(request)
    if patron is None:
        return JsonResponse({'success': False, 'expired': True,
                             'error': 'That took a while — please scan or type your '
                                      'details again.'})

    direction = (request.POST.get('direction') or '').strip().lower()
    if direction not in ('entry', 'exit'):
        return JsonResponse({'success': False, 'error': 'Choose Entry or Exit.'})

    problem = _account_problem(patron)
    if problem:
        forget_pending(request)
        return JsonResponse({'success': False, 'error': problem})

    purpose = (request.POST.get('purpose') or '').strip()
    if purpose == 'Other':
        purpose = (request.POST.get('purpose_other') or '').strip() or 'Other'
    school = (request.POST.get('school') or '').strip()

    if direction == 'entry':
        result = _sign_in(patron, purpose, school)
        result['card'] = patron.card_display
        result['at'] = timezone.localtime(timezone.now()).strftime('%I:%M %p')
        forget_pending(request)
        remember_desk_viewer(request, patron)
        return JsonResponse(result)

    open_visit = _open_visit_for(patron)
    if open_visit is None:
        # Exit pressed with nothing open.
        forget_pending(request)
        return JsonResponse({
            'success': False,
            'action': 'not_inside',
            'name': patron.fullname,
            'error': (_first_name(patron) + ', there is no open visit under your name. '
                      'If you are leaving, you are already signed out.'),
        })

    open_visit.exit_time = timezone.now()
    open_visit.save(update_fields=['exit_time'])
    forget_pending(request)
    forget_desk_viewer(request)
    stay = _stay_length(open_visit.entry_time, open_visit.exit_time)
    return JsonResponse({
        'success': True,
        'action': 'exit',
        'name': patron.fullname,
        'card': patron.card_display,
        'at': timezone.localtime(open_visit.exit_time).strftime('%I:%M %p'),
        'stay': stay,
        'message': ('Goodbye, ' + _first_name(patron) + ' — you were here for '
                    + stay + '.'),
    })


def desk_visitor(request):
    """The walk-in who is not a member."""
    blocked = _require_armed_or_staff(request)
    if blocked:
        return blocked

    name = ' '.join((request.POST.get('name') or '').split())
    contact = (request.POST.get('contact') or '').strip()
    patron_type = (request.POST.get('patron_type') or 'General Visitor').strip()
    school = (request.POST.get('school') or '').strip()
    purpose = (request.POST.get('purpose') or '').strip()
    if purpose == 'Other':
        purpose = (request.POST.get('purpose_other') or '').strip() or 'Other'

    direction = (request.POST.get('direction') or 'entry').strip().lower()
    if direction not in ('entry', 'exit'):
        direction = 'entry'

    if len(''.join(tokenise(name))) < 2:
        return JsonResponse({'success': False, 'field': 'name',
                             'error': 'Please type your full name.'})
    if patron_type not in dict(Patron.PATRON_TYPE_CHOICES):
        patron_type = 'General Visitor'
    if direction == 'entry' and not purpose:
        return JsonResponse({'success': False, 'field': 'purpose',
                             'error': 'Please choose what brings you in today.'})

    if direction == 'exit':
        return _visitor_exit(request, name, contact)

    # A returning walk-in is the same person, not a second row.
    patron, error = _find_patron(name, contact)
    if error:
        return JsonResponse({'success': False, 'error': error})

    if patron is not None:
        problem = _account_problem(patron)
        if problem:
            return JsonResponse({'success': False, 'error': problem})
        result = _sign_in(patron, purpose, school)
        result['is_visitor'] = patron.account_status == 'Visitor'
        result['card'] = patron.card_display
        result['at'] = timezone.localtime(timezone.now()).strftime('%I:%M %p')
        if patron.account_status != 'Visitor':
            # A member signed in through the visitor form.
            result['note'] = ('You are already a member, so this visit was added to '
                              'your record. Next time, just scan your card.')
        remember_desk_viewer(request, patron)
        return JsonResponse(result)

    first, middle, last = parse_name(name)
    visitor = Patron.objects.create(
        fullname=name,
        first_name=first,
        middle_name=middle,
        last_name=last,
        email=contact if '@' in contact else None,
        contact_number=None if '@' in contact else (contact or None),
        patron_type=patron_type,
        # Typed once here, so their next visit does not ask again.
        school=school or None,
        account_status='Visitor',
        password_hash=hash_password(None),   # unusable: visitors do not sign in
        qr_code=None,                        # and cannot borrow
        registration_channel='On-site',
        otp_verified=False,
    )
    result = _sign_in(visitor, purpose, school)
    result['is_visitor'] = True
    result['created_visitor'] = True
    result['at'] = timezone.localtime(timezone.now()).strftime('%I:%M %p')
    result['note'] = ('Signed in as a visitor. See the librarian with a valid ID if you '
                      'would like to become a member and borrow books.')
    remember_desk_viewer(request, visitor)
    return JsonResponse(result)


def _visitor_exit(request, name, contact):
    """A walk-in signing out."""
    if not contact:
        return JsonResponse({
            'success': False, 'field': 'contact',
            'error': ('Please type the email or mobile number you gave when you '
                      'arrived, so we sign out the right visit.'),
        })

    patron, error = _find_patron(name, contact)
    if error:
        return JsonResponse({'success': False, 'error': error})
    if patron is None:
        return JsonResponse({
            'success': False,
            'error': ('No visit today matches that name and contact number. Please '
                      'check them, or see the librarian at the counter.'),
        })

    open_visit = _open_visit_for(patron)
    if open_visit is None:
        return JsonResponse({
            'success': False, 'action': 'not_inside',
            'error': (_first_name(patron) + ', there is no open visit under your name. '
                      'If you are leaving, you are already signed out.'),
        })

    open_visit.exit_time = timezone.now()
    open_visit.save(update_fields=['exit_time'])
    forget_desk_viewer(request)
    stay = _stay_length(open_visit.entry_time, open_visit.exit_time)
    return JsonResponse({
        'success': True,
        'action': 'exit',
        'name': patron.fullname,
        'at': timezone.localtime(open_visit.exit_time).strftime('%I:%M %p'),
        'stay': stay,
        'message': ('Goodbye, ' + _first_name(patron) + ' — you were here for '
                    + stay + '.'),
    })
