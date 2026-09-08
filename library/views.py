from django.shortcuts import redirect, render, get_object_or_404
from django.contrib import messages
from django.db.models import (Count, F, IntegerField, Max, OuterRef, Q,
                              Subquery)
from django.db import transaction
from django.utils import timezone
from django.core.exceptions import SuspiciousFileOperation
from django.http import HttpResponse, JsonResponse, Http404
from django.conf import settings
from django.core.paginator import Paginator
from datetime import datetime, timedelta
from io import BytesIO
from decimal import Decimal, InvalidOperation
import json

from uuid import uuid4
import openpyxl
from openpyxl import Workbook
import logging
import qrcode
import os
import math
import heapq
import secrets
import re

from .auth_utils import (
    LOGIN_FAILED_TEXT,
    check_password,
    clear_login_failures,
    login_locked_message,
    record_login_failure,
    waste_password_time,
    hash_password,
    patron_login_required,
    admin_login_required,
    admin_only_required,
    admin_module_required,
    granted_module_required,
    staff_only_required,
    module_required,
    admin_or_module_required,
    password_length_error,
)
from .modules import STAFF_MODULES, clean_module_keys
from .names import compose_name, parse_name
from .desk import PURPOSE_CHOICES, close_stale_visits, desk_is_armed, desk_viewer_id

# Map admin page-URL names to their Library Staff equivalents so that shared
# action endpoints can return whichever portal the current user belongs to.
STAFF_PORTAL_MAP = {
    'admin_management': 'staff_manage_books',
    'admin_book_detail': 'staff_book_detail',
    'admin_transaction': 'staff_transaction',
    'donation_management': 'staff_donations',
    'admin_log_management': 'staff_logs',
    'shelf_manager': 'staff_shelf',
}


def portal_redirect(request, name, *args, **kwargs):
    """redirect() that keeps Library Staff inside the /library-staff/ portal."""
    if request.session.get('admin_role') == 'Staff':
        name = STAFF_PORTAL_MAP.get(name, name)
    return redirect(name, *args, **kwargs)
from .audit import log_admin_action, log_patron_action, log_system_action
from .eligibility import check_patron_eligibility
from .emails import (
    announcement_email,
    borrow_confirmation_email,
    bulk_connection,
    return_receipt_email,
    lost_book_email,
    extension_approved_email,
    extension_declined_email,
    otp_email,
    account_action_otp_email,
    reactivation_approved_email,
    reactivation_declined_email,
    password_reset_otp_email,
    registration_approved_email,
    registration_rejected_email,
)
from . import labels
from .reports import (REPORT_TYPES, SNAPSHOT_REPORTS, parse_date_range, build_report,
                      render_report_pdf, render_report_excel)
from .models import Book, Patron, PatronLog, Transaction, User, ShelfLevel, Donation, Announcement, FloorPlan, Shelf, Room, Door, Waypoint, BLEBeacon, WaypointConnection, SystemLog, BorrowingRule, Obstacle, Stairway, PasswordResetOTP, InventoryRecord, StockMovement, StockAudit, DueDateExtension, ReactivationRequest

# Patron views
def patron_login(request):
    """Patron sign-in.

    The order here matters and is not the obvious one. Status used to be checked
    before the password, so anyone could type an address with any password and
    learn from the reply whether it belonged to a pending applicant, a
    deactivated member, or nobody at all.

    Those messages are worth keeping -- an applicant genuinely needs to be told
    their registration is still waiting, and a deactivated member needs the way
    back. So the password is verified first and the status is only explained to
    someone who has just proved the account is theirs. A guesser sees one
    sentence, always the same one.
    """
    if request.method == 'POST':
        email = (request.POST.get('email') or '').strip()
        password = request.POST.get('password') or ''

        locked = login_locked_message('patron', email)
        if locked:
            return render(request, 'patron/patronlogin.html', {'error': locked})

        patron = Patron.objects.filter(email__iexact=email).first()
        if patron is None:
            waste_password_time()      # a missing account costs what a real one costs
            password_ok = False
        else:
            password_ok = check_password(password, patron.password_hash)

        if not password_ok:
            remaining = record_login_failure('patron', email)
            if patron is not None:
                # Logged against the account whose password was missed, which is
                # what makes a run of them legible as an attack on that account.
                log_patron_action(request, 'Login failed', 'Patron', patron.patron_id,
                                  'Incorrect password', patron=patron)
            else:
                log_system_action('Login failed', 'Patron', None,
                                  f'Failed patron sign-in for "{email[:120]}"')
            error = LOGIN_FAILED_TEXT
            if remaining is not None and 0 < remaining <= 2:
                error += f' {remaining} attempt(s) left before this account is locked.'
            return render(request, 'patron/patronlogin.html', {'error': error})

        # Password correct: from here the account is demonstrably theirs, so the
        # real reason they cannot get in is safe -- and necessary -- to give.
        clear_login_failures('patron', email)

        if patron.account_status == 'Pending':
            return render(request, 'patron/patronlogin.html',
                          {'error': 'Your registration is awaiting administrator approval. You will receive an email once it is approved.'})

        if patron.account_status == 'Inactive':
            return render(request, 'patron/patronlogin.html',
                          {'error': 'Your account has been deactivated.', 'show_reactivate': True})

        if patron.account_status != 'Active':
            return render(request, 'patron/patronlogin.html', {'error': 'Your account is suspended or inactive'})

        # Session fixation: the id the browser arrived holding must not be the
        # one it leaves authenticated with. flush() rather than cycle_key() so
        # nothing seeded into the pre-login session survives the sign-in either.
        request.session.flush()
        request.session['patron_id'] = patron.patron_id
        request.session['patron_fullname'] = patron.fullname
        log_patron_action(request, 'Login', 'Patron', patron.patron_id,
                          'Signed in to the patron portal', patron=patron)
        return redirect('/patron/dashboard/')

    return render(request, 'patron/patronlogin.html')


def patron_logout(request):
    # Recorded before flush(): afterwards there is no session to say who left.
    log_patron_action(request, 'Logout', 'Patron', request.session.get('patron_id'),
                      'Signed out of the patron portal')
    request.session.flush()
    return redirect('/patron/login/')


def patron_dashboard(request):
    # Home is the borrowing summary, which a guest does not have. Search is
    # what they came for, so that is where Home takes them.
    if 'patron_id' not in request.session:
        return redirect('/patron/catalog/')
    patron_id = request.session.get('patron_id')
    patron = get_object_or_404(Patron, patron_id=patron_id)

    active_loans = Transaction.objects.filter(
        patron=patron,
        transaction_type='Borrow',
        return_date__isnull=True,
    ).select_related('book').order_by('due_date')

    borrowed_count = active_loans.count()

    overdue_loans = active_loans.filter(overdue_flag=True)
    overdue_count = overdue_loans.count()

    recent_history = Transaction.objects.filter(
        patron=patron,
        return_date__isnull=False,
    ).select_related('book').order_by('-return_date')[:3]

    history_count = Transaction.objects.filter(
        patron=patron,
        return_date__isnull=False,
    ).count()

    recent_announcements = Announcement.objects.filter(
        is_active=True
    ).order_by('-created_at')[:3]

    context = {
        'patron': patron,
        'borrowed_count': borrowed_count,
        'overdue_count': overdue_count,
        'recent_announcements': recent_announcements,
        'active_loans': active_loans,
        'recent_history': recent_history,
        'overdue_loans': overdue_loans,
        'history_count': history_count,
    }
    return render(request, 'patron/patrondashboard.html', context)


def _name_from_post(request):
    """Read first / middle / surname off a form, in that shape or the old one.

    Older forms (and the odd script) still post a single `fullname`; it is
    split rather than refused, so nothing that used to work stops working.
    Returns (first, middle, last, error).
    """
    first = ' '.join((request.POST.get('first_name') or '').split())
    middle = ' '.join((request.POST.get('middle_name') or '').split())
    last = ' '.join((request.POST.get('last_name') or '').split())

    if not (first or last):
        raw = ' '.join((request.POST.get('fullname') or '').split())
        if raw:
            first, middle, last = parse_name(raw)

    if not first:
        return '', '', '', 'A first name is required.'
    if not last:
        return '', '', '', 'A surname is required.'
    return first, middle, last, None


def _new_otp():
    """6-digit numeric one-time password.

    Drawn from `secrets`, not `random`: the latter is a Mersenne Twister whose
    future output is derivable from enough observed values, and these codes
    stand between an attacker and someone's account.
    """
    return f'{secrets.randbelow(1000000):06d}'


def _otp_matches(supplied, stored):
    """Constant-time comparison of a supplied code against the stored one.

    Both sides are encoded to bytes first: compare_digest rejects non-ASCII
    str outright, and a patron typing an accented character into the code box
    should get "incorrect code", not a 500.
    """
    if not stored or not supplied:
        return False
    return secrets.compare_digest(supplied.encode('utf-8'), stored.encode('utf-8'))


# How long to wait between sending one code and the next, for the same
# account. Without it "send code" mails someone's inbox as fast as it can be
# clicked -- by anyone who knows the address, on the flows that need no login.
OTP_RESEND_COOLDOWN = timedelta(seconds=60)


def _otp_cooldown_left(last_sent_at):
    """Whole seconds still to wait before another code may go out (0 = clear)."""
    if not last_sent_at:
        return 0
    remaining = OTP_RESEND_COOLDOWN - (timezone.now() - last_sent_at)
    return max(0, math.ceil(remaining.total_seconds()))


def _reset_cooldown_left(account_type, email):
    """Same, for the PasswordResetOTP-backed flows, off the last row's created_at."""
    last = (PasswordResetOTP.objects
            .filter(account_type=account_type, email__iexact=email)
            .order_by('-created_at').first())
    return _otp_cooldown_left(last.created_at if last else None)


# ─── Self-service account-action OTPs ─────────────────────────────────────
# Deactivation, reactivation and password changes all confirm by emailed code.
# They share Patron.otp_code, so every code carries the purpose it was issued
# for and is checked against it -- see Patron.otp_purpose.

def _issue_account_otp(patron, purpose, action_label):
    """Mint a purpose-scoped code on the patron and email it. True if sent.

    Callers are expected to have cleared _otp_cooldown_left() first; how a
    refusal is worded differs by flow, so it is not decided here.
    """
    patron.otp_code = _new_otp()
    patron.otp_purpose = purpose
    patron.otp_expires_at = timezone.now() + timedelta(minutes=10)
    patron.otp_attempts = 0
    patron.otp_last_sent_at = timezone.now()
    patron.save(update_fields=['otp_code', 'otp_purpose', 'otp_expires_at',
                               'otp_attempts', 'otp_last_sent_at'])
    return account_action_otp_email(patron.email, patron.fullname,
                                    patron.otp_code, action_label)


def _count_failed_otp(patron):
    """Record one wrong guess and return the message to show for it.

    The code is burnt once the ceiling is reached rather than merely refused,
    so the limit actually costs the guesser their code instead of letting them
    keep hammering the same one.
    """
    patron.otp_attempts += 1
    remaining = Patron.MAX_OTP_ATTEMPTS - patron.otp_attempts
    if remaining <= 0:
        patron.otp_code = None
        patron.otp_purpose = ''
        patron.otp_expires_at = None
        patron.save(update_fields=['otp_code', 'otp_purpose', 'otp_expires_at', 'otp_attempts'])
        return 'Too many incorrect codes. Request a new one to try again.'
    patron.save(update_fields=['otp_attempts'])
    return f'Incorrect code. {remaining} attempt(s) left.'


def _check_account_otp(patron, purpose, code):
    """Return an error message for a bad code, or None if it is good.

    A code issued without a purpose (registration's) has otp_purpose '', which
    matches no caller here -- so those can never be spent on an account action.
    """
    if not patron.otp_code or not patron.otp_expires_at or timezone.now() > patron.otp_expires_at:
        # Covers never-requested, already-spent and timed-out alike: from the
        # patron's side the fix is the same, and saying which it was would tell
        # anyone holding the session more than they need to know.
        return 'That code is no longer valid. Request a new one.'
    if patron.otp_purpose != purpose:
        return 'That code was issued for a different request. Request a new one.'
    if not _otp_matches(code, patron.otp_code):
        return _count_failed_otp(patron)
    return None


def _clear_account_otp(patron, extra_fields=()):
    """Spend the code so it cannot be replayed, saving any fields alongside."""
    patron.otp_code = None
    patron.otp_purpose = ''
    patron.otp_expires_at = None
    patron.otp_attempts = 0
    patron.save(update_fields=['otp_code', 'otp_purpose', 'otp_expires_at',
                               'otp_attempts', *extra_fields])


# What each accepted format actually starts with. Checked because the
# extension is chosen by whoever uploads the file and means nothing on its own:
# naming something .png does not make it a PNG. Low risk while these files are
# only ever served as a download, but the check costs three lines and stops the
# store filling with things that are not what they claim.
_CREDENTIAL_MAGIC = {
    '.jpg': (b'\xff\xd8\xff',),
    '.jpeg': (b'\xff\xd8\xff',),
    '.png': (b'\x89PNG\r\n\x1a\n',),
    '.pdf': (b'%PDF-',),
}


def _save_credential_document(uploaded, patron_email):
    """Store an uploaded ID / proof-of-residency file under media/credentials/.

    Returns the media-relative path, or None when nothing was uploaded."""
    if not uploaded:
        return None
    ext = os.path.splitext(uploaded.name)[1].lower()
    if ext not in _CREDENTIAL_MAGIC:
        raise ValueError('Credential must be a JPG, PNG, or PDF file.')
    if uploaded.size > 5 * 1024 * 1024:
        raise ValueError('Credential file must be 5 MB or smaller.')

    head = uploaded.read(8)
    uploaded.seek(0)      # the writer below re-reads from the start
    if not any(head.startswith(sig) for sig in _CREDENTIAL_MAGIC[ext]):
        raise ValueError('That file does not look like a real JPG, PNG, or PDF. '
                         'Please upload a photo or scan of your ID.')
    cred_dir = os.path.join(settings.MEDIA_ROOT, 'credentials')
    os.makedirs(cred_dir, exist_ok=True)
    filename = f'credential_{uuid4().hex}{ext}'
    with open(os.path.join(cred_dir, filename), 'wb') as fh:
        for chunk in uploaded.chunks():
            fh.write(chunk)
    return f'credentials/{filename}'


def patron_register(request):
    """Online registration: form → email OTP → pending Administrator approval.

    The account stays 'Pending' (no login possible) until an Administrator
    approves it in Manage Patrons, at which point the identity QR is
    generated and the patron is emailed."""
    if request.method != 'POST':
        return render(request, 'patron/patronregister.html', {'stage': 'form'})

    action = request.POST.get('action', 'register')

    # ── Step 2: OTP verification ─────────────────────────────
    if action == 'verify_otp':
        email = (request.POST.get('email') or '').strip()
        code = (request.POST.get('otp') or '').strip()
        patron = Patron.objects.filter(
            email__iexact=email, account_status='Pending', otp_verified=False
        ).first()
        if patron is None:
            return render(request, 'patron/patronregister.html',
                          {'stage': 'form', 'error': 'No pending registration found for that email. Please register again.'})
        if not patron.otp_code or not patron.otp_expires_at or timezone.now() > patron.otp_expires_at:
            return render(request, 'patron/patronregister.html',
                          {'stage': 'otp', 'otp_email': email,
                           'error': 'That code has expired. Click "Resend code" to get a new one.'})
        if not _otp_matches(code, patron.otp_code):
            return render(request, 'patron/patronregister.html',
                          {'stage': 'otp', 'otp_email': email,
                           'error': _count_failed_otp(patron)})
        patron.otp_verified = True
        patron.otp_code = None
        patron.otp_expires_at = None
        patron.otp_attempts = 0
        patron.save(update_fields=['otp_verified', 'otp_code', 'otp_expires_at', 'otp_attempts'])
        return render(request, 'patron/patronregister.html', {'stage': 'pending'})

    # ── Resend OTP ───────────────────────────────────────────
    if action == 'resend_otp':
        email = (request.POST.get('email') or '').strip()
        patron = Patron.objects.filter(
            email__iexact=email, account_status='Pending', otp_verified=False
        ).first()
        if patron is None:
            return render(request, 'patron/patronregister.html',
                          {'stage': 'form', 'error': 'No pending registration found for that email. Please register again.'})
        # This flow already tells the visitor whether a pending registration
        # exists, so a real countdown reveals nothing further.
        wait = _otp_cooldown_left(patron.otp_last_sent_at)
        if wait:
            return render(request, 'patron/patronregister.html',
                          {'stage': 'otp', 'otp_email': email,
                           'error': f'A code was just sent. Please wait {wait} second(s) before requesting another.'})
        patron.otp_code = _new_otp()
        patron.otp_expires_at = timezone.now() + timedelta(minutes=10)
        patron.otp_attempts = 0
        patron.otp_last_sent_at = timezone.now()
        patron.save(update_fields=['otp_code', 'otp_expires_at', 'otp_attempts', 'otp_last_sent_at'])
        if not otp_email(patron.email, patron.fullname, patron.otp_code):
            return render(request, 'patron/patronregister.html',
                          {'stage': 'otp', 'otp_email': email,
                           'error': 'We could not send the code right now. '
                                    'Please try again in a moment or contact the library.'})
        return render(request, 'patron/patronregister.html',
                      {'stage': 'otp', 'otp_email': email,
                       'info': 'A new code has been sent to your email.'})

    # ── Step 1: submit the registration form ────────────────
    first_name, middle_name, last_name, name_error = _name_from_post(request)
    fullname = compose_name(first_name, middle_name, last_name)
    email = (request.POST.get('email') or '').strip()
    password = request.POST.get('password')
    confirm_password = request.POST.get('confirm_password')
    patron_type = request.POST.get('patron_type')
    contact_number = (request.POST.get('contact_number') or '').strip()
    address = (request.POST.get('address') or '').strip()

    def _form_error(msg):
        return render(request, 'patron/patronregister.html', {'stage': 'form', 'error': msg})

    if name_error:
        return _form_error(name_error)
    if not all([fullname, email, password, confirm_password, patron_type, contact_number, address]):
        return _form_error('All fields are required')
    # Registration checked no length at all before this, so a one-character
    # password was accepted at sign-up.
    length_error = password_length_error(password)
    if length_error:
        return _form_error(length_error)
    if password != confirm_password:
        return _form_error('Passwords do not match')

    existing = Patron.objects.filter(email__iexact=email).first()
    if existing is not None:
        # A stale unverified application may be replaced; anything else is a duplicate.
        if existing.account_status == 'Pending' and not existing.otp_verified:
            existing.delete()
        else:
            return _form_error('Email already exists')

    # Nobody sees an online applicant, so the uploaded ID is the whole identity
    # check. The form marks the field required, but that is only a browser hint
    # - a request that skips it must be refused here too.
    uploaded_id = request.FILES.get('credential_document')
    if uploaded_id is None:
        return _form_error('Please attach a photo or scan of your valid ID. '
                           'The library reviews it before activating your account.')
    try:
        credential_path = _save_credential_document(uploaded_id, email)
    except ValueError as exc:
        return _form_error(str(exc))

    patron = Patron.objects.create(
        fullname=fullname,
        first_name=first_name,
        middle_name=middle_name,
        last_name=last_name,
        email=email,
        password_hash=hash_password(password),
        patron_type=patron_type,
        contact_number=contact_number,
        address=address,
        account_status='Pending',
        registration_channel='Online',
        credential_document=credential_path,
        otp_code=_new_otp(),
        otp_expires_at=timezone.now() + timedelta(minutes=10),
        otp_last_sent_at=timezone.now(),
        otp_verified=False,
    )
    if not otp_email(patron.email, patron.fullname, patron.otp_code):
        # The account exists but the code never left the building — say so
        # instead of parking the applicant on a code screen forever.
        return render(request, 'patron/patronregister.html',
                      {'stage': 'otp', 'otp_email': patron.email,
                       'error': 'Your details were saved, but we could not email your '
                                'verification code. Click "Resend code" to try again.'})
    return render(request, 'patron/patronregister.html',
                  {'stage': 'otp', 'otp_email': patron.email})



# ─── Open to visitors who have not registered ─────────────────────────────
# Looking a book up and being walked to its shelf is the library's public
# service; an account is only needed to take a book home. That is already the
# rule at the desk -- desk.py logs a walk-in as a Visitor and tells them to see
# a librarian with an ID before borrowing -- so the online catalogue follows it
# rather than inventing a stricter one. Nothing below reads the patron session.
def patron_catalog(request):
    search_query = request.GET.get('search', '').strip()

    books = Book.objects.filter(status='Available').select_related(
        'shelf_level', 'shelf_level__shelf'
    ).order_by('title')

    if search_query:
        books = books.filter(_book_search_q(search_query))

    if search_query:
        # Only real searches, never plain browsing -- a log entry per page view
        # would bury everything else and answer no question worth asking.
        log_patron_action(request, 'Search', 'Book', None,
                          f'Searched the catalogue for "{search_query[:80]}" '
                          f'({books.count()} result(s))')

    context = {
        'books': books,
        'search_query': search_query,
    }
    return render(request, 'patron/patroncatalog.html', context)


def patron_book_details(request, book_id):
    book = get_object_or_404(
        Book.objects.select_related(
            'shelf_level',
            'shelf_level__shelf',
            'shelf_level__shelf__room',
            'shelf_level__shelf__room__floor_plan',
        ),
        book_id=book_id,
    )

    # Walk the location hierarchy: Shelf Level -> Shelf -> Room -> FloorPlan
    shelf_level = book.shelf_level
    shelf = shelf_level.shelf if shelf_level else None
    room = shelf.room if shelf else None
    floor_plan = room.floor_plan if room else None

    context = {
        'book': book,
        'shelf_level': shelf_level,
        'shelf': shelf,
        'room': room,
        'floor_plan': floor_plan,
    }
    return render(request, 'patron/patronbook-details.html', context)


def patron_map(request):
    target = {}
    book_id = request.GET.get('book_id')
    if book_id:
        book = Book.objects.select_related('shelf_level__shelf').filter(book_id=book_id).first()
        if book:
            target['book_id'] = book.book_id
            target['book_title'] = book.title
            level = book.shelf_level
            shelf = level.shelf if level else None
            if shelf:
                target['shelf_id'] = shelf.shelf_id
                target['shelf_name'] = shelf.name
                # Which board, and how far along it. Sent as words rather than
                # as a second set of coordinates: a floor plan is flat, so the
                # level is height and can only ever be described, while the
                # slot and column are a real place along the shelf's face.
                target['level_label'] = level.label
                target['level_number'] = level.level_number
                target['column_number'] = level.column_number
                target['is_top'] = level.is_top
                target['is_under'] = level.is_under
                target['shelf_slot'] = book.shelf_slot
                target['location'] = book_location_words(book)
                # Everything positional is computed from the copies actually on
                # the shelf, so a book borrowed an hour ago does not still push
                # the count -- and the marker -- one space to the right.
                layout = level_layout(level, book) or {}
                focus = layout.get('focus') or {}
                target['position'] = focus.get('position')
                target['position_of'] = focus.get('of')
                target['on_shelf'] = focus.get('on_shelf', False)
                target['copy_status'] = focus.get('status', '')
                target['before'] = focus.get('before', '')
                target['after'] = focus.get('after', '')
                target['layout'] = layout.get('rows', [])
            # The navigation route is recomputed roughly once a second while the
            # map is open, so logging that would produce thousands of rows saying
            # the same thing. This records the patron asking to be taken to a
            # book, which is the event with any meaning in it.
            log_patron_action(request, 'Navigate', 'Book', book.book_id,
                              f'Opened the map to "{book.title[:60]}"'
                              + (f' at {target["shelf_name"]}' if target.get('shelf_name')
                                 else ' (shelf not placed on the map)'))
    return render(request, 'patron/patronmap.html', {'target': target})


def patron_announcements(request):
    announcements = Announcement.objects.filter(
        is_active=True
    ).order_by('-created_at')

    context = {
        'announcements': announcements,
    }
    return render(request, 'patron/patronannouncements.html', context)


def _patron_search_q(term):
    """One definition of "search for a patron", used by every patron search box.

    Four boxes had grown their own version and all four looked for the name and
    the email only -- while the Manage Patrons box promised "name, student ID,
    email" in its own placeholder. Most patrons have neither an email nor a
    contact number recorded, so in practice only the name worked and searching
    by the ID printed on a library card silently found nothing.

    The ID is matched exactly rather than as a substring: typing 18 should not
    return patrons 18, 180 and 1802 when the librarian is reading one number off
    a card.
    """
    term = (term or '').strip()
    if not term:
        return Q()
    q = (Q(fullname__icontains=term)
         | Q(email__icontains=term)
         | Q(contact_number__icontains=term))
    if term.isdigit():
        q |= Q(patron_id=int(term))
    return q


def _book_search_q(term):
    """One definition of "search for a book", used by every book search box.

    ISBNs are read off a back cover, where they are printed with hyphens, and
    stored here without them. Matching only the raw string means a correctly
    typed ISBN finds nothing, so the punctuation is stripped from the query and
    both forms are tried.
    """
    term = (term or '').strip()
    if not term:
        return Q()
    q = (Q(title__icontains=term)
         | Q(author__icontains=term)
         | Q(ISBN__icontains=term)
         | Q(genre__icontains=term))
    bare = re.sub(r'[^0-9A-Za-z]', '', term)
    if bare and bare != term:
        q |= Q(ISBN__icontains=bare)
    return q


def _floor_for_request(request, target_shelf=None):
    """Which floor's map to draw.

    Three questions in priority order, because they answer each other's gaps:
    an explicit ?floor= wins (the patron used the switcher); otherwise the
    floor the target book sits on (they asked to be taken to a book, so show
    the floor it is on); otherwise the lowest floor in service, which is where
    someone walking in from the street starts.
    """
    live = FloorPlan.objects.filter(is_active=True).order_by('floor_number', 'floor_plan_id')

    raw = (request.GET.get('floor') or '').strip()
    if raw.isdigit():
        chosen = live.filter(floor_plan_id=int(raw)).first()
        if chosen:
            return chosen, live

    if target_shelf is not None and target_shelf.room_id:
        on = live.filter(room__shelf=target_shelf).first()
        if on:
            return on, live

    return live.first(), live


def _floor_short_label(plan):
    """The one or two characters that fit on a lift button: 2, G, B1."""
    n = plan.floor_number if plan.floor_number is not None else 1
    if n < 0:
        return f'B{abs(n)}'
    if n == 0:
        return 'G'
    return str(n)


def _floor_payload(plans, current):
    """The floor switcher's options."""
    return [
        {
            'floor_plan_id': p.floor_plan_id,
            'floor_number': p.floor_number,
            'name': p.name,
            'label': p.floor_label,
            'short': _floor_short_label(p),
            'is_current': (current is not None and p.floor_plan_id == current.floor_plan_id),
        }
        for p in plans
    ]


@patron_login_required
def patron_account(request):
    patron_id = request.session.get('patron_id')
    patron = get_object_or_404(Patron, patron_id=patron_id)

    transactions = Transaction.objects.filter(
        patron=patron
    ).select_related('book').order_by('-transaction_date')

    has_overdue = transactions.filter(overdue_flag=True).exists()
    overdue_transactions = transactions.filter(overdue_flag=True)

    # Convenience splits for the template (derived from `transactions`)
    active_loans = list(transactions.filter(
        transaction_type='Borrow', return_date__isnull=True
    ))
    history = transactions.filter(return_date__isnull=False)

    # A loan can have at most one open request at a time (enforced in
    # patron_request_extension), so this is a lookup, not a list.
    pending_by_tx = {
        e.transaction_id: e
        for e in DueDateExtension.objects.filter(
            transaction__in=active_loans, status='Pending')
    }
    for tx in active_loans:
        tx.pending_extension = pending_by_tx.get(tx.transaction_id)

    context = {
        'patron': patron,
        'transactions': transactions,
        'has_overdue': has_overdue,
        'overdue_transactions': overdue_transactions,
        'active_loans': active_loans,
        'history': history,
    }
    return render(request, 'patron/patronaccount.html', context)


@patron_login_required
def patron_request_extension(request):
    """Patron asks to push a loan's due date out; staff decide from here."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})
    patron = get_object_or_404(Patron, patron_id=request.session.get('patron_id'))

    tx = Transaction.objects.filter(
        transaction_id=request.POST.get('transaction_id'),
        patron=patron, transaction_type='Borrow', return_date__isnull=True,
    ).first()
    if tx is None:
        return JsonResponse({'success': False, 'error': 'Loan not found.'})
    if not tx.due_date:
        return JsonResponse({'success': False, 'error': 'This loan has no due date to extend.'})
    if DueDateExtension.objects.filter(transaction=tx, status='Pending').exists():
        return JsonResponse({'success': False, 'error': 'You already have a pending request for this book.'})

    reason = (request.POST.get('reason') or '').strip()[:255]
    rule = BorrowingRule.current()
    today = timezone.localdate()
    # A fresh full loan period from today, not from the old due date: an
    # overdue book extended from its own (past) due date could still land in
    # the past or barely in the future, which answers "extended" with a date
    # that does not actually buy the patron more time.
    requested_due = today + timedelta(days=rule.loan_period_days)

    extension = DueDateExtension.objects.create(
        transaction=tx, requested_by_patron=True,
        previous_due_date=tx.due_date, requested_due_date=requested_due,
        reason=reason or None,
    )
    return JsonResponse({
        'success': True,
        'extension_id': extension.extension_id,
        'requested_due_date': requested_due.strftime('%b %d, %Y'),
    })


@patron_login_required
def patron_update_profile(request):
    """Save profile edits from the My Account page (AJAX)."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})
    patron = get_object_or_404(Patron, patron_id=request.session.get('patron_id'))

    first_name, middle_name, last_name, name_error = _name_from_post(request)
    fullname = compose_name(first_name, middle_name, last_name)
    email = (request.POST.get('email') or '').strip()
    contact_number = (request.POST.get('contact_number') or '').strip()
    address = (request.POST.get('address') or '').strip()

    if name_error:
        return JsonResponse({'success': False, 'error': name_error})
    if not email:
        return JsonResponse({'success': False, 'error': 'Email is required.'})
    if Patron.objects.exclude(patron_id=patron.patron_id).filter(email__iexact=email).exists():
        return JsonResponse({'success': False, 'error': 'That email is already in use by another account.'})

    patron.fullname = fullname
    patron.email = email
    patron.contact_number = contact_number or None
    patron.address = address or None
    patron.first_name = first_name
    patron.middle_name = middle_name
    patron.last_name = last_name
    patron.save(update_fields=['fullname', 'first_name', 'middle_name', 'last_name',
                               'email', 'contact_number', 'address'])
    request.session['patron_fullname'] = patron.fullname
    log_patron_action(request, 'Update', 'Patron', patron.patron_id,
                      'Updated their own profile details', patron=patron)
    return JsonResponse({'success': True, 'message': 'Profile updated.'})


@patron_login_required
def patron_change_password(request):
    """Change the login password: current password, then an emailed OTP.

    Both passwords are revalidated at the verify step rather than trusted from
    the request that sent the code, so the change that lands is checked against
    the account as it stands when it is actually applied.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})
    patron = get_object_or_404(Patron, patron_id=request.session.get('patron_id'))
    action = (request.POST.get('action') or 'request').strip()

    current = request.POST.get('current_password') or ''
    new = request.POST.get('new_password') or ''
    if not check_password(current, patron.password_hash):
        return JsonResponse({'success': False, 'error': 'Current password is incorrect.'})
    # Checked after the current password, so a stranger poking at this endpoint
    # never learns anything about the password already set.
    policy_error = password_length_error(new, current_hash=patron.password_hash)
    if policy_error:
        return JsonResponse({'success': False, 'error': policy_error})

    if action in ('request', 'resend'):
        if not patron.email:
            return JsonResponse({'success': False, 'error': 'Your account has no email address on file to send a code to.'})
        wait = _otp_cooldown_left(patron.otp_last_sent_at)
        if wait:
            return JsonResponse({'success': False,
                                 'error': f'A code was just sent. Please wait {wait} second(s) before requesting another.'})
        if not _issue_account_otp(patron, 'password', 'change the password on'):
            return JsonResponse({'success': False, 'error': 'Could not send the verification code. Please try again.'})
        return JsonResponse({'success': True, 'stage': 'otp'})

    if action == 'verify':
        error = _check_account_otp(patron, 'password', (request.POST.get('code') or '').strip())
        if error:
            return JsonResponse({'success': False, 'error': error})

        patron.password_hash = hash_password(new)
        _clear_account_otp(patron, extra_fields=['password_hash'])
        log_patron_action(request, 'Password change', 'Patron', patron.patron_id,
                          'Changed their own password, verified by OTP', patron=patron)
        return JsonResponse({'success': True, 'stage': 'done', 'message': 'Password changed.'})

    return JsonResponse({'success': False, 'error': 'Invalid action.'})


@patron_login_required
def patron_deactivate_account(request):
    """Self-service account deactivation (Figure 20): OTP-verified, immediate.

    A patron with any unreturned book is refused -- the library's own
    definition of the diagram's "account status check... not ready for
    deactivation" step.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})
    patron = get_object_or_404(Patron, patron_id=request.session.get('patron_id'))
    action = (request.POST.get('action') or 'request').strip()

    active_loans = Transaction.objects.filter(
        patron=patron, transaction_type='Borrow', return_date__isnull=True,
    ).select_related('book')
    if active_loans.exists():
        titles = ', '.join(tx.book.title if tx.book else 'Unknown title' for tx in active_loans)
        return JsonResponse({
            'success': False,
            'error': f'You still have unreturned book(s): {titles}. Return them before deactivating your account.',
        })

    if action in ('request', 'resend'):
        # Signed in, so the account plainly exists -- a real countdown here
        # gives nothing away and beats a silent no-op.
        wait = _otp_cooldown_left(patron.otp_last_sent_at)
        if wait:
            return JsonResponse({'success': False,
                                 'error': f'A code was just sent. Please wait {wait} second(s) before requesting another.'})
        if not _issue_account_otp(patron, 'deactivate', 'deactivate'):
            return JsonResponse({'success': False, 'error': 'Could not send the verification code. Please try again.'})
        return JsonResponse({'success': True, 'stage': 'otp'})

    if action == 'verify':
        error = _check_account_otp(patron, 'deactivate', (request.POST.get('code') or '').strip())
        if error:
            return JsonResponse({'success': False, 'error': error})

        patron.account_status = 'Inactive'
        _clear_account_otp(patron, extra_fields=['account_status'])
        log_patron_action(request, 'Deactivate', 'Patron', patron.patron_id,
                          'Deactivated their own account, verified by OTP', patron=patron)
        request.session.flush()
        return JsonResponse({'success': True, 'stage': 'done'})

    return JsonResponse({'success': False, 'error': 'Invalid action.'})


# Admin views
def _portal_login(request, template, scope, expected_role, home, wrong_portal_text):
    """One sign-in implementation for the Administrator and Library Staff doors.

    Three things it does that the two hand-written copies did not.

    Throttling: the run of failures is counted per email address, and five
    misses inside half an hour close the door for fifteen minutes. A miss
    against an address with no account is counted too -- if those were free, the
    throttle would answer "does this address exist?" all by itself.

    Session rotation: cycle_key() before anything is written into the session.
    Without it the ID the browser arrived with is the ID it keeps, so anyone able
    to plant a cookie on a shared desk machine -- and desk mode means these
    machines are shared by design -- holds a valid staff session the moment a
    librarian signs in on it.

    One error message: the old code answered "suspended or inactive" for a real
    account and "invalid email or password" for an unknown one, and it checked
    status *before* the password. Any address could therefore be tested for
    existence without knowing its password at all.
    """
    if request.method != 'POST':
        return render(request, template)

    email = (request.POST.get('email') or '').strip()
    password = request.POST.get('password') or ''

    locked = login_locked_message(scope, email)
    if locked:
        return render(request, template, {'error': locked})

    user = User.objects.filter(email__iexact=email).first()

    # The password is verified before anything else is looked at, and a missing
    # account still pays for a hash, so the two cases cost the same and look the
    # same from outside.
    if user is None:
        waste_password_time()
        password_ok = False
    else:
        password_ok = check_password(password, user.password_hash)

    if user is None or not password_ok or user.account_status != 'Active' or user.role != expected_role:
        remaining = record_login_failure(scope, email)

        # Failed staff and Administrator sign-ins were not recorded anywhere,
        # while patron ones were -- so the accounts worth attacking had the
        # weaker trail. They are recorded now, without the password and without
        # saying which part was wrong.
        log_system_action(
            'Login failed', 'Auth',
            getattr(user, 'admin_id', None),
            f'Failed {scope} sign-in for "{email[:120]}"',
        )

        # A wrong portal is worth naming: it is only reachable with a correct
        # password, so it discloses nothing an attacker does not already hold,
        # and staff do land on the wrong page.
        if user is not None and password_ok and user.account_status == 'Active' and user.role != expected_role:
            return render(request, template, {'error': wrong_portal_text})

        error = LOGIN_FAILED_TEXT
        if remaining is not None and 0 < remaining <= 2:
            error += f' {remaining} attempt(s) left before this account is locked.'
        return render(request, template, {'error': error})

    clear_login_failures(scope, email)
    # flush(), not cycle_key(). Both give the browser a new session id, which is
    # what defeats fixation -- but cycle_key keeps the *data* that was in the old
    # session, so anything an attacker managed to seed there (a desk-mode flag, a
    # stale patron_id) would ride across the login. flush() starts empty.
    request.session.flush()
    request.session['admin_id'] = user.admin_id
    request.session['admin_fullname'] = user.fullname
    request.session['admin_role'] = user.role
    log_admin_action(request, 'Login', 'Auth', user.admin_id,
                     f'{user.fullname} ({user.role}) logged in')
    return redirect(home)


def admin_login(request):
    return _portal_login(
        request, 'admin/signin.html', 'admin', 'Admin', '/admin-portal/dashboard/',
        'This is the administrator portal. Please use the Library Staff login.',
    )


def admin_logout(request):
    request.session.flush()
    return redirect('/admin-portal/login/')


# Library Staff portal authentication
def staff_login(request):
    return _portal_login(
        request, 'library_staff/signin.html', 'staff', 'Staff', '/library-staff/dashboard/',
        'This portal is for library staff. Please use the administrator login.',
    )


def staff_logout(request):
    request.session.flush()
    return redirect('/library-staff/login/')


# ─── FORGOT PASSWORD (OTP) — all three portals ────────────────────────────
# One implementation, three thin entry points. The portal decides which table
# and which role the email is resolved against, so a code issued at the staff
# login cannot be spent at the admin login or vice versa.

PASSWORD_RESET_PORTALS = {
    'patron': {
        'account_type': 'Patron',
        'template': 'patron/patronforgotpassword.html',
        'login_url': 'patron_login',
        'reset_url': 'patron_forgot_password',
        'role_label': 'patron account',
    },
    'staff': {
        'account_type': 'Staff',
        'template': 'library_staff/forgotpassword.html',
        'login_url': 'staff_login',
        'reset_url': 'staff_forgot_password',
        'role_label': 'library staff account',
    },
    'admin': {
        'account_type': 'Admin',
        'template': 'admin/forgotpassword.html',
        'login_url': 'admin_login',
        'reset_url': 'admin_forgot_password',
        'role_label': 'administrator account',
    },
}

# Deliberately identical whether or not the email matched an account, so the
# form cannot be used to discover which addresses are registered.
_RESET_SENT_NOTE = ('If an account exists for that email, a 6-digit code is on its way. '
                    'The code expires in 10 minutes.')


def _find_reset_account(account_type, email):
    """The Patron or User row this reset applies to, or None."""
    if not email:
        return None
    if account_type == 'Patron':
        return Patron.objects.filter(email__iexact=email).first()
    return User.objects.filter(email__iexact=email, role=account_type).first()


def _issue_reset_code(account_type, email, fullname, role_label):
    """Invalidate any outstanding codes, mint a new one, and email it."""
    PasswordResetOTP.objects.filter(
        account_type=account_type, email__iexact=email, used_at__isnull=True
    ).update(used_at=timezone.now())

    reset = PasswordResetOTP.objects.create(
        account_type=account_type,
        email=email,
        code=_new_otp(),
        expires_at=timezone.now() + timedelta(minutes=10),
    )
    return password_reset_otp_email(email, fullname, reset.code, role_label)


def _redeem_reset_code(account_type, email, code):
    """Check a reset code, counting the attempt against its limit.

    Returns (reset, error, exhausted). `reset` is the row to mark used once the
    caller has actually applied the change; `exhausted` says the code is burnt
    and the patron/staff has to request a new one rather than retype this one.

    Shared by the forgot-password flow and the signed-in change-password flow,
    so both get the same expiry and the same MAX_ATTEMPTS ceiling instead of one
    of them quietly allowing unlimited guesses.
    """
    reset = (PasswordResetOTP.objects
             .filter(account_type=account_type, email__iexact=email, used_at__isnull=True)
             .order_by('-created_at').first())
    if reset is None or not reset.is_usable:
        return None, 'That code is no longer valid. Request a new one.', True

    if not _otp_matches(code, reset.code):
        reset.attempts += 1
        reset.save(update_fields=['attempts'])
        remaining = PasswordResetOTP.MAX_ATTEMPTS - reset.attempts
        if remaining <= 0:
            return None, 'Too many incorrect codes. Request a new one to try again.', True
        return None, f'Incorrect code. {remaining} attempt(s) left.', False

    return reset, None, False


def _password_reset_view(request, portal):
    cfg = PASSWORD_RESET_PORTALS[portal]
    template = cfg['template']
    account_type = cfg['account_type']

    def _render(stage, **extra):
        context = {'stage': stage, 'portal': portal,
                   'login_url_name': cfg['login_url'], 'reset_url_name': cfg['reset_url']}
        context.update(extra)
        return render(request, template, context)

    if request.method != 'POST':
        return _render('request')

    action = (request.POST.get('action') or '').strip()
    email = (request.POST.get('email') or '').strip()

    # ── Ask for a code ───────────────────────────────────────
    if action in ('request', 'resend'):
        if not email:
            return _render('request', error='Enter the email address on your account.')

        account = _find_reset_account(account_type, email)
        # Applied silently, like the account lookup itself: a visible "wait 40
        # seconds" would fire only for addresses that exist, which is exactly
        # what answering every address identically is meant to hide.
        cooling = _reset_cooldown_left(account_type, email)
        if account is not None and not cooling:
            fullname = getattr(account, 'fullname', '') or 'there'
            if not _issue_reset_code(account_type, email, fullname, cfg['role_label']):
                return _render('request', email=email,
                               error='We could not send the code right now. '
                                     'Please try again in a moment.')
        # No account: fall through and show the same screen anyway.
        note = ('A new code has been sent if the account exists.'
                if action == 'resend' else _RESET_SENT_NOTE)
        # Said to everyone, so it explains the wait without confirming anything.
        note += (f' If you already asked for one, you can request another after '
                 f'{int(OTP_RESEND_COOLDOWN.total_seconds())} seconds.')
        return _render('otp', email=email, info=note)

    # ── Submit the code and the new password ─────────────────
    if action == 'reset':
        code = (request.POST.get('code') or '').strip()
        new_password = request.POST.get('new_password') or ''
        confirm_password = request.POST.get('confirm_password') or ''

        if not code:
            return _render('otp', email=email, error='Enter the 6-digit code from your email.')
        if new_password != confirm_password:
            return _render('otp', email=email, error='The two passwords do not match.')
        # The full policy, not just the length. This path had drifted back to a
        # bare length check, which meant the one flow an attacker reaches after
        # compromising an inbox was also the flow with the weakest rules --
        # "password" and "aaaaaaaa" both passed here while being refused
        # everywhere else.
        policy_error = password_length_error(new_password)
        if policy_error:
            return _render('otp', email=email, error=policy_error)

        reset, error, exhausted = _redeem_reset_code(account_type, email, code)
        if error:
            return _render('request' if exhausted else 'otp', email=email, error=error)

        account = _find_reset_account(account_type, email)
        if account is None:
            # The account disappeared between issuing and redeeming the code.
            reset.used_at = timezone.now()
            reset.save(update_fields=['used_at'])
            return _render('request', error='That account is no longer available.')

        account.password_hash = hash_password(new_password)
        account.save(update_fields=['password_hash'])
        reset.used_at = timezone.now()
        reset.save(update_fields=['used_at'])

        # A password reset happens with nobody signed in, so there is no session
        # to name the actor. Attributing it to the account being reset is the
        # only truthful option -- and the account is exactly what an auditor
        # would search for.
        if account_type == 'Patron':
            log_patron_action(request, 'Password reset', 'Patron', account.patron_id,
                              f'Reset their password by emailed code ({account.email})',
                              patron=account)
        else:
            log_system_action('Password reset', 'User', account.admin_id,
                              f'{cfg["role_label"]} {account.fullname} reset their password '
                              f'by emailed code ({account.email})')

        return _render('done')

    return _render('request')


def patron_forgot_password(request):
    return _password_reset_view(request, 'patron')


def patron_reactivate_request(request):
    """Self-service reactivation (Figure 19): OTP, then forwarded to an Admin.

    Unlike deactivation, a verified OTP here does not reactivate the account
    by itself -- it only creates a Pending ReactivationRequest for an Admin
    to approve or reject, matching the diagram's admin-review step. An
    Inactive patron cannot log in, so this view is reached from the login
    page rather than the (authenticated) Account page.
    """
    def _render(stage, **extra):
        context = {'stage': stage}
        context.update(extra)
        return render(request, 'patron/patronreactivate.html', context)

    # Deliberately identical whether or not the email matches an eligible
    # account, so the form cannot be used to discover account status --
    # mirrors the same rule in _password_reset_view above.
    note = 'If that account is eligible for reactivation, a verification code has been sent.'

    if request.method != 'POST':
        return _render('request')

    action = (request.POST.get('action') or '').strip()
    email = (request.POST.get('email') or '').strip()

    if action in ('request', 'resend'):
        if not email:
            return _render('request', error='Enter the email address on your account.')
        # Only Inactive accounts are eligible -- Suspended is a separate,
        # Admin-only status this self-service flow does not touch.
        patron = Patron.objects.filter(email__iexact=email, account_status='Inactive').first()
        # The cooldown is applied silently: surfacing "wait 40 seconds" only
        # for real accounts would undo the whole point of answering every
        # address identically. The reply below is the same either way.
        if patron is not None and not _otp_cooldown_left(patron.otp_last_sent_at):
            _issue_account_otp(patron, 'reactivate', 'reactivate')
        return _render('otp', email=email, info=note)

    if action == 'verify':
        code = (request.POST.get('code') or '').strip()
        if not code:
            return _render('otp', email=email, error='Enter the 6-digit code from your email.')
        patron = Patron.objects.filter(email__iexact=email, account_status='Inactive').first()
        if patron is None:
            return _render('otp', email=email, error='That code is no longer valid. Request a new one.')
        error = _check_account_otp(patron, 'reactivate', code)
        if error:
            return _render('otp', email=email, error=error)

        _clear_account_otp(patron)
        ReactivationRequest.objects.get_or_create(patron=patron, status='Pending')
        return _render('done')

    return _render('request')


def staff_forgot_password(request):
    return _password_reset_view(request, 'staff')


def admin_forgot_password(request):
    return _password_reset_view(request, 'admin')


@admin_login_required
def portal_change_password(request):
    """Signed-in Library Staff / Administrator changes their own password.

    Confirmed by an emailed code, the same as the patron flow. The code rides
    on PasswordResetOTP rather than new columns on User: that model already
    scopes codes by account_type, expires them, and caps guesses at
    MAX_ATTEMPTS, all of which this needs. A code minted here and one minted by
    "forgot password" grant the same thing -- set a new password on this
    account, having proved control of its inbox -- so there is nothing to
    separate them for, and this path additionally demands the session and the
    current password on top.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})
    user = get_object_or_404(User, admin_id=request.session.get('admin_id'))
    action = (request.POST.get('action') or 'request').strip()

    current = request.POST.get('current_password') or ''
    new = request.POST.get('new_password') or ''
    if not check_password(current, user.password_hash):
        return JsonResponse({'success': False, 'error': 'Current password is incorrect.'})
    # Checked after the current password, so a stranger poking at this endpoint
    # never learns anything about the password already set.
    policy_error = password_length_error(new, current_hash=user.password_hash)
    if policy_error:
        return JsonResponse({'success': False, 'error': policy_error})

    account_type = 'Admin' if user.role == 'Admin' else 'Staff'
    role_label = 'administrator account' if user.role == 'Admin' else 'library staff account'

    if action in ('request', 'resend'):
        if not user.email:
            return JsonResponse({'success': False, 'error': 'Your account has no email address on file to send a code to.'})
        wait = _reset_cooldown_left(account_type, user.email)
        if wait:
            return JsonResponse({'success': False,
                                 'error': f'A code was just sent. Please wait {wait} second(s) before requesting another.'})
        if not _issue_reset_code(account_type, user.email, user.fullname, role_label):
            return JsonResponse({'success': False, 'error': 'Could not send the verification code. Please try again.'})
        return JsonResponse({'success': True, 'stage': 'otp'})

    if action == 'verify':
        code = (request.POST.get('code') or '').strip()
        if not code:
            return JsonResponse({'success': False, 'error': 'Enter the 6-digit code from your email.'})
        reset, error, _exhausted = _redeem_reset_code(account_type, user.email, code)
        if error:
            return JsonResponse({'success': False, 'error': error})

        user.password_hash = hash_password(new)
        user.save(update_fields=['password_hash'])
        reset.used_at = timezone.now()
        reset.save(update_fields=['used_at'])
        log_admin_action(request, 'Update', 'User', user.admin_id,
                         f'Self-service password change via OTP for {user.email}')
        return JsonResponse({'success': True, 'stage': 'done', 'message': 'Password changed.'})

    return JsonResponse({'success': False, 'error': 'Invalid action.'})


@admin_only_required
def admin_dashboard(request):
    admin_id = request.session.get('admin_id')
    admin = User.objects.filter(admin_id=admin_id).first()
    today = timezone.localdate()

    # Book counts by status
    books_available = Book.objects.filter(status='Available').count()
    books_borrowed = Book.objects.filter(status='Borrowed').count()
    books_overdue = Book.objects.filter(status='Overdue').count()
    books_lost = Book.objects.filter(status='Lost').count()
    books_donated = Book.objects.filter(status='Donated').count()
    books_being_read = Book.objects.filter(status='Being Read').count()

    # Patron counts
    total_patrons = Patron.objects.count()
    patrons_active = Patron.objects.filter(account_status='Active').count()
    patrons_suspended = Patron.objects.filter(account_status='Suspended').count()
    patrons_inactive = Patron.objects.filter(account_status='Inactive').count()

    # Recent transactions (5 most recent)
    recent_transactions = Transaction.objects.select_related('patron', 'book').order_by('-transaction_date')[:5]

    # PatronLog visit count for today (one session row per visit)
    visitors_today = PatronLog.objects.filter(entry_time__date=today).count()


    context = {
        'admin': admin,
        # Book status counts
        'books_available': books_available,
        'books_borrowed': books_borrowed,
        'books_overdue': books_overdue,
        'books_lost': books_lost,
        'books_donated': books_donated,
        'books_being_read': books_being_read,
        # Patron counts
        'total_patrons': total_patrons,
        'patrons_active': patrons_active,
        'patrons_suspended': patrons_suspended,
        'patrons_inactive': patrons_inactive,
        # Transactions and logs
        'recent_transactions': recent_transactions,
        'visitors_today': visitors_today,
    }
    return render(request, 'admin/dashboard.html', context)


@staff_only_required
def staff_dashboard(request):
    """Scaled-down dashboard for Library Staff (book/transaction/visit metrics only)."""
    admin_id = request.session.get('admin_id')
    admin = User.objects.filter(admin_id=admin_id).first()
    today = timezone.localdate()

    books_available = Book.objects.filter(status='Available').count()
    books_borrowed = Book.objects.filter(status='Borrowed').count()
    books_overdue = Book.objects.filter(status='Overdue').count()
    books_being_read = Book.objects.filter(status='Being Read').count()

    recent_transactions = Transaction.objects.select_related('patron', 'book').order_by('-transaction_date')[:5]
    visitors_today = PatronLog.objects.filter(entry_time__date=today).count()

    context = {
        'admin': admin,
        'books_available': books_available,
        'books_borrowed': books_borrowed,
        'books_overdue': books_overdue,
        'books_being_read': books_being_read,
        'recent_transactions': recent_transactions,
        'visitors_today': visitors_today,
    }
    return render(request, 'library_staff/dashboard.html', context)


def admin_signin(request):
    return render(request, 'admin/signin.html')


# What the Copies dropdown offers. '+' means "or more"; parse_copies_filter
# reads both forms, so a hand-typed ?copies=7 works as well.
COPIES_FILTER_CHOICES = [
    ('1', 'Single copy only'),
    ('2', 'Exactly 2'),
    ('3', 'Exactly 3'),
    ('4', 'Exactly 4'),
    ('2+', '2 or more (duplicated)'),
    ('5+', '5 or more'),
]


def _copies_annotation():
    """How many copies of this book the library holds, per row.

    A copy is its own Book row -- each has its own QR code and its own loan
    history -- so 'copies' is the number of rows sharing a title and author.
    The same definition as the book detail panel, deliberately: two places
    answering 'how many do we have' with different numbers is worse than
    either answer being arguable.
    """
    same_book = (
        Book.objects
        .filter(title=OuterRef('title'), author=OuterRef('author'))
        .order_by().values('title', 'author')
        .annotate(n=Count('*')).values('n')[:1]
    )
    return Subquery(same_book, output_field=IntegerField())


def parse_copies_filter(raw):
    """'3' -> exactly three, '3+' -> three or more, anything else -> no filter."""
    text = (raw or '').strip()
    at_least = text.endswith('+')
    digits = text[:-1] if at_least else text
    if not digits.isdigit():
        return None
    n = int(digits)
    if n < 1 or n > 99:
        return None
    return {'copies__gte': n} if at_least else {'copies': n}


def _books_page(request, template):
    from urllib.parse import urlencode
    q = (request.GET.get('q') or '').strip()
    status = (request.GET.get('status') or '').strip()
    genre = (request.GET.get('genre') or '').strip()
    material = (request.GET.get('material') or '').strip()
    copies = (request.GET.get('copies') or '').strip()

    books_queryset = (
        Book.objects.select_related('shelf_level', 'shelf_level__shelf')
        .annotate(copies=_copies_annotation())
    )
    if q:
        books_queryset = books_queryset.filter(_book_search_q(q))
    valid_status = [choice[0] for choice in Book.STATUS_CHOICES]
    if status in valid_status:
        books_queryset = books_queryset.filter(status=status)
    if genre:
        books_queryset = books_queryset.filter(genre=genre)
    # What kind of material it is, as opposed to what it is about -- a separate
    # question from genre, and the one a librarian filters on to find the
    # magazines or the bound journals.
    valid_material = [choice[0] for choice in Book.MATERIAL_TYPE_CHOICES]
    if material in valid_material:
        books_queryset = books_queryset.filter(material_type=material)
    # 'Which titles do we hold four of' is a stock-taking question, and until
    # now the only way to answer it was to scroll.
    copies_filter = parse_copies_filter(copies)
    if copies_filter:
        books_queryset = books_queryset.filter(**copies_filter)
    else:
        copies = ''
    books_queryset = books_queryset.order_by('title')

    # Global stats (independent of the filters above).
    total_copies = Book.objects.count()
    # The card above this one is headed "Unique books", so it has to count
    # books rather than rows: seven copies of one encyclopaedia are seven
    # copies, and saying otherwise contradicts the Copies column beneath it.
    total_books = (Book.objects.order_by()
                   .values('title', 'author').distinct().count())
    available_count = Book.objects.filter(status='Available').count()
    borrowed_count = Book.objects.filter(status='Borrowed').count()
    shelf_levels = ShelfLevel.objects.select_related('shelf').all()
    genres = list(
        Book.objects.exclude(genre__isnull=True).exclude(genre='')
        .order_by('genre').values_list('genre', flat=True).distinct()
    )

    paginator = Paginator(books_queryset, 15)  # 15 books per page
    books = paginator.get_page(request.GET.get('page', 1))


    params = {}
    for key, value in (('q', q), ('status', status), ('genre', genre),
                       ('material', material), ('copies', copies)):
        if value:
            params[key] = value

    context = {
        'books': books,
        'total_books': total_books,
        'total_copies': total_copies,
        'available_count': available_count,
        'borrowed_count': borrowed_count,
        'shelf_levels': shelf_levels,
        'genres': genres,
        'material_choices': Book.MATERIAL_TYPE_CHOICES,
        'material': material,
        'status_choices': valid_status,
        'q': q,
        'status': status,
        'genre': genre,
        'copies': copies,
        'copies_choices': COPIES_FILTER_CHOICES,
        'querystring': urlencode(params),
        'paginator': paginator,
    }
    return render(request, template, context)


@admin_module_required('books')
def admin_management(request):
    return _books_page(request, 'admin/managebooks.html')


@module_required('books')
def staff_manage_books(request):
    return _books_page(request, 'library_staff/managebooks.html')


@granted_module_required('books')
def admin_add_book(request):
    error = None

    if request.method == 'POST':
        title = request.POST.get('title', '').strip()
        author = request.POST.get('author', '').strip()
        isbn = request.POST.get('ISBN', '').strip()
        genre = request.POST.get('genre', '').strip()
        # Unknown or missing falls back to Book rather than being rejected: the
        # type is a convenience for filtering, not something worth blocking a
        # catalogue entry over.
        material_type = request.POST.get('material_type', 'Book').strip()
        if material_type not in {c[0] for c in Book.MATERIAL_TYPE_CHOICES}:
            material_type = 'Book'
        status = request.POST.get('status', 'Available').strip()
        shelf_level_id = request.POST.get('shelf_level', '').strip()
        shelf_slot = parse_shelf_slot(request.POST.get('shelf_slot'))
        condition = (request.POST.get('condition') or '').strip().title()
        if condition not in dict(Book.CONDITION_CHOICES):
            condition = 'Good'
        call_number = (request.POST.get('call_number') or '').strip() or None
        publication_year = request.POST.get('publication_year', '').strip()
        cover_img_url = request.POST.get('cover_img_url', '').strip()

        if not title or not author:
            error = 'Title and author are required.'
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                from django.http import JsonResponse
                return JsonResponse({'success': False, 'error': error})
        else:
            shelf_level = None

            if shelf_level_id:
                shelf_level = ShelfLevel.objects.filter(shelf_level_id=shelf_level_id).first()

            qr_code = str(uuid4())

            book = Book.objects.create(
                title=title,
                author=author,
                ISBN=isbn,
                genre=genre,
                material_type=material_type,
                status=status or 'Available',
                shelf_level=shelf_level,
                shelf_slot=shelf_slot,
                condition=condition,
                call_number=call_number,
                publication_year=int(publication_year) if publication_year else None,
                cover_img_url=cover_img_url if cover_img_url else None,
                qr_code=qr_code
            )
            log_admin_action(request, 'Create', 'Book', book.book_id, f'Added "{book.title}"')

            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                from django.http import JsonResponse
                return JsonResponse({'success': True, 'message': 'Book added successfully.'})
            return portal_redirect(request, 'admin_management')

    # Get books list context for managebooks.html
    books = Book.objects.select_related('shelf_level', 'shelf_level__shelf').order_by('title')
    total_books = books.count()
    total_copies = total_books
    available_count = books.filter(status='Available').count()
    borrowed_count = books.filter(status='Borrowed').count()
    shelf_levels = ShelfLevel.objects.select_related('shelf').all()

    context = {
        'books': books,
        'total_books': total_books,
        'total_copies': total_copies,
        'available_count': available_count,
        'borrowed_count': borrowed_count,
        'shelf_levels': shelf_levels,
        'error': error,
    }
    return render(request, 'admin/managebooks.html', context)


def _patron_page_redirect(request):
    """Staff land back on their patron desk, Administrators on the full module."""
    if request.session.get('admin_role') == 'Staff':
        return redirect('staff_manage_patron')
    return redirect('admin_manage_patron')


def _patron_page_template(request):
    if request.session.get('admin_role') == 'Staff':
        return 'library_staff/managepatron.html'
    return 'admin/managepatron.html'


@admin_or_module_required('patrons')
def admin_add_patron(request):
    error = None
    initial = {}

    if request.method == 'POST':
        first_name, middle_name, last_name, name_error = _name_from_post(request)
        fullname = compose_name(first_name, middle_name, last_name)
        patron_type = request.POST.get('patron_type', '').strip()
        email = request.POST.get('email', '').strip()
        contact_number = request.POST.get('contact_number', '').strip()
        address = request.POST.get('address', '').strip()
        password = request.POST.get('password', '').strip()
        account_status = request.POST.get('account_status', 'Active').strip()
        # On-site identity validation (Ch.1 ¶242, Fig. 5). The patron presents
        # a physical ID across the desk; nothing about the document is stored,
        # only the fact that a named staff member checked it at a given time.
        id_confirmed = request.POST.get('id_confirmed', '').strip() in ('1', 'true', 'on', 'yes')

        initial = {
            'fullname': fullname,
            'first_name': first_name,
            'middle_name': middle_name,
            'last_name': last_name,
            'patron_type': patron_type,
            'email': email,
            'contact_number': contact_number,
            'address': address,
            'account_status': account_status,
        }

        if name_error:
            error = name_error
        elif not all([patron_type, email, password]):
            error = 'Patron type, email, and password are required.'
        elif Patron.objects.filter(email=email).exists():
            error = 'A patron with that email already exists.'
        elif not id_confirmed:
            error = 'Confirm that you checked the patron\'s physical ID before registering them.'

        if error is None:
            hashed_password = hash_password(password)
            verifier = User.objects.filter(admin_id=request.session.get('admin_id')).first()
            patron = Patron.objects.create(
                fullname=fullname,
                first_name=first_name,
                middle_name=middle_name,
                last_name=last_name,
                email=email,
                password_hash=hashed_password,
                patron_type=patron_type,
                contact_number=contact_number,
                address=address,
                account_status=account_status or 'Active',
                registration_channel='On-site',
                qr_code=str(uuid4()),
                otp_verified=True,
                identity_verified_by=verifier,
                identity_verified_at=timezone.now(),
            )
            log_admin_action(request, 'Create', 'Patron', patron.patron_id,
                             f'Added "{patron.fullname}" — physical ID presented and '
                             f'checked at the desk')
            return _patron_page_redirect(request)

    # Get patron list context
    patrons = Patron.objects.annotate(
        active_borrows=Count(
            'transaction',
            filter=Q(transaction__transaction_type='Borrow', transaction__return_date__isnull=True)
        ),
        overdue_count=Count(
            'transaction',
            filter=Q(transaction__overdue_flag=True)
        ),
        borrow_count=Count(
            'transaction',
            filter=Q(transaction__transaction_type='Borrow')
        ),
    ).order_by('-registration_date')

    total_patrons = patrons.count()
    active_patrons = Patron.objects.filter(account_status='Active').count()
    patrons_with_borrows = patrons.filter(active_borrows__gt=0).count()
    patrons_overdue = patrons.filter(overdue_count__gt=0).count()

    context = {
        'patrons': patrons,
        'total_patrons': total_patrons,
        'active_patrons': active_patrons,
        'patrons_with_borrows': patrons_with_borrows,
        'patrons_overdue': patrons_overdue,
        'add_mode': True,
        'error': error,
        'initial': initial,
    }
    return render(request, _patron_page_template(request), context)


@admin_or_module_required('patrons')
def approve_patron(request, patron_id):
    """Approve a pending registration once its uploaded ID has been reviewed.

    An online applicant is never seen in person, so the ID they uploaded is the
    only identity evidence the library has: approval requires both that a
    document is on file and that the reviewer confirms having opened it.

    Someone handed over by the desk screen is standing right there instead, so
    there is no upload to open and the reviewer checks the physical ID the same
    way they would for any walk-in. Either way the account is only activated by
    a person who has looked at an ID and said so."""
    if request.method != 'POST':
        return _patron_page_redirect(request)
    patron = Patron.objects.filter(patron_id=patron_id, account_status='Pending').first()
    if patron is None:
        messages.error(request, 'Pending patron not found.')
        return _patron_page_redirect(request)

    applied_online = patron.registration_channel == 'Online'
    if applied_online and not patron.credential_document:
        messages.error(request, f'{patron.fullname} applied online with no ID attached, so '
                                f'their identity cannot be verified. Reject the application '
                                f'and ask them to register again with a valid ID.')
        return _patron_page_redirect(request)
    if (request.POST.get('id_reviewed') or '').strip() not in ('1', 'true', 'on', 'yes'):
        messages.error(request, 'Check the applicant\'s ID before approving this '
                                'registration.')
        return _patron_page_redirect(request)

    reviewer = User.objects.filter(admin_id=request.session.get('admin_id')).first()
    patron.account_status = 'Active'
    if not patron.qr_code:
        patron.qr_code = str(uuid4())
    patron.identity_verified_by = reviewer
    patron.identity_verified_at = timezone.now()
    patron.save(update_fields=['account_status', 'qr_code',
                               'identity_verified_by', 'identity_verified_at'])
    how = 'uploaded ID reviewed' if applied_online else 'physical ID checked at the desk'
    log_admin_action(request, 'Update', 'Patron', patron.patron_id,
                     f'Approved registration of "{patron.fullname}" — {how}')
    registration_approved_email(patron)
    messages.success(request, f'{patron.fullname} approved. Their QR code is now active.')
    return _patron_page_redirect(request)


@admin_or_module_required('patrons')
def promote_visitor(request, patron_id):
    """Turn a visitor into a membership application.

    The same row is reused rather than a fresh one created, so every visit they
    already made stays attached to them. It becomes a pending on-site
    registration, which puts it through exactly the same ID check as any other
    walk-in instead of quietly granting borrowing rights.
    """
    if request.method != 'POST':
        return _patron_page_redirect(request)
    visitor = Patron.objects.filter(patron_id=patron_id, account_status='Visitor').first()
    if visitor is None:
        messages.error(request, 'Visitor not found.')
        return _patron_page_redirect(request)

    visitor.account_status = 'Pending'
    visitor.registration_channel = 'On-site'
    visitor.save(update_fields=['account_status', 'registration_channel'])
    log_admin_action(request, 'Update', 'Patron', visitor.patron_id,
                     f'"{visitor.fullname}" moved from visitor to a pending membership')
    messages.success(request, f'{visitor.fullname} is now awaiting an ID check in '
                              f'Pending Registrations.')
    return _patron_page_redirect(request)


@admin_or_module_required('patrons')
def reject_patron(request, patron_id):
    """Reject a pending registration: notify the applicant and remove the row."""
    if request.method != 'POST':
        return _patron_page_redirect(request)
    patron = Patron.objects.filter(patron_id=patron_id, account_status='Pending').first()
    if patron is None:
        messages.error(request, 'Pending patron not found.')
        return _patron_page_redirect(request)

    reason = (request.POST.get('reason') or '').strip() or None
    fullname, email = patron.fullname, patron.email
    log_admin_action(request, 'Delete', 'Patron', patron.patron_id,
                     f'Rejected registration of "{fullname}"' + (f' — {reason}' if reason else ''))
    # Remove the uploaded credential file along with the application.
    if patron.credential_document:
        try:
            os.remove(os.path.join(settings.MEDIA_ROOT, patron.credential_document))
        except OSError:
            pass
    patron.delete()
    registration_rejected_email(email, fullname, reason)
    messages.success(request, f'Registration of {fullname} rejected.')
    return _patron_page_redirect(request)


@admin_or_module_required('patrons')
def respond_to_reactivation(request, request_id):
    """Admin approves or rejects a patron's OTP-verified reactivation request.

    The approval step Figure 19 puts between OTP verification and the
    account actually going live again.
    """
    if request.method != 'POST':
        return _patron_page_redirect(request)
    action = request.POST.get('action')
    admin = User.objects.filter(admin_id=request.session.get('admin_id')).first()

    # Locked for the whole decision, like the extension queue: two
    # administrators resolving the same request both used to pass the Pending
    # check, and the patron got two emails about one decision. Emails are sent
    # after the block so nothing is announced that a rollback would undo.
    notify = None
    try:
        with transaction.atomic():
            reactivation = (ReactivationRequest.objects.select_for_update()
                            .select_related('patron')
                            .filter(request_id=request_id, status='Pending').first())
            if reactivation is None:
                raise _AlreadyResolved()
            patron = reactivation.patron

            if action == 'approve':
                patron.account_status = 'Active'
                patron.save(update_fields=['account_status'])
                reactivation.status = 'Approved'
                reactivation.resolved_by = admin
                reactivation.resolved_at = timezone.now()
                reactivation.save(update_fields=['status', 'resolved_by', 'resolved_at'])
                log_admin_action(request, 'Approve', 'ReactivationRequest', reactivation.request_id,
                                 f'Reactivated "{patron.fullname}"')
                if patron.email:
                    notify = ('approved', patron, None)
                messages.success(request, f'{patron.fullname} is now Active.')
            elif action == 'reject':
                note = (request.POST.get('staff_note') or '').strip()[:255]
                reactivation.status = 'Rejected'
                reactivation.resolved_by = admin
                reactivation.resolved_at = timezone.now()
                reactivation.staff_note = note or None
                reactivation.save(update_fields=['status', 'resolved_by', 'resolved_at', 'staff_note'])
                log_admin_action(request, 'Reject', 'ReactivationRequest', reactivation.request_id,
                                 f'Rejected reactivation for "{patron.fullname}"'
                                 + (f' — {note}' if note else ''))
                if patron.email:
                    notify = ('rejected', patron, note)
                messages.success(request, f'Reactivation request for {patron.fullname} rejected.')
            else:
                raise _AlreadyResolved('Unknown action.')
    except _AlreadyResolved as stop:
        messages.error(request, str(stop)
                       or 'Reactivation request not found, or it was already resolved.')
        return _patron_page_redirect(request)

    # Committed. Safe to tell the patron now.
    if notify and notify[0] == 'approved':
        reactivation_approved_email(notify[1])
    elif notify:
        reactivation_declined_email(notify[1], notify[2])
    return _patron_page_redirect(request)


@admin_login_required
# Deliberately admin_login_required rather than the 'patrons' module, for the
# same reason get_books_for_placement is: the Transactions page has to look a
# patron up to process a loan, and an account can hold 'transactions' without
# holding 'patrons'. Gated the other way, the search box received a login
# redirect where it expected JSON, so results silently never appeared -- which
# reads as a broken search rather than a permission being missing. It discloses
# only the name and email of active patrons, which whoever is standing at the
# desk processing their loan is already looking at.
def patron_search_json(request):
    """Name/email lookup for the Transactions page's patron picker."""
    q = (request.GET.get('search') or '').strip()
    if not q:
        return JsonResponse({'patrons': []})
    patrons = Patron.objects.filter(
        _patron_search_q(q)
    ).filter(account_status='Active')[:10]
    return JsonResponse({'patrons': [
        {
            'patron_id': p.patron_id,
            'fullname': p.fullname,
            'email': p.email,
            'patron_type': p.patron_type,
        }
        for p in patrons
    ]})


@admin_module_required('patrons')
def admin_manage_patron(request):
    return _patrons_page(request, 'admin/managepatron.html')


@module_required('patrons')
def staff_manage_patron(request):
    """The Library Staff patron desk.

    Deliberately not the full module: the same list and the same review of
    pending sign-ups, but editing, deleting and bulk import stay with the
    Administrator, who owns the record itself.
    """
    return _patrons_page(request, 'library_staff/managepatron.html')


def _patrons_page(request, template):
    # Visits left open on earlier days are closed before any count is shown,
    # so "currently inside" never accumulates people who simply went home.
    close_stale_visits()
    search_query = request.GET.get('search', '').strip()
    
    # Handle AJAX search requests
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest' and search_query:
        patrons = Patron.objects.filter(
            _patron_search_q(search_query)
        ).filter(account_status='Active')[:10]
        
        patron_list = []
        for patron in patrons:
            patron_list.append({
                'patron_id': patron.patron_id,
                'fullname': patron.fullname,
                'email': patron.email,
                'patron_type': patron.patron_type,
            })
        
        return JsonResponse({'patrons': patron_list})
    
    # Regular page load. Visitors used the library without joining it, so they
    # are kept out of the member directory and counted on their own tab —
    # mixing the two would make every membership figure wrong.
    show_visitors = request.GET.get('view') == 'visitors'
    base_patrons = (Patron.objects.filter(account_status='Visitor') if show_visitors
                    else Patron.objects.exclude(account_status='Visitor'))
    patrons_queryset = base_patrons.annotate(
        active_borrows=Count(
            'transaction',
            filter=Q(transaction__transaction_type='Borrow', transaction__return_date__isnull=True)
        ),
        overdue_count=Count(
            'transaction',
            filter=Q(transaction__overdue_flag=True)
        ),
        borrow_count=Count(
            'transaction',
            filter=Q(transaction__transaction_type='Borrow')
        ),
    ).order_by('-registration_date')

    # Global stats (independent of the search filter below).
    total_patrons = patrons_queryset.count()
    active_patrons = Patron.objects.filter(account_status='Active').count()
    patrons_with_borrows = patrons_queryset.filter(active_borrows__gt=0).count()
    patrons_overdue = patrons_queryset.filter(overdue_count__gt=0).count()
    visitor_count = Patron.objects.filter(account_status='Visitor').count()

    # Registrations awaiting review — online sign-ups and desk hand-offs alike.
    pending_patrons = Patron.objects.filter(account_status='Pending').order_by('-registration_date')

    # Self-service reactivation requests awaiting Admin approval (Figure 19).
    pending_reactivations = ReactivationRequest.objects.select_related('patron').filter(
        status='Pending').order_by('-requested_at')

    # Apply the search filter to the table list.
    if search_query:
        patrons_queryset = patrons_queryset.filter(_patron_search_q(search_query))

    page_number = request.GET.get('page', 1)
    paginator = Paginator(patrons_queryset, 15)  # 15 patrons per page
    patrons = paginator.get_page(page_number)


    from urllib.parse import urlencode
    context = {
        'patrons': patrons,
        'total_patrons': total_patrons,
        'active_patrons': active_patrons,
        'patrons_with_borrows': patrons_with_borrows,
        'patrons_overdue': patrons_overdue,
        'pending_patrons': pending_patrons,
        'pending_reactivations': pending_reactivations,
        'visitor_count': visitor_count,
        'show_visitors': show_visitors,
        'paginator': paginator,
        'search_query': search_query,
        'querystring': urlencode({'search': search_query}) if search_query else '',
    }
    return render(request, template, context)


@admin_module_required('patrons')
def admin_edit_patron(request, patron_id):
    patron = Patron.objects.filter(patron_id=patron_id).first()
    if patron is None:
        return redirect('admin_manage_patron')

    error = None
    success = None
    initial = {
        'fullname': patron.fullname,
        'first_name': patron.first_name,
        'middle_name': patron.middle_name,
        'last_name': patron.last_name,
        'patron_type': patron.patron_type,
        'email': patron.email,
        'contact_number': patron.contact_number,
        'address': patron.address,
        'account_status': patron.account_status,
    }

    if request.method == 'POST':
        first_name, middle_name, last_name, name_error = _name_from_post(request)
        fullname = compose_name(first_name, middle_name, last_name)
        patron_type = request.POST.get('patron_type', '').strip()
        email = request.POST.get('email', '').strip()
        contact_number = request.POST.get('contact_number', '').strip()
        address = request.POST.get('address', '').strip()
        password = request.POST.get('password', '').strip()
        account_status = request.POST.get('account_status', 'Active').strip()

        if name_error:
            error = name_error
        elif not all([patron_type, email]):
            error = 'Patron type and email are required.'
        elif Patron.objects.exclude(patron_id=patron_id).filter(email=email).exists():
            error = 'A different patron already uses that email.'
        else:
            patron.fullname = fullname
            patron.first_name = first_name
            patron.middle_name = middle_name
            patron.last_name = last_name
            patron.patron_type = patron_type
            patron.email = email
            patron.contact_number = contact_number
            patron.address = address
            patron.account_status = account_status or 'Active'
            if password:
                patron.password_hash = hash_password(password)
            patron.save()
            log_admin_action(request, 'Update', 'Patron', patron.patron_id, f'Updated "{patron.fullname}"')
            success = 'Patron updated successfully.'
            initial.update({
                'fullname': fullname,
                'first_name': first_name,
                'middle_name': middle_name,
                'last_name': last_name,
                'patron_type': patron_type,
                'email': email,
                'contact_number': contact_number,
                'address': address,
                'account_status': account_status,
            })

    # Get patron list context
    patrons = Patron.objects.annotate(
        active_borrows=Count(
            'transaction',
            filter=Q(transaction__transaction_type='Borrow', transaction__return_date__isnull=True)
        ),
        overdue_count=Count(
            'transaction',
            filter=Q(transaction__overdue_flag=True)
        ),
        borrow_count=Count(
            'transaction',
            filter=Q(transaction__transaction_type='Borrow')
        ),
    ).order_by('-registration_date')

    total_patrons = patrons.count()
    active_patrons = Patron.objects.filter(account_status='Active').count()
    patrons_with_borrows = patrons.filter(active_borrows__gt=0).count()
    patrons_overdue = patrons.filter(overdue_count__gt=0).count()

    context = {
        'patrons': patrons,
        'total_patrons': total_patrons,
        'active_patrons': active_patrons,
        'patrons_with_borrows': patrons_with_borrows,
        'patrons_overdue': patrons_overdue,
        'edit_mode': True,
        'patron': patron,
        'error': error,
        'success': success,
        'initial': initial,
    }
    return render(request, 'admin/managepatron.html', context)


@admin_module_required('patrons')
def admin_delete_patron(request, patron_id):
    if request.method == 'POST':
        patron = Patron.objects.filter(patron_id=patron_id).first()
        if patron:
            name = patron.fullname
            patron.delete()
            log_admin_action(request, 'Delete', 'Patron', patron_id, f'Deleted "{name}"')
    return redirect('admin_manage_patron')


@granted_module_required('books')
def admin_edit_book(request, book_id):
    book = Book.objects.filter(book_id=book_id).first()
    if book is None:
        return portal_redirect(request, 'admin_management')

    error = None
    success = None
    initial = {
        'title': book.title,
        'author': book.author,
        'ISBN': book.ISBN,
        'publication_year': book.publication_year,
        'genre': book.genre,
        'cover_img_url': book.cover_img_url,
        'status': book.status,
        'shelf_level_id': book.shelf_level.shelf_level_id if book.shelf_level else '',
        'shelf_slot': book.shelf_slot or '',
        'condition': book.condition or 'Good',
        'call_number': book.call_number or '',
    }

    if request.method == 'POST':
        title = request.POST.get('title', '').strip()
        author = request.POST.get('author', '').strip()
        isbn = request.POST.get('ISBN', '').strip()
        publication_year = request.POST.get('publication_year', '').strip()
        genre = request.POST.get('genre', '').strip()
        cover_img_url = request.POST.get('cover_img_url', '').strip()
        status = request.POST.get('status', '').strip()
        shelf_level_id = request.POST.get('shelf_level', '').strip()

        if not title or not author:
            error = 'Title and author are required.'
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                from django.http import JsonResponse
                return JsonResponse({'success': False, 'error': error})
        else:
            book.title = title
            book.author = author
            book.ISBN = isbn
            book.publication_year = int(publication_year) if publication_year else None
            book.genre = genre
            book.cover_img_url = cover_img_url if cover_img_url else None
            book.status = status or book.status
            if shelf_level_id:
                book.shelf_level = ShelfLevel.objects.filter(shelf_level_id=shelf_level_id).first()
            # Present-but-blank clears it; absent leaves it alone, so a form
            # that does not carry the field cannot wipe a recorded position.
            if 'shelf_slot' in request.POST:
                book.shelf_slot = parse_shelf_slot(request.POST.get('shelf_slot'))
            if 'condition' in request.POST:
                value = (request.POST.get('condition') or '').strip().title()
                if value in dict(Book.CONDITION_CHOICES):
                    book.condition = value
            if 'call_number' in request.POST:
                # Cleared deliberately means "renumber it": save() fills a blank.
                book.call_number = (request.POST.get('call_number') or '').strip() or None
            book.save()
            log_admin_action(request, 'Update', 'Book', book.book_id, f'Updated "{book.title}"')
            success = 'Book details updated successfully.'
            initial.update({
                'title': title,
                'author': author,
                'ISBN': isbn,
                'publication_year': publication_year,
                'genre': genre,
                'cover_img_url': cover_img_url,
                'status': status,
                'shelf_level_id': shelf_level_id,
                'shelf_slot': request.POST.get('shelf_slot', ''),
            })
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                from django.http import JsonResponse
                return JsonResponse({'success': True, 'message': success})

    # Get books list context for managebooks.html
    books = Book.objects.select_related('shelf_level', 'shelf_level__shelf').order_by('title')
    total_books = books.count()
    total_copies = total_books
    available_count = books.filter(status='Available').count()
    borrowed_count = books.filter(status='Borrowed').count()
    shelf_levels = ShelfLevel.objects.select_related('shelf').all()

    context = {
        'books': books,
        'total_books': total_books,
        'total_copies': total_copies,
        'available_count': available_count,
        'borrowed_count': borrowed_count,
        'shelf_levels': shelf_levels,
        'book': book,
        'error': error,
        'success': success,
        'initial': initial,
    }
    return render(request, 'admin/managebooks.html', context)


@granted_module_required('books')
def admin_delete_book(request, book_id):
    if request.method == 'POST':
        book = Book.objects.filter(book_id=book_id).first()
        if book:
            title = book.title
            book.delete()
            log_admin_action(request, 'Delete', 'Book', book_id, f'Deleted "{title}"')
    return portal_redirect(request, 'admin_management')


@granted_module_required('transactions')
def transaction_action_preview(request, transaction_id):
    """What Mark Returned / Mark Lost would do, for the confirmation modal.

    Read-only: the figures shown are computed the same way the action computes
    them, so the modal cannot promise one fine and charge another.
    """
    tx = (Transaction.objects.select_related('book', 'patron')
          .filter(transaction_id=transaction_id).first())
    if tx is None:
        return JsonResponse({'success': False, 'error': 'Transaction not found'})

    action = (request.GET.get('action') or 'return').strip()
    rule = BorrowingRule.current()
    today = timezone.localdate()
    overdue_fine = rule.compute_fine(tx.due_date, today)
    is_overdue = bool(tx.due_date and today > tx.due_date)

    data = {
        'transaction_id': tx.transaction_id,
        'book': tx.book.title if tx.book else '—',
        'patron': tx.patron.fullname if tx.patron else '—',
        'due_date': tx.due_date.strftime('%B %d, %Y') if tx.due_date else '—',
        'is_overdue': is_overdue,
        'overdue_fine': f'{overdue_fine:.2f}',
        'already_closed': tx.return_date is not None,
    }
    if action == 'lost':
        data.update({
            'action': 'lost',
            'lost_fee': f'{rule.lost_book_fee:.2f}',
            'total_fine': f'{(overdue_fine + rule.lost_book_fee):.2f}',
        })
    else:
        data.update({'action': 'return', 'total_fine': f'{overdue_fine:.2f}'})
    return JsonResponse({'success': True, 'preview': data})


@granted_module_required('transactions')
def admin_transaction_action(request, transaction_id):
    if request.method == 'POST':
        action = request.POST.get('action')
        tx = Transaction.objects.select_related('book', 'patron').filter(transaction_id=transaction_id).first()
        if tx and action == 'return' and tx.return_date is None:
            tx.return_date = timezone.localdate()
            tx.overdue_flag = bool(tx.due_date and tx.return_date > tx.due_date)
            tx.fine_amount = BorrowingRule.current().compute_fine(tx.due_date, tx.return_date)
            tx.save()
            if tx.book and tx.transaction_type == 'Borrow':
                # Back at the desk, not back on the shelf. Sending the next
                # patron to its shelf now would waste their walk, so it waits in
                # the reshelving queue until someone physically puts it back.
                tx.book.status = 'For Reshelving'
                tx.book.save()
            log_admin_action(request, 'Process', 'Transaction', tx.transaction_id,
                             f'Returned "{tx.book.title if tx.book else ""}"',
                             patron=tx.patron)
            # Email the patron a return receipt.
            if tx.patron and tx.book:
                return_receipt_email(tx.patron, [tx.book], had_overdue=tx.overdue_flag)
        elif tx and action == 'lost' and tx.return_date is None and tx.transaction_type == 'Borrow':
            rule = BorrowingRule.current()
            today = timezone.localdate()
            # Total owed = any accrued overdue fine + the configured lost-book fee.
            tx.overdue_flag = bool(tx.due_date and today > tx.due_date)
            tx.fine_amount = rule.compute_fine(tx.due_date, today) + rule.lost_book_fee
            tx.save()  # transaction stays open so it counts as an outstanding lost-book penalty
            if tx.book:
                tx.book.status = 'Lost'
                tx.book.save()
            # A copy written off at the desk is a stock movement too, so the
            # inventory follows automatically rather than by a second manual step.
            flagged = flag_inventory_copy_lost(
                request, tx.book,
                f'Marked lost on transaction #{tx.transaction_id}'
                + (f' by {tx.patron.fullname}' if tx.patron else ''))
            log_admin_action(request, 'Process', 'Transaction', tx.transaction_id,
                             f'Marked "{tx.book.title if tx.book else ""}" lost (fine {tx.fine_amount})'
                             + (' — inventory copy flagged' if flagged else ''),
                             patron=tx.patron)
            if tx.patron and tx.book:
                lost_book_email(tx.patron, tx.book, tx.fine_amount)

        # The confirmation modal posts by fetch and refreshes the table itself.
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            if tx is None:
                return JsonResponse({'success': False, 'error': 'Transaction not found'})
            return JsonResponse({
                'success': True,
                'action': action,
                'transaction_id': tx.transaction_id,
                'book': tx.book.title if tx.book else '',
                'fine_amount': f'{tx.fine_amount:.2f}' if tx.fine_amount else '0.00',
            })
    return portal_redirect(request, 'admin_transaction')


@granted_module_required('transactions')
def respond_to_extension(request):
    """Staff approves or declines a patron's pending due-date extension request."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    action = request.POST.get('action')
    admin = User.objects.filter(admin_id=request.session.get('admin_id')).first()

    # The row is locked for as long as it takes to read it and resolve it. Two
    # librarians clicking Approve at the same moment both used to pass the
    # Pending check, which resolved one request twice -- two audit entries for
    # one decision, and two emails to the patron about it. The second request
    # now waits for the first to commit, finds the row no longer Pending, and
    # says so.
    #
    # Emails are sent after the block, never inside it: a message posted from
    # within a transaction that later rolls back cannot be recalled.
    notify = None
    try:
        with transaction.atomic():
            # No select_related on the locked query. transaction__book and
            # transaction__patron are nullable, so joining them makes a LEFT
            # OUTER JOIN, and "FOR UPDATE cannot be applied to the nullable
            # side of an outer join" -- Postgres refuses it outright. Lock the
            # one row that needs locking; the related rows are read afterwards
            # and are not what two clicks are racing over.
            extension = (DueDateExtension.objects.select_for_update()
                         .filter(extension_id=request.POST.get('extension_id'),
                                 status='Pending').first())
            if extension is None:
                raise _AlreadyResolved()
            tx = (Transaction.objects
                  .select_related('book', 'patron')
                  .filter(transaction_id=extension.transaction_id).first())
            if tx is None:
                raise _AlreadyResolved('That loan no longer exists.')

            if action == 'approve':
                # tx.return_date could have been set between the request and
                # this click -- the book already came back, so there is nothing
                # left to extend.
                if tx.return_date is not None:
                    raise _AlreadyResolved('This book has already been returned.')
                tx.due_date = extension.requested_due_date
                tx.overdue_flag = bool(tx.due_date and timezone.localdate() > tx.due_date)
                tx.save(update_fields=['due_date', 'overdue_flag'])
                extension.status = 'Approved'
                extension.resolved_by = admin
                extension.resolved_at = timezone.now()
                extension.save(update_fields=['status', 'resolved_by', 'resolved_at'])
                log_admin_action(request, 'Approve', 'DueDateExtension', extension.extension_id,
                                 f'Extended "{tx.book.title}" to {tx.due_date}'
                                 + (f' for {tx.patron.fullname}' if tx.patron else ''))
                if tx.patron and tx.patron.email and tx.book:
                    notify = ('approved', tx.patron, tx.book, tx.due_date)
            elif action == 'decline':
                note = (request.POST.get('staff_note') or '').strip()[:255]
                extension.status = 'Declined'
                extension.resolved_by = admin
                extension.resolved_at = timezone.now()
                extension.staff_note = note or None
                extension.save(update_fields=['status', 'resolved_by', 'resolved_at', 'staff_note'])
                log_admin_action(request, 'Decline', 'DueDateExtension', extension.extension_id,
                                 f'Declined extension for "{tx.book.title}"'
                                 + (f' — {note}' if note else ''))
                if tx.patron and tx.patron.email and tx.book:
                    notify = ('declined', tx.patron, tx.book, note)
            else:
                raise _AlreadyResolved('Unknown action.')
    except _AlreadyResolved as stop:
        reason = str(stop) or 'Request not found, or it was already resolved.'
        return JsonResponse({'success': False, 'error': reason})

    # Committed. Only now is it safe to tell the patron.
    if notify and notify[0] == 'approved':
        extension_approved_email(notify[1], notify[2], notify[3])
    elif notify:
        extension_declined_email(notify[1], notify[2], notify[3])

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'success': True, 'action': action, 'extension_id': extension.extension_id})
    return portal_redirect(request, 'admin_transaction')


@granted_module_required('transactions')
def adjust_due_date(request):
    """Staff sets a due date directly -- the desk case: a patron asks in person
    rather than through their account, so there is no request to review, only
    a change to make and log."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    tx = Transaction.objects.select_related('book', 'patron').filter(
        transaction_id=request.POST.get('transaction_id'),
        transaction_type='Borrow', return_date__isnull=True,
    ).first()
    if tx is None:
        return JsonResponse({'success': False, 'error': 'Active loan not found.'})

    raw = (request.POST.get('new_due_date') or '').strip()
    try:
        new_due = datetime.strptime(raw, '%Y-%m-%d').date()
    except ValueError:
        return JsonResponse({'success': False, 'error': 'Enter a valid date.'})

    admin = User.objects.filter(admin_id=request.session.get('admin_id')).first()
    old_due = tx.due_date

    # Logged as an already-approved, staff-initiated extension so it shows up
    # in the same history as patron requests -- one record of every due-date
    # change on this loan, however it happened.
    DueDateExtension.objects.create(
        transaction=tx, requested_by_patron=False,
        previous_due_date=old_due or new_due, requested_due_date=new_due,
        status='Approved', resolved_by=admin, resolved_at=timezone.now(),
    )
    tx.due_date = new_due
    tx.overdue_flag = bool(new_due and timezone.localdate() > new_due)
    tx.save(update_fields=['due_date', 'overdue_flag'])

    log_admin_action(request, 'Update', 'Transaction', tx.transaction_id,
                     f'Due date for "{tx.book.title}" changed from {old_due or "—"} to {new_due}'
                     + (f' for {tx.patron.fullname}' if tx.patron else ''))
    return JsonResponse({
        'success': True,
        'transaction_id': tx.transaction_id,
        'due_date': new_due.strftime('%b %d, %Y'),
        'overdue_flag': tx.overdue_flag,
    })


def _book_detail_page(request, template):
    books = Book.objects.select_related('shelf_level', 'shelf_level__shelf').order_by('title')
    total_books = books.count()
    available_count = books.filter(status='Available').count()
    borrowed_count = books.filter(status='Borrowed').count()
    overdue_count = books.filter(status='Overdue').count()

    context = {
        'books': books,
        'total_books': total_books,
        'available_count': available_count,
        'borrowed_count': borrowed_count,
        'overdue_count': overdue_count,
    }
    return render(request, template, context)


@admin_module_required('books')
def admin_book_detail(request):
    return _book_detail_page(request, 'admin/bookdetail.html')


@module_required('books')
def staff_book_detail(request):
    return _book_detail_page(request, 'library_staff/bookdetail.html')


def _transaction_page(request, template):
    from datetime import datetime
    from urllib.parse import urlencode

    q = (request.GET.get('q') or '').strip()
    status = (request.GET.get('status') or '').strip().lower()
    date_from = (request.GET.get('date_from') or '').strip()
    date_to = (request.GET.get('date_to') or '').strip()

    transactions_queryset = Transaction.objects.select_related('patron', 'book')
    if q:
        transactions_queryset = transactions_queryset.filter(
            Q(patron__fullname__icontains=q) | Q(book__title__icontains=q) | Q(book__ISBN__icontains=q)
        )
    if status == 'borrowed':
        transactions_queryset = transactions_queryset.filter(transaction_type='Borrow', return_date__isnull=True)
    elif status == 'returned':
        transactions_queryset = transactions_queryset.filter(return_date__isnull=False)
    elif status == 'overdue':
        transactions_queryset = transactions_queryset.filter(overdue_flag=True)

    def _parse_date(s):
        try:
            return datetime.strptime(s, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            return None
    df, dt = _parse_date(date_from), _parse_date(date_to)
    if df:
        transactions_queryset = transactions_queryset.filter(transaction_date__gte=df)
    if dt:
        transactions_queryset = transactions_queryset.filter(transaction_date__lte=dt)

    transactions_queryset = transactions_queryset.order_by('-transaction_date')

    total_borrowed = Transaction.objects.filter(transaction_type='Borrow').count()
    # A return is recorded by stamping return_date on the original Borrow row —
    # nothing in the system ever writes a row of type 'Return'. Counting that
    # type reported zero returns no matter how many books came back.
    total_returned = Transaction.objects.filter(return_date__isnull=False).count()
    currently_out = Transaction.objects.filter(transaction_type='Borrow', return_date__isnull=True).count()
    overdue_count = Transaction.objects.filter(overdue_flag=True).count()
    transaction_count = transactions_queryset.count()

    paginator = Paginator(transactions_queryset, 20)
    transactions = paginator.get_page(request.GET.get('page', 1))

    # Shown above the table regardless of the current filter/search/page --
    # a request waiting on staff is not something a search term should be
    # able to hide.
    pending_extensions = (DueDateExtension.objects
                          .filter(status='Pending')
                          .select_related('transaction', 'transaction__book', 'transaction__patron')
                          .order_by('requested_at'))

    params = {}
    for key, value in (('q', q), ('status', status), ('date_from', date_from), ('date_to', date_to)):
        if value:
            params[key] = value

    context = {
        'transactions': transactions,
        'total_borrowed': total_borrowed,
        'total_returned': total_returned,
        'currently_out': currently_out,
        'overdue_count': overdue_count,
        'transaction_count': transaction_count,
        'pending_extensions': pending_extensions,
        'paginator': paginator,
        'q': q,
        'status': status,
        'date_from': date_from,
        'date_to': date_to,
        'querystring': urlencode(params),
    }
    return render(request, template, context)


@admin_module_required('transactions')
def admin_transaction(request):
    return _transaction_page(request, 'admin/transaction.html')


@module_required('transactions')
def staff_transaction(request):
    return _transaction_page(request, 'library_staff/transaction.html')


def _indoor_map_page(request, template, is_admin_view=False):
    # Every floor, lowest first. The switcher on the map is a lift panel, and a
    # lift panel whose buttons are not in storey order is unreadable -- the old
    # "-uploaded_at" order listed them by whenever someone happened to draw them.
    floorplans = FloorPlan.objects.all().order_by('floor_number', 'floor_plan_id')

    # Which shelf, if any, the visitor came here looking for. Read before the
    # floor is chosen, because it is one of the things that chooses it.
    shelf_param = (request.GET.get('shelf') or '').strip()

    # Determine which floor plan to display
    floorplan = None
    no_floorplans = False

    if floorplans.exists():
        # An explicit choice wins: a floor button was pressed.
        requested_floorplan_id = request.GET.get('floorplan')
        if requested_floorplan_id:
            try:
                floorplan = FloorPlan.objects.filter(floor_plan_id=int(requested_floorplan_id)).first()
            except (ValueError, TypeError):
                pass

        # Otherwise, arriving from "Locate on Map" for a shelf upstairs: open the
        # floor that shelf is on. Opening the ground floor with nothing
        # highlighted is the one answer that is never what was asked for.
        if not floorplan and shelf_param.isdigit():
            floorplan = floorplans.filter(room__shelf__shelf_id=int(shelf_param)).first()

        # Otherwise the lowest floor in service, where someone walking in starts.
        if not floorplan:
            floorplan = floorplans.filter(is_active=True).first()

        # Fallback to any plan at all, so a library holding only drafts still draws.
        if not floorplan:
            floorplan = floorplans.first()
    else:
        no_floorplans = True
    
    # Fetch related entities with coordinates if floor plan exists
    rooms_data = []
    obstacles_data = []
    stairways_data = []
    shelves_data = []
    waypoints_data = []
    beacons_data = []

    if floorplan and not no_floorplans:
        # Serialize rooms with coordinates
        rooms = Room.objects.filter(floor_plan=floorplan, is_active=True)
        for room in rooms:
            if room.map_x is not None and room.map_y is not None:
                rooms_data.append({
                    'id': room.room_id,
                    'name': room.name,
                    'geometry': room.geometry or None,
                    'doors': [
                        _door_payload(d)
                        for d in Door.objects.filter(room=room, is_active=True)
                    ],
                    'x': room.map_x,
                    'y': room.map_y,
                    'description': room.description or ''
                })
        
        # Furniture and obstacles. Only the active ones: an inactive shape is a
        # partition that has come down, and drawing it would send a patron
        # around something that is no longer there.
        for o in Obstacle.objects.filter(floor_plan=floorplan, is_active=True):
            if o.geometry:
                obstacles_data.append({
                    'id': o.obstacle_id,
                    'kind': o.kind,
                    'label': o.label,
                    'geometry': o.geometry,
                    'x': o.map_x,
                    'y': o.map_y,
                })

        for st in Stairway.objects.select_related('connects_to').filter(
                floor_plan=floorplan, is_active=True):
            if st.geometry:
                stairways_data.append({
                    'id': st.stairway_id, 'kind': st.kind, 'label': st.label,
                    'geometry': st.geometry, 'x': st.map_x, 'y': st.map_y,
                    'bearing': st.bearing or 0, 'direction': st.direction,
                    'destination': st.destination_label,
                    'treads': ([] if st.kind == 'Elevator'
                               else _stair_treads(st.geometry, st.bearing)),
                })

        # Serialize shelves with coordinates (through room relationship)
        shelves = Shelf.objects.filter(room__floor_plan=floorplan, is_active=True).select_related('room')
        for shelf in shelves:
            if shelf.map_x is not None and shelf.map_y is not None:
                shelves_data.append({
                    'id': shelf.shelf_id,
                    'name': shelf.name,
                    'x': shelf.map_x,
                    'y': shelf.map_y,
                    'rotation': shelf.rotation or 0,
                    'width': shelf.width or 46,
                    'depth': shelf.depth or 14,
                    'kind': shelf.kind,
                    'geometry': shelf.geometry or None,
                    'footprint': shelf.footprint(),
                    'description': shelf.description or '',
                    'room_id': shelf.room.room_id if shelf.room else None
                })
        
        # Serialize waypoints with coordinates
        waypoints = Waypoint.objects.filter(floor_plan=floorplan)
        for waypoint in waypoints:
            if waypoint.map_x is not None and waypoint.map_y is not None:
                waypoints_data.append({
                    'id': waypoint.waypoint_id,
                    'label': waypoint.label or '',
                    'x': waypoint.map_x,
                    'y': waypoint.map_y,
                    'linked_shelf_id': waypoint.linked_shelf.shelf_id if waypoint.linked_shelf else None
                })

        # Beacon positions are infrastructure, not patron-facing: they are only
        # serialised for the Administrator's map (see `show_beacons` below) and
        # never reach patron/patronmap.html.
        if is_admin_view:
            for beacon in BLEBeacon.objects.filter(floor_plan=floorplan):
                if beacon.map_x is not None and beacon.map_y is not None:
                    beacons_data.append({
                        'id': beacon.beacon_id,
                        'label': beacon.label or '',
                        'uuid': beacon.beacon_uuid,
                        'x': beacon.map_x,
                        'y': beacon.map_y,
                    })

    # Serialize floor plan for JavaScript
    floorplan_data = None
    if floorplan:
        canvas_width, canvas_height = _floorplan_canvas_size(floorplan)
        floorplan_data = {
            'id': floorplan.floor_plan_id,
            'name': floorplan.name,
            'canvas_width': canvas_width,
            'canvas_height': canvas_height,
            'is_active': floorplan.is_active,
            'uploaded_at': floorplan.uploaded_at.isoformat() if floorplan.uploaded_at else None,
            'renovation_notice': floorplan.renovation_notice,
            'renovation_message': floorplan.renovation_message
        }
    
    # Serialize all floor plans for the floor switcher
    floorplans_list = []
    for fp in floorplans:
        floorplans_list.append({
            'id': fp.floor_plan_id,
            'name': fp.name,
            'floor_number': fp.floor_number,
            'label': fp.floor_label,
            'short': _floor_short_label(fp),
            'is_active': fp.is_active,
            'uploaded_at': fp.uploaded_at.isoformat() if fp.uploaded_at else None
        })
    

    # A specific shelf to land on, e.g. arriving from "Locate on Map" on a
    # book's detail modal. `book` is a display label only (not looked up).
    target_shelf = None
    target_elsewhere = None
    if shelf_param.isdigit():
        shelf = (Shelf.objects.filter(shelf_id=int(shelf_param))
                 .select_related('room__floor_plan').first())
        on_plan = shelf.room.floor_plan if (shelf and shelf.room_id) else None
        if on_plan and floorplan and on_plan.floor_plan_id == floorplan.floor_plan_id:
            target_shelf = {
                'id': shelf.shelf_id,
                'name': shelf.name,
                'room_name': shelf.room.name if shelf.room else None,
                'book': request.GET.get('book') or '',
            }
        elif on_plan:
            # The shelf exists, just not on the floor being drawn. Saying which
            # floor it is on, with a way to get there, beats an empty panel that
            # reads as a bug.
            target_elsewhere = {
                'id': shelf.shelf_id,
                'name': shelf.name,
                'floor_plan_id': on_plan.floor_plan_id,
                'label': on_plan.floor_label,
            }

    context = {
        'floorplan': json.dumps(floorplan_data) if floorplan_data else 'null',
        'floorplans': json.dumps(floorplans_list),
        'rooms': json.dumps(rooms_data),
        'obstacles': json.dumps(obstacles_data),
        'stairways': json.dumps(stairways_data),
        'shelves': json.dumps(shelves_data),
        'waypoints': json.dumps(waypoints_data),
        'beacons': json.dumps(beacons_data),
        'show_beacons': is_admin_view,
        'no_floorplans': no_floorplans,
        'target_shelf': json.dumps(target_shelf) if target_shelf else 'null',
        'target_elsewhere': json.dumps(target_elsewhere) if target_elsewhere else 'null',
        'current_floor_id': floorplan.floor_plan_id if floorplan else None,
    }

    return render(request, template, context)


@admin_only_required
def admin_indoor_map(request):
    return _indoor_map_page(request, 'admin/indoormap.html', is_admin_view=True)


@module_required('indoor_map')
def staff_indoor_map(request):
    return _indoor_map_page(request, 'library_staff/indoormap.html')


def _mask_email(email):
    """j•••@gmail.com — enough for its owner to recognise, no use to anyone else."""
    if not email or '@' not in email:
        return email
    local, _, domain = email.partition('@')
    return (local[0] if local else '') + '•' * 3 + '@' + domain


def _logs_page(request, template):
    from datetime import datetime
    from urllib.parse import urlencode

    close_stale_visits()
    today = timezone.localdate()
    q = (request.GET.get('q') or '').strip()
    status = (request.GET.get('status') or '').strip().lower()   # '', 'inside', 'completed'
    date_str = (request.GET.get('date') or '').strip()
    try:
        sel_date = datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else today
    except ValueError:
        sel_date = today

    # Needed before the queryset below, not only for the template further down.
    desk_mode = desk_is_armed(request)

    logs_qs = PatronLog.objects.select_related('patron').filter(entry_time__date=sel_date)
    if desk_mode:
        # Desk mode puts this table in front of whoever is standing at the PC.
        # Showing them every other patron's name, school and visit times is the
        # data-privacy problem the professor raised, so the table is narrowed to
        # the person who just identified themselves -- and shows nothing at all
        # until someone does. The counts above stay aggregate, which names
        # nobody.
        viewer_id = desk_viewer_id(request)
        logs_qs = logs_qs.filter(patron_id=viewer_id) if viewer_id else logs_qs.none()
    if q:
        if q.isdigit():
            logs_qs = logs_qs.filter(Q(patron__patron_id=int(q)) | Q(patron__fullname__icontains=q))
        else:
            logs_qs = logs_qs.filter(patron__fullname__icontains=q)
    if status == 'inside':
        logs_qs = logs_qs.filter(exit_time__isnull=True)
    elif status == 'completed':
        logs_qs = logs_qs.filter(exit_time__isnull=False)
    logs_qs = logs_qs.order_by('-entry_time')

    paginator = Paginator(logs_qs, 20)
    logs = paginator.get_page(request.GET.get('page', 1))
    log_count = paginator.count

    # In desk mode the person reading this table is whoever just walked in, so
    # the contact details of everyone who visited today are masked. The log is
    # theirs to add to, not to mine.
    for entry in logs:
        entry.display_email = (_mask_email(entry.patron.email) if desk_mode
                               else entry.patron.email)

    # Stat cards reflect today's overall activity, independent of the table filters.
    todays_entries = PatronLog.objects.filter(entry_time__date=today).count()
    todays_exits = PatronLog.objects.filter(exit_time__date=today).count()
    currently_inside = PatronLog.objects.filter(exit_time__isnull=True).count()
    # "How many members used the library" and "how many people came in" are
    # different questions, and the answer to one should not stand in for the other.
    todays_member_visits = PatronLog.objects.filter(
        entry_time__date=today).exclude(patron__account_status='Visitor').count()
    todays_visitor_visits = PatronLog.objects.filter(
        entry_time__date=today, patron__account_status='Visitor').count()

    params = {}
    if q:
        params['q'] = q
    if status:
        params['status'] = status
    if date_str:
        params['date'] = date_str

    context = {
        'logs': logs,
        'paginator': paginator,
        'log_count': log_count,
        'desk_mode': desk_mode,
        'patron_type_choices': Patron.PATRON_TYPE_CHOICES,
        'purpose_choices': PURPOSE_CHOICES,
        'todays_member_visits': todays_member_visits,
        'todays_visitor_visits': todays_visitor_visits,
        'todays_entries': todays_entries,
        'todays_exits': todays_exits,
        'currently_inside': currently_inside,
        'today': today,
        'sel_date': sel_date,
        'q': q,
        'status': status,
        'querystring': urlencode(params),
        'patron_types': [choice[0] for choice in Patron.PATRON_TYPE_CHOICES],
    }
    return render(request, template, context)


@admin_or_module_required('books')
def reshelving_queue(request):
    """Books returned to the desk but not yet put back on their shelf.

    GET lists them; POST with a book_id marks one shelved and returns it to
    Available. Deliberately a separate step from processing the return: the
    person at the desk taking books back is rarely the person walking them to
    the aisles, and the catalogue should not claim a book is at its shelf until
    someone has actually taken it there.
    """
    if request.method == 'POST':
        # One id or many. A trolley coming back from the aisles is thirty books,
        # and thirty round trips is thirty chances to lose count of which ones
        # were actually put away.
        raw_ids = request.POST.getlist('book_id') or request.POST.getlist('book_ids')
        ids = []
        for chunk in raw_ids:
            for part in str(chunk).split(','):
                part = part.strip()
                if part.isdigit() and int(part) not in ids:
                    ids.append(int(part))
        if not ids:
            return JsonResponse({'success': False, 'error': 'Nothing selected to shelve.'})

        waiting = {b.book_id: b for b in
                   Book.objects.filter(book_id__in=ids, status='For Reshelving')}
        shelved, skipped = [], []
        for book_id in ids:
            book = waiting.get(book_id)
            if book is None:
                skipped.append(book_id)
                continue
            shelved.append(book)

        if shelved:
            with transaction.atomic():
                Book.objects.filter(book_id__in=[b.book_id for b in shelved]).update(
                    status='Available')
            if len(shelved) == 1:
                log_admin_action(request, 'Update', 'Book', shelved[0].book_id,
                                 f'Shelved "{shelved[0].title[:60]}" - back on the '
                                 'shelf and borrowable')
            else:
                # One entry for the trolley rather than thirty near-identical
                # ones, which is what makes the log readable afterwards.
                log_admin_action(request, 'Update', 'Book', None,
                                 f'Shelved {len(shelved)} book(s) from the reshelving '
                                 f'queue: ' + ', '.join(b.title[:40] for b in shelved[:5])
                                 + (f' and {len(shelved) - 5} more' if len(shelved) > 5 else ''))

        remaining = Book.objects.filter(status='For Reshelving').count()
        if not shelved:
            return JsonResponse({
                'success': False, 'remaining': remaining,
                'error': 'Those were already shelved by someone else.'})
        return JsonResponse({
            'success': True,
            'remaining': remaining,
            'shelved_count': len(shelved),
            'shelved_ids': [b.book_id for b in shelved],
            'skipped_count': len(skipped),
            'title': shelved[0].title if len(shelved) == 1 else None,
        })

    waiting = (Book.objects
               .filter(status='For Reshelving')
               .select_related('shelf_level', 'shelf_level__shelf')
               .order_by('shelf_level__shelf__name', 'title'))
    return render(request, 'admin/reshelving.html', {
        'active': 'reshelving',
        'books': waiting,
        'total_waiting': waiting.count(),
    })


@admin_or_module_required('indoor_map')
def floorplan_print(request):
    """A printable wayfinding map of one floor.

    Rooms and shelves with their names and sections, drawn as plain SVG rather
    than Leaflet: a tiled, scripted map does not survive a print dialog, and
    this has to come out of a printer and go on a wall. Waypoints and beacons
    are left off deliberately -- this is for a patron looking for the Fiction
    aisle, not for whoever installs the hardware. Stairs are on, and say where
    they lead: a printed map is the one a patron reads when the phone is in
    their pocket, and "2nd floor" is the whole answer for half the shelves.
    """
    floor_plan, live_plans = _floor_for_request(request)
    if floor_plan is None:
        messages.error(request, 'No floor plan is in service.')
        return portal_redirect(request, 'admin_indoor_map')

    width, height = _floorplan_canvas_size(floor_plan)
    rooms = [
        {
            'name': r.name,
            'points': ' '.join(f'{x},{y}' for x, y in (r.geometry or [])),
            'label_x': r.map_x,
            'label_y': r.map_y,
            'has_shape': bool(r.geometry and len(r.geometry) >= 3),
        }
        for r in Room.objects.filter(floor_plan=floor_plan, is_active=True)
    ]

    shelves = []
    for sh in (Shelf.objects
               .filter(room__floor_plan=floor_plan, is_active=True,
                       map_x__isnull=False, map_y__isnull=False)
               .prefetch_related('shelflevel_set')):
        w = sh.width or 46
        d = sh.depth or 14
        cats = [lv.category for lv in sh.shelflevel_set.all() if lv.is_active and lv.category]
        shelves.append({
            'name': sh.name,
            'kind': sh.kind,
            # A traced shelf prints as its outline; a rectangular one keeps the
            # transform, which is what lets its label ride along with it.
            'points': (' '.join('%s,%s' % (x, y) for x, y in sh.footprint())
                       if sh.geometry else ''),
            # Rotated rectangles are drawn with a transform rather than four
            # computed corners, so the label can ride along with the shape.
            'x': sh.map_x - w / 2,
            'y': sh.map_y - d / 2,
            'w': w,
            'h': d,
            'cx': sh.map_x,
            'cy': sh.map_y,
            'rotation': sh.rotation or 0,
            'sections': ', '.join(sorted(set(cats))),
        })

    obstacles = [
        {
            'label': o.label,
            'kind': o.kind,
            'points': ' '.join(f'{x},{y}' for x, y in (o.geometry or [])),
            'label_x': o.map_x,
            'label_y': o.map_y,
            # A pillar is a structural fact worth showing on a wall map; a
            # named table earns its label, an unnamed one would just be noise.
            'show_label': bool((o.name or '').strip()),
        }
        for o in Obstacle.objects.filter(floor_plan=floor_plan, is_active=True)
        if o.geometry and len(o.geometry) >= 3
    ]

    # A wall map without the stairs on it is a map of one floor pretending to
    # be the whole building, and now that a route can send somebody up them,
    # the printed copy has to agree with the one on the phone.
    stairways = [
        {
            'label': st.label,
            'kind': st.kind,
            'points': ' '.join(f'{x},{y}' for x, y in (st.geometry or [])),
            'treads': ([] if st.kind == 'Elevator'
                       else _stair_treads(st.geometry, st.bearing)),
            'label_x': st.map_x,
            'label_y': st.map_y,
            # The offset is computed here, not with |add: -- that filter casts
            # through int and drops the fraction on the way.
            'dest_y': st.map_y + 9,
            'destination': st.destination_label if st.connects_to_id else '',
        }
        for st in (Stairway.objects
                   .select_related('connects_to')
                   .filter(floor_plan=floor_plan, is_active=True))
        if st.geometry and len(st.geometry) >= 3
    ]

    return render(request, 'admin/floorplanprint.html', {
        'plan': floor_plan,
        'obstacles': obstacles,
        'stairways': stairways,
        'floors': _floor_payload(live_plans, floor_plan),
        'canvas_width': width,
        'canvas_height': height,
        'rooms': rooms,
        'shelves': shelves,
        'printed_on': timezone.localdate(),
    })


@admin_only_required
def activity_logs(request):
    """The Activity Logs viewer (ERD, Figure 87).

    Every write into SystemLog already existed -- 58 call sites across the
    admin, staff and now patron portals -- with nothing anywhere that read it
    back. This is that missing half.

    Deliberately read-only: an audit trail with an edit button answers nothing,
    because any entry could then have been changed by the person it accuses.
    Rows are never deleted from here either; retention is a database decision,
    not a button.
    """
    from urllib.parse import urlencode
    from datetime import datetime

    logs = SystemLog.objects.select_related('admin', 'patron')

    q = (request.GET.get('q') or '').strip()
    role = (request.GET.get('role') or '').strip()
    action = (request.GET.get('action') or '').strip()
    entity = (request.GET.get('entity') or '').strip()
    date_from = (request.GET.get('from') or '').strip()
    date_to = (request.GET.get('to') or '').strip()

    if q:
        # Searches the actor, the affected patron and the detail line together,
        # because "what happened to this patron" and "what did this person do"
        # are the same question asked from two ends.
        logs = logs.filter(
            Q(admin_name__icontains=q)
            | Q(detail__icontains=q)
            | Q(entity_type__icontains=q)
            | Q(entity_id__iexact=q)
            | Q(patron__fullname__icontains=q)
        )
    if role in dict(SystemLog.ACTOR_ROLE_CHOICES):
        logs = logs.filter(actor_role=role)
    if action:
        logs = logs.filter(action__iexact=action)
    if entity:
        logs = logs.filter(entity_type__iexact=entity)

    def _parse(value):
        try:
            return datetime.strptime(value, '%Y-%m-%d').date()
        except ValueError:
            return None

    d_from, d_to = _parse(date_from), _parse(date_to)
    if d_from:
        logs = logs.filter(timestamp__date__gte=d_from)
    if d_to:
        logs = logs.filter(timestamp__date__lte=d_to)

    logs = logs.order_by('-timestamp')

    # Filter menus are built from what is actually in the table rather than a
    # hardcoded list, so a new action verb appears without anyone remembering
    # to add it here.
    all_actions = list(
        SystemLog.objects.order_by('action').values_list('action', flat=True).distinct()
    )
    all_entities = list(
        SystemLog.objects.order_by('entity_type').values_list('entity_type', flat=True).distinct()
    )

    total = SystemLog.objects.count()
    # Built as a list of triples rather than a dict: a Django template cannot
    # look a dict up by a loop variable, so a dict here would need a custom
    # filter to display at all.
    counts = dict(
        SystemLog.objects.values_list('actor_role')
        .annotate(n=Count('actor_role')).values_list('actor_role', 'n')
    )
    role_stats = [
        (key, label, counts.get(key, 0))
        for key, label in SystemLog.ACTOR_ROLE_CHOICES
    ]

    paginator = Paginator(logs, 25)
    page_obj = paginator.get_page(request.GET.get('page'))

    querystring = urlencode({k: v for k, v in {
        'q': q, 'role': role, 'action': action,
        'entity': entity, 'from': date_from, 'to': date_to,
    }.items() if v})

    return render(request, 'admin/activitylogs.html', {
        'active': 'activity',
        'page_obj': page_obj,
        'total_logs': total,
        'filtered_count': paginator.count,
        'role_stats': role_stats,
        'role_choices': SystemLog.ACTOR_ROLE_CHOICES,
        'all_actions': all_actions,
        'all_entities': all_entities,
        'q': q, 'role': role, 'action': action,
        'entity': entity, 'date_from': date_from, 'date_to': date_to,
        'querystring': querystring,
        'is_filtered': bool(q or role or action or entity or date_from or date_to),
    })


@admin_module_required('logs')
def admin_log_management(request):
    return _logs_page(request, 'admin/logmanagement.html')


@module_required('logs')
def staff_logs(request):
    return _logs_page(request, 'library_staff/logmanagement.html')


# How many past loans the details panel carries. Enough to see the pattern
# of a book's use without turning one modal into the transactions page.
BOOK_HISTORY_LIMIT = 25


# Only a copy marked Available is actually standing on the shelf. Borrowed
# and Overdue are with a patron, Being Read is on a table somewhere in the
# building, and For Reshelving is behind the desk waiting to be put back --
# none of them are a spine the patron can count.
ON_SHELF_STATUS = 'Available'
# How wide the strip drawn in the details panel may get before it stops being
# readable. A level holding more than this is shown around the book instead.
LAYOUT_WINDOW = 24


def level_layout(level, focus_book=None):
    """Every copy on a level, in shelf order, with what is really there.

    A stored slot says where a book was last shelved. It cannot say what a
    patron will see, because two things move underneath it: a borrowed book
    leaves a gap, and a tidy-up pushes the rest together. So the number shown
    is never the stored one -- it is recomputed here from the copies actually
    on the shelf, every time anybody asks.

    `shelf_slot` is therefore an ordering key rather than a physical space. It
    only has to sort; it need not be dense, and renumbering after a reorder
    costs nothing.
    """
    if level is None:
        return None

    books = list(Book.objects.filter(shelf_level=level)
                 .order_by(F('shelf_slot').asc(nulls_last=True), 'title', 'book_id'))
    present = [b for b in books if b.status == ON_SHELF_STATUS]

    rows = []
    for space, book in enumerate(books, start=1):
        here = book.status == ON_SHELF_STATUS
        rows.append({
            'book_id': book.book_id,
            'title': book.title,
            'space': space,
            'position': (present.index(book) + 1) if here else None,
            'on_shelf': here,
            'status': book.status,
            'is_focus': bool(focus_book and book.book_id == focus_book.book_id),
        })

    layout = {
        'rows': rows,
        'total': len(books),
        'on_shelf': len(present),
        'level_label': level.label,
        'shelf_name': level.shelf.name if level.shelf else '',
    }

    if focus_book is not None:
        layout['focus'] = _focus_position(focus_book, books, present)
    return layout


def _focus_position(book, books, present):
    """Where one copy sits, and what stands either side of it."""
    if book.status != ON_SHELF_STATUS:
        return {'on_shelf': False, 'status': book.status,
                'position': None, 'space': None, 'before': '', 'after': ''}
    index = present.index(book)
    return {
        'on_shelf': True,
        'status': book.status,
        # What a patron counting spines will actually arrive at.
        'position': index + 1,
        'of': len(present),
        # And where it sits in the recorded order, gaps included, so the two
        # numbers disagreeing is visible rather than mysterious.
        'space': books.index(book) + 1,
        'spaces': len(books),
        # Neighbours are how people really find a book: recognising the titles
        # either side survives a miscount, which a number never does.
        'before': present[index - 1].title if index > 0 else '',
        'after': present[index + 1].title if index + 1 < len(present) else '',
    }


def book_location_words(book):
    """Where this copy is, in the order somebody walks to it.

    Shelf, then which board, then how far along. Each part is dropped when it
    is not recorded, so a book with only a shelf still reads sensibly instead
    of claiming a precision nobody entered.
    """
    level = book.shelf_level
    if level is None:
        return ''
    parts = []
    if level.shelf:
        parts.append(level.shelf.name)
    parts.append(level.label)          # 'Top', 'Underneath', 'Level 3, Column 2'

    focus = (level_layout(level, book) or {}).get('focus')
    if focus and focus.get('on_shelf'):
        # Counted among the copies actually there, not from the stored slot:
        # with two of its neighbours out on loan, "9th" sends a patron two
        # books past the one they came for.
        parts.append('%s book along' % _ordinal(focus['position']))
    elif focus:
        parts.append('not on the shelf (%s)' % focus['status'].lower())
    return ' \u00b7 '.join(p for p in parts if p)


# Real sheets do not write "Worn". They write "GOOD CONDITION", "bad",
# "fair", "needs repair" -- and a value nobody recognises used to become Good,
# so a shelf of books marked BAD CONDITION imported as though it were fine.
CONDITION_WORDS = {
    'good': 'Good', 'goodcondition': 'Good', 'new': 'Good', 'fine': 'Good',
    'excellent': 'Good', 'ok': 'Good', 'okay': 'Good', 'usable': 'Good',
    'verygood': 'Good', 'g': 'Good',

    'worn': 'Worn', 'worncondition': 'Worn', 'fair': 'Worn', 'used': 'Worn',
    'aged': 'Worn', 'faircondition': 'Worn', 'moderate': 'Worn', 'w': 'Worn',

    'bad': 'Damaged', 'badcondition': 'Damaged', 'damaged': 'Damaged',
    'damagedcondition': 'Damaged', 'poor': 'Damaged', 'poorcondition': 'Damaged',
    'torn': 'Damaged', 'brokenspine': 'Damaged', 'needsrepair': 'Damaged',
    'forrepair': 'Damaged', 'unusable': 'Damaged', 'd': 'Damaged',
}


def parse_condition(raw):
    """The condition a sheet means, or None when it says nothing recognisable.

    None rather than Good, so the caller can tell "the column was blank" from
    "the column said something I could not read" and report the second.
    """
    text = ''.join(ch for ch in str(raw or '').lower() if ch.isalnum())
    if not text:
        return None
    return CONDITION_WORDS.get(text)


def parse_shelf_slot(raw):
    """The slot number as typed, or None.

    Blank means "not recorded", which is different from position 1 and has to
    stay different: a book nobody has placed yet should not claim the first
    space on the shelf. Anything that is not a positive whole number is treated
    as blank rather than rejected, because a slot is a convenience and losing
    it must never be a reason a book cannot be catalogued.
    """
    raw = (raw or '').strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if 1 <= value <= 9999 else None


def _ordinal(n):
    if 10 <= (n % 100) <= 20:
        suffix = 'th'
    else:
        suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
    return '%d%s' % (n, suffix)


def book_history(book, limit=BOOK_HISTORY_LIMIT):
    """This copy's borrowing history, newest first."""
    rows = (Transaction.objects
            .filter(book=book)
            .select_related('patron', 'processed_by')
            .order_by('-transaction_date', '-transaction_id')[:limit])
    out = []
    for t in rows:
        if t.return_date:
            state = 'Returned'
        elif t.overdue_flag:
            state = 'Overdue'
        else:
            state = 'Out'
        out.append({
            'transaction_id': t.transaction_id,
            'type': t.transaction_type,
            'patron': t.patron.fullname if t.patron else '\u2014',
            'borrowed': t.transaction_date.isoformat() if t.transaction_date else '',
            'due': t.due_date.isoformat() if t.due_date else '',
            'returned': t.return_date.isoformat() if t.return_date else '',
            'state': state,
            'fine': str(t.fine_amount or 0),
            'staff': t.processed_by.fullname if t.processed_by else '',
        })
    return out


# Deliberately admin_login_required rather than one module: shared by Manage Books and Shelf Manager,
# and it only reads data the calling page already gated.
@admin_login_required
def admin_book_details_ajax(request, book_id):
    from django.http import JsonResponse
    book = Book.objects.filter(book_id=book_id).select_related('shelf_level', 'shelf_level__shelf').first()
    if book is None:
        return JsonResponse({'success': False, 'error': 'Book not found'})

    book_data = {
        'success': True,
        'book_id': book.book_id,
        'title': book.title,
        'author': book.author,
        'ISBN': book.ISBN or 'N/A',
        'genre': book.genre or 'General',
        'publication_year': book.publication_year or 'N/A',
        'status': book.status,
        'cover_img_url': book.cover_img_url,
        'qr_code': book.qr_code,
        'category': book.shelf_level.category if book.shelf_level else 'N/A',
        'shelf': book.shelf_level.shelf.name if (book.shelf_level and book.shelf_level.shelf) else 'N/A',
        'shelf_id': book.shelf_level.shelf.shelf_id if (book.shelf_level and book.shelf_level.shelf) else None,
        'shelf_level': (book.shelf_level.label if book.shelf_level else 'N/A'),
        'level_number': (book.shelf_level.level_number if book.shelf_level else None),
        'column_number': (book.shelf_level.column_number if book.shelf_level else None),
        'is_top': (book.shelf_level.is_top if book.shelf_level else False),
        'is_under': (book.shelf_level.is_under if book.shelf_level else False),
        'shelf_slot': book.shelf_slot,
        'location': book_location_words(book),
        # The strip of the level, so the gaps are visible rather than implied.
        'layout': level_layout(book.shelf_level, book),
        'copies': Book.objects.filter(title=book.title, author=book.author).count(),
        # Admin and library staff only. The patron endpoint is separate and
        # deliberately says nothing about who has had a book.
        'history': book_history(book),
        'history_limit': BOOK_HISTORY_LIMIT,
        'history_total': Transaction.objects.filter(book=book).count(),
    }
    return JsonResponse(book_data)


@admin_login_required
def book_qr_png(request, book_id):
    """Serve a book's QR as a PNG, inline for display or as a download.

    The modal used to point an <img> at api.qrserver.com. Three problems with
    that, and the third is why this exists at all:

    1. It needs the internet. `_qr_data_uri` already exists in this file and
       its docstring says why -- a library front desk mid-brownout still has to
       print a card. Book QRs are no different.
    2. It sends every book's QR payload to a third party to render.
    3. A cross-origin image cannot be downloaded. `<a download>` is ignored
       across origins and a canvas that has drawn one is tainted, so no button
       pointed at that URL could ever have produced a file.

    ?download=1 sets the attachment disposition; without it the same URL is
    the inline <img> source, so the picture on screen and the file saved are
    byte-identical rather than two separate renderings.
    """
    book = Book.objects.filter(book_id=book_id).first()
    if book is None:
        raise Http404('Book not found')
    if not book.qr_code:
        raise Http404('This book has no QR code')

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(book.qr_code)
    qr.make(fit=True)
    buffer = BytesIO()
    qr.make_image(fill_color='black', back_color='white').save(buffer, format='PNG')

    response = HttpResponse(buffer.getvalue(), content_type='image/png')
    if request.GET.get('download'):
        # Whoever opens the file later needs to know which book it belongs to,
        # so the title goes in the filename -- reduced to characters that are
        # safe on every filesystem, since titles carry colons and slashes.
        safe = re.sub(r'[^A-Za-z0-9]+', '-', book.title or 'book').strip('-')[:60] or 'book'
        filename = f'QR-{safe}-BOOK-{book.book_id}.png'
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        log_admin_action(request, 'Download', 'Book', book.book_id,
                         f'Downloaded the QR code for "{book.title[:60]}"')
    else:
        response['Content-Disposition'] = 'inline'
    return response


@admin_or_module_required('books')
def book_label_picker_data(request):
    """Every book, with where it sits, for the QR label picker.

    Sent in one go rather than searched over the wire: the picker filters as you
    type, and a round trip per keystroke would make choosing forty books out of
    three hundred feel like work. The whole catalogue at this shape is a few
    hundred kilobytes, and the cap below keeps that true for a library that
    grows past what one person would ever select by hand.

    Separate from get_books_for_placement, which caps at 300 and reports a bare
    level name -- neither is any use for picking labels off a shelf list.
    """
    books = (Book.objects
             .select_related('shelf_level', 'shelf_level__shelf')
             .order_by('title', 'book_id'))
    total = books.count()

    data = []
    for b in books[:MAX_PICKER_BOOKS]:
        level = b.shelf_level
        shelf = level.shelf if level and level.shelf_id else None
        data.append({
            'book_id': b.book_id,
            'title': b.title,
            'author': b.author,
            'isbn': b.ISBN or '',
            'status': b.status,
            'shelf_id': shelf.shelf_id if shelf else None,
            'shelf_name': shelf.name if shelf else '',
            'level_id': level.shelf_level_id if level else None,
            'level_number': level.level_number if level else None,
            'is_top': bool(level.is_top) if level else False,
            'level_category': (level.category or '') if level else '',
            'location': labels.location_of(b),
        })

    return JsonResponse({
        'success': True,
        'books': data,
        'total': total,
        'truncated': total > MAX_PICKER_BOOKS,
        'max_per_sheet': MAX_LABELS_PER_SHEET,
    })


@admin_or_module_required('books')
def book_qr_labels(request):
    """A printable sheet of QR labels for the selected books.

    Answers the job that actually has to be done once a delivery is catalogued:
    print the stickers, cut them, put them on the books. That is a batch
    operation, so this takes a list of ids rather than one -- downloading
    eighty PNGs one at a time and pasting them into Word is not a workflow.

    Books are emitted in shelf order, not in the order the ids arrive, so two
    people printing the same selection get the same sheet and a reprint lines up
    with the first run. The sheet stays packed edge to edge -- see
    labels.sort_for_printing for why the groups are not given their own rows.
    """
    raw_ids = request.GET.get('ids') or request.POST.get('ids') or ''
    ids = []
    for chunk in raw_ids.replace('\n', ',').split(','):
        chunk = chunk.strip()
        if chunk.isdigit():
            ids.append(int(chunk))
    if not ids:
        return JsonResponse({'success': False, 'error': 'Select at least one book first.'})
    if len(ids) > MAX_LABELS_PER_SHEET:
        return JsonResponse({
            'success': False,
            'error': f'That is {len(ids)} labels. Print at most '
                     f'{MAX_LABELS_PER_SHEET} at a time so the file stays usable.'})

    books = list(Book.objects.filter(book_id__in=ids)
                 .select_related('shelf_level', 'shelf_level__shelf'))
    # Shelf order by default, so the cut pile is already in the order you walk
    # the room. ?sort=id falls back to catalogue order for anyone who wants it.
    if (request.GET.get('sort') or 'location').lower() == 'location':
        books = labels.sort_for_printing(books)
    else:
        books.sort(key=lambda b: b.book_id)
    if not books:
        return JsonResponse({'success': False, 'error': 'None of those books exist.'})

    # A book with no QR cannot be labelled, and the label sheet is exactly when
    # anyone notices. Minting it here beats sending someone back to fix each
    # one by hand -- the value is a fresh UUID either way.
    missing = [b for b in books if not b.qr_code]
    if missing:
        with transaction.atomic():
            for b in missing:
                b.qr_code = str(uuid4())
                b.save(update_fields=['qr_code'])
        log_admin_action(request, 'Update', 'Book', None,
                         f'Minted QR codes for {len(missing)} book(s) while printing labels')

    def _flag(name, default=False):
        raw = request.GET.get(name)
        if raw is None:
            return default
        return raw.strip().lower() in ('1', 'true', 'yes', 'on')

    show = {
        'title': _flag('show_title', True),
        # On by default, and the id is not: the spine number is what somebody
        # holding the book reads to put it back, which a database id never was.
        'call_number': _flag('show_call_number', True),
        'copy': _flag('show_copy', True),
        'book_id': _flag('show_id', False),
        'author': _flag('show_author', False),
        'location': _flag('show_location', False),
    }
    try:
        qr_mm = float(request.GET.get('qr_mm') or labels.DEFAULT_QR_MM)
    except (TypeError, ValueError):
        qr_mm = labels.DEFAULT_QR_MM
    try:
        skip = max(0, int(request.GET.get('skip') or 0))
    except (TypeError, ValueError):
        skip = 0

    pdf, layout = labels.build_label_sheet(
        books,
        page=request.GET.get('page') or labels.DEFAULT_PAGE,
        qr_mm=qr_mm,
        show=show,
        cut_guides=_flag('cut_guides', True),
        skip=skip,
    )

    log_admin_action(request, 'Download', 'Book', None,
                     f'Printed {len(books)} QR label(s) at {layout["qr_mm"]:.0f}mm '
                     f'({layout["pages"]} page(s))')

    response = HttpResponse(pdf, content_type='application/pdf')
    response['Content-Disposition'] = (
        'attachment; filename="AYLA-QR-labels-%d-books.pdf"' % len(books))
    return response


# Deliberately admin_login_required rather than one module: shared by Manage Books and Transactions,
# and it only reads data the calling page already gated.
@admin_login_required
def search_book_by_qr(request):
    qr_code = request.GET.get('qr_code', '').strip()
    if not qr_code:
        return JsonResponse({'success': False, 'error': 'QR code is required'})
    
    book = Book.objects.filter(qr_code=qr_code).select_related('shelf_level', 'shelf_level__shelf').first()
    if book is None:
        return JsonResponse({'success': False, 'error': 'Book not found'})

    book_data = {
        'success': True,
        'book': {
            'book_id': book.book_id,
            'title': book.title,
            'author': book.author,
            'ISBN': book.ISBN or 'N/A',
            'genre': book.genre or 'General',
            'publication_year': book.publication_year or 'N/A',
            'status': book.status,
            'cover_img_url': book.cover_img_url,
            'qr_code': book.qr_code,
            'category': book.shelf_level.category if book.shelf_level else 'N/A',
            'shelf_level': (book.shelf_level.label if book.shelf_level else 'N/A'),
        }
    }
    return JsonResponse(book_data)


@granted_module_required('transactions')
def search_patron_by_qr(request):
    """Resolve a scanned patron identity QR to a patron record.

    Desk-side counterpart of search_book_by_qr: Library Staff / the Administrator
    scan the QR printed on the patron's card, never the patron themselves. The
    payload is the Patron.qr_code UUID minted on approval (approve_patron) — the
    same value patronaccount.html renders as the patron's QR image.
    """
    qr_code = request.GET.get('qr_code', '').strip()
    if not qr_code:
        return JsonResponse({'success': False, 'error': 'QR code is required'})

    patron = Patron.objects.filter(qr_code=qr_code).first()
    if patron is None:
        return JsonResponse({'success': False, 'error': 'No patron matches that QR code'})

    active_borrows = Transaction.objects.filter(
        patron=patron, transaction_type='Borrow', return_date__isnull=True
    ).count()
    eligible, violations = check_patron_eligibility(patron)

    return JsonResponse({
        'success': True,
        'patron': {
            'patron_id': patron.patron_id,
            'fullname': patron.fullname,
            'email': patron.email,
            'patron_type': patron.patron_type,
            'account_status': patron.account_status,
            'active_borrows': active_borrows,
            'eligible': eligible,
            'violations': violations,
        }
    })


@granted_module_required('books')
def download_book_template(request):
    wb = Workbook()
    ws = wb.active
    ws.title = "Book Import Template"
    
    # Matched by name, so order does not matter and extra columns are ignored.
    headers = ['Title', 'Author', 'Publication Year', 'ISBN', 'Genre',
               'Condition', 'Code Label', 'Quantity', 'Location']
    ws.append(headers)
    # Condition and Code Label may both be left empty: the first defaults to
    # Good, and the second is worked out from the other columns.
    # Two rows, showing both ways of writing a location: the long form and
    # the short one. Either is read back the same.
    ws.append(['Example Book Title', 'Surname, First', 2019, '9780000000000',
               'Fiction', 'Good', '', 2, 'Shelf A Column 1 Level 2'])
    ws.append(['Pride and Prejudice', 'Austen, Jane', 1963, '', 'Fiction',
               'Worn', 'FIC A31p 1963', 1, 'A C1 L2'])
    
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename=book_import_template.xlsx'
    wb.save(response)
    return response


# An uploaded workbook is attacker-controllable input even when the attacker is
# a staff member with a mistyped file. openpyxl will happily allocate for a sheet
# claiming a million rows, and a spreadsheet that expands enormously when parsed
# is the oldest denial-of-service in the format. Both are capped before parsing.
MAX_IMPORT_BYTES = 5 * 1024 * 1024
MAX_IMPORT_ROWS = 5000

# One sheet of labels is a printing job, not a data export. Past a few hundred
# the PDF gets slow to build and nobody is cutting that many out in one sitting
# anyway -- and an unbounded id list is a free way to tie up the server.
MAX_LABELS_PER_SHEET = 500

# How many books the picker will hold in the browser at once. Far above the
# print cap on purpose -- you browse the whole catalogue to choose from it, and
# only the chosen few end up on a sheet.
MAX_PICKER_BOOKS = 5000

# One sweep of a room. Past this the progress call stops being cheap, and a
# stock-take that large should be filed in stages anyway.
MAX_AUDIT_SCANS = 3000


logger = logging.getLogger(__name__)


def import_failed(what, exc):
    """Log a genuine fault, and answer with a reference instead of its guts.

    Every error these import views mean to show a person is returned explicitly
    as JSON on the way through, so anything reaching their `except` is a fault
    nobody anticipated -- a driver error, a corrupt workbook, a bug. str(exc) on
    one of those is a stack of internals: table names, absolute paths, driver
    text. Returning it put that on a librarian's screen, where it tells them
    nothing they can act on, and tells anyone else rather more about the system
    than they ought to know.

    The log keeps the whole traceback. The browser gets a short reference, so
    the two can be tied back together when somebody reports it.
    """
    reference = uuid4().hex[:8]
    # exc is passed explicitly rather than left to logger.exception's ambient
    # sys.exc_info(): that only carries a traceback while an except block is
    # actually running, so a caller one refactor away from calling this outside
    # one would silently log the reference and nothing else.
    logger.exception('[%s] %s failed: %r', reference, what, exc)
    return JsonResponse({
        'success': False,
        # The reference goes last, where it is easy to find and copy.
        'error': 'Something went wrong at our end and nothing was saved. '
                 'Quote this reference when reporting it: %s' % reference,
    })


def check_import_upload(uploaded):
    """Return an error string for a workbook we should not parse, or None."""
    if uploaded is None:
        return 'No file was uploaded.'
    name = (getattr(uploaded, 'name', '') or '').lower()
    if not name.endswith(('.xlsx', '.xlsm')):
        return 'Please upload an .xlsx spreadsheet.'
    if uploaded.size > MAX_IMPORT_BYTES:
        return f'That file is too large. The limit is {MAX_IMPORT_BYTES // (1024 * 1024)} MB.'
    # ZIP-based formats all start with PK; a renamed .csv or .exe does not.
    head = uploaded.read(2)
    uploaded.seek(0)
    if head != b'PK':
        return 'That file is not a valid Excel workbook.'
    return None


def check_import_size(worksheet):
    """Return an error string for a sheet with more rows than we will accept."""
    rows = worksheet.max_row or 0
    if rows > MAX_IMPORT_ROWS:
        return (f'That sheet has {rows:,} rows. Please import at most '
                f'{MAX_IMPORT_ROWS:,} at a time.')
    return None


# "Shelf A Column 1 Level 2" and "A C1 L2" are the same place said at two
# lengths, and a sheet will carry whichever the person typing it preferred.
LOCATION_COLUMN_RE = re.compile(r'\b(?:c|col|column)\s*\.?\s*(\d+)\b', re.I)
LOCATION_LEVEL_RE = re.compile(r'\b(?:l|lvl|level)\s*\.?\s*(\d+)\b', re.I)
LOCATION_TOP_RE = re.compile(r'\btop\b', re.I)
LOCATION_UNDER_RE = re.compile(r'\bunder(?:neath)?\b', re.I)


def parse_location(text):
    """Pull a shelf name, column and board out of a written location.

    Returns (shelf_name, level_number, column_number, is_top, is_under), any of
    which may be None. Deliberately forgiving: separators vary, people write
    "L2" or "Level 2", and a sheet that only names the bay is still useful.
    """
    raw = (text or '').strip()
    if not raw:
        return ('', None, None, False, False)

    # Bullets, commas and dashes are all just "and then" here.
    cleaned = re.sub(r'[\u00b7,;/|]+', ' ', raw)

    column = LOCATION_COLUMN_RE.search(cleaned)
    level = LOCATION_LEVEL_RE.search(cleaned)
    is_top = bool(LOCATION_TOP_RE.search(cleaned))
    is_under = bool(LOCATION_UNDER_RE.search(cleaned))

    # Whatever is left once the position words are taken out is the bay's name.
    name = cleaned
    for pattern in (LOCATION_COLUMN_RE, LOCATION_LEVEL_RE, LOCATION_TOP_RE, LOCATION_UNDER_RE):
        name = pattern.sub(' ', name)
    name = ' '.join(name.split()).strip(' -')

    return (name,
            int(level.group(1)) if level else None,
            int(column.group(1)) if column else None,
            is_top, is_under)


def _shelf_by_written_name(name):
    """The bay a sheet means by "Shelf A", "A", or "shelf a"."""
    name = (name or '').strip()
    if not name:
        return None
    shelf = Shelf.objects.filter(name__iexact=name).first()
    if shelf is not None:
        return shelf
    # "A" for a bay actually called "Shelf A", and the other way round.
    shelf = Shelf.objects.filter(name__iexact='Shelf ' + name).first()
    if shelf is not None:
        return shelf
    for candidate in Shelf.objects.all():
        words = (candidate.name or '').split()
        if words and words[-1].lower() == name.lower():
            return candidate
    return None


def _resolve_written_location(text):
    """The exact board a written location names, created if it is missing.

    Returns None when the text says nothing about a position, so the caller can
    fall back to the older "storage area" matching that treats the whole string
    as a category name.
    """
    name, level_no, column_no, is_top, is_under = parse_location(text)
    if not (level_no or column_no or is_top or is_under):
        return None

    shelf = _shelf_by_written_name(name)
    if shelf is None:
        return None

    levels = ShelfLevel.objects.filter(shelf=shelf)
    if is_top:
        found = levels.filter(is_top=True).first()
    elif is_under:
        found = levels.filter(is_under=True).first()
    else:
        found = levels.filter(level_number=level_no or 1,
                              is_top=False, is_under=False).first()
    if found is not None and column_no:
        # The board is right but the bay across it may not be.
        exact = levels.filter(level_number=found.level_number,
                              column_number=column_no).first()
        if exact is not None:
            return exact
    if found is not None and not column_no:
        return found

    # Nothing matching: make it, so the location travels with the import
    # rather than being quietly dropped on the floor.
    return ShelfLevel.objects.create(
        shelf=shelf,
        level_number=level_no or (levels.aggregate(n=Max('level_number'))['n'] or 0) + 1,
        column_number=column_no,
        is_top=is_top,
        is_under=is_under)


def _resolve_storage_area(name):
    """Find the shelf level a sheet's "Storage Area" refers to, creating it once.

    Sheets record a human label ("Zone 3"), not a database id. Match an existing
    level or shelf by that label; failing that create the level so the location
    travels with the import instead of being silently dropped. Returns None only
    when there is no shelf at all to hang it from.
    """
    label = (name or '').strip()
    if not label:
        return None

    # A written position wins: "Shelf A Column 1 Level 2" names one board, and
    # matching it against category names would file it by accident.
    precise = _resolve_written_location(label)
    if precise is not None:
        return precise

    level = ShelfLevel.objects.filter(category__iexact=label).first()
    if level is not None:
        return level

    shelf = Shelf.objects.filter(name__iexact=label).first()
    if shelf is not None:
        level = ShelfLevel.objects.filter(shelf=shelf).order_by('level_number').first()
        if level is not None:
            return level
        return ShelfLevel.objects.create(shelf=shelf, level_number=1, category=label)

    shelf = Shelf.objects.order_by('shelf_id').first()
    if shelf is None:
        return None
    next_level = (ShelfLevel.objects.filter(shelf=shelf).count() or 0) + 1
    return ShelfLevel.objects.create(shelf=shelf, level_number=next_level, category=label)


class _PreviewOnly(Exception):
    """Raised to unwind a preview run once its summary has been taken.

    The alternative -- a second code path that predicts what the real one would
    do -- is a copy that drifts, and a preview that lies about the import is
    worse than no preview. So the import genuinely runs and is then rolled
    back, which means what is shown is what happened.
    """

    def __init__(self, payload):
        self.payload = payload


@granted_module_required('books')
def import_books(request):
    """Import a sheet, or say what importing it would do.

    A preview runs the real import inside a transaction and rolls it back, so
    what the confirmation box reports is what actually happened rather than a
    second implementation's guess at it.
    """
    if request.POST.get('preview') in ('1', 'true', 'True', 'on'):
        try:
            with transaction.atomic():
                raise _PreviewOnly(_import_books_body(request))
        except _PreviewOnly as done:
            return done.payload
    return _import_books_body(request)


def _import_books_body(request):
    """Bulk-import books from a spreadsheet.

    Columns are matched by *header name*, not position, so a sheet recorded in
    a different order still imports. Aliases cover what libraries actually
    write in their own sheets ("Barcode" for the ISBN, "Year Publish" for the
    publication year, and so on).

    Quantity creates that many physical copies — the catalogue stores one row
    per copy — and Storage Area is resolved to a shelf level by name so a sheet
    can carry its own locations.

    The ids of everything created come back in `created_ids`, because the next
    thing anyone does after importing a delivery is print the QR stickers for
    it — see book_qr_labels.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    if 'excel_file' not in request.FILES:
        return JsonResponse({'success': False, 'error': 'No file uploaded'})

    excel_file = request.FILES['excel_file']
    # Name, size and magic bytes, not just the extension: openpyxl allocates for
    # whatever a workbook claims to hold, and a renamed file is not a workbook.
    upload_error = check_import_upload(excel_file)
    if upload_error:
        return JsonResponse({'success': False, 'error': upload_error})

    # A preview does the real work and throws it away, so the numbers shown are
    # measured rather than predicted.
    preview = request.POST.get('preview') in ('1', 'true', 'True', 'on')

    # header text (normalised) -> field. Several spellings map to one field.
    HEADER_ALIASES = {
        'title': 'title', 'booktitle': 'title', 'bookname': 'title', 'name': 'title',
        'author': 'author', 'authors': 'author', 'writer': 'author',
        'publicationyear': 'publication_year', 'yearpublish': 'publication_year',
        'yearpublished': 'publication_year', 'year': 'publication_year',
        'copyright': 'publication_year',
        'isbn': 'ISBN', 'isbn13': 'ISBN', 'isbn10': 'ISBN', 'barcode': 'ISBN',
        'accessionnumber': 'ISBN',
        'genre': 'genre', 'genr': 'genre', 'category': 'genre', 'subject': 'genre',
        'shelflevelid': 'shelf_level_id', 'shelflevel': 'shelf_level_id',
        'storagearea': 'storage_area', 'storage': 'storage_area',
        'location': 'storage_area', 'shelf': 'storage_area', 'zone': 'storage_area',
        'shelflocation': 'storage_area', 'position': 'storage_area',
        'whereitis': 'storage_area',
        'quantity': 'quantity', 'qty': 'quantity', 'copies': 'quantity',
        'numberofcopies': 'quantity',
        'condition': 'condition', 'bookcondition': 'condition', 'state': 'condition',
        # The spine number. Left blank it is worked out from the genre, author,
        # title and year, so a sheet that never had a column for it still comes
        # out with every book numbered.
        'codelabel': 'call_number', 'callnumber': 'call_number',
        'callno': 'call_number', 'spinelabel': 'call_number',
        'classification': 'call_number',
    }

    def norm(text):
        return ''.join(ch for ch in str(text or '').lower() if ch.isalnum())

    def clean(value):
        text = str(value).strip() if value is not None else ''
        # Sheets write "N/A", "none" or "-" for a blank; treat them as blank.
        return '' if text.lower() in ('', 'n/a', 'na', 'none', '-', '--') else text

    try:
        wb = openpyxl.load_workbook(excel_file, data_only=True)
        ws = wb.active
        size_error = check_import_size(ws)
        if size_error:
            return JsonResponse({'success': False, 'error': size_error})

        header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), None)
        if not header_row:
            return JsonResponse({'success': False, 'error': 'The sheet is empty.'})

        columns = {}
        for index, cell in enumerate(header_row):
            field = HEADER_ALIASES.get(norm(cell))
            if field and field not in columns:
                columns[field] = index

        if 'title' not in columns or 'author' not in columns:
            found = ', '.join(str(h) for h in header_row if h) or '(none)'
            return JsonResponse({'success': False, 'error':
                                 'Could not find a Title and Author column. '
                                 'Headers found: ' + found})

        def field(row, name):
            idx = columns.get(name)
            return clean(row[idx]) if idx is not None and idx < len(row) else ''

        imported = skipped_dup = skipped_blank = copies_created = 0
        # Collected so the caller can print labels for exactly this delivery.
        # Filtering the table by hand afterwards to find "the ones I just added"
        # is guesswork the moment two imports happen on the same day.
        created_ids = []
        unmatched_areas = set()
        unreadable_conditions = set()
        skipped_repeat = 0

        # What was already on the shelves before this file was opened. A row
        # with no ISBN has nothing else to be recognised by, so re-importing a
        # sheet used to add every one of its books again -- and most of a real
        # catalogue has no ISBN at all.
        #
        # Snapshotted up front, deliberately: copies created by this very
        # import must not block each other, or a Quantity of 3 would import one
        # book and skip two.
        already_here = set(
            (str(t or '').strip().lower(), str(a or '').strip().lower(), lvl)
            for t, a, lvl in Book.objects.values_list('title', 'author', 'shelf_level_id')
        )

        for row in ws.iter_rows(min_row=2, values_only=True):
            title = field(row, 'title')
            author = field(row, 'author')
            if not title or not author:
                skipped_blank += 1
                continue
            # A totals row at the foot of a sheet has no real title.
            if title.lower().startswith('total'):
                skipped_blank += 1
                continue

            isbn = field(row, 'ISBN')
            if isbn and Book.objects.filter(ISBN=isbn).exists():
                skipped_dup += 1
                continue

            raw_year = field(row, 'publication_year')
            publication_year = None
            if raw_year:
                try:
                    year = int(float(raw_year))
                    # Ignore impossible years rather than storing them.
                    if 1000 <= year <= timezone.localdate().year + 1:
                        publication_year = year
                except (TypeError, ValueError):
                    publication_year = None

            # Unreadable still becomes Good rather than failing the row -- a
            # word nobody anticipated is not a reason to refuse a book -- but it
            # is counted and reported, because silently calling a damaged book
            # Good is how a shelf of them goes unnoticed.
            raw_condition = field(row, 'condition')
            condition = parse_condition(raw_condition)
            if condition is None:
                if raw_condition:
                    unreadable_conditions.add(str(raw_condition).strip()[:40])
                condition = 'Good'

            quantity = 1
            raw_qty = field(row, 'quantity')
            if raw_qty:
                try:
                    quantity = max(1, min(int(float(raw_qty)), 50))
                except (TypeError, ValueError):
                    quantity = 1

            shelf_level = None
            raw_level_id = field(row, 'shelf_level_id')
            if raw_level_id:
                shelf_level = ShelfLevel.objects.filter(shelf_level_id=raw_level_id).first()
            if shelf_level is None:
                area = field(row, 'storage_area')
                if area:
                    shelf_level = _resolve_storage_area(area)
                    if shelf_level is None:
                        unmatched_areas.add(area)

            # With no ISBN there is nothing else to recognise a row by, so this
            # is what stops a second run of the same sheet adding every one of
            # its books again.
            #
            # Compared only against the snapshot taken before the import began,
            # and deliberately not added to as rows are created: two rows for
            # one book *within a sheet* are two physical copies, and the sheet
            # listing them separately is how a library says so. It is the same
            # book turning up in a later import that means nothing new.
            if not isbn:
                identity = (title.strip().lower(), author.strip().lower(),
                            shelf_level.shelf_level_id if shelf_level else None)
                if identity in already_here:
                    skipped_repeat += 1
                    continue

            for copy_no in range(quantity):
                # Only the first copy carries the ISBN: it is unique to the
                # title, and the duplicate check above relies on that.
                book = Book.objects.create(
                    title=title,
                    author=author,
                    publication_year=publication_year,
                    ISBN=isbn if copy_no == 0 else None,
                    genre=field(row, 'genre') or None,
                    condition=condition,
                    # Blank is the normal case: Book.save() derives it.
                    call_number=field(row, 'call_number') or None,
                    shelf_level=shelf_level,
                    status='Available',
                    qr_code=str(uuid4()),
                )
                imported += 1
                created_ids.append(book.book_id)
                if copy_no > 0:
                    copies_created += 1

        # A preview describes what will happen, not what has. A confirmation
        # box that reads "Imported 46 books" invites the reader to think the
        # job is already done and close it.
        if preview:
            parts = [f'{imported} book record(s) will be imported']
            skipped_word = 'will skip'
        else:
            parts = [f'Imported {imported} book record(s)']
            skipped_word = 'skipped'
        if copies_created:
            parts.append(f'including {copies_created} extra copy/copies from Quantity')
        if skipped_dup:
            parts.append(f'{skipped_word} {skipped_dup} row(s) whose ISBN already exists')
        if skipped_repeat:
            parts.append(f'{skipped_word} {skipped_repeat} row(s) already on the same shelf '
                         f'(no ISBN to tell copies apart)')
        if skipped_blank:
            parts.append(f'{skipped_word} {skipped_blank} row(s) with no title or author')
        if unreadable_conditions:
            # Named, not just counted: "BAD CONDITION" filed as Good is the kind
            # of thing nobody notices until a shelf of damaged books is found.
            parts.append('could not read the condition "'
                         + '", "'.join(sorted(unreadable_conditions)[:5])
                         + '" — filed as Good')
        if unmatched_areas:
            parts.append('could not match storage area: ' + ', '.join(sorted(unmatched_areas)))

        payload = {
            'success': True,
            'message': '. '.join(parts) + '.',
            'imported': imported,
            'matched_columns': sorted(columns),
            # Capped at the label sheet's own limit: past that the follow-up
            # offer is not something anyone would accept anyway. Empty for a
            # preview: those rows are about to be rolled back, and handing out
            # ids that will not exist a moment later is a trap for whatever
            # tries to use them.
            'created_ids': [] if preview else created_ids[:MAX_LABELS_PER_SHEET],
            'created_truncated': len(created_ids) > MAX_LABELS_PER_SHEET,
            'preview': preview,
            'skipped_dup': skipped_dup,
            'skipped_repeat': skipped_repeat,
            'skipped_blank': skipped_blank,
            'copies_created': copies_created,
        }
        return JsonResponse(payload)

    except Exception as exc:
        return import_failed('Book import', exc)


@admin_module_required('patrons')
def download_patron_template(request):
    wb = Workbook()
    ws = wb.active
    ws.title = "Patron Import Template"
    
    headers = ['first_name', 'middle_name', 'last_name', 'patron_type', 'email',
               'contact_number', 'address', 'account_status']
    ws.append(headers)
    
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename=patron_import_template.xlsx'
    wb.save(response)
    return response


@admin_module_required('patrons')
def import_patrons(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})
    
    if 'excel_file' not in request.FILES:
        return JsonResponse({'success': False, 'error': 'No file uploaded'})
    
    excel_file = request.FILES['excel_file']

    # Name, size and magic bytes, not just the extension.
    upload_error = check_import_upload(excel_file)
    if upload_error:
        return JsonResponse({'success': False, 'error': upload_error})
    
    try:
        wb = openpyxl.load_workbook(excel_file)
        ws = wb.active
        size_error = check_import_size(ws)
        if size_error:
            return JsonResponse({'success': False, 'error': size_error})
        
        imported_count = 0
        skipped_count = 0
        
        for row in ws.iter_rows(min_row=2):
            # Three name columns now; a file made with the old single-column
            # template still imports, since a lone name splits the same way a
            # walk-in typed at the desk does.
            first_name = (row[0].value or '') if row[0].value else ''
            middle_name = (row[1].value or '') if len(row) > 1 and row[1].value else ''
            last_name = (row[2].value or '') if len(row) > 2 and row[2].value else ''
            if not last_name and first_name and ' ' in str(first_name).strip():
                first_name, middle_name, last_name = parse_name(str(first_name))
            fullname = compose_name(str(first_name), str(middle_name), str(last_name))
            patron_type = row[3].value
            email = row[4].value
            contact_number = row[5].value
            address = row[6].value if len(row) > 6 else None
            account_status = row[7].value if len(row) > 7 else None
            
            if not fullname or not email:
                continue
            
            if Patron.objects.filter(email=email).exists():
                skipped_count += 1
                continue
            
            patron = Patron.objects.create(
                fullname=fullname,
                first_name=str(first_name).strip(),
                middle_name=str(middle_name).strip(),
                last_name=str(last_name).strip(),
                patron_type=patron_type if patron_type else 'Student',
                email=email,
                contact_number=contact_number,
                address=address,
                account_status=account_status if account_status else 'Active',
                registration_date=timezone.now(),
                registration_channel='On-site',
                qr_code=str(uuid4()),
                otp_verified=True,
            )
            
            imported_count += 1
        
        return JsonResponse({
            'success': True,
            'message': f'Successfully imported {imported_count} patrons. Skipped {skipped_count} duplicates.'
        })
        
    except Exception as exc:
        return import_failed('Patron import', exc)


@admin_module_required('inventory')
def download_donation_template(request):
    wb = Workbook()
    ws = wb.active
    ws.title = "Donation Import Template"
    
    headers = ['donor_name', 'date_donated', 'title', 'author', 'ISBN', 'genre', 'publication_year']
    ws.append(headers)
    
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename=donation_import_template.xlsx'
    wb.save(response)
    return response


@admin_module_required('inventory')
def import_donations(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})
    
    if 'excel_file' not in request.FILES:
        return JsonResponse({'success': False, 'error': 'No file uploaded'})
    
    excel_file = request.FILES['excel_file']

    # Name, size and magic bytes, not just the extension.
    upload_error = check_import_upload(excel_file)
    if upload_error:
        return JsonResponse({'success': False, 'error': upload_error})
    
    try:
        wb = openpyxl.load_workbook(excel_file)
        ws = wb.active
        size_error = check_import_size(ws)
        if size_error:
            return JsonResponse({'success': False, 'error': size_error})
        
        imported_count = 0
        skipped_count = 0
        
        for row in ws.iter_rows(min_row=2):
            donor_name = row[0].value
            date_donated = row[1].value
            title = row[2].value
            author = row[3].value
            isbn = row[4].value
            genre = row[5].value
            # Spreadsheets predating this column simply produce None.
            material_type = (str(row[7].value).strip() if len(row) > 7 and row[7].value else 'Book')
            if material_type not in {c[0] for c in Book.MATERIAL_TYPE_CHOICES}:
                material_type = 'Book'
            publication_year = row[6].value
            
            if not donor_name or not title or not author:
                continue
            
            book = Book.objects.create(
                title=title,
                author=author,
                ISBN=isbn,
                genre=genre,
                material_type=material_type,
                publication_year=int(publication_year) if publication_year else None,
                status='Available'
            )
            
            donated_on = date_donated if date_donated else timezone.localdate()
            donation = Donation.objects.create(
                donor_name=donor_name,
                date_donated=donated_on,
                book=book,
                status='Received'
            )

            # A bulk import is still an intake, so it lands in Inventory like
            # any other donation — otherwise it would be a second way in.
            record = InventoryRecord.objects.create(
                book=book,
                source='Donation',
                donor_name=donor_name,
                donated_date=donated_on if not hasattr(donated_on, 'date') else donated_on.date(),
                processing_stage='Received',
                condition='Good',
                status='In Stock',
                qr_label=str(uuid4()),
                donation=donation,
            )
            _record_movement(record, 'Received', request,
                             reason='Received via donation import',
                             source='Donation import', after='Good')

            imported_count += 1
        
        return JsonResponse({
            'success': True,
            'message': f'Successfully imported {imported_count} donations.'
        })
        
    except Exception as exc:
        return import_failed('Donation import', exc)


@admin_only_required
def download_announcement_template(request):
    wb = Workbook()
    ws = wb.active
    ws.title = "Announcement Import Template"
    
    headers = ['title', 'message']
    ws.append(headers)
    
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename=announcement_import_template.xlsx'
    wb.save(response)
    return response


@admin_only_required
def import_announcements(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})
    
    if 'excel_file' not in request.FILES:
        return JsonResponse({'success': False, 'error': 'No file uploaded'})
    
    excel_file = request.FILES['excel_file']

    # Name, size and magic bytes, not just the extension.
    upload_error = check_import_upload(excel_file)
    if upload_error:
        return JsonResponse({'success': False, 'error': upload_error})
    
    try:
        wb = openpyxl.load_workbook(excel_file)
        ws = wb.active
        size_error = check_import_size(ws)
        if size_error:
            return JsonResponse({'success': False, 'error': size_error})
        
        imported_count = 0
        
        for row in ws.iter_rows(min_row=2):
            title = row[0].value
            message = row[1].value
            
            if not title or not message:
                continue
            
            announcement = Announcement.objects.create(
                title=title,
                message=message,
                is_active=True,
                posted_by=request.user
            )
            
            imported_count += 1
        
        return JsonResponse({
            'success': True,
            'message': f'Successfully imported {imported_count} announcements.'
        })
        
    except Exception as exc:
        return import_failed('Announcement import', exc)


# Deliberately admin_login_required rather than one module: shared by Manage Books and Transactions,
# and it only reads data the calling page already gated.
@admin_login_required
def get_book_by_id(request):
    book_id = request.GET.get('book_id', '').strip()
    if not book_id:
        return JsonResponse({'success': False, 'error': 'book_id is required'})
    
    try:
        book_id_int = int(book_id)
    except ValueError:
        return JsonResponse({'success': False, 'error': 'Invalid book_id format'})
    
    book = Book.objects.filter(book_id=book_id_int).first()
    if book is None:
        return JsonResponse({'success': False, 'error': 'Book not found'})
    
    book_data = {
        'success': True,
        'book_id': book.book_id,
        'title': book.title,
        'author': book.author,
        'status': book.status,
    }
    return JsonResponse(book_data)


class _AlreadyResolved(Exception):
    """Raised inside a lock when another request resolved the row first."""


class _BasketAborted(Exception):
    """Raised inside the transaction to roll the whole basket back.

    Processing used to validate and write one book at a time in the same loop,
    so a basket of five where the third was already on loan left the first two
    committed as borrowed -- while the staff member saw an error and reasonably
    assumed nothing had happened.
    """

    def __init__(self, errors):
        super().__init__('; '.join(errors))
        self.errors = list(errors)


def _apply_basket(request, transaction_type, book_ids, patron, admin, rule):
    """Do the whole basket inside one locked transaction, or do none of it.

    Split out of process_transaction so the atomic block is a function boundary
    rather than an extra level of indentation wrapped around a hundred lines --
    and so the emails and the audit line, which must not fire for a basket that
    rolled back, are plainly outside it.

    Returns (processed_titles, borrowed_books, borrow_due_date, returned_books,
    returned_overdue). Raises _BasketAborted if anything in the basket fails.
    """
    errors = []
    processed_books = []
    borrowed_books = []
    borrow_due_date = None
    returned_books = []
    returned_overdue = False
    today = timezone.localdate()

    with transaction.atomic():
        if transaction_type == 'Borrow':
            eligible, violations = check_patron_eligibility(patron)
            if not eligible:
                raise _BasketAborted(violations)

            # Counted inside the transaction so two baskets processed at the same
            # moment cannot each see the patron under the limit and together push
            # them over it.
            active_borrows = Transaction.objects.filter(
                patron=patron, transaction_type='Borrow', return_date__isnull=True
            ).count()
            if active_borrows + len(book_ids) > rule.max_books_per_patron:
                raise _BasketAborted([
                    f'Borrowing limit is {rule.max_books_per_patron} book(s). '
                    f'This patron already has {active_borrows} active borrow(s).'
                ])

        # Ids are resolved and sorted before any lock is taken: two staff
        # processing {A, B} and {B, A} at the same time would otherwise each hold
        # the row the other is waiting for, and deadlock.
        wanted = []
        for raw in book_ids:
            try:
                wanted.append(int(raw))
            except (TypeError, ValueError):
                errors.append(f'Invalid book_id: {raw}')
        wanted = sorted(set(wanted))

        for book_id_int in wanted:
            # select_for_update holds this row until the transaction ends, which
            # is what turns the status check below from a guess into a decision.
            # Without it two terminals could both read "Available" for the same
            # copy and both write "Borrowed" -- two open loans, one physical book.
            book = Book.objects.select_for_update().filter(book_id=book_id_int).first()
            if book is None:
                errors.append(f'Book not found: {book_id_int}')
                continue

            if transaction_type == 'Borrow':
                if book.status != 'Available':
                    errors.append(f'Book "{book.title}" is not Available (current status: {book.status})')
                    continue
            elif transaction_type == 'Return':
                if book.status not in ['Borrowed', 'Overdue']:
                    errors.append(f'Book "{book.title}" is not Borrowed or Overdue (current status: {book.status})')
                    continue
            elif transaction_type == 'In-Library Reading':
                if book.status != 'Available':
                    errors.append(f'Book "{book.title}" is not Available (current status: {book.status})')
                    continue

            if transaction_type == 'Borrow':
                due_date = today + timedelta(days=rule.loan_period_days)
                book.status = 'Borrowed'
                book.save()
                Transaction.objects.create(
                    patron=patron,
                    book=book,
                    processed_by=admin,
                    transaction_type='Borrow',
                    due_date=due_date,
                )
                borrowed_books.append(book)
                borrow_due_date = due_date
            elif transaction_type == 'Return':
                book.status = 'Available'
                book.save()
                tx = Transaction.objects.select_for_update().filter(
                    book=book,
                    transaction_type='Borrow',
                    return_date__isnull=True,
                ).first()
                if tx:
                    tx.return_date = today
                    tx.overdue_flag = bool(tx.due_date and today > tx.due_date)
                    tx.fine_amount = rule.compute_fine(tx.due_date, today)
                    tx.save()
                    if tx.overdue_flag:
                        returned_overdue = True
                returned_books.append(book)
            elif transaction_type == 'In-Library Reading':
                book.status = 'Being Read'
                book.save()
                Transaction.objects.create(
                    patron=None,
                    book=book,
                    processed_by=admin,
                    transaction_type='In-Library Reading',
                    due_date=None,
                )

            processed_books.append(book.title)

        # Raised rather than returned: the exception is what unwinds the atomic
        # block, and unwinding it is what undoes the books already written above.
        if errors:
            raise _BasketAborted(errors)

    return processed_books, borrowed_books, borrow_due_date, returned_books, returned_overdue


@granted_module_required('transactions')
def process_transaction(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})
    
    transaction_type = request.POST.get('transaction_type', '').strip()
    patron_id = request.POST.get('patron_id', '').strip()
    # Identity from the Scan Patron QR tab.
    patron_qr = request.POST.get('patron_qr', '').strip()
    book_ids = request.POST.getlist('book_ids')
    
    if not transaction_type:
        return JsonResponse({'success': False, 'error': 'transaction_type is required'})
    
    if transaction_type not in ['Borrow', 'Return', 'In-Library Reading']:
        return JsonResponse({'success': False, 'error': 'Invalid transaction_type'})
    
    if not book_ids:
        return JsonResponse({'success': False, 'error': 'book_ids is required'})
    
    # Validate patron for Borrow and Return transactions
    patron = None
    verification = ''
    if transaction_type in ['Borrow', 'Return']:
        if patron_qr:
            # Scanned identity: the QR is re-resolved here rather than trusting the
            # patron_id the browser sent, so the scan is what actually commits.
            patron = Patron.objects.filter(qr_code=patron_qr).first()
            if patron is None:
                return JsonResponse({'success': False, 'error': 'No patron matches that QR code'})
            verification = ' [QR verified]'
        elif patron_id:
            try:
                patron_id_int = int(patron_id)
                patron = Patron.objects.filter(patron_id=patron_id_int).first()
                if patron is None:
                    return JsonResponse({'success': False, 'error': 'Patron not found'})
            except ValueError:
                return JsonResponse({'success': False, 'error': 'Invalid patron_id format'})
        else:
            return JsonResponse({'success': False, 'error': 'patron_id is required for this transaction type'})

    # Get admin from session
    admin_id = request.session.get('admin_id')
    admin = User.objects.filter(admin_id=admin_id).first()

    # Active borrowing rule (loan period, limit, penalties).
    rule = BorrowingRule.current()

    # 3-point patron eligibility check before borrowing (overdue items,
    # account suspension, outstanding lost-book penalty).

    try:
        (processed_books, borrowed_books, borrow_due_date,
         returned_books, returned_overdue) = _apply_basket(
            request, transaction_type, book_ids, patron, admin, rule)
    except _BasketAborted as aborted:
        # Nothing was written: the transaction rolled back on the way out, so
        # there is no partial basket to report.
        return JsonResponse({
            'success': False,
            'error': 'Transaction validation failed',
            'errors': aborted.errors,
            'processed': [],
        })

    patron_label = f' for {patron.fullname}' if patron else ''
    log_admin_action(
        request, 'Process', 'Transaction', None,
        f'{transaction_type}: {len(processed_books)} book(s){patron_label}{verification}',
        patron=patron,
    )

    # Email the patron a borrowing confirmation with the due date.
    if transaction_type == 'Borrow' and patron and borrowed_books:
        borrow_confirmation_email(patron, borrowed_books, borrow_due_date)

    # Email the patron a return receipt.
    if transaction_type == 'Return' and patron and returned_books:
        return_receipt_email(patron, returned_books, had_overdue=returned_overdue)

    return JsonResponse({
        'success': True,
        'message': f'Successfully processed {len(processed_books)} books',
        'processed_books': processed_books
    })


# Donation Management Views
def _donations_page(request, template):
    """The donation accessioning queue (Ch.1 ¶258, Fig. 7).

    Intake itself lives in Inventory — ¶268, Fig. 9 and Fig. 78 all place
    "receive books / process donations" there — so this page no longer creates
    donations. It tracks copies received in Inventory through
    Received → Processing → Shelved.
    """
    # Ordered so the title-lines of one intake sit together: a donor who brings
    # five titles created five rows, and reading them as five separate
    # donations is the thing that makes this page misleading.
    donations_queryset = (Donation.objects
                          .select_related('book')
                          .prefetch_related('inventory_copies')
                          .order_by('-date_donated', 'donor_name', 'donation_id'))

    if request.method == 'POST':
        # Donation intake moved to Inventory; nothing is created here any more.
        messages.info(request, 'Donations are received in Inventory Management, '
                               'then tracked here through to Shelved.')
        return portal_redirect(request, 'donation_management')

    # One donation is one donor on one day, however many titles came with it.
    # A library acknowledges the gift, not each title inside it, so the page is
    # grouped that way and paginated by donation rather than by line.
    groups = []
    total_titles = 0
    total_copies = 0
    for line in donations_queryset:
        copies = len(line.inventory_copies.all())    # prefetched; no extra query
        total_titles += 1
        total_copies += copies
        key = (line.donor_name, line.date_donated)
        if not groups or groups[-1]['key'] != key:
            groups.append({
                'key': key,
                'donor_name': line.donor_name,
                'date_donated': line.date_donated,
                'lines': [],
                'copies': 0,
                'stages': set(),
            })
        line.copy_count = copies
        groups[-1]['lines'].append(line)
        groups[-1]['copies'] += copies
        groups[-1]['stages'].add(line.status)

    for group in groups:
        stages = group['stages']
        # One label for the gift as a whole. "Mixed" is the honest answer when
        # its titles are at different points, rather than picking one of them.
        group['stage'] = stages.pop() if len(stages) == 1 else 'Mixed'
        group['title_count'] = len(group['lines'])

    page_number = request.GET.get('page', 1)
    paginator = Paginator(groups, 10)   # 10 donations per page
    donations = paginator.get_page(page_number)

    # Counted here rather than in the template, and counted over the whole
    # queryset rather than the page being shown: a stage total that only
    # described page one would quietly disagree with itself as you paged.
    # order_by() is cleared deliberately: an ordering field joins the GROUP BY
    # of a values().annotate(), so grouping by status alone requires dropping
    # the date ordering first. Left in, it counts one group per date and every
    # stage reports 1.
    stage_counts = {row['status']: row['n'] for row in
                    donations_queryset.order_by().values('status').annotate(n=Count('status'))}


    return render(request, template, {
        'donations': donations,
        'paginator': paginator,
        # Three different numbers that were all previously called "donations".
        'total_donations': paginator.count,     # gifts received
        'total_titles': total_titles,           # accessioning lines
        'total_copies': total_copies,           # physical books
        'received_count': stage_counts.get('Received', 0),
        'processing_count': stage_counts.get('Processing', 0),
        'shelved_count': stage_counts.get('Shelved', 0),
    })


@admin_module_required('donations')
def donation_management(request):
    return _donations_page(request, 'admin/donationadmin.html')


@module_required('donations')
def staff_donations(request):
    return _donations_page(request, 'library_staff/donationadmin.html')


@granted_module_required('donations')
def update_donation_status(request):
    if request.method == 'POST':
        donation_id = request.POST.get('donation_id')
        status = request.POST.get('status')
        
        donation = Donation.objects.filter(donation_id=donation_id).first()
        if donation and status in dict(Donation.STATUS_CHOICES):
            donation.status = status
            donation.save()

            # Shelving the last stage of accessioning normally makes the title
            # available — but only if nothing else already has a claim on it.
            # A copy out on loan, marked lost, or being read in the library is
            # a fact about the physical book, and an accessioning step must not
            # overwrite it: the catalogue would advertise a book that is in
            # somebody's bag.
            if status == 'Shelved' and donation.book:
                claimed = donation.book.status in ('Borrowed', 'Overdue', 'Being Read', 'Lost')
                if not claimed:
                    donation.book.status = 'Available'
                    donation.book.save()
            # Carry the stage back to the inventory copies this row tracks, so
            # the two never disagree about where a donation has got to.
            donation.inventory_copies.update(processing_stage=status)
            log_admin_action(request, 'Update', 'Donation', donation.donation_id,
                             f'Status set to {status}')

    return portal_redirect(request, 'donation_management')


@granted_module_required('donations')
def delete_donation(request):
    if request.method == 'POST':
        donation_id = request.POST.get('donation_id')
        donation = Donation.objects.filter(donation_id=donation_id).first()
        if donation:
            detail = f'"{donation.book.title}" from {donation.donor_name}' if donation.book else donation.donor_name
            # Copies still tracked by this accessioning row would be left with
            # no record of where they came from, so they are dealt with first.
            held = donation.inventory_copies.exclude(status='Removed').count()
            if held:
                messages.error(
                    request,
                    f'{held} copy(ies) from this donation are still in Inventory. '
                    'Deaccession them there before deleting the accessioning record.'
                )
                return portal_redirect(request, 'donation_management')

            # Only the accessioning row goes. Deleting the catalogue record here
            # took its loan history with it -- Transaction.book cascades -- so
            # removing a mis-keyed donation could erase who had borrowed the
            # book and when. A catalogue record that should not exist is removed
            # in Manage Books, where that is the visible, intended consequence.
            donation.delete()
            log_admin_action(request, 'Delete', 'Donation', donation_id, detail)
            messages.success(
                request,
                f'Accessioning record for {detail} removed. The catalogue entry '
                'remains — delete it in Manage Books if it was created in error.'
            )

    return portal_redirect(request, 'donation_management')


# Announcement Management Views
def _announcement_stats(queryset):
    """The four figures above the table.

    Counted here rather than in the template. The template was reaching for
    `|dictsort:"is_active"|slice:":1"`, which does not count anything -- it sorts
    the page and hands back a one-item *list*, so all three cards rendered as
    `[<Announcement: Announcement object (1)>]` instead of a number. Two of them
    were the same expression as well, so Active and Inactive could never have
    disagreed even had it worked.
    """
    active = queryset.filter(is_active=True).count()
    total = queryset.count()
    return {
        'total_count': total,
        'active_count': active,
        'inactive_count': total - active,
        'today_count': queryset.filter(created_at__date=timezone.localdate()).count(),
    }


@admin_only_required
def announcement_management(request):
    announcements_queryset = Announcement.objects.select_related('posted_by').order_by('-created_at')
    
    if request.method == 'POST':
        title = request.POST.get('title', '').strip()
        message = request.POST.get('message', '').strip()
        
        if not all([title, message]):
            error = 'Title and message are required.'
            context = {'announcements': announcements_queryset, 'error': error}
            context.update(_announcement_stats(announcements_queryset))
            return render(request, 'admin/announcementadmin.html', context)
        
        admin_id = request.session.get('admin_id')
        admin = User.objects.filter(admin_id=admin_id).first()
        
        announcement = Announcement.objects.create(
            posted_by=admin,
            title=title,
            message=message,
            is_active=True
        )
        log_admin_action(request, 'Create', 'Announcement', announcement.announcement_id, f'Posted "{title}"')

        # Optionally email the announcement to all active patrons. One SMTP
        # connection for the whole broadcast — reconnecting per patron costs
        # about a second each and would stall the request.
        if request.POST.get('email_patrons'):
            recipients = list(
                Patron.objects.filter(account_status='Active').exclude(email='')
            )
            sent = 0
            connection = bulk_connection()
            try:
                for p in recipients:
                    if announcement_email(p, announcement, connection=connection):
                        sent += 1
            finally:
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass

            failed = len(recipients) - sent
            log_admin_action(request, 'Notify', 'Announcement', announcement.announcement_id,
                             f'Emailed "{title}" to {sent} of {len(recipients)} patron(s)')
            if not recipients:
                messages.warning(request, 'Announcement posted. No active patrons had an email address.')
            elif failed:
                messages.warning(
                    request,
                    f'Announcement posted and emailed to {sent} of {len(recipients)} patron(s) — '
                    f'{failed} failed to send. Check the server log for the reason.'
                )
            else:
                messages.success(request, f'Announcement posted and emailed to {sent} patron(s).')
        else:
            messages.success(request, 'Announcement posted.')

        return redirect('announcement_management')
    
    # Pagination
    page_number = request.GET.get('page', 1)
    paginator = Paginator(announcements_queryset, 10)  # 10 announcements per page
    announcements = paginator.get_page(page_number)
    
    
    context = {
        'announcements': announcements,
        'paginator': paginator,
    }
    context.update(_announcement_stats(announcements_queryset))
    return render(request, 'admin/announcementadmin.html', context)


@admin_only_required
def toggle_announcement(request):
    if request.method == 'POST':
        announcement_id = request.POST.get('announcement_id')
        announcement = Announcement.objects.filter(announcement_id=announcement_id).first()
        if announcement:
            announcement.is_active = not announcement.is_active
            announcement.save()
            state = 'activated' if announcement.is_active else 'deactivated'
            log_admin_action(request, 'Update', 'Announcement', announcement.announcement_id,
                             f'"{announcement.title}" {state}')

    return redirect('announcement_management')


@admin_only_required
def delete_announcement(request):
    if request.method == 'POST':
        announcement_id = request.POST.get('announcement_id')
        announcement = Announcement.objects.filter(announcement_id=announcement_id).first()
        if announcement:
            title = announcement.title
            announcement.delete()
            log_admin_action(request, 'Delete', 'Announcement', announcement_id, f'Deleted "{title}"')

    return redirect('announcement_management')


# Floor Plan Management Views
@admin_only_required
def floorplan_management(request):
    floorplans = FloorPlan.objects.prefetch_related('room_set__shelf_set__shelflevel_set').order_by('-uploaded_at')

    if request.method == 'POST':
        # A floor plan is a blank vector canvas — the Administrator draws the
        # rooms on it. No image is uploaded.
        name = (request.POST.get('name') or '').strip()
        if not name:
            error = 'Floor plan name is required.'
            return render(request, 'admin/floorplanadmin.html', {'floorplans': floorplans, 'error': error})

        try:
            canvas_width = float(request.POST.get('canvas_width') or 1000)
            canvas_height = float(request.POST.get('canvas_height') or 800)
        except (TypeError, ValueError):
            error = 'Canvas width and height must be numbers.'
            return render(request, 'admin/floorplanadmin.html', {'floorplans': floorplans, 'error': error})

        if not (100 <= canvas_width <= 10000) or not (100 <= canvas_height <= 10000):
            error = 'Canvas width and height must be between 100 and 10000.'
            return render(request, 'admin/floorplanadmin.html', {'floorplans': floorplans, 'error': error})

        # Optional at creation; BLE positioning stays disabled until it is set.
        raw_scale = (request.POST.get('pixels_per_meter') or '').strip()
        pixels_per_meter = None
        if raw_scale:
            try:
                pixels_per_meter = float(raw_scale)
            except (TypeError, ValueError):
                error = 'Scale must be a number.'
                return render(request, 'admin/floorplanadmin.html', {'floorplans': floorplans, 'error': error})
            if not (0.1 <= pixels_per_meter <= 1000):
                error = 'Scale must be between 0.1 and 1000 canvas units per metre.'
                return render(request, 'admin/floorplanadmin.html', {'floorplans': floorplans, 'error': error})

        FloorPlan.objects.create(
            name=name,
            canvas_width=canvas_width,
            canvas_height=canvas_height,
            pixels_per_meter=pixels_per_meter,
            is_active=False,
        )

        return redirect('floorplan_management')

    # Which plan the map editor opens on: ?plan=<id>, else the live one, else
    # the newest. A draft can be drawn in full before it is made active.
    selected = None
    requested = request.GET.get('plan')
    if requested:
        try:
            selected = floorplans.filter(floor_plan_id=int(requested)).first()
        except (TypeError, ValueError):
            selected = None
    if selected is None:
        selected = floorplans.filter(is_active=True).first() or floorplans.first()

    return render(request, 'admin/floorplanadmin.html', {
        'floorplans': floorplans,
        'selected_plan_id': selected.floor_plan_id if selected else None,
        'active_count': floorplans.filter(is_active=True).count(),
        'renovation_count': floorplans.exclude(
            Q(renovation_notice__isnull=True) | Q(renovation_notice='')
        ).count(),
    })


@admin_only_required
def set_active_floorplan(request):
    if request.method == 'POST':
        floorplan_id = request.POST.get('floorplan_id')
        floorplan = FloorPlan.objects.filter(floor_plan_id=floorplan_id).first()
        if floorplan:
            # A plain toggle. This used to deactivate every other plan, on the
            # assumption that exactly one floor existed -- which meant putting an
            # upper floor into service took the ground floor's books out of the
            # catalogue. Floors are independent now.
            floorplan.is_active = not floorplan.is_active
            floorplan.save(update_fields=['is_active'])

    return redirect('floorplan_management')


@admin_only_required
def set_floorplan_scale(request):
    """Set how many canvas units represent one real-world metre.

    BLE path-loss gives distances in metres while every stored coordinate is in
    canvas units; this is the conversion between them. Without it the client
    disables trilateration instead of silently misplacing the patron.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    plan = FloorPlan.objects.filter(floor_plan_id=request.POST.get('floorplan_id')).first()
    if plan is None:
        return JsonResponse({'success': False, 'error': 'Floor plan not found'})

    raw = (request.POST.get('pixels_per_meter') or '').strip()
    if raw == '':
        plan.pixels_per_meter = None
        plan.save(update_fields=['pixels_per_meter'])
        log_admin_action(request, 'Update', 'FloorPlan', plan.floor_plan_id,
                         'Cleared the map scale - BLE positioning disabled')
        return JsonResponse({'success': True, 'pixels_per_meter': None})

    try:
        scale = float(raw)
    except (TypeError, ValueError):
        return JsonResponse({'success': False, 'error': 'Scale must be a number'})
    if not (0.1 <= scale <= 1000):
        return JsonResponse({'success': False,
                             'error': 'Scale must be between 0.1 and 1000 canvas units per metre'})

    plan.pixels_per_meter = scale
    plan.save(update_fields=['pixels_per_meter'])
    log_admin_action(request, 'Update', 'FloorPlan', plan.floor_plan_id,
                     f'Map scale set to {scale:g} canvas units per metre')
    return JsonResponse({'success': True, 'pixels_per_meter': scale})


@admin_only_required
def set_floorplan_floor_number(request):
    """Which storey a plan represents.

    Backfilled by creation order when multi-floor support landed, which is a
    guess -- the order plans were drawn in is not the order they are stacked in.
    This is how an Administrator corrects it. The number orders the patron's
    floor switcher and names each entry ("2nd floor"), so it is the one thing
    that has to match the building.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    plan = FloorPlan.objects.filter(floor_plan_id=request.POST.get('floorplan_id')).first()
    if plan is None:
        return JsonResponse({'success': False, 'error': 'Floor plan not found'})

    try:
        number = int((request.POST.get('floor_number') or '').strip())
    except (TypeError, ValueError):
        return JsonResponse({'success': False, 'error': 'Floor number must be a whole number.'})
    if not (-5 <= number <= 100):
        return JsonResponse({'success': False,
                             'error': 'Floor number should be between -5 (basements) and 100.'})

    before = plan.floor_number
    plan.floor_number = number
    plan.save(update_fields=['floor_number'])
    log_admin_action(request, 'Update', 'FloorPlan', plan.floor_plan_id,
                     f'{plan.name}: floor number {before} -> {number}')
    return JsonResponse({'success': True, 'floor_number': number, 'label': plan.floor_label})


@admin_only_required
def set_floorplan_north(request):
    """Record how far the plan's "up" is from magnetic north.

    Dead reckoning turns a compass bearing into a direction on this map, so an
    unmeasured offset does not degrade the result gracefully -- it sends the
    marker off at a fixed angle to wherever the patron actually walked. Zero is
    only correct if the plan happens to have been drawn with north at the top.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    plan = FloorPlan.objects.filter(floor_plan_id=request.POST.get('floorplan_id')).first()
    if plan is None:
        return JsonResponse({'success': False, 'error': 'Floor plan not found'})

    try:
        offset = float((request.POST.get('north_offset_deg') or '').strip())
    except (TypeError, ValueError):
        return JsonResponse({'success': False, 'error': 'Offset must be a number of degrees'})

    offset = offset % 360        # a bearing, so 370 and -350 both mean 10
    plan.north_offset_deg = offset
    plan.save(update_fields=['north_offset_deg'])
    log_admin_action(request, 'Update', 'FloorPlan', plan.floor_plan_id,
                     f'Map north offset set to {offset:.1f} degrees')
    return JsonResponse({'success': True, 'north_offset_deg': offset})


@admin_only_required
def toggle_renovation(request):
    if request.method == 'POST':
        floorplan_id = request.POST.get('floorplan_id')
        renovation_notice = request.POST.get('renovation_notice', '').strip()
        renovation_message = request.POST.get('renovation_message', '').strip()
        
        floorplan = FloorPlan.objects.filter(floor_plan_id=floorplan_id).first()
        if floorplan:
            floorplan.renovation_notice = renovation_notice if renovation_notice else None
            floorplan.renovation_message = renovation_message if renovation_message else None
            floorplan.save()
    
    return redirect('floorplan_management')


@admin_only_required
def delete_floorplan(request):
    if request.method == 'POST':
        floorplan_id = request.POST.get('floorplan_id')
        FloorPlan.objects.filter(floor_plan_id=floorplan_id).delete()
    
    return redirect('floorplan_management')


# Transaction Import/Export
@granted_module_required('transactions')
def download_transaction_template(request):
    wb = Workbook()
    ws = wb.active
    ws.title = "Transaction Import Template"
    
    headers = ['patron_id', 'book_id', 'transaction_type', 'transaction_date', 'due_date', 'return_date']
    ws.append(headers)
    
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename=transaction_import_template.xlsx'
    wb.save(response)
    return response


@granted_module_required('transactions')
def import_transactions(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})
    
    if 'excel_file' not in request.FILES:
        return JsonResponse({'success': False, 'error': 'No file uploaded'})
    
    excel_file = request.FILES['excel_file']

    # Name, size and magic bytes, not just the extension.
    upload_error = check_import_upload(excel_file)
    if upload_error:
        return JsonResponse({'success': False, 'error': upload_error})
    
    try:
        wb = openpyxl.load_workbook(excel_file)
        ws = wb.active
        size_error = check_import_size(ws)
        if size_error:
            return JsonResponse({'success': False, 'error': size_error})
        
        imported_count = 0
        skipped_count = 0
        
        admin_id = request.session.get('admin_id')
        admin = User.objects.filter(admin_id=admin_id).first()
        
        for row in ws.iter_rows(min_row=2):
            patron_id = row[0].value
            book_id = row[1].value
            transaction_type = row[2].value
            transaction_date = row[3].value
            due_date = row[4].value
            return_date = row[5].value
            
            if not book_id or not transaction_type:
                continue
            
            patron = None
            if patron_id:
                patron = Patron.objects.filter(patron_id=patron_id).first()
            
            book = Book.objects.filter(book_id=book_id).first()
            if not book:
                skipped_count += 1
                continue
            
            # Parse dates
            from datetime import datetime
            trans_date = None
            if transaction_date:
                if isinstance(transaction_date, datetime):
                    trans_date = transaction_date.date()
                else:
                    trans_date = datetime.strptime(str(transaction_date), '%Y-%m-%d').date()
            
            due_d = None
            if due_date:
                if isinstance(due_date, datetime):
                    due_d = due_date.date()
                else:
                    due_d = datetime.strptime(str(due_date), '%Y-%m-%d').date()
            
            ret_date = None
            if return_date:
                if isinstance(return_date, datetime):
                    ret_date = return_date.date()
                else:
                    ret_date = datetime.strptime(str(return_date), '%Y-%m-%d').date()
            
            Transaction.objects.create(
                patron=patron,
                book=book,
                processed_by=admin,
                transaction_type=transaction_type,
                transaction_date=trans_date or timezone.localdate(),
                due_date=due_d,
                return_date=ret_date
            )
            
            imported_count += 1
        
        return JsonResponse({
            'success': True,
            'message': f'Successfully imported {imported_count} transactions. Skipped {skipped_count} invalid entries.'
        })
        
    except Exception as exc:
        return import_failed('Transaction import', exc)


# Log Import/Export
@admin_login_required
def download_log_template(request):
    wb = Workbook()
    ws = wb.active
    ws.title = "Log Import Template"
    
    headers = ['patron_id', 'school', 'purpose_of_visit', 'entry_time', 'exit_time']
    ws.append(headers)

    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename=log_import_template.xlsx'
    wb.save(response)
    return response


@admin_login_required
def import_logs(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})
    
    if 'excel_file' not in request.FILES:
        return JsonResponse({'success': False, 'error': 'No file uploaded'})
    
    excel_file = request.FILES['excel_file']

    # Name, size and magic bytes, not just the extension.
    upload_error = check_import_upload(excel_file)
    if upload_error:
        return JsonResponse({'success': False, 'error': upload_error})
    
    try:
        wb = openpyxl.load_workbook(excel_file)
        ws = wb.active
        size_error = check_import_size(ws)
        if size_error:
            return JsonResponse({'success': False, 'error': size_error})
        
        imported_count = 0
        skipped_count = 0
        
        from datetime import datetime

        def _parse_dt(value):
            if not value:
                return None
            if isinstance(value, datetime):
                return value
            return datetime.strptime(str(value), '%Y-%m-%d %H:%M:%S')

        for row in ws.iter_rows(min_row=2):
            patron_id = row[0].value
            school = row[1].value
            purpose = row[2].value
            entry_time = row[3].value
            exit_time = row[4].value

            if not patron_id:
                continue

            patron = Patron.objects.filter(patron_id=patron_id).first()
            if not patron:
                skipped_count += 1
                continue

            PatronLog.objects.create(
                patron=patron,
                school=school or None,
                purpose_of_visit=purpose or None,
                entry_time=_parse_dt(entry_time) or timezone.now(),
                exit_time=_parse_dt(exit_time),
            )

            imported_count += 1
        
        return JsonResponse({
            'success': True,
            'message': f'Successfully imported {imported_count} logs. Skipped {skipped_count} invalid entries.'
        })
        
    except Exception as exc:
        return import_failed('Log import', exc)


# ─── REPORTS VIEWS ───────────────────────────────────────────────
@admin_only_required
def admin_reports(request):
    """Reports hub: builds the selected report only when Generate is pressed.

    Opening the page used to run a report immediately, which meant every visit
    paid for a query nobody had asked for and the screen filled with a default
    nobody chose. The report is now built only when the form is submitted, so
    landing here is free and what you see is always something you asked for.
    """
    report_type = request.GET.get('type', 'transactions')
    if report_type not in dict(REPORT_TYPES):
        report_type = 'transactions'
    start, end = parse_date_range(request.GET.get('start'), request.GET.get('end'))

    # The form carries generate=1; a bare visit to the page does not.
    generated = bool(request.GET.get('generate'))
    report = build_report(report_type, start, end) if generated else None


    context = {
        'report': report,
        'generated': generated,
        'report_types': REPORT_TYPES,
        'selected_type': report_type,
        'start_date': start.strftime('%Y-%m-%d'),
        'end_date': end.strftime('%Y-%m-%d'),
        # Was a hardcoded {'books', 'patrons'}, which left the date range showing
        # on Stock Levels -- a point-in-time count that ignores it. reports.py
        # already names the set; there is no reason for a second, staler copy.
        'is_snapshot': report_type in SNAPSHOT_REPORTS,
        'snapshot_types_json': json.dumps(sorted(SNAPSHOT_REPORTS)),
    }
    return render(request, 'admin/reports.html', context)


@admin_only_required
def admin_report_pdf(request):
    """Download the selected report as a PDF."""
    report_type = request.GET.get('type', 'transactions')
    if report_type not in dict(REPORT_TYPES):
        report_type = 'transactions'
    start, end = parse_date_range(request.GET.get('start'), request.GET.get('end'))
    report = build_report(report_type, start, end)

    buf = render_report_pdf(report)
    log_admin_action(request, 'Export', 'Report',
                     detail=f"{report['title']} PDF ({report['period_label']})")

    response = HttpResponse(buf, content_type='application/pdf')
    filename = f"{report_type}_report_{start}_{end}.pdf"
    response['Content-Disposition'] = f'attachment; filename={filename}'
    return response


@admin_only_required
def admin_report_excel(request):
    """Download the selected report as an Excel (.xlsx) file."""
    report_type = request.GET.get('type', 'transactions')
    if report_type not in dict(REPORT_TYPES):
        report_type = 'transactions'
    start, end = parse_date_range(request.GET.get('start'), request.GET.get('end'))
    report = build_report(report_type, start, end)

    buf = render_report_excel(report)
    log_admin_action(request, 'Export', 'Report',
                     detail=f"{report['title']} Excel ({report['period_label']})")

    response = HttpResponse(
        buf,
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    filename = f"{report_type}_report_{start}_{end}.xlsx"
    response['Content-Disposition'] = f'attachment; filename={filename}'
    return response


# ─── BORROWING RULES (SETTINGS) ──────────────────────────────────
@admin_only_required
def admin_borrowing_rules(request):
    """View/edit the library-wide borrowing policy."""
    rule = BorrowingRule.current()
    saved = False
    error = None

    if request.method == 'POST':
        try:
            loan = int(request.POST.get('loan_period_days', rule.loan_period_days))
            maxb = int(request.POST.get('max_books_per_patron', rule.max_books_per_patron))
            grace = int(request.POST.get('grace_period_days', rule.grace_period_days))
            fine = Decimal(request.POST.get('fine_per_day') or '0')
            lost = Decimal(request.POST.get('lost_book_fee') or '0')

            if loan < 1 or maxb < 1:
                error = 'Loan period and borrowing limit must be at least 1.'
            elif grace < 0 or fine < 0 or lost < 0:
                error = 'Grace period and fees cannot be negative.'
            else:
                rule.loan_period_days = loan
                rule.max_books_per_patron = maxb
                rule.grace_period_days = grace
                rule.fine_per_day = fine
                rule.lost_book_fee = lost
                rule.save()
                log_admin_action(request, 'Update', 'BorrowingRule', rule.rule_id,
                                 f'loan {loan}d, max {maxb}, fine {fine}/day, grace {grace}d')
                saved = True
        except (ValueError, InvalidOperation):
            error = 'Please enter valid numeric values.'

    return render(request, 'admin/borrowingrules.html', {
        'rule': rule,
        'saved': saved,
        'error': error,
    })


# ─── SHELF MANAGEMENT VIEWS ──────────────────────────────────────
def _shelf_page(request, template):
    """IDE-style hierarchical manager for shelves, sections, levels and books."""
    return render(request, template, {
    })


@admin_only_required
def position_test(request):
    """Check positioning against the real beacons, from the Administrator's side.

    The patron map only ever scans continuously, because the alternative --
    admitting each beacon through the browser's device chooser -- means putting
    library hardware in front of a reader, which is not theirs to handle. That
    leaves whoever installs the beacons no way to tell a bad calibration from a
    browser that cannot scan, since both look like a map that never moves.

    This is that missing instrument. It runs the same decoding and the same
    trilateration as the patron map, but admits beacons the way a setup tool
    may, so the arithmetic can be verified on hardware the patron map cannot
    use. Nothing here is reachable from a patron session.
    """
    return render(request, 'admin/positiontest.html')


@admin_only_required
def shelf_manager(request):
    return _shelf_page(request, 'admin/shelfmanager.html')


@module_required('shelf')
def staff_shelf(request):
    return _shelf_page(request, 'library_staff/shelfmanager.html')


@admin_login_required
# Deliberately admin_login_required rather than one module: this is a
# read-only book search shared by the shelf-placement picker and the
# Transactions page's book search, and it discloses nothing a staff member
# couldn't already see on Manage Books. Gating it to 'shelf' alone (as it
# briefly was) broke book search for any account holding 'transactions'
# without also holding 'shelf' -- the page returned a login redirect where
# the search box expected JSON, so results silently failed to appear.
def get_books_for_placement(request):
    """Searchable list of books, for the shelf-placement picker and Transactions."""
    q = (request.GET.get('q') or '').strip()
    books = Book.objects.select_related('shelf_level').order_by('title')
    if q:
        books = books.filter(_book_search_q(q))
    data = []
    for b in books[:300]:
        if b.shelf_level:
            location = b.shelf_level.category or f'Level {b.shelf_level.level_number}'
        else:
            location = ''
        data.append({
            'book_id': b.book_id,
            'title': b.title,
            'author': b.author,
            'status': b.status,
            'shelf_level_id': b.shelf_level_id,
            'location': location,
        })
    return JsonResponse({'success': True, 'books': data})


def _shelf_capacity():
    """Every shelf level with how many books already sit on it.

    Shown beside the receiving form so whoever is unpacking a box can see where
    it can go without leaving the page and losing what they have typed. Emptiest
    first, because that is the question being asked -- where is there room.

    There is no capacity field on ShelfLevel, so "room" is relative rather than
    absolute: the counts rank the levels, they do not claim a level is full.
    """
    levels = (ShelfLevel.objects
              .select_related('shelf', 'shelf__room')
              .annotate(book_count=Count('book'))
              .order_by('book_count', 'shelf__name', 'level_number'))
    return [
        {
            'shelf_level_id': lv.shelf_level_id,
            'shelf': lv.shelf.name if lv.shelf else 'Unplaced',
            'room': lv.shelf.room.name if (lv.shelf and lv.shelf.room) else '',
            'level_number': lv.level_number,
            'category': lv.category or '',
            'book_count': lv.book_count,
        }
        for lv in levels
    ]


@admin_or_module_required('shelf')
def assign_books_to_level(request):
    """Place the selected books onto a shelf level."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    level = ShelfLevel.objects.filter(
        shelf_level_id=request.POST.get('shelf_level_id')
    ).first()
    if level is None:
        return JsonResponse({'success': False, 'error': 'Shelf level not found'})

    raw = request.POST.get('book_ids', '')
    ids = [int(x) for x in raw.split(',') if x.strip().isdigit()]
    if not ids:
        return JsonResponse({'success': False, 'error': 'No books selected'})

    updated = Book.objects.filter(book_id__in=ids).update(shelf_level=level)
    return JsonResponse({'success': True, 'updated': updated})


@admin_or_module_required('shelf')
def reorder_books_on_level(request):
    """Set the order of the books on one level, moving any that arrived.

    Slots are rewritten as 1..N over the order given, which is what makes them
    an ordering key rather than a claim about physical spaces: a tidy-up is one
    drag instead of editing every book, and gaps left by the old numbering stop
    mattering because nothing reads them as spaces.

    Books dragged in from another level are reassigned on the way, so moving a
    copy and reordering it are one action rather than two.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    level = ShelfLevel.objects.filter(
        shelf_level_id=request.POST.get('shelf_level_id')).first()
    if level is None:
        return JsonResponse({'success': False, 'error': 'Shelf level not found'})

    raw = request.POST.get('book_ids', '')
    ids = [int(x) for x in raw.split(',') if x.strip().isdigit()]
    if not ids:
        return JsonResponse({'success': False, 'error': 'No books in the new order'})

    # One query, then work in memory: an ordering is only meaningful as a whole,
    # so a half-applied one is worse than none.
    books = {b.book_id: b for b in Book.objects.filter(book_id__in=ids)}
    missing = [i for i in ids if i not in books]
    if missing:
        return JsonResponse({'success': False,
                             'error': 'Some of those books no longer exist'})

    moved = 0
    with transaction.atomic():
        for position, book_id in enumerate(ids, start=1):
            book = books[book_id]
            changed = []
            if book.shelf_level_id != level.shelf_level_id:
                book.shelf_level = level
                changed.append('shelf_level')
                moved += 1
            if book.shelf_slot != position:
                book.shelf_slot = position
                changed.append('shelf_slot')
            if changed:
                book.save(update_fields=changed)

    detail = f'Reordered {len(ids)} book(s) on {level.shelf.name if level.shelf else "a shelf"} {level.label}'
    if moved:
        detail += f'; {moved} moved here from elsewhere'
    log_admin_action(request, 'Update', 'ShelfLevel', level.shelf_level_id, detail)
    return JsonResponse({'success': True, 'ordered': len(ids), 'moved': moved})


@admin_or_module_required('shelf')
def get_shelf_tree(request):
    """Returns full nested hierarchy FloorPlan > Room > Shelf > ShelfLevel > Books"""
    # Ordered by storey. More than one floor can be in service now, so the tree
    # has several roots where it used to have one -- ground floor first is the
    # only order that reads correctly.
    floor_plans = FloorPlan.objects.filter(is_active=True).order_by(
        'floor_number', 'floor_plan_id'
    ).prefetch_related(
        'room_set__shelf_set__shelflevel_set__book_set'
    )

    tree_data = []
    for fp in floor_plans:
        # Named, not numbered. "Floor Plan 16" gave no clue which storey it was
        # or why it might be empty; "2nd floor - 2nd floor" does.
        label = fp.floor_label
        if fp.name and fp.name.strip().lower() != label.lower():
            label = f'{label} — {fp.name}'
        fp_node = {
            'type': 'floorplan',
            'id': fp.floor_plan_id,
            'name': label,
            'floor_number': fp.floor_number,
            'is_active': fp.is_active,
            'children': []
        }
        
        for room in fp.room_set.all():
            room_node = {
                'type': 'room',
                'id': room.room_id,
                'name': room.name,
                'map_x': room.map_x,
                'map_y': room.map_y,
                'description': room.description,
                'is_active': room.is_active,
                'children': []
            }
            
            for shelf in room.shelf_set.all():
                shelf_node = {
                    'type': 'shelf',
                    'id': shelf.shelf_id,
                    'name': shelf.name,
                    'map_x': shelf.map_x,
                    'map_y': shelf.map_y,
                    'description': shelf.description,
                    'is_active': shelf.is_active,
                    'children': []
                }
                
                for shelf_level in shelf.shelflevel_set.all():
                    books = shelf_level.book_set.order_by(
                        F('shelf_slot').asc(nulls_last=True), 'title', 'book_id')
                    book_count = books.count()
                    shelf_level_node = {
                        'type': 'shelflevel',
                        'id': shelf_level.shelf_level_id,
                        'name': f'Level {shelf_level.level_number}',
                        'category': shelf_level.category or '',
                        'level_number': shelf_level.level_number,
                        'is_active': shelf_level.is_active,
                        'book_count': book_count,
                        'children': []
                    }

                    for book in books:
                        book_node = {
                            'type': 'book',
                            'id': book.book_id,
                            'name': book.title,
                            'author': book.author,
                            'status': book.status
                        }
                        shelf_level_node['children'].append(book_node)

                    shelf_node['children'].append(shelf_level_node)

                room_node['children'].append(shelf_node)
            
            fp_node['children'].append(room_node)
        
        tree_data.append(fp_node)
    
    return JsonResponse({'tree': tree_data})


@admin_or_module_required('shelf')
def get_shelf_levels_flat(request):
    """Returns flattened list of all ShelfLevels with breadcrumb path"""
    shelf_levels = ShelfLevel.objects.select_related(
        'shelf__room__floor_plan'
    ).all()

    flat_data = []
    for sl in shelf_levels:
        shelf = sl.shelf
        room = shelf.room if shelf else None
        floor_plan = room.floor_plan if room else None
        path_parts = []
        if floor_plan:
            path_parts.append(f'Floor Plan {floor_plan.floor_plan_id}')
        if room:
            path_parts.append(room.name)
        if shelf:
            path_parts.append(shelf.name)

        path = ' / '.join(path_parts)
        book_count = sl.book_set.count()

        flat_data.append({
            'id': sl.shelf_level_id,
            'path': path,
            'category': sl.category or f'Level {sl.level_number}',
            'level_number': sl.level_number,
            'book_count': book_count,
            'is_active': sl.is_active,
            'shelf_id': shelf.shelf_id if shelf else None,
            'room_id': room.room_id if room else None,
            'floor_plan_id': floor_plan.floor_plan_id if floor_plan else None
        })

    return JsonResponse({'shelf_levels': flat_data})


@admin_or_module_required('shelf')
def add_room(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})
    
    floor_plan_id = request.POST.get('floor_plan_id')
    name = request.POST.get('name')
    map_x = request.POST.get('map_x', 0)
    map_y = request.POST.get('map_y', 0)
    description = request.POST.get('description', '')

    if not floor_plan_id or not name:
        return JsonResponse({'success': False, 'error': 'floor_plan_id and name are required'})

    # Rooms are drawn as polygons on the vector canvas; the centroid becomes the
    # label anchor so map_x/map_y stay meaningful for everything that reads them.
    geometry, geo_error = _parse_geometry(request.POST.get('geometry'))
    if geo_error:
        return JsonResponse({'success': False, 'error': geo_error})
    if geometry:
        map_x, map_y = _polygon_centroid(geometry)

    try:
        floor_plan = FloorPlan.objects.get(floor_plan_id=floor_plan_id)
        room = Room.objects.create(
            floor_plan=floor_plan,
            name=name,
            geometry=geometry,
            map_x=float(map_x) if map_x else 0,
            map_y=float(map_y) if map_y else 0,
            description=description
        )
        return JsonResponse({'success': True, 'room_id': room.room_id, 'geometry': room.geometry})
    except FloorPlan.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Floor plan not found'})


# Gated like the rest of the Shelf Manager family rather than Administrator-only.
# add_room, add/edit/delete_shelf and the shelf-level operations all use
# admin_or_module_required('shelf'); these two were left behind when the module
# system came in, which let a Library Staff member create a room and then fail
# to rename or remove it -- with both buttons sitting right there in their UI.
@admin_or_module_required('shelf')
def edit_room(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})
    
    room_id = request.POST.get('room_id')
    name = request.POST.get('name')
    map_x = request.POST.get('map_x')
    map_y = request.POST.get('map_y')
    description = request.POST.get('description')
    
    if not room_id:
        return JsonResponse({'success': False, 'error': 'room_id is required'})

    # Reshaping a room replaces its polygon only — neighbouring rooms are
    # independent shapes and are never touched.
    geometry, geo_error = _parse_geometry(request.POST.get('geometry'))
    if geo_error:
        return JsonResponse({'success': False, 'error': geo_error})

    try:
        room = Room.objects.get(room_id=room_id)
        if name:
            room.name = name
        if geometry:
            room.geometry = geometry
            room.map_x, room.map_y = _polygon_centroid(geometry)
        else:
            if map_x is not None:
                room.map_x = float(map_x)
            if map_y is not None:
                room.map_y = float(map_y)
        if description is not None:
            room.description = description
        room.save()
        # A door is a hole in a particular wall. Move the wall and the door is
        # no longer in it -- it sits in mid-air, still drawn, still routable,
        # simply wrong. Nothing used to correct that: the editor claimed the
        # doors were "re-snapped server-side on next load", but no code ever
        # did it, so a reshaped room left its doors behind for good.
        moved = _resnap_room_doors(room) if geometry else []
        return JsonResponse({'success': True, 'geometry': room.geometry,
                             'doors_moved': moved})
    except Room.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Room not found'})


def _resnap_room_doors(room):
    """Pull every door of `room` back onto its (possibly new) outline.

    Returns the ones that actually moved, so the editor can say so rather than
    silently rearranging the plan. "Nearest edge" is a guess when a room has
    been reshaped heavily -- a door can land on a different wall than intended
    -- which is exactly why it is reported instead of done quietly.
    """
    if not room.geometry or len(room.geometry) < 3:
        return []
    moved = []
    for door in Door.objects.filter(room=room):
        x, y, bearing = _snap_to_polygon_edge(room.geometry, door.map_x, door.map_y)
        shift = math.hypot(x - door.map_x, y - door.map_y)
        if shift < 0.5 and abs((bearing - (door.rotation or 0)) % 360) < 0.5:
            continue
        door.map_x, door.map_y, door.rotation = x, y, bearing
        updates = ['map_x', 'map_y', 'rotation']
        # The far room was true of the old wall. Once the door has moved, that
        # claim has to be re-earned rather than carried along.
        unlinked = ''
        if door.room_b_id:
            still = _facing_room(room.floor_plan, x, y, room.room_id)
            if still is None or still.room_id != door.room_b_id:
                unlinked = door.room_b.name
                door.room_b = None
                updates.append('room_b')
        door.save(update_fields=updates)
        moved.append({'door_id': door.door_id,
                      'label': door.label or 'Door',
                      'moved_by': round(shift, 1),
                      'unlinked_from': unlinked})
    return moved


# Module-gated to match add_room and the shelf/level operations -- see edit_room.
@admin_or_module_required('shelf')
def delete_room(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})
    
    room_id = request.POST.get('room_id')
    if not room_id:
        return JsonResponse({'success': False, 'error': 'room_id is required'})
    
    Room.objects.filter(room_id=room_id).delete()
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'success': True})
    return redirect('floorplan_management')


# ─── Stairways / lifts ────────────────────────────────────────────
# The only objects on a plan that mean something on another floor, so they are
# also what cross-floor guidance is built on -- see get_navigation_route.

@admin_or_module_required('shelf')
def add_stairway(request):
    """Save a drawn staircase, lift or ramp, and what it connects to."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    floor_plan = FloorPlan.objects.filter(
        floor_plan_id=request.POST.get('floor_plan_id')).first()
    if floor_plan is None:
        return JsonResponse({'success': False, 'error': 'Floor plan not found'})

    geometry, geo_error = _parse_geometry(request.POST.get('geometry'))
    if geo_error:
        return JsonResponse({'success': False, 'error': geo_error})
    if not geometry:
        return JsonResponse({'success': False, 'error': 'Draw the shape first.'})

    kind = (request.POST.get('kind') or 'Stairs').strip()
    if kind not in dict(Stairway.KIND_CHOICES):
        kind = 'Stairs'
    direction = (request.POST.get('direction') or 'both').strip()
    if direction not in dict(Stairway.DIRECTION_CHOICES):
        direction = 'both'

    connects_to = None
    raw_to = (request.POST.get('connects_to') or '').strip()
    if raw_to.isdigit():
        connects_to = FloorPlan.objects.filter(floor_plan_id=int(raw_to)).first()
        # A stair that arrives back where it started is a drawing mistake, and
        # would make the cross-floor hint advise walking to the floor you are on.
        if connects_to and connects_to.floor_plan_id == floor_plan.floor_plan_id:
            return JsonResponse({'success': False,
                                 'error': 'A stairway cannot connect a floor to itself.'})

    try:
        bearing = float(request.POST.get('bearing') or 0) % 360
    except (TypeError, ValueError):
        bearing = 0

    map_x, map_y = _polygon_centroid(geometry)
    stairway = Stairway.objects.create(
        floor_plan=floor_plan, kind=kind, direction=direction,
        name=(request.POST.get('name') or '').strip()[:255] or None,
        geometry=geometry, map_x=map_x, map_y=map_y,
        bearing=bearing, connects_to=connects_to,
    )
    log_admin_action(request, 'Create', 'Stairway', stairway.stairway_id,
                     f'Added {stairway.label} on {floor_plan.floor_label}'
                     + (f' to {stairway.destination_label}' if connects_to else ''))
    return JsonResponse({'success': True, 'stairway': _stairway_payload(stairway)})


@admin_or_module_required('shelf')
def edit_stairway(request):
    """Rename a stairway, turn it, or change which floor it reaches."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    st = Stairway.objects.filter(stairway_id=request.POST.get('stairway_id')).first()
    if st is None:
        return JsonResponse({'success': False, 'error': 'Stairway not found'})

    fields = []
    if 'kind' in request.POST and request.POST['kind'] in dict(Stairway.KIND_CHOICES):
        st.kind = request.POST['kind']; fields.append('kind')
    if 'direction' in request.POST and request.POST['direction'] in dict(Stairway.DIRECTION_CHOICES):
        st.direction = request.POST['direction']; fields.append('direction')
    if 'name' in request.POST:
        st.name = (request.POST.get('name') or '').strip()[:255] or None
        fields.append('name')
    if 'bearing' in request.POST:
        try:
            st.bearing = float(request.POST['bearing']) % 360
            fields.append('bearing')
        except (TypeError, ValueError):
            pass
    if 'connects_to' in request.POST:
        raw = (request.POST.get('connects_to') or '').strip()
        if not raw:
            st.connects_to = None
        elif raw.isdigit():
            dest = FloorPlan.objects.filter(floor_plan_id=int(raw)).first()
            if dest and dest.floor_plan_id == st.floor_plan_id:
                return JsonResponse({'success': False,
                                     'error': 'A stairway cannot connect a floor to itself.'})
            st.connects_to = dest
        fields.append('connects_to')
    if 'is_active' in request.POST:
        st.is_active = request.POST.get('is_active') in ('1', 'true', 'True', 'on')
        fields.append('is_active')

    if fields:
        st.save(update_fields=fields)
        log_admin_action(request, 'Update', 'Stairway', st.stairway_id, f'Edited {st.label}')
    return JsonResponse({'success': True, 'stairway': _stairway_payload(st)})


@admin_or_module_required('shelf')
def delete_stairway(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})
    st = Stairway.objects.filter(stairway_id=request.POST.get('stairway_id')).first()
    if st is None:
        return JsonResponse({'success': False, 'error': 'Stairway not found'})
    label, sid = st.label, st.stairway_id
    st.delete()
    log_admin_action(request, 'Delete', 'Stairway', sid, f'Removed {label}')
    return JsonResponse({'success': True})


# ─── Obstacles / furniture ────────────────────────────────────────
# Same shape as the room endpoints above, because an obstacle is the same kind
# of thing to the map: a polygon with a centroid. Gated like the rest of the
# Shelf Manager family so Library Staff holding 'shelf' can maintain the layout.

@admin_or_module_required('shelf')
def add_obstacle(request):
    """Save a shape the Administrator drew for a table, counter, pillar, etc."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    floor_plan_id = request.POST.get('floor_plan_id')
    if not floor_plan_id:
        return JsonResponse({'success': False, 'error': 'floor_plan_id is required'})

    geometry, geo_error = _parse_geometry(request.POST.get('geometry'))
    if geo_error:
        return JsonResponse({'success': False, 'error': geo_error})
    if not geometry:
        return JsonResponse({'success': False, 'error': 'Draw the shape first.'})

    kind = (request.POST.get('kind') or 'Table').strip()
    if kind not in dict(Obstacle.KIND_CHOICES):
        kind = 'Other'
    name = (request.POST.get('name') or '').strip()[:255]

    floor_plan = FloorPlan.objects.filter(floor_plan_id=floor_plan_id).first()
    if floor_plan is None:
        return JsonResponse({'success': False, 'error': 'Floor plan not found'})

    map_x, map_y = _polygon_centroid(geometry)

    # Something books are kept on is not an obstacle, whatever it looks like.
    # A book points at a ShelfLevel, so anything that holds one has to be a
    # Shelf -- the alternative, letting a level hang off either parent, would
    # put every shelf_level.shelf lookup in the codebase at risk of None.
    # One drawing tool, two destinations, decided by the tick in the dialog.
    if request.POST.get('holds_books') in ('1', 'true', 'True', 'on'):
        room = (Room.objects.filter(floor_plan=floor_plan, is_active=True)
                .order_by('room_id').first())
        if room is None:
            return JsonResponse({
                'success': False,
                'error': 'Draw a room first — a table that holds books is '
                         'catalogued inside one, so it can be found.'})
        shelf = Shelf.objects.create(
            room=room, name=name or f'{dict(Obstacle.KIND_CHOICES).get(kind, kind)}',
            kind='Table' if kind in ('Table', 'Counter') else 'Display',
            geometry=geometry, map_x=map_x, map_y=map_y,
        )
        # One level, because a table has exactly one surface. Naming it Top
        # rather than Level 1 is what a patron sent there will actually see.
        ShelfLevel.objects.create(shelf=shelf, level_number=1, is_top=True,
                                  category=(request.POST.get('category') or '').strip() or None)
        log_admin_action(request, 'Create', 'Shelf', shelf.shelf_id,
                         f'Added {shelf.label} to {floor_plan.floor_label} '
                         '(drawn as furniture that holds books)')
        return JsonResponse({'success': True, 'as_shelf': True,
                             'shelf_id': shelf.shelf_id, 'name': shelf.name,
                             'reload': True})

    obstacle = Obstacle.objects.create(
        floor_plan=floor_plan, kind=kind, name=name or None,
        geometry=geometry, map_x=map_x, map_y=map_y,
    )
    log_admin_action(request, 'Create', 'Obstacle', obstacle.obstacle_id,
                     f'Added {obstacle.label} to {floor_plan.floor_label}')
    return JsonResponse({'success': True, 'obstacle': _obstacle_payload(obstacle)})


@admin_or_module_required('shelf')
def edit_obstacle(request):
    """Rename an obstacle, change what it is, or reshape it."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    obstacle = Obstacle.objects.filter(
        obstacle_id=request.POST.get('obstacle_id')).first()
    if obstacle is None:
        return JsonResponse({'success': False, 'error': 'Obstacle not found'})

    fields = []
    if 'kind' in request.POST:
        kind = (request.POST.get('kind') or '').strip()
        if kind in dict(Obstacle.KIND_CHOICES):
            obstacle.kind = kind
            fields.append('kind')
    if 'name' in request.POST:
        obstacle.name = (request.POST.get('name') or '').strip()[:255] or None
        fields.append('name')
    if 'geometry' in request.POST:
        geometry, geo_error = _parse_geometry(request.POST.get('geometry'))
        if geo_error:
            return JsonResponse({'success': False, 'error': geo_error})
        if geometry:
            obstacle.geometry = geometry
            obstacle.map_x, obstacle.map_y = _polygon_centroid(geometry)
            fields += ['geometry', 'map_x', 'map_y']
    if 'is_active' in request.POST:
        obstacle.is_active = request.POST.get('is_active') in ('1', 'true', 'True', 'on')
        fields.append('is_active')

    if fields:
        obstacle.save(update_fields=fields)
        log_admin_action(request, 'Update', 'Obstacle', obstacle.obstacle_id,
                         f'Edited {obstacle.label}')
    return JsonResponse({'success': True, 'obstacle': _obstacle_payload(obstacle)})


@admin_or_module_required('shelf')
def delete_obstacle(request):
    """Remove a drawn obstacle. Nothing else references it, so it just goes."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    obstacle = Obstacle.objects.filter(
        obstacle_id=request.POST.get('obstacle_id')).first()
    if obstacle is None:
        return JsonResponse({'success': False, 'error': 'Obstacle not found'})

    label, oid = obstacle.label, obstacle.obstacle_id
    obstacle.delete()
    log_admin_action(request, 'Delete', 'Obstacle', oid, f'Removed {label}')
    return JsonResponse({'success': True})


@admin_or_module_required('shelf')
def add_shelf(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})
    
    room_id = request.POST.get('room_id')
    name = request.POST.get('name')
    map_x = request.POST.get('map_x', 0)
    map_y = request.POST.get('map_y', 0)
    description = request.POST.get('description', '')
    
    if not room_id or not name:
        return JsonResponse({'success': False, 'error': 'room_id and name are required'})
    
    # A shelf added from Shelf Manager has no position yet: it stays unplaced
    # until the Administrator puts it on the floor plan (Fig. 48).
    try:
        x = float(map_x) if map_x not in (None, '', '0', 0) else None
        y = float(map_y) if map_y not in (None, '', '0', 0) else None
    except (TypeError, ValueError):
        x = y = None

    # Optional, and only sent when a shelf is pasted: a copy that arrives as a
    # default-sized rectangle facing north is not a copy, and would have to be
    # resized and rotated back by hand every time.
    def _num(field, fallback):
        raw = request.POST.get(field)
        try:
            return float(raw) if raw not in (None, '') else fallback
        except (TypeError, ValueError):
            return fallback

    kind = (request.POST.get('kind') or 'Shelf').strip()
    if kind not in dict(Shelf.KIND_CHOICES):
        kind = 'Shelf'

    mount = (request.POST.get('mount') or 'Floor').strip()
    if mount not in dict(Shelf.MOUNT_CHOICES):
        mount = 'Floor'
    raw_height = (request.POST.get('mount_height_m') or '').strip()
    try:
        mount_height_m = float(raw_height) if raw_height else None
    except (TypeError, ValueError):
        mount_height_m = None
    if mount_height_m is not None and not (0 <= mount_height_m <= 10):
        return JsonResponse({'success': False,
                             'error': 'Mounting height is in metres above the floor (0-10).'})

    geometry = None
    if request.POST.get('geometry'):
        geometry, geo_error = _parse_geometry(request.POST.get('geometry'))
        if geo_error:
            return JsonResponse({'success': False, 'error': geo_error})

    try:
        room = Room.objects.get(room_id=room_id)
        shelf = Shelf.objects.create(
            room=room,
            name=name,
            map_x=x,
            map_y=y,
            description=description,
            width=_num('width', 46),
            depth=_num('depth', 14),
            rotation=_num('rotation', 0) % 360,
            kind=kind,
            geometry=geometry,
            mount=mount,
            mount_height_m=mount_height_m,
        )
        # A traced shelf is positioned by its own outline, not by where the
        # click happened to land, or the label and the route target would sit
        # off the shape.
        if geometry:
            shelf.map_x, shelf.map_y = _polygon_centroid(geometry)
            shelf.save(update_fields=['map_x', 'map_y'])

        # The boards, said once here rather than one dialog at a time in Shelf
        # Manager. A shelf asked for with no levels gets none, which is what
        # placing an empty bay to fill in later means.
        levels, columns = _grid_counts(request)
        made = _build_shelf_grid(shelf, levels, columns) if levels else 0

        return JsonResponse({'success': True, 'shelf_id': shelf.shelf_id,
                             'geometry': shelf.geometry,
                             'footprint': shelf.footprint(),
                             'levels_created': made})
    except Room.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Room not found'})


# A bay of shelving is a grid: so many boards up, and sometimes divided into
# bays across. Bounded because these are physical objects -- nobody has a
# forty-level bookcase, and a typo that made four hundred rows would be a
# tedious thing to undo one row at a time.
MAX_SHELF_LEVELS = 20
MAX_SHELF_COLUMNS = 12


def _grid_counts(request):
    """The levels and columns asked for, clamped to what can physically exist."""
    def count(field, default, cap, floor):
        raw = (request.POST.get(field) or '').strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return default
        return max(floor, min(cap, value))
    # Levels may be zero -- that is how an empty bay is placed, to be filled in
    # once somebody has counted the boards. Columns cannot: a shelf that is not
    # divided still has one bay across it.
    return (count('levels', 1, MAX_SHELF_LEVELS, 0),
            count('columns', 1, MAX_SHELF_COLUMNS, 1))


def _build_shelf_grid(shelf, levels, columns):
    """Fill in the boards a shelf has, skipping any that already exist.

    Creating them one at a time is the single most tedious part of setting up
    a real bay -- five levels three bays wide is fifteen dialogs. The grid is
    the same information said once.

    Column 1 of a single-column shelf is left null rather than stored as 1, so
    a plain bookcase reads "Level 3" and not "Level 3, Column 1".
    """
    existing = set(
        (lv.level_number, lv.column_number)
        for lv in ShelfLevel.objects.filter(shelf=shelf)
    )
    made = []
    for level in range(1, levels + 1):
        for column in range(1, columns + 1):
            key = (level, None if columns == 1 else column)
            if key in existing:
                continue
            made.append(ShelfLevel(shelf=shelf, level_number=level,
                                   column_number=key[1]))
    if made:
        ShelfLevel.objects.bulk_create(made)
    return len(made)


@admin_or_module_required('shelf')
def set_shelf_grid(request):
    """Change how many levels and columns a shelf has, after the fact.

    Only ever adds. Removing a level would take its books with it -- and a
    board being deleted because somebody mistyped a number is not a mistake
    worth making easy, so surplus levels are reported and left alone for the
    Administrator to delete deliberately.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    shelf = Shelf.objects.filter(shelf_id=request.POST.get('shelf_id')).first()
    if shelf is None:
        return JsonResponse({'success': False, 'error': 'Shelf not found'})

    levels, columns = _grid_counts(request)
    added = _build_shelf_grid(shelf, levels, columns)

    # Plenty of bays have books stacked on the top surface, which is not one of
    # the numbered boards and is where the overflow ends up. It is a level like
    # any other so a book can be shelved and found on it -- just one that reads
    # "Top" rather than "Level 6".
    top_note = ''
    if 'has_top' in request.POST:
        wants_top = request.POST.get('has_top') in ('1', 'true', 'True', 'on')
        top = ShelfLevel.objects.filter(shelf=shelf, is_top=True).first()
        if wants_top and top is None:
            ShelfLevel.objects.create(shelf=shelf, level_number=levels + 1, is_top=True)
            added += 1
            top_note = 'The top surface was added.'
        elif not wants_top and top is not None:
            # Unticking removes it only while it is empty. A top with books on
            # it is a board like any other, and the rule everywhere else here is
            # that a number in a box never deletes books.
            if top.book_set.exists():
                top_note = ('The top surface holds %d book(s), so it was kept.'
                            % top.book_set.count())
            else:
                top.delete()
                top_note = 'The top surface was removed.'

    surplus = [lv for lv in ShelfLevel.objects.filter(shelf=shelf)
               if not lv.is_top and not lv.is_under
               and (lv.level_number > levels or (lv.column_number or 1) > columns)]
    log_admin_action(request, 'Update', 'Shelf', shelf.shelf_id,
                     f'{shelf.name}: grid set to {levels} level(s) x {columns} column(s), '
                     f'{added} added')
    return JsonResponse({
        'success': True,
        'added': added,
        'levels': levels,
        'columns': columns,
        'top_note': top_note,
        'has_top': ShelfLevel.objects.filter(shelf=shelf, is_top=True).exists(),
        'surplus': [{'shelf_level_id': lv.shelf_level_id, 'label': lv.label,
                     'books': lv.book_set.count()} for lv in surplus],
    })


@admin_or_module_required('shelf')
def edit_shelf(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})
    
    shelf_id = request.POST.get('shelf_id')
    name = request.POST.get('name')
    map_x = request.POST.get('map_x')
    map_y = request.POST.get('map_y')
    description = request.POST.get('description')
    
    if not shelf_id:
        return JsonResponse({'success': False, 'error': 'shelf_id is required'})
    
    try:
        shelf = Shelf.objects.get(shelf_id=shelf_id)
        if name:
            shelf.name = name
        if map_x is not None:
            shelf.map_x = float(map_x)
        if map_y is not None:
            shelf.map_y = float(map_y)
        if description is not None:
            shelf.description = description

        # Dragging a corner reshapes the outline, which is the one edit that
        # cannot be expressed as width and depth. A shelf that was a plain
        # rectangle becomes a traced one the moment its corners are moved --
        # that is what dragging a corner means, and the alternative is refusing
        # the gesture on every shelf that was not drawn by hand.
        if 'geometry' in request.POST:
            geometry, geo_error = _parse_geometry(request.POST.get('geometry'))
            if geo_error:
                return JsonResponse({'success': False, 'error': geo_error})
            if geometry:
                shelf.geometry = geometry
                shelf.map_x, shelf.map_y = _polygon_centroid(geometry)

        shelf.save()
        return JsonResponse({'success': True, 'geometry': shelf.geometry,
                             'map_x': shelf.map_x, 'map_y': shelf.map_y,
                             'footprint': shelf.footprint()})
    except Shelf.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Shelf not found'})


# ─── SHELF PLACEMENT ON THE FLOOR PLAN ────────────────────────────────
# One endpoint per Administrator action so each traces to its activity diagram:
# Place Shelf (Fig. 48), Move Shelf (Fig. 49), Rotate Shelf (Fig. 50) and
# Unplace Shelf (Fig. 51). Confirmation happens in the UI before these are hit.

def _translate_geometry(geometry, dx, dy):
    """Slide an outline across the plan without changing its shape."""
    return [[round(p[0] + dx, 2), round(p[1] + dy, 2)] for p in geometry]


def _rotate_geometry(geometry, degrees, cx, cy):
    """Turn an outline about a point, keeping every edge the length it was."""
    rad = math.radians(degrees)
    cos, sin = math.cos(rad), math.sin(rad)
    out = []
    for x, y in geometry:
        ox, oy = x - cx, y - cy
        out.append([round(cx + ox * cos - oy * sin, 2),
                    round(cy + ox * sin + oy * cos, 2)])
    return out


def _set_shelf_position(request, action):
    """Shared body for Place Shelf and Move Shelf."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    shelf_id = request.POST.get('shelf_id')
    if not shelf_id:
        return JsonResponse({'success': False, 'error': 'shelf_id is required'})

    try:
        map_x = float(request.POST.get('map_x'))
        map_y = float(request.POST.get('map_y'))
    except (TypeError, ValueError):
        return JsonResponse({'success': False, 'error': 'map_x and map_y are required'})

    shelf = Shelf.objects.filter(shelf_id=shelf_id).first()
    if shelf is None:
        return JsonResponse({'success': False, 'error': 'Shelf not found'})

    # A traced shelf -- one set into a corner, drawn corner by corner rather
    # than sized -- carries an outline in absolute canvas coordinates. Moving
    # only the anchor left that outline exactly where it was, so the shelf
    # appeared not to move at all once the page was reloaded. The outline is
    # carried along by the same offset the anchor travelled.
    fields = ['map_x', 'map_y']
    if shelf.geometry and len(shelf.geometry) >= 3 and shelf.map_x is not None:
        shelf.geometry = _translate_geometry(shelf.geometry,
                                             map_x - shelf.map_x, map_y - shelf.map_y)
        fields.append('geometry')

    shelf.map_x, shelf.map_y = map_x, map_y
    shelf.save(update_fields=fields)
    log_admin_action(request, action, 'Shelf', shelf.shelf_id,
                     f'{shelf.name} {action.lower()} at ({map_x:.0f}, {map_y:.0f})')
    return JsonResponse({'success': True, 'map_x': shelf.map_x, 'map_y': shelf.map_y,
                         'geometry': shelf.geometry})


@admin_only_required
def place_shelf(request):
    """Fig. 48 — position a shelf on the floor plan for the first time."""
    return _set_shelf_position(request, 'Place')


@admin_only_required
def move_shelf(request):
    """Fig. 49 — move an already-placed shelf to a new position."""
    return _set_shelf_position(request, 'Move')


@admin_only_required
def rotate_shelf(request):
    """Fig. 50 — store a shelf's orientation so the map matches the real aisle."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    shelf_id = request.POST.get('shelf_id')
    if not shelf_id:
        return JsonResponse({'success': False, 'error': 'shelf_id is required'})

    try:
        rotation = float(request.POST.get('rotation'))
    except (TypeError, ValueError):
        return JsonResponse({'success': False, 'error': 'rotation must be a number'})

    shelf = Shelf.objects.filter(shelf_id=shelf_id).first()
    if shelf is None:
        return JsonResponse({'success': False, 'error': 'Shelf not found'})

    # For a sized shelf the rotation is enough on its own -- the rectangle is
    # drawn from it. A traced outline is not derived from anything, so turning
    # the shelf has to turn the outline, about the anchor the rectangle would
    # have turned about.
    fields = ['rotation']
    turn = (rotation % 360) - (shelf.rotation or 0)
    if (shelf.geometry and len(shelf.geometry) >= 3
            and shelf.map_x is not None and abs(turn) > 1e-9):
        shelf.geometry = _rotate_geometry(shelf.geometry, turn, shelf.map_x, shelf.map_y)
        fields.append('geometry')

    shelf.rotation = rotation % 360        # keep it in 0–359 whatever is sent
    shelf.save(update_fields=fields)
    log_admin_action(request, 'Rotate', 'Shelf', shelf.shelf_id,
                     f'{shelf.name} rotated to {shelf.rotation:.0f}°')
    return JsonResponse({'success': True, 'rotation': shelf.rotation,
                         'geometry': shelf.geometry})


# ─── DOORS ON ROOM WALLS ──────────────────────────────────────────────
# Walls, and the one legal way through them.
#
# A waypoint connection asserts "a person can walk straight between these two
# points". Nothing used to check that claim, so two waypoints in neighbouring
# rooms could be joined through the wall between them and A* would route a
# patron through masonry -- drawn as a confident line, on a map that looks
# correct.
WALL_EPS = 1e-6
# How far past a door's own half-width a crossing may sit and still count as
# going through it. Rooms are traced by hand, so the two sides of one doorway
# rarely line up to the unit.
DOOR_APERTURE_SLACK = 6.0
# How far another room's wall may sit from a door and still be the far side of
# it. Rooms are traced by hand: two that look joined on screen are routinely a
# fraction of a unit apart, and this deployment's own plan has had pairs
# 0.11 and 0.50 units from touching. Inferring adjacency with no tolerance
# would silently miss exactly those.
ROOM_FACING_TOLERANCE = 2.0


def _facing_room(floor_plan, x, y, exclude_room_id):
    """The other room whose wall passes through (x, y), if there is one.

    A suggestion, never an assertion -- the caller offers it and somebody says
    yes. Guessing outright would quietly record a connection between two rooms
    that merely have walls near each other, and a wrong adjacency is worse
    than a missing one because nothing later questions it.
    """
    best, best_gap = None, ROOM_FACING_TOLERANCE
    for room in Room.objects.filter(floor_plan=floor_plan, is_active=True).exclude(
            room_id=exclude_room_id):
        geom = room.geometry or []
        if len(geom) < 3:
            continue
        for i in range(len(geom)):
            ax, ay = geom[i]
            bx, by = geom[(i + 1) % len(geom)]
            gap = _point_to_segment(x, y, ax, ay, bx, by)
            if gap <= best_gap:
                best, best_gap = room, gap
    return best


def _orient(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _proper_crossing(p1, p2, p3, p4):
    """Where segments p1p2 and p3p4 properly cross, or None.

    "Properly" excludes touching and running along each other: a waypoint
    sitting exactly on a wall, or a corridor drawn down the line of one, is not
    passing through it.
    """
    d1, d2 = _orient(p3, p4, p1), _orient(p3, p4, p2)
    d3, d4 = _orient(p1, p2, p3), _orient(p1, p2, p4)
    if not (((d1 > WALL_EPS and d2 < -WALL_EPS) or (d1 < -WALL_EPS and d2 > WALL_EPS))
            and ((d3 > WALL_EPS and d4 < -WALL_EPS) or (d3 < -WALL_EPS and d4 > WALL_EPS))):
        return None
    denom = ((p1[0] - p2[0]) * (p3[1] - p4[1]) - (p1[1] - p2[1]) * (p3[0] - p4[0]))
    if abs(denom) < WALL_EPS:
        return None
    a = p1[0] * p2[1] - p1[1] * p2[0]
    b = p3[0] * p4[1] - p3[1] * p4[0]
    return ((a * (p3[0] - p4[0]) - (p1[0] - p2[0]) * b) / denom,
            (a * (p3[1] - p4[1]) - (p1[1] - p2[1]) * b) / denom)


def _walls_crossed(floor_plan, ax, ay, bx, by):
    """Rooms whose wall this segment goes through without using a door.

    A door is matched by proximity rather than by which room owns it: in a
    shared doorway only one of the two rooms holds the Door row, and refusing
    the crossing from the other side would be wrong.
    """
    doors = [(d.map_x, d.map_y, (d.width or DOOR_DEFAULT_WIDTH) / 2.0,
              {d.room_id, d.room_b_id} if d.room_b_id else None)
             for d in Door.objects.filter(room__floor_plan=floor_plan, is_active=True)]
    blocked = []
    seg = ((ax, ay), (bx, by))
    for room in Room.objects.filter(floor_plan=floor_plan, is_active=True):
        geom = room.geometry or []
        if len(geom) < 3:
            continue
        for i in range(len(geom)):
            p3 = (geom[i][0], geom[i][1])
            p4 = (geom[(i + 1) % len(geom)][0], geom[(i + 1) % len(geom)][1])
            hit = _proper_crossing(seg[0], seg[1], p3, p4)
            if hit is None:
                continue
            # A door that names both its rooms only excuses a crossing of one
            # of those two. One that names a single room is taken at face
            # value -- it belongs to whatever wall it sits in -- because until
            # somebody links it we have no better information than the geometry.
            through_door = any(
                math.hypot(hit[0] - dx, hit[1] - dy) <= half + DOOR_APERTURE_SLACK
                and (joins is None or room.room_id in joins)
                for dx, dy, half, joins in doors)
            if not through_door:
                blocked.append(room.name)
                break
    return blocked


# A door narrower than DOOR_MIN_WIDTH is not a doorway, and one wider than
# DOOR_MAX_WIDTH is a missing wall rather than an opening. Kept as constants
# because add_door and edit_door both enforce them and must not drift apart.
DOOR_MIN_WIDTH = 6
DOOR_MAX_WIDTH = 400
DOOR_DEFAULT_WIDTH = 28
DOOR_WIDTH_ERROR = (f'Door width must be between {DOOR_MIN_WIDTH} and {DOOR_MAX_WIDTH}')


def _snap_to_polygon_edge(points, x, y):
    """Project (x, y) onto the nearest edge of a polygon.

    Returns (snapped_x, snapped_y, bearing_degrees). Doing this server-side
    keeps a door genuinely on its wall regardless of how imprecisely the
    Administrator clicked.
    """
    best = None
    count = len(points)
    for i in range(count):
        ax, ay = points[i]
        bx, by = points[(i + 1) % count]
        dx, dy = bx - ax, by - ay
        seg_len_sq = dx * dx + dy * dy
        if seg_len_sq < 1e-9:
            continue
        # Parametric position of the closest point, clamped to the segment.
        t = ((x - ax) * dx + (y - ay) * dy) / seg_len_sq
        t = max(0.0, min(1.0, t))
        px, py = ax + t * dx, ay + t * dy
        dist_sq = (x - px) ** 2 + (y - py) ** 2
        if best is None or dist_sq < best[0]:
            bearing = math.degrees(math.atan2(dy, dx)) % 360
            best = (dist_sq, px, py, bearing)

    if best is None:
        return x, y, 0.0
    return best[1], best[2], best[3]


@admin_only_required
def add_door(request):
    """Place a door on a room's wall, snapped to the nearest edge."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    room_id = request.POST.get('room_id')
    if not room_id:
        return JsonResponse({'success': False, 'error': 'room_id is required'})

    try:
        click_x = float(request.POST.get('map_x'))
        click_y = float(request.POST.get('map_y'))
    except (TypeError, ValueError):
        return JsonResponse({'success': False, 'error': 'map_x and map_y are required'})

    try:
        width = float(request.POST.get('width') or DOOR_DEFAULT_WIDTH)
    except (TypeError, ValueError):
        width = float(DOOR_DEFAULT_WIDTH)
    if not (DOOR_MIN_WIDTH <= width <= DOOR_MAX_WIDTH):
        return JsonResponse({'success': False, 'error': DOOR_WIDTH_ERROR})

    room = Room.objects.filter(room_id=room_id).first()
    if room is None:
        return JsonResponse({'success': False, 'error': 'Room not found'})
    if not room.geometry or len(room.geometry) < 3:
        return JsonResponse({'success': False, 'error': 'Draw the room shape before adding a door'})

    x, y, bearing = _snap_to_polygon_edge(room.geometry, click_x, click_y)

    swing = -1 if request.POST.get('swing') == '-1' else 1
    door = Door.objects.create(
        room=room, map_x=x, map_y=y, width=width, rotation=bearing,
        swing=swing, label=(request.POST.get('label') or '').strip() or None,
    )
    log_admin_action(request, 'Add', 'Door', door.door_id,
                     f'Door on {room.name} at ({x:.0f}, {y:.0f})')

    # Offered, not applied. The editor shows it as a one-click suggestion.
    facing = _facing_room(room.floor_plan, x, y, room.room_id)
    return JsonResponse({
        'success': True,
        'door': _door_payload(door),
        'suggested_room_b': ({'room_id': facing.room_id, 'name': facing.name}
                             if facing else None),
    })


@admin_only_required
def move_door(request):
    """Reposition an existing door after a drag along its wall.

    Re-snaps to the room's nearest edge server-side, same as placement —
    the client only constrains the drag to look right in real time; the
    saved position always comes from the authoritative geometry.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    door = Door.objects.filter(door_id=request.POST.get('door_id')).select_related('room').first()
    if door is None:
        return JsonResponse({'success': False, 'error': 'Door not found'})

    try:
        drag_x = float(request.POST.get('map_x'))
        drag_y = float(request.POST.get('map_y'))
    except (TypeError, ValueError):
        return JsonResponse({'success': False, 'error': 'map_x and map_y are required'})

    room = door.room
    if not room.geometry or len(room.geometry) < 3:
        return JsonResponse({'success': False, 'error': 'This room has no shape to snap to'})

    x, y, bearing = _snap_to_polygon_edge(room.geometry, drag_x, drag_y)
    door.map_x, door.map_y, door.rotation = x, y, bearing
    door.save(update_fields=['map_x', 'map_y', 'rotation'])
    return JsonResponse({'success': True, 'x': door.map_x, 'y': door.map_y, 'rotation': door.rotation})


@admin_only_required
def edit_door(request):
    """Resize or rename a door.

    Width is the opening measured along the wall, in canvas units, so it is
    the one property that cannot be derived: position and angle both follow
    from the wall the door was snapped to, but how wide the doorway is is a
    fact about the building. Until this existed every door stayed at the
    default width for ever, because add_door was the only place it could be
    set and the editor never sent one.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    door = Door.objects.filter(door_id=request.POST.get('door_id')).first()
    if door is None:
        return JsonResponse({'success': False, 'error': 'Door not found'})

    fields = []
    if 'width' in request.POST:
        try:
            width = float(request.POST['width'])
        except (TypeError, ValueError):
            return JsonResponse({'success': False, 'error': DOOR_WIDTH_ERROR})
        if not (DOOR_MIN_WIDTH <= width <= DOOR_MAX_WIDTH):
            return JsonResponse({'success': False, 'error': DOOR_WIDTH_ERROR})
        door.width = width
        fields.append('width')

    # Dragging one end of a door both widens it and shifts its middle, since
    # the far end stays put. Accepting the new centre here keeps that a single
    # request: sending the width and the position separately would leave the
    # door briefly the wrong size on one and in the wrong place on the other,
    # and a failure between the two would persist exactly that.
    if 'map_x' in request.POST and 'map_y' in request.POST:
        try:
            cx = float(request.POST['map_x'])
            cy = float(request.POST['map_y'])
        except (TypeError, ValueError):
            return JsonResponse({'success': False, 'error': 'map_x and map_y must be numbers'})
        room = door.room
        if not room.geometry or len(room.geometry) < 3:
            return JsonResponse({'success': False, 'error': 'This room has no shape to snap to'})
        x, y, bearing = _snap_to_polygon_edge(room.geometry, cx, cy)
        door.map_x, door.map_y, door.rotation = x, y, bearing
        fields += ['map_x', 'map_y', 'rotation']

    if 'room_b' in request.POST:
        raw = (request.POST.get('room_b') or '').strip()
        if not raw:
            door.room_b = None
        else:
            far = Room.objects.filter(room_id=raw).first()
            if far is None:
                return JsonResponse({'success': False, 'error': 'That room does not exist'})
            if far.room_id == door.room_id:
                return JsonResponse({'success': False,
                                     'error': 'A door cannot join a room to itself'})
            if far.floor_plan_id != door.room.floor_plan_id:
                return JsonResponse({'success': False,
                                     'error': 'Both rooms must be on the same floor'})
            door.room_b = far
        fields.append('room_b')

    if 'label' in request.POST:
        door.label = (request.POST.get('label') or '').strip()[:255] or None
        fields.append('label')

    if not fields:
        return JsonResponse({'success': False, 'error': 'Nothing to change'})

    door.save(update_fields=fields)
    log_admin_action(request, 'Edit', 'Door', door.door_id,
                     f'{", ".join(fields)} changed')
    return JsonResponse({'success': True, 'door': _door_payload(door)})


@admin_only_required
def delete_door(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    door = Door.objects.filter(door_id=request.POST.get('door_id')).select_related('room').first()
    if door is None:
        return JsonResponse({'success': False, 'error': 'Door not found'})

    room_name, door_id = door.room.name, door.door_id
    door.delete()
    log_admin_action(request, 'Delete', 'Door', door_id, f'Door removed from {room_name}')
    return JsonResponse({'success': True})


@admin_only_required
def flip_door(request):
    """Reverse which way a door swings."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    door = Door.objects.filter(door_id=request.POST.get('door_id')).first()
    if door is None:
        return JsonResponse({'success': False, 'error': 'Door not found'})

    door.swing = -1 if door.swing >= 0 else 1
    door.save(update_fields=['swing'])
    return JsonResponse({'success': True, 'swing': door.swing})


@admin_only_required
def resize_shelf(request):
    """Set a shelf's footprint. Shelves in the library are not all one size.

    Dragging a resize handle on anything but a corner-preserving axis shifts
    the shelf's centre (e.g. pulling the right edge out while the left edge
    stays put moves the centre right by half the delta) — map_x/map_y are
    therefore optional and, when sent, are saved in the same call so the
    shape never visibly snaps back before the recentred position lands.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    shelf_id = request.POST.get('shelf_id')
    if not shelf_id:
        return JsonResponse({'success': False, 'error': 'shelf_id is required'})

    try:
        width = float(request.POST.get('width'))
        depth = float(request.POST.get('depth'))
    except (TypeError, ValueError):
        return JsonResponse({'success': False, 'error': 'width and depth must be numbers'})

    if not (4 <= width <= 2000) or not (4 <= depth <= 2000):
        return JsonResponse({'success': False, 'error': 'Width and depth must be between 4 and 2000'})

    map_x_raw, map_y_raw = request.POST.get('map_x'), request.POST.get('map_y')
    new_x = new_y = None
    if map_x_raw is not None and map_y_raw is not None:
        try:
            new_x, new_y = float(map_x_raw), float(map_y_raw)
        except (TypeError, ValueError):
            return JsonResponse({'success': False, 'error': 'map_x and map_y must be numbers'})

    shelf = Shelf.objects.filter(shelf_id=shelf_id).first()
    if shelf is None:
        return JsonResponse({'success': False, 'error': 'Shelf not found'})

    fields = ['width', 'depth']
    shelf.width, shelf.depth = width, depth
    if new_x is not None:
        shelf.map_x, shelf.map_y = new_x, new_y
        fields += ['map_x', 'map_y']
    shelf.save(update_fields=fields)
    log_admin_action(request, 'Resize', 'Shelf', shelf.shelf_id,
                     f'{shelf.name} resized to {width:.0f} x {depth:.0f}')
    return JsonResponse({'success': True, 'width': shelf.width, 'depth': shelf.depth,
                         'map_x': shelf.map_x, 'map_y': shelf.map_y})


@admin_only_required
def unplace_shelf(request):
    """Fig. 51 — take a shelf off the map while keeping its record and books."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    shelf_id = request.POST.get('shelf_id')
    if not shelf_id:
        return JsonResponse({'success': False, 'error': 'shelf_id is required'})

    shelf = Shelf.objects.filter(shelf_id=shelf_id).first()
    if shelf is None:
        return JsonResponse({'success': False, 'error': 'Shelf not found'})

    shelf.map_x = None
    shelf.map_y = None
    shelf.save(update_fields=['map_x', 'map_y'])
    log_admin_action(request, 'Unplace', 'Shelf', shelf.shelf_id,
                     f'{shelf.name} removed from the floor plan')
    return JsonResponse({'success': True})


@admin_or_module_required('shelf')
def delete_shelf(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})
    
    shelf_id = request.POST.get('shelf_id')
    if not shelf_id:
        return JsonResponse({'success': False, 'error': 'shelf_id is required'})
    
    Shelf.objects.filter(shelf_id=shelf_id).delete()
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'success': True})
    return redirect('floorplan_management')


@admin_or_module_required('shelf')
def add_shelf_level(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    shelf_id = request.POST.get('shelf_id')
    level_number = request.POST.get('level_number')
    category = request.POST.get('category', '')
    is_top = request.POST.get('is_top') in ('1', 'true', 'True', 'on')
    is_under = request.POST.get('is_under') in ('1', 'true', 'True', 'on')
    raw_column = (request.POST.get('column_number') or '').strip()
    try:
        column_number = int(raw_column) if raw_column else None
    except (TypeError, ValueError):
        return JsonResponse({'success': False, 'error': 'Column must be a number'})
    if column_number is not None and column_number < 1:
        column_number = None
    # A shelf is either on top of the case or underneath it, never both.
    if is_top and is_under:
        return JsonResponse({'success': False,
                             'error': 'A level is either the top or underneath, not both.'})

    # Top and underneath have no number to give, so none is asked for.
    if not shelf_id or (not level_number and not is_top and not is_under):
        return JsonResponse({'success': False, 'error': 'shelf_id and level_number are required'})

    try:
        shelf = Shelf.objects.get(shelf_id=shelf_id)
        if is_top and ShelfLevel.objects.filter(shelf=shelf, is_top=True).exists():
            return JsonResponse({'success': False,
                                 'error': f'{shelf.name} already has a top surface.'})
        if is_under and ShelfLevel.objects.filter(shelf=shelf, is_under=True,
                                                  column_number=column_number).exists():
            return JsonResponse({'success': False,
                                 'error': f'{shelf.name} already has an underneath space.'})
        if is_top and not level_number:
            # Sorts above every real level, which is where it physically is.
            highest = ShelfLevel.objects.filter(shelf=shelf).order_by('-level_number').first()
            level_number = (highest.level_number + 1) if highest else 1
        if is_under and not level_number:
            # Below everything, so it sorts first.
            level_number = 0
        shelf_level = ShelfLevel.objects.create(
            shelf=shelf,
            level_number=int(level_number),
            category=category,
            is_top=is_top,
            is_under=is_under,
            column_number=column_number,
        )
        return JsonResponse({'success': True, 'shelf_level_id': shelf_level.shelf_level_id,
                             'label': shelf_level.label})
    except Shelf.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Shelf not found'})


@admin_or_module_required('shelf')
def edit_shelf_level(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    shelf_level_id = request.POST.get('shelf_level_id')
    level_number = request.POST.get('level_number')
    category = request.POST.get('category')

    if not shelf_level_id:
        return JsonResponse({'success': False, 'error': 'shelf_level_id is required'})

    try:
        shelf_level = ShelfLevel.objects.get(shelf_level_id=shelf_level_id)
        if level_number:
            shelf_level.level_number = int(level_number)
        if category is not None:
            shelf_level.category = category

        # Whether a board is the top surface or the space underneath could be
        # set when it was created and never afterwards, so a level recorded
        # wrongly had to be deleted and made again -- taking its books with it.
        # Only ever one of the two: a shelf top is not also its underside.
        if 'is_top' in request.POST or 'is_under' in request.POST:
            top = request.POST.get('is_top') in ('1', 'true', 'True', 'on')
            under = request.POST.get('is_under') in ('1', 'true', 'True', 'on')
            if top and under:
                return JsonResponse({
                    'success': False,
                    'error': 'A board is either the top of the shelf or the space '
                             'underneath it, not both.'})
            shelf_level.is_top = top
            shelf_level.is_under = under

        if 'column_number' in request.POST:
            raw = (request.POST.get('column_number') or '').strip()
            shelf_level.column_number = int(raw) if raw.isdigit() and int(raw) > 0 else None

        shelf_level.save()
        return JsonResponse({'success': True, 'label': shelf_level.label})
    except ShelfLevel.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Shelf level not found'})


@admin_or_module_required('shelf')
def delete_shelf_level(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})
    
    shelf_level_id = request.POST.get('shelf_level_id')
    if not shelf_level_id:
        return JsonResponse({'success': False, 'error': 'shelf_level_id is required'})
    
    ShelfLevel.objects.filter(shelf_level_id=shelf_level_id).delete()
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'success': True})
    return redirect('floorplan_management')


@admin_or_module_required('shelf')
def toggle_active(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})
    
    model_type = request.POST.get('model_type')
    item_id = request.POST.get('id')
    
    if not model_type or not item_id:
        return JsonResponse({'success': False, 'error': 'model_type and id are required'})
    
    model_map = {
        'room': Room,
        'shelf': Shelf,
        'shelflevel': ShelfLevel
    }
    
    if model_type not in model_map:
        return JsonResponse({'success': False, 'error': 'Invalid model_type'})
    
    try:
        model_class = model_map[model_type]
        item = model_class.objects.get(pk=item_id)
        item.is_active = not item.is_active
        item.save()
        return JsonResponse({'success': True, 'is_active': item.is_active})
    except model_class.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Item not found'})


# ─── MAP CONFIGURATION: BEACONS & WAYPOINTS ──────────────────────
def _floorplan_canvas_size(floor_plan):
    """Return the (width, height) of the floor plan's drawing canvas.

    Floor plans are vector canvases, not images: every map_x/map_y stored for
    rooms, shelves, waypoints and beacons is expressed in this coordinate space.
    """
    return float(floor_plan.canvas_width or 1000), float(floor_plan.canvas_height or 800)


def _door_payload(door, suggest=False):
    """`suggest` asks whether another room's wall runs through this doorway.

    Only the editor wants that: it is what turns "this door looks shared" into
    a button. Computed on demand rather than always, so the patron map does not
    pay for a question it never asks.
    """
    facing = None
    if suggest and not door.room_b_id and door.room_id:
        found = _facing_room(door.room.floor_plan, door.map_x, door.map_y, door.room_id)
        if found is not None:
            facing = {'room_id': found.room_id, 'name': found.name}
    return {
        'suggested_room_b': facing,
        'door_id': door.door_id,
        'room_id': door.room_id,
        'x': door.map_x,
        'y': door.map_y,
        'width': door.width or 28,
        'rotation': door.rotation or 0,
        'swing': door.swing if door.swing in (1, -1) else 1,
        'label': door.label or '',
        'room_b': door.room_b_id,
        'room_name': door.room.name if door.room_id else '',
        'room_b_name': door.room_b.name if door.room_b_id else '',
    }


def _room_payload(room, doors_by_room=None, suggest_doors=False):
    """Serialise a room including its drawn polygon (may be None if never drawn)."""
    doors = (doors_by_room or {}).get(room.room_id, [])
    return {
        'room_id': room.room_id,
        'name': room.name,
        'geometry': room.geometry or None,
        'map_x': room.map_x,
        'map_y': room.map_y,
        'doors': [_door_payload(d, suggest=suggest_doors) for d in doors],
    }


def _doors_for_floorplan(floor_plan, active_only=False):
    """Group a floor plan's doors by room id, ready for _room_payload."""
    qs = Door.objects.filter(room__floor_plan=floor_plan)
    if active_only:
        qs = qs.filter(is_active=True)
    grouped = {}
    for door in qs:
        grouped.setdefault(door.room_id, []).append(door)
    return grouped


def _point_in_polygon(x, y, poly):
    """Ray-casting: is (x, y) inside this outline?"""
    inside = False
    n = len(poly)
    for i in range(n):
        xi, yi = poly[i][0], poly[i][1]
        xj, yj = poly[i - 1][0], poly[i - 1][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
    return inside


def _point_to_segment(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    t = 0.0 if length_sq == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _polygon_gap(a, b):
    """Closest approach between two outlines; 0 when they touch."""
    best = float('inf')
    for poly, other in ((a, b), (b, a)):
        for pt in poly:
            for i in range(len(other)):
                ax, ay = other[i]
                bx, by = other[(i + 1) % len(other)]
                best = min(best, _point_to_segment(pt[0], pt[1], ax, ay, bx, by))
    return best


# How far into a room a doorway's waypoint stands. Far enough to be clearly
# inside rather than in the threshold, close enough that the step through the
# door is a short straight line.
DOORWAY_STANDOFF = 45.0
# A shelf is approached from its face, not its middle.
SHELF_STANDOFF = 55.0


def _polygon_bounds(poly):
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return min(xs), min(ys), max(xs), max(ys)


def _interior_point(poly):
    """A point comfortably inside a room, even a concave one.

    The centroid of an L-shaped room can land in the notch outside it, so the
    centroid is only used when it is genuinely inside; otherwise the room is
    sampled and the point furthest from any wall wins. That point is the middle
    of the widest open space, which is where somebody would actually stand.
    """
    cx, cy = _polygon_centroid(poly)
    if _point_in_polygon(cx, cy, poly):
        return cx, cy

    min_x, min_y, max_x, max_y = _polygon_bounds(poly)
    best, best_clear = None, -1.0
    steps = 18
    for i in range(1, steps):
        for j in range(1, steps):
            x = min_x + (max_x - min_x) * i / steps
            y = min_y + (max_y - min_y) * j / steps
            if not _point_in_polygon(x, y, poly):
                continue
            clear = min(_point_to_segment(x, y, poly[k][0], poly[k][1],
                                          poly[(k + 1) % len(poly)][0],
                                          poly[(k + 1) % len(poly)][1])
                        for k in range(len(poly)))
            if clear > best_clear:
                best, best_clear = (x, y), clear
    return best if best else (cx, cy)


def _crosses_obstacle(floor_plan, ax, ay, bx, by):
    """Does this step walk through a table, counter or pillar?

    Walls are refused outright; furniture is merely avoided while generating,
    because furniture moves and an Administrator may well want to connect
    across where a trolley happens to be today.
    """
    for o in Obstacle.objects.filter(floor_plan=floor_plan, is_active=True):
        geom = o.geometry or []
        if len(geom) < 3:
            continue
        for i in range(len(geom)):
            p3 = (geom[i][0], geom[i][1])
            p4 = (geom[(i + 1) % len(geom)][0], geom[(i + 1) % len(geom)][1])
            if _proper_crossing((ax, ay), (bx, by), p3, p4) is not None:
                return True
    return False


@admin_or_module_required('shelf')
def generate_waypoints(request):
    """Lay a walkable network over the plan, and say what it could not reach.

    Placing every waypoint and drawing every connection by hand is the most
    laborious part of preparing a floor, and the part most likely to be left
    half-finished. What can be derived is derived: a pair of points either side
    of each doorway, one in the open middle of each room, one in front of each
    shelf, joined wherever the straight line between them is actually walkable.

    Only generated waypoints are replaced. Anything placed or moved by hand
    survives, so this is safe to run again after correcting it -- the second
    press being destructive is what would make it untrustworthy.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    plan = FloorPlan.objects.filter(floor_plan_id=request.POST.get('floor_plan_id')).first()
    if plan is None:
        return JsonResponse({'success': False, 'error': 'Floor plan not found'})

    rooms = [r for r in Room.objects.filter(floor_plan=plan, is_active=True)
             if r.geometry and len(r.geometry) >= 3]
    if not rooms:
        return JsonResponse({'success': False,
                             'error': 'Draw at least one room before generating a route.'})

    with transaction.atomic():
        removed = Waypoint.objects.filter(floor_plan=plan, is_generated=True).count()
        Waypoint.objects.filter(floor_plan=plan, is_generated=True).delete()

        def room_at(x, y):
            for room in rooms:
                if _point_in_polygon(x, y, room.geometry):
                    return room
            return None

        # room_id -> [Waypoint]
        by_room = dict((r.room_id, []) for r in rooms)
        made = []

        def place(x, y, room, label):
            wp = Waypoint.objects.create(floor_plan=plan, map_x=x, map_y=y,
                                         label=label, is_generated=True)
            by_room[room.room_id].append(wp)
            made.append(wp)
            return wp

        # ---- one pair per doorway ---------------------------------------
        door_pairs = []
        for door in Door.objects.filter(room__floor_plan=plan, is_active=True):
            rad = math.radians(door.rotation or 0)
            # Across the wall, not along it.
            nx, ny = -math.sin(rad), math.cos(rad)
            sides = []
            for sign in (1, -1):
                x = door.map_x + nx * DOORWAY_STANDOFF * sign
                y = door.map_y + ny * DOORWAY_STANDOFF * sign
                room = room_at(x, y)
                if room is not None:
                    sides.append(place(x, y, room, 'Doorway'))
            # Both sides indoors means this doorway is a way between two rooms,
            # and the step through it is the one crossing that is allowed.
            if len(sides) == 2:
                door_pairs.append((sides[0], sides[1]))

        # ---- one in the open middle of each room ------------------------
        for room in rooms:
            x, y = _interior_point(room.geometry)
            place(x, y, room, room.name)

        # ---- one in front of each shelf ---------------------------------
        for shelf in Shelf.objects.filter(room__floor_plan=plan).select_related('room'):
            if shelf.map_x is None or shelf.map_y is None:
                continue
            rad = math.radians(shelf.rotation or 0)
            placed = False
            # Try the front first, then the back: one of the two long sides
            # faces into the room, and which one is not recorded anywhere.
            for sign in (1, -1):
                x = shelf.map_x - math.sin(rad) * SHELF_STANDOFF * sign
                y = shelf.map_y + math.cos(rad) * SHELF_STANDOFF * sign
                room = room_at(x, y)
                if room is not None:
                    place(x, y, room, shelf.name or 'Shelf')
                    placed = True
                    break
            if not placed:
                continue

        # ---- join what can actually be walked between --------------------
        links = 0
        seen = set()

        def join(a, b):
            nonlocal links
            key = (min(a.waypoint_id, b.waypoint_id), max(a.waypoint_id, b.waypoint_id))
            if key in seen:
                return
            seen.add(key)
            WaypointConnection.objects.create(
                waypoint_from=a, waypoint_to=b,
                distance=math.hypot(a.map_x - b.map_x, a.map_y - b.map_y))
            links += 1

        # Through each doorway: the one wall crossing that is legal.
        for a, b in door_pairs:
            join(a, b)

        # Within a room, wherever the straight line is clear.
        for room in rooms:
            points = by_room[room.room_id]
            for i in range(len(points)):
                for j in range(i + 1, len(points)):
                    a, b = points[i], points[j]
                    if _walls_crossed(plan, a.map_x, a.map_y, b.map_x, b.map_y):
                        continue
                    if _crosses_obstacle(plan, a.map_x, a.map_y, b.map_x, b.map_y):
                        continue
                    join(a, b)

        # Hand-placed waypoints are not rewired, but a room with only
        # hand-placed ones should still count as reached.
        manual = Waypoint.objects.filter(floor_plan=plan, is_generated=False)
        unreachable = []
        for room in rooms:
            if by_room[room.room_id]:
                continue
            if any(_point_in_polygon(w.map_x, w.map_y, room.geometry) for w in manual):
                continue
            unreachable.append(room.name)

    log_admin_action(request, 'Create', 'Waypoint', plan.floor_plan_id,
                     f'Generated {len(made)} waypoints and {links} connections '
                     f'on {plan.name}')
    return JsonResponse({
        'success': True,
        'placed': len(made),
        'connections': links,
        'replaced': removed,
        'kept_manual': manual.count(),
        'unreachable': unreachable,
    })


def _room_adjacency(floor_plan):
    """Which rooms connect to which, according to the doors.

    This is the question the system could not answer at all before doors
    carried both sides: a floor plan knew its rooms and knew its doors, and
    nothing joined the two facts together.
    """
    pairs = set()
    for d in Door.objects.filter(room__floor_plan=floor_plan, is_active=True):
        if d.room_b_id:
            pairs.add((min(d.room_id, d.room_b_id), max(d.room_id, d.room_b_id)))
    return pairs


@admin_or_module_required('shelf')
def floor_plan_readiness(request):
    """What still stops this floor plan working, in one list.

    Everything checked here fails silently otherwise: a plan with no scale
    draws perfectly and simply never shows a position, a room with no waypoint
    in it is unreachable without saying so, and two walls a tenth of a unit
    apart look joined at every zoom level while refusing to connect.
    """
    plan = FloorPlan.objects.filter(floor_plan_id=request.GET.get('floor_plan_id')).first()
    if plan is None:
        return JsonResponse({'success': False, 'error': 'Floor plan not found'})

    issues = []

    def add(level, text, detail=''):
        issues.append({'level': level, 'text': text, 'detail': detail})

    rooms = list(Room.objects.filter(floor_plan=plan, is_active=True))
    waypoints = list(Waypoint.objects.filter(floor_plan=plan))
    beacons = list(BLEBeacon.objects.filter(floor_plan=plan))
    doors = Door.objects.filter(room__floor_plan=plan, is_active=True).count()

    # ---- positioning -----------------------------------------------------
    if not plan.pixels_per_meter:
        add('blocker', 'No scale set',
            'Beacon distances are in metres and the plan is drawn in canvas units. '
            'Without a scale the two cannot be combined, so no position is ever shown.')
    if len(beacons) < 3:
        add('blocker', '%d beacon(s) placed' % len(beacons),
            'Three is the minimum any position can be worked out from.')
    else:
        uncalibrated = [b for b in beacons if b.tx_power is None]
        if uncalibrated:
            add('warning', '%d beacon(s) not calibrated' % len(uncalibrated),
                'Without a measured 1 m reading every distance is a guess, and all of '
                'them being wrong by the same factor moves the whole fix.')
        unmeasured = [b for b in beacons if b.height is None]
        if unmeasured:
            add('warning', '%d beacon(s) have no mounting height' % len(unmeasured),
                'A beacon 2.4 m up reads as over a metre away from someone standing '
                'directly underneath it. The height is what takes that back out.')

    # ---- the plan itself -------------------------------------------------
    if not rooms:
        add('blocker', 'No rooms drawn', 'There is nothing to navigate around yet.')
    if not plan.is_active:
        add('warning', 'This plan is a draft',
            'Patrons are not shown a floor plan until it is set live.')

    # ---- routing ---------------------------------------------------------
    if not waypoints:
        add('blocker', 'No waypoints placed',
            'Routes are walked along waypoints. Without them the map can show a '
            'position but can never give directions.')
    else:
        wp_ids = set(w.waypoint_id for w in waypoints)
        links = list(WaypointConnection.objects.filter(
            waypoint_from_id__in=wp_ids, waypoint_to_id__in=wp_ids))

        parent = dict((i, i) for i in wp_ids)

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        joined = set()
        for c in links:
            joined.add(c.waypoint_from_id)
            joined.add(c.waypoint_to_id)
            ra, rb = find(c.waypoint_from_id), find(c.waypoint_to_id)
            if ra != rb:
                parent[ra] = rb

        orphans = wp_ids - joined
        if orphans:
            add('warning', '%d waypoint(s) connected to nothing' % len(orphans),
                'A waypoint with no connection is never walked through.')

        islands = len(set(find(i) for i in joined))
        if islands > 1:
            add('warning', 'The route network is in %d separate pieces' % islands,
                'A patron cannot get from one piece to another, so some books will '
                'have no route at all.')

        for room in rooms:
            geom = room.geometry or []
            if len(geom) < 3:
                continue
            if not any(_point_in_polygon(w.map_x, w.map_y, geom) for w in waypoints):
                add('warning', 'No waypoint inside "%s"' % room.name,
                    'Nothing in this room can be routed to.')

    # ---- geometry that looks right and is not ----------------------------
    if doors == 0 and len(rooms) > 1:
        add('warning', 'No doors placed',
            'Doors are what say where people may pass between rooms, and a '
            'connection through a wall without one is refused.')
    elif len(rooms) > 1:
        # Which rooms a door joins is what makes the plan a connected place
        # rather than a set of unrelated outlines.
        pairs = _room_adjacency(plan)
        linked = set()
        for a, b in pairs:
            linked.add(a)
            linked.add(b)
        stranded = [r.name for r in rooms if r.room_id not in linked]
        if stranded:
            add('warning',
                '%d room(s) not joined to any other by a door' % len(stranded),
                'No door records a way between %s and anywhere else, so nothing '
                'can say how a patron gets there.' % ', '.join('"%s"' % n for n in stranded))

        unlinked_doors = [d for d in Door.objects.filter(
            room__floor_plan=plan, is_active=True, room_b__isnull=True)]
        shared = []
        for d in unlinked_doors:
            facing = _facing_room(plan, d.map_x, d.map_y, d.room_id)
            if facing is not None:
                shared.append((d, facing))
        if shared:
            add('warning',
                '%d door(s) look shared but are not linked' % len(shared),
                'Another room\'s wall runs through %s. Linking them is what lets a '
                'route pass between the two.'
                % ', '.join('"%s"/"%s"' % (d.room.name, f.name) for d, f in shared))

    for i in range(len(rooms)):
        for j in range(i + 1, len(rooms)):
            ga = rooms[i].geometry or []
            gb = rooms[j].geometry or []
            if len(ga) < 3 or len(gb) < 3:
                continue
            gap = _polygon_gap(ga, gb)
            if 0 < gap < 2.0:
                add('warning',
                    '"%s" and "%s" almost touch' % (rooms[i].name, rooms[j].name),
                    'They are %.2f units apart. That is invisible on screen, but they '
                    'are two separate walls, so passing between them needs a door.' % gap)

    blockers = sum(1 for i in issues if i['level'] == 'blocker')
    return JsonResponse({
        'success': True,
        'plan': plan.name,
        'ready': blockers == 0,
        'blockers': blockers,
        'warnings': len(issues) - blockers,
        'issues': issues,
        'counts': {'rooms': len(rooms), 'doors': doors,
                   'waypoints': len(waypoints), 'beacons': len(beacons)},
    })


def _polygon_centroid(points):
    """Area-weighted centroid of a closed polygon, used as the room label anchor.

    Falls back to the arithmetic mean for degenerate (zero-area) input.
    """
    if not points:
        return 0.0, 0.0
    if len(points) < 3:
        return (
            sum(p[0] for p in points) / len(points),
            sum(p[1] for p in points) / len(points),
        )

    area = cx = cy = 0.0
    for i in range(len(points)):
        x0, y0 = points[i]
        x1, y1 = points[(i + 1) % len(points)]
        cross = x0 * y1 - x1 * y0
        area += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    area *= 0.5

    if abs(area) < 1e-9:
        return (
            sum(p[0] for p in points) / len(points),
            sum(p[1] for p in points) / len(points),
        )
    return cx / (6 * area), cy / (6 * area)


def _parse_geometry(raw):
    """Validate a posted polygon: a list of at least three [x, y] pairs.

    Returns (points, error). Points are floats so the centroid maths is safe.
    """
    if raw in (None, ''):
        return None, None
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None, 'Geometry must be valid JSON'

    if not isinstance(data, list) or len(data) < 3:
        return None, 'A room needs at least three points'

    points = []
    for pair in data:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            return None, 'Each geometry point must be an [x, y] pair'
        try:
            points.append([float(pair[0]), float(pair[1])])
        except (TypeError, ValueError):
            return None, 'Geometry coordinates must be numbers'
    return points, None


@admin_only_required
def get_map_data(request):
    """Return one floor plan plus its beacons, waypoints, connections and the
    list of shelves (for the waypoint-link dropdown) as JSON.

    Defaults to the active plan, but any plan may be requested by id so the
    Administrator can draw a new layout before making it live.
    """
    requested_id = request.GET.get('floor_plan_id')
    floor_plan = None
    if requested_id:
        try:
            floor_plan = FloorPlan.objects.filter(floor_plan_id=int(requested_id)).first()
        except (TypeError, ValueError):
            floor_plan = None
    if floor_plan is None:
        floor_plan = FloorPlan.objects.filter(is_active=True).first()
    if floor_plan is None:
        floor_plan = FloorPlan.objects.order_by('-uploaded_at').first()
    if floor_plan is None:
        return JsonResponse({'success': False, 'has_active': False, 'error': 'No floor plan yet'})

    width, height = _floorplan_canvas_size(floor_plan)

    beacons = [_beacon_payload(b) for b in BLEBeacon.objects.filter(floor_plan=floor_plan)]

    waypoints = [
        {
            'waypoint_id': w.waypoint_id,
            'map_x': w.map_x,
            'map_y': w.map_y,
            'label': w.label or '',
            'linked_shelf_id': w.linked_shelf.shelf_id if w.linked_shelf else None,
            'linked_shelf_name': w.linked_shelf.name if w.linked_shelf else None,
        }
        for w in Waypoint.objects.filter(floor_plan=floor_plan).select_related('linked_shelf')
    ]

    wp_ids = [w['waypoint_id'] for w in waypoints]
    connections = [
        {
            'connection_id': c.connection_id,
            'waypoint_from_id': c.waypoint_from_id,
            'waypoint_to_id': c.waypoint_to_id,
            'distance': c.distance,
        }
        for c in WaypointConnection.objects.filter(
            waypoint_from_id__in=wp_ids, waypoint_to_id__in=wp_ids
        )
    ]

    doors_by_room = _doors_for_floorplan(floor_plan)
    rooms = [
        _room_payload(r, doors_by_room, suggest_doors=True)
        for r in Room.objects.filter(floor_plan=floor_plan)
    ]

    obstacles = [
        _obstacle_payload(o)
        for o in Obstacle.objects.filter(floor_plan=floor_plan)
    ]

    stairways = [
        _stairway_payload(st)
        for st in Stairway.objects.select_related('connects_to').filter(floor_plan=floor_plan)
    ]

    shelves = [
        {
            'shelf_id': s.shelf_id, 'name': s.name,
            'kind': s.kind, 'label': s.label,
            'mount': s.mount, 'elevated': s.is_elevated,
            'map_x': s.map_x, 'map_y': s.map_y, 'rotation': s.rotation or 0,
            'width': s.width or 46, 'depth': s.depth or 14,
            # The outline to draw. Sent already resolved so no map has to know
            # the difference between a traced shelf and a rectangular one.
            'geometry': s.geometry or None,
            'footprint': s.footprint(),
            'placed': s.map_x is not None and s.map_y is not None,
            'room_id': s.room_id,
        }
        for s in Shelf.objects.filter(room__floor_plan=floor_plan).order_by('name')
    ]

    return JsonResponse({
        'success': True,
        'has_active': True,
        'floor_plan_id': floor_plan.floor_plan_id,
        'name': floor_plan.name,
        'is_active': floor_plan.is_active,
        'canvas_width': width,
        'canvas_height': height,
        'pixels_per_meter': floor_plan.pixels_per_meter,
        'north_offset_deg': floor_plan.north_offset_deg or 0,
        'beacons': beacons,
        'waypoints': waypoints,
        'connections': connections,
        'rooms': rooms,
        'obstacles': obstacles,
        'stairways': stairways,
        # For the "connects to" picker: every other floor in the building.
        'other_floors': [
            {'floor_plan_id': f.floor_plan_id, 'label': f.floor_label, 'name': f.name}
            for f in FloorPlan.objects.exclude(floor_plan_id=floor_plan.floor_plan_id)
                                      .order_by('floor_number', 'floor_plan_id')
        ],
        'shelves': shelves,
    })


def _stair_treads(geometry, bearing, count=None):
    """The tread lines that make a footprint read as steps.

    Computed here, once, rather than in each of the four maps that draw a stair.
    The footprint is measured along the direction of travel, the treads are laid
    across it at right angles, and each one is clipped to the shape's bounding
    box -- enough for the conventional symbol without pulling in a polygon
    clipping library for a rectangle that is nearly always a rectangle.

    Returns [[[x1, y1], [x2, y2]], ...] in canvas coordinates.
    """
    if not geometry or len(geometry) < 3:
        return []

    xs = [p[0] for p in geometry]
    ys = [p[1] for p in geometry]
    cx, cy = (min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0
    width, height = max(xs) - min(xs), max(ys) - min(ys)

    rad = math.radians(bearing or 0)
    # Unit vector along travel, and the one across it that the treads follow.
    ux, uy = math.sin(rad), -math.cos(rad)
    vx, vy = -uy, ux

    # How far the shape reaches along each axis. A projection of the bounding
    # box is close enough and cannot fail on a concave shape.
    along = abs(width * ux) + abs(height * uy)
    across = abs(width * vx) + abs(height * vy)
    if along <= 0 or across <= 0:
        return []

    if count is None:
        # About one tread every 12 canvas units, kept inside sane bounds so a
        # long staircase does not turn into a solid block of lines.
        count = int(max(3, min(14, round(along / 12.0))))

    treads = []
    for i in range(1, count):
        t = (i / float(count) - 0.5) * along
        mx, my = cx + ux * t, cy + uy * t
        half = across / 2.0
        treads.append([
            [round(mx - vx * half, 2), round(my - vy * half, 2)],
            [round(mx + vx * half, 2), round(my + vy * half, 2)],
        ])
    return treads


def _stairway_payload(st):
    """Everything a map needs to draw one stairway and say where it goes."""
    return {
        'stairway_id': st.stairway_id,
        'kind': st.kind,
        'kind_label': st.get_kind_display(),
        'name': st.name or '',
        'label': st.label,
        'geometry': st.geometry or [],
        'map_x': st.map_x,
        'map_y': st.map_y,
        'bearing': st.bearing or 0,
        'direction': st.direction,
        'connects_to': st.connects_to_id,
        'destination': st.destination_label,
        'is_active': st.is_active,
        # A lift has no treads; drawing them on one would be wrong, not merely
        # decorative.
        'treads': ([] if st.kind == 'Elevator'
                   else _stair_treads(st.geometry, st.bearing)),
    }


def _obstacle_payload(o):
    """Everything a map needs to draw one obstacle."""
    return {
        'obstacle_id': o.obstacle_id,
        'kind': o.kind,
        'kind_label': o.get_kind_display(),
        'name': o.name or '',
        'label': o.label,
        'geometry': o.geometry or [],
        'map_x': o.map_x,
        'map_y': o.map_y,
        'is_active': o.is_active,
    }


def _beacon_payload(b):
    """Everything the client needs to recognise this beacon and range from it."""
    return {
        'beacon_id': b.beacon_id,
        'beacon_uuid': b.beacon_uuid,
        'advertisement_type': b.advertisement_type or 'iBeacon',
        'major': b.major,
        'minor': b.minor,
        'namespace_id': (b.namespace_id or '').lower() or None,
        'instance_id': (b.instance_id or '').lower() or None,
        'tx_power': b.tx_power,
        'path_loss_n': b.path_loss_n,
        'height': b.height,
        'map_x': b.map_x,
        'map_y': b.map_y,
        'label': b.label or '',
    }


@admin_only_required
def add_beacon(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    floor_plan_id = request.POST.get('floor_plan_id')
    beacon_uuid = (request.POST.get('beacon_uuid') or '').strip()
    label = (request.POST.get('label') or '').strip()
    map_x = request.POST.get('map_x')
    map_y = request.POST.get('map_y')

    if not floor_plan_id or not beacon_uuid or map_x is None or map_y is None:
        return JsonResponse({'success': False, 'error': 'floor_plan_id, beacon_uuid, map_x and map_y are required'})

    floor_plan = FloorPlan.objects.filter(floor_plan_id=floor_plan_id).first()
    if floor_plan is None:
        return JsonResponse({'success': False, 'error': 'Floor plan not found'})

    try:
        map_x = float(map_x)
        map_y = float(map_y)
    except (TypeError, ValueError):
        return JsonResponse({'success': False, 'error': 'Invalid coordinates'})

    adv_type = (request.POST.get('advertisement_type') or 'iBeacon').strip()
    if adv_type not in dict(BLEBeacon.ADVERTISEMENT_TYPE_CHOICES):
        adv_type = 'iBeacon'

    def _int_or_none(key):
        raw = (request.POST.get(key) or '').strip()
        if raw == '':
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            raise ValueError(key)

    def _float_or_none(key):
        raw = (request.POST.get(key) or '').strip()
        if raw == '':
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            raise ValueError(key)

    try:
        major, minor, tx_power = _int_or_none('major'), _int_or_none('minor'), _int_or_none('tx_power')
        path_loss_n = _float_or_none('path_loss_n')
        height = _float_or_none('height')
    except ValueError as bad:
        return JsonResponse({'success': False, 'error': f'{bad} must be a number'})

    if height is not None and not (0 <= height <= 10):
        return JsonResponse({'success': False,
                             'error': 'Mounting height is measured in metres above the floor, '
                                      'so it should be between 0 and 10.'})

    beacon = BLEBeacon.objects.create(
        floor_plan=floor_plan,
        beacon_uuid=beacon_uuid,
        advertisement_type=adv_type,
        major=major,
        minor=minor,
        namespace_id=(request.POST.get('namespace_id') or '').strip().lower() or None,
        instance_id=(request.POST.get('instance_id') or '').strip().lower() or None,
        tx_power=tx_power,
        path_loss_n=path_loss_n,
        height=height,
        map_x=map_x,
        map_y=map_y,
        label=label or None,
    )
    return JsonResponse({'success': True, 'beacon': _beacon_payload(beacon)})


@admin_only_required
def delete_beacon(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    beacon_id = request.POST.get('beacon_id')
    if not beacon_id:
        return JsonResponse({'success': False, 'error': 'beacon_id is required'})

    BLEBeacon.objects.filter(beacon_id=beacon_id).delete()
    return JsonResponse({'success': True})


@admin_only_required
def move_beacon(request):
    """Reposition an existing beacon after a drag on the map canvas."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    beacon = BLEBeacon.objects.filter(beacon_id=request.POST.get('beacon_id')).first()
    if beacon is None:
        return JsonResponse({'success': False, 'error': 'Beacon not found'})

    try:
        map_x = float(request.POST.get('map_x'))
        map_y = float(request.POST.get('map_y'))
    except (TypeError, ValueError):
        return JsonResponse({'success': False, 'error': 'map_x and map_y are required'})

    beacon.map_x, beacon.map_y = map_x, map_y
    beacon.save(update_fields=['map_x', 'map_y'])
    log_admin_action(request, 'Move', 'Beacon', beacon.beacon_id,
                     f'{beacon.label or beacon.beacon_uuid} moved to ({map_x:.0f}, {map_y:.0f})')
    return JsonResponse({'success': True, 'map_x': beacon.map_x, 'map_y': beacon.map_y})


@admin_only_required
def update_beacon(request):
    """Change a beacon's identity or calibration after it has been placed.

    Everything here was settable when the beacon was added and nowhere
    afterwards, which made the most common correction — realising the hardware
    advertises as iBeacon rather than a service UUID — impossible without
    deleting the beacon and losing its position. A beacon matched on the wrong
    scheme produces no readings at all while looking perfectly configured, so
    being able to fix it is the difference between a working map and an
    afternoon spent suspecting the hardware.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    beacon = BLEBeacon.objects.filter(beacon_id=request.POST.get('beacon_id')).first()
    if beacon is None:
        return JsonResponse({'success': False, 'error': 'Beacon not found'})

    uuid_value = (request.POST.get('beacon_uuid') or '').strip()
    if not uuid_value:
        return JsonResponse({'success': False, 'error': 'Beacon UUID is required'})

    adv_type = (request.POST.get('advertisement_type') or '').strip()
    if adv_type not in dict(BLEBeacon.ADVERTISEMENT_TYPE_CHOICES):
        adv_type = beacon.advertisement_type

    def _int(field):
        raw = (request.POST.get(field) or '').strip()
        if raw == '':
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    def _float(field):
        raw = (request.POST.get(field) or '').strip()
        if raw == '':
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    path_loss = _float('path_loss_n')
    if path_loss is not None and not (1.0 <= path_loss <= 6.0):
        return JsonResponse({'success': False,
                             'error': 'Path-loss n is normally between 1.5 and 4. '
                                      'Free space is 2.0; a room with metal shelving is 2.5-3.5.'})
    tx_power = _int('tx_power')
    if tx_power is not None and not (-120 <= tx_power <= 0):
        return JsonResponse({'success': False,
                             'error': 'Tx power is an RSSI reading at 1 m, so it is negative '
                                      '— usually between -55 and -70.'})
    height = _float('height')
    if height is not None and not (0 <= height <= 10):
        return JsonResponse({'success': False,
                             'error': 'Mounting height is measured in metres above the floor, '
                                      'so it should be between 0 and 10.'})

    before = beacon.advertisement_type
    beacon.beacon_uuid = uuid_value
    beacon.label = (request.POST.get('label') or '').strip() or None
    beacon.advertisement_type = adv_type
    beacon.major = _int('major')
    beacon.minor = _int('minor')
    beacon.namespace_id = (request.POST.get('namespace_id') or '').strip() or None
    beacon.instance_id = (request.POST.get('instance_id') or '').strip() or None
    beacon.tx_power = tx_power
    beacon.path_loss_n = path_loss
    beacon.height = height
    beacon.save()

    detail = f'{beacon.label or beacon.beacon_uuid[:8]} — {adv_type}'
    if before != adv_type:
        detail += f' (was {before})'
    log_admin_action(request, 'Update', 'BLEBeacon', beacon.beacon_id, detail)
    return JsonResponse({'success': True, 'beacon': _beacon_payload(beacon)})


@admin_only_required
def calibrate_beacon(request):
    """Write back only tx_power and path_loss_n, fitted from measurements.

    Deliberately not update_beacon: that endpoint rewrites the whole identity
    (UUID, major/minor, namespace) from its POST, so calling it with just two
    calibration numbers would blank the very fields that decide whether the
    beacon is ever matched again. Calibration is measured far more often than
    identity is corrected, so it gets its own narrow endpoint.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    beacon = BLEBeacon.objects.filter(beacon_id=request.POST.get('beacon_id')).first()
    if beacon is None:
        return JsonResponse({'success': False, 'error': 'Beacon not found'})

    try:
        tx_power = int(float(request.POST.get('tx_power')))
        path_loss_n = float(request.POST.get('path_loss_n'))
    except (TypeError, ValueError):
        return JsonResponse({'success': False, 'error': 'tx_power and path_loss_n must be numbers'})

    if not (-120 <= tx_power <= 0):
        return JsonResponse({'success': False,
                             'error': 'Tx power is an RSSI reading at 1 m, so it is negative.'})
    if not (1.0 <= path_loss_n <= 6.0):
        return JsonResponse({'success': False,
                             'error': 'Path-loss n is normally between 1.5 and 4.'})

    before = (beacon.tx_power, beacon.path_loss_n)
    beacon.tx_power = tx_power
    beacon.path_loss_n = path_loss_n
    beacon.save(update_fields=['tx_power', 'path_loss_n'])
    log_admin_action(request, 'Calibrate', 'BLEBeacon', beacon.beacon_id,
                     f'{beacon.label or beacon.beacon_uuid[:8]}: '
                     f'tx {before[0]} -> {tx_power}, n {before[1]} -> {path_loss_n}')
    return JsonResponse({'success': True, 'beacon': _beacon_payload(beacon)})


@admin_only_required
def add_waypoint(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    floor_plan_id = request.POST.get('floor_plan_id')
    label = (request.POST.get('label') or '').strip()
    linked_shelf_id = (request.POST.get('linked_shelf_id') or '').strip()
    map_x = request.POST.get('map_x')
    map_y = request.POST.get('map_y')

    if not floor_plan_id or map_x is None or map_y is None:
        return JsonResponse({'success': False, 'error': 'floor_plan_id, map_x and map_y are required'})

    floor_plan = FloorPlan.objects.filter(floor_plan_id=floor_plan_id).first()
    if floor_plan is None:
        return JsonResponse({'success': False, 'error': 'Floor plan not found'})

    try:
        map_x = float(map_x)
        map_y = float(map_y)
    except (TypeError, ValueError):
        return JsonResponse({'success': False, 'error': 'Invalid coordinates'})

    linked_shelf = None
    if linked_shelf_id:
        linked_shelf = Shelf.objects.filter(shelf_id=linked_shelf_id).first()

    waypoint = Waypoint.objects.create(
        floor_plan=floor_plan,
        map_x=map_x,
        map_y=map_y,
        label=label or None,
        linked_shelf=linked_shelf,
    )
    return JsonResponse({
        'success': True,
        'waypoint': {
            'waypoint_id': waypoint.waypoint_id,
            'map_x': waypoint.map_x,
            'map_y': waypoint.map_y,
            'label': waypoint.label or '',
            'linked_shelf_id': linked_shelf.shelf_id if linked_shelf else None,
            'linked_shelf_name': linked_shelf.name if linked_shelf else None,
        },
    })


@admin_only_required
def move_waypoint(request):
    """Reposition an existing waypoint after a drag on the map canvas."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    waypoint = Waypoint.objects.filter(waypoint_id=request.POST.get('waypoint_id')).first()
    if waypoint is None:
        return JsonResponse({'success': False, 'error': 'Waypoint not found'})

    try:
        map_x = float(request.POST.get('map_x'))
        map_y = float(request.POST.get('map_y'))
    except (TypeError, ValueError):
        return JsonResponse({'success': False, 'error': 'map_x and map_y are required'})

    waypoint.map_x, waypoint.map_y = map_x, map_y
    waypoint.save(update_fields=['map_x', 'map_y'])
    log_admin_action(request, 'Move', 'Waypoint', waypoint.waypoint_id,
                     f'{waypoint.label or "Waypoint"} moved to ({map_x:.0f}, {map_y:.0f})')
    return JsonResponse({'success': True, 'map_x': waypoint.map_x, 'map_y': waypoint.map_y})


@admin_only_required
def delete_waypoint(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    waypoint_id = request.POST.get('waypoint_id')
    if not waypoint_id:
        return JsonResponse({'success': False, 'error': 'waypoint_id is required'})

    # Remove all connections referencing this waypoint (either direction), then the waypoint.
    WaypointConnection.objects.filter(
        Q(waypoint_from_id=waypoint_id) | Q(waypoint_to_id=waypoint_id)
    ).delete()
    Waypoint.objects.filter(waypoint_id=waypoint_id).delete()
    return JsonResponse({'success': True})


@admin_only_required
def add_waypoint_connection(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    from_id = request.POST.get('waypoint_from_id')
    to_id = request.POST.get('waypoint_to_id')

    if not from_id or not to_id:
        return JsonResponse({'success': False, 'error': 'waypoint_from_id and waypoint_to_id are required'})
    if from_id == to_id:
        return JsonResponse({'success': False, 'error': 'Cannot connect a waypoint to itself'})

    wp_from = Waypoint.objects.filter(waypoint_id=from_id).first()
    wp_to = Waypoint.objects.filter(waypoint_id=to_id).first()
    if wp_from is None or wp_to is None:
        return JsonResponse({'success': False, 'error': 'Waypoint not found'})

    # Avoid duplicate connections in either direction.
    existing = WaypointConnection.objects.filter(
        Q(waypoint_from=wp_from, waypoint_to=wp_to) |
        Q(waypoint_from=wp_to, waypoint_to=wp_from)
    ).first()
    if existing:
        return JsonResponse({'success': False, 'error': 'These waypoints are already connected'})

    if wp_from.floor_plan_id != wp_to.floor_plan_id:
        return JsonResponse({'success': False,
                             'error': 'Those waypoints are on different floors. '
                                      'Floors are joined by stairways, not connections.'})

    blocked = _walls_crossed(wp_from.floor_plan, wp_from.map_x, wp_from.map_y,
                             wp_to.map_x, wp_to.map_y)
    if blocked:
        names = ' and '.join(sorted(set(blocked)))
        return JsonResponse({
            'success': False,
            'error': f'This would walk through the wall of {names}. '
                     f'Put a door where people actually pass, then connect through it.',
        })

    distance = math.sqrt(
        (wp_from.map_x - wp_to.map_x) ** 2 + (wp_from.map_y - wp_to.map_y) ** 2
    )

    connection = WaypointConnection.objects.create(
        waypoint_from=wp_from,
        waypoint_to=wp_to,
        distance=distance,
    )
    return JsonResponse({
        'success': True,
        'connection': {
            'connection_id': connection.connection_id,
            'waypoint_from_id': wp_from.waypoint_id,
            'waypoint_to_id': wp_to.waypoint_id,
            'distance': distance,
        },
    })


@admin_only_required
def delete_waypoint_connection(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    connection_id = request.POST.get('connection_id')
    if not connection_id:
        return JsonResponse({'success': False, 'error': 'connection_id is required'})

    WaypointConnection.objects.filter(connection_id=connection_id).delete()
    return JsonResponse({'success': True})


# ─── PATRON NAVIGATION: A* PATHFINDING OVER THE WAYPOINT GRAPH ────
# How much a flight of stairs "costs" compared with walking on the flat.
#
# A staircase is not free and is not the same as its own footprint: climbing one
# takes appreciably longer than crossing the same distance on level ground, and
# a route that ignored that would happily send someone up and down two floors to
# save a few metres of corridor. Expressed in metres and converted per plan, so
# a floor drawn at a different scale still gets a sensible penalty.
STAIR_TRAVERSAL_METRES = 15.0
STAIR_TRAVERSAL_FALLBACK = 220.0     # canvas units, when a plan has no scale set

# How many waypoints a stairway hooks into on its own floor. One is enough in
# principle; two means a single badly-placed waypoint cannot strand the stairs.
STAIR_LINK_NEIGHBOURS = 2

# How far apart the two ends of one staircase may sit in plan before they are
# taken to be two different staircases. Generous, because two floor plans are
# separate images and are rarely traced to the same origin -- but far short of
# the distance between one stairwell and the next.
PAIR_MAX_OFFSET_METRES = 6.0
PAIR_MAX_OFFSET_FALLBACK = 90.0      # canvas units, when a plan has no scale set


def _stair_node(stairway_id):
    """Stairways live in the same graph as waypoints, under a string key.

    Keys stay mixed on purpose -- waypoints keep their integer ids so every
    response field and every client that reads waypoint_id keeps working, and
    stairways take 'S<id>' so the two can never collide.
    """
    return 'S%d' % stairway_id


def _pair_stairways(stairways, scale_by_floor=None):
    """Match each stairway to the one it meets on the floor above or below.

    A staircase is one object seen from two floors, so the pair is found by
    position: among the stairways on the destination floor, take the nearest in
    plan. A staircase sits above itself, so that is nearly always exactly right.

    Two stairways that name each other are unambiguous and always win. Failing
    that, one that names no floor at all will do, provided it is close enough
    overhead to be the same staircase -- because the common way to get this
    wrong is to link the flight going up and forget the one coming down, and a
    building where the stairs work in one direction only is not worth shipping
    over a checkbox. Anything further apart than PAIR_MAX_OFFSET_METRES is two
    different staircases and is left alone.

    Returns [(a, b), ...] with each pair listed once.
    """
    scale_by_floor = scale_by_floor or {}
    by_floor = {}
    for st in stairways:
        by_floor.setdefault(st.floor_plan_id, []).append(st)

    def offset(a, b):
        return math.hypot(a.map_x - b.map_x, a.map_y - b.map_y)

    def ceiling(a, b):
        ppm = scale_by_floor.get(a.floor_plan_id) or scale_by_floor.get(b.floor_plan_id)
        return (PAIR_MAX_OFFSET_METRES * ppm) if ppm else PAIR_MAX_OFFSET_FALLBACK

    pairs = []
    seen = set()
    for st in stairways:
        if not st.connects_to_id:
            continue
        over_there = by_floor.get(st.connects_to_id, [])
        candidates = [o for o in over_there if o.connects_to_id == st.floor_plan_id]
        if not candidates:
            candidates = [o for o in over_there
                          if not o.connects_to_id and offset(o, st) <= ceiling(o, st)]
        if not candidates:
            continue
        partner = min(candidates, key=lambda o: offset(o, st))
        key = tuple(sorted((st.stairway_id, partner.stairway_id)))
        if key in seen:
            continue
        seen.add(key)
        pairs.append((st, partner))
    return pairs


def _build_route_graph(floor_plans, include_stairs=True):
    """One graph over every floor given, joined wherever stairs pair up.

    Returns (coords, adjacency, node_floor, stair_nodes, edges_by_floor).

    `include_stairs=False` leaves the stairways out entirely, giving exactly the
    per-floor graph an Administrator drew. Hooking a stairway to the waypoints
    beside it asserts that the gap between them is walkable, which is true at a
    staircase and needless everywhere else -- so a route that stays on one floor
    does not pay for the assumption.

    Horizontal edges come from WaypointConnection exactly as before. The new
    part is vertical: each stairway becomes a node, hooks into the nearest
    waypoints on its own floor, and joins its partner on the other floor with a
    single weighted edge. A* then walks the whole building without knowing that
    floors exist -- which is the point, because a shortest path that has to be
    stitched together afterwards is not a shortest path.
    """
    floor_ids = [f.floor_plan_id for f in floor_plans]
    scale_by_floor = {f.floor_plan_id: (f.pixels_per_meter or 0) for f in floor_plans}

    waypoints = list(Waypoint.objects.filter(floor_plan_id__in=floor_ids))
    coords = {w.waypoint_id: (w.map_x, w.map_y) for w in waypoints}
    node_floor = {w.waypoint_id: w.floor_plan_id for w in waypoints}
    adjacency = {w.waypoint_id: [] for w in waypoints}
    edges_by_floor = {fid: [] for fid in floor_ids}

    wp_ids = set(coords)
    for c in WaypointConnection.objects.filter(
        waypoint_from_id__in=wp_ids, waypoint_to_id__in=wp_ids
    ):
        a, b = c.waypoint_from_id, c.waypoint_to_id
        # A connection drawn between two floors would be a data error rather
        # than a shortcut: the vertical links belong to the stairs.
        if node_floor.get(a) != node_floor.get(b):
            continue
        adjacency[a].append((b, c.distance))
        adjacency[b].append((a, c.distance))
        edges_by_floor[node_floor[a]].append((a, b))

    stairways = (list(Stairway.objects.filter(
        floor_plan_id__in=floor_ids, is_active=True)) if include_stairs else [])
    stair_nodes = {}
    for st in stairways:
        node = _stair_node(st.stairway_id)
        coords[node] = (st.map_x, st.map_y)
        node_floor[node] = st.floor_plan_id
        adjacency.setdefault(node, [])
        stair_nodes[node] = st

        # Hook it into the floor it stands on.
        same_floor = [w for w in waypoints if w.floor_plan_id == st.floor_plan_id]
        same_floor.sort(key=lambda w: math.hypot(w.map_x - st.map_x, w.map_y - st.map_y))
        for w in same_floor[:STAIR_LINK_NEIGHBOURS]:
            d = math.hypot(w.map_x - st.map_x, w.map_y - st.map_y)
            adjacency[node].append((w.waypoint_id, d))
            adjacency[w.waypoint_id].append((node, d))

    for a, b in _pair_stairways(stairways, scale_by_floor):
        na, nb = _stair_node(a.stairway_id), _stair_node(b.stairway_id)
        if na not in adjacency or nb not in adjacency:
            continue
        ppm = scale_by_floor.get(a.floor_plan_id) or scale_by_floor.get(b.floor_plan_id)
        cost = (STAIR_TRAVERSAL_METRES * ppm) if ppm else STAIR_TRAVERSAL_FALLBACK
        adjacency[na].append((nb, cost))
        adjacency[nb].append((na, cost))

    return coords, adjacency, node_floor, stair_nodes, edges_by_floor


def _astar(start_id, goal_id, coords, adjacency):
    """A* shortest path over the waypoint graph.

    coords: {waypoint_id: (x, y)}; adjacency: {waypoint_id: [(neighbor_id, weight), ...]}.
    Returns (ordered_waypoint_ids, total_cost) or (None, None) if no path exists.
    The heuristic is the straight-line (Euclidean) distance to the goal.
    """
    def h(node):
        ax, ay = coords[node]
        gx, gy = coords[goal_id]
        return math.hypot(ax - gx, ay - gy)

    open_heap = [(h(start_id), 0.0, start_id)]
    came_from = {}
    g_score = {start_id: 0.0}
    visited = set()

    while open_heap:
        _, g_cur, current = heapq.heappop(open_heap)
        if current == goal_id:
            path = [current]
            while current in came_from:
                current = came_from[current]
                path.append(current)
            path.reverse()
            return path, g_cur
        if current in visited:
            continue
        visited.add(current)
        for neighbor, weight in adjacency.get(current, []):
            tentative = g_cur + weight
            if neighbor not in g_score or tentative < g_score[neighbor]:
                g_score[neighbor] = tentative
                came_from[neighbor] = current
                heapq.heappush(open_heap, (tentative + h(neighbor), tentative, neighbor))
    return None, None


def get_patron_map_data(request):
    """Read-only map payload for the patron navigation map: one floor's rooms,
    shelves, waypoints, beacons and connections, plus the list of floors.

    Which floor is decided by ?floor=<id> when the patron used the switcher,
    and otherwise by the lowest floor in service. The floors list is what the
    switcher is built from, so a library with one floor simply gets a list of
    one and the control hides itself.
    """
    # ?shelf= lets the map open on the floor the target book is on, without the
    # client having to know which floor that is -- it only knows the shelf.
    raw_shelf = (request.GET.get('shelf') or '').strip()
    target_shelf = (Shelf.objects.filter(shelf_id=raw_shelf).first()
                    if raw_shelf.isdigit() else None)
    floor_plan, live_plans = _floor_for_request(request, target_shelf=target_shelf)
    if floor_plan is None:
        return JsonResponse({'success': False, 'has_active': False,
                             'error': 'No floor plan is in service'})

    width, height = _floorplan_canvas_size(floor_plan)

    doors_by_room = _doors_for_floorplan(floor_plan, active_only=True)
    rooms = [
        _room_payload(r, doors_by_room)
        for r in Room.objects.filter(floor_plan=floor_plan, is_active=True)
    ]
    # Unplaced shelves have no position, so they cannot be drawn or navigated to.
    shelf_qs = (
        Shelf.objects
        .filter(room__floor_plan=floor_plan, is_active=True,
                map_x__isnull=False, map_y__isnull=False)
        .prefetch_related('shelflevel_set')
    )
    shelves = [
        {
            'shelf_id': s.shelf_id, 'name': s.name,
            'kind': s.kind, 'label': s.label,
            'mount': s.mount, 'elevated': s.is_elevated,
            'x': s.map_x, 'y': s.map_y, 'rotation': s.rotation or 0,
            'width': s.width or 46, 'depth': s.depth or 14,
            'geometry': s.geometry or None,
            'footprint': s.footprint(),
            # Feeds the "section labels" and "shelf level indicators" layers.
            'levels': [
                {'level_number': lv.level_number, 'category': lv.category or '',
                 'is_top': lv.is_top, 'is_under': lv.is_under,
                 'column_number': lv.column_number, 'label': lv.label}
                for lv in s.shelflevel_set.all() if lv.is_active
            ],
        }
        for s in shelf_qs
    ]
    waypoints = [
        {
            'waypoint_id': w.waypoint_id,
            'x': w.map_x,
            'y': w.map_y,
            'label': w.label or '',
            'linked_shelf_id': w.linked_shelf_id,
        }
        for w in Waypoint.objects.filter(floor_plan=floor_plan)
    ]
    beacons = [
        # Same serialiser as the editor, plus x/y aliases the patron map uses.
        dict(_beacon_payload(b), x=b.map_x, y=b.map_y)
        for b in BLEBeacon.objects.filter(floor_plan=floor_plan)
    ]
    wp_ids = [w['waypoint_id'] for w in waypoints]
    connections = [
        {'from': c.waypoint_from_id, 'to': c.waypoint_to_id}
        for c in WaypointConnection.objects.filter(
            waypoint_from_id__in=wp_ids, waypoint_to_id__in=wp_ids
        )
    ]

    return JsonResponse({
        'success': True,
        'has_active': True,
        'floor_plan_id': floor_plan.floor_plan_id,
        'name': floor_plan.name,
        'canvas_width': width,
        'canvas_height': height,
        # Null until an Administrator measures it; the client refuses to
        # trilaterate without it rather than mixing metres with canvas units.
        'pixels_per_meter': floor_plan.pixels_per_meter,
        'north_offset_deg': floor_plan.north_offset_deg or 0,
        'floor_number': floor_plan.floor_number,
        'floor_label': floor_plan.floor_label,
        # What the floor switcher is built from. One floor gives a list
        # of one, and the control hides itself.
        'floors': _floor_payload(live_plans, floor_plan),
        # Which floor the requested shelf is on, so the map can say "it is on
        # the 2nd floor" when the patron is looking at a different one.
        'target_floor_id': (
            live_plans.filter(room__shelf=target_shelf).values_list('floor_plan_id', flat=True).first()
            if target_shelf is not None else None
        ),
        'renovation_notice': floor_plan.renovation_notice or '',
        'rooms': rooms,
        # Tables, counters and pillars. A patron following a route needs to see
        # what is physically in the way, not an empty room with shelves in it.
        'obstacles': [
            {'obstacle_id': o.obstacle_id, 'kind': o.kind, 'label': o.label,
             'geometry': o.geometry or [], 'map_x': o.map_x, 'map_y': o.map_y}
            for o in Obstacle.objects.filter(floor_plan=floor_plan, is_active=True)
            if o.geometry
        ],
        # The only objects that mean anything on another floor. Sent even when
        # the patron is not going anywhere: knowing where the stairs are is
        # useful on its own.
        'stairways': [
            _stairway_payload(st)
            for st in Stairway.objects.select_related('connects_to').filter(
                floor_plan=floor_plan, is_active=True)
            if st.geometry
        ],
        'shelves': shelves,
        'waypoints': waypoints,
        'beacons': beacons,
        'connections': connections,
    })


def get_navigation_route(request):
    """A* from the patron's position to a target shelf (or waypoint).

    The graph spans the whole building, not one floor. Waypoints carry the
    corridors an Administrator drew; each stairway is a node in that same graph,
    hooked into the waypoints beside it and joined to its opposite number on the
    floor it reaches. A* therefore runs once, over one graph, and the path it
    returns is genuinely the shortest way to the book -- including which of
    three staircases to use, which is exactly the question a two-leg route
    stitched together afterwards cannot answer.

    Floors reappear only when the answer is drawn. A map can only show one, so
    the path is split at the stairs into `legs`, and `route` stays the leg for
    the floor the patron is standing on -- the same shape, in the same field,
    that every existing caller already draws.

    `from_floor` is where the patron is. Without it the target's floor is
    assumed, which is what a single-floor caller wants and always got.
    """
    target_shelf_id = request.GET.get('target_shelf_id')
    resolving_shelf = (Shelf.objects.filter(shelf_id=target_shelf_id).first()
                       if target_shelf_id else None)
    target_floor, _live = _floor_for_request(request, target_shelf=resolving_shelf)
    # Derived from the shelf itself, never from ?floor=. _floor_for_request lets
    # an explicit ?floor= win -- correct for "which map am I looking at", wrong
    # for "which floor is the book on", and the patron map always sends one. Read
    # the other way round, a request from floor 1 for a shelf on floor 2 looked
    # like a same-floor request and quietly produced no route at all.
    if resolving_shelf is not None and resolving_shelf.room_id:
        shelf_floor = FloorPlan.objects.filter(
            room__shelf=resolving_shelf, is_active=True).first()
        if shelf_floor is not None:
            target_floor = shelf_floor
    if target_floor is None:
        return JsonResponse({'success': False, 'error': 'No floor plan is in service'})

    # Which floor the patron is standing on. Normally the target's; different
    # only when they say otherwise.
    floor_plan = target_floor
    raw_from = (request.GET.get('from_floor') or '').strip()
    if raw_from.isdigit() and int(raw_from) != target_floor.floor_plan_id:
        standing_on = FloorPlan.objects.filter(
            floor_plan_id=int(raw_from), is_active=True).first()
        if standing_on is not None:
            floor_plan = standing_on
    crossing_floors = floor_plan.floor_plan_id != target_floor.floor_plan_id
    here_id = floor_plan.floor_plan_id

    try:
        start_x = float(request.GET.get('start_x'))
        start_y = float(request.GET.get('start_y'))
    except (TypeError, ValueError):
        return JsonResponse({'success': False, 'error': 'start_x and start_y are required'})

    target_waypoint_id = request.GET.get('target_waypoint_id')
    if not target_shelf_id and not target_waypoint_id:
        return JsonResponse({'success': False, 'error': 'A target_shelf_id or target_waypoint_id is required'})

    # Only widen the graph when the walk actually leaves this floor. A same-floor
    # route then traverses precisely the graph it always did -- the stair links
    # are an assumption about what is walkable near a staircase, and there is no
    # reason to let that assumption anywhere near a route that does not need it.
    if crossing_floors:
        floors = list(FloorPlan.objects.filter(is_active=True))
        known = {f.floor_plan_id for f in floors}
        for f in (floor_plan, target_floor):
            if f.floor_plan_id not in known:
                floors.append(f)
                known.add(f.floor_plan_id)
    else:
        floors = [floor_plan]

    coords, adjacency, node_floor, stair_nodes, edges_by_floor = _build_route_graph(
        floors, include_stairs=crossing_floors)
    edges = edges_by_floor.get(here_id, [])
    # Waypoints specifically, not nodes: a floor with a staircase drawn on it
    # but no corridor yet has nowhere for a route to start, and asking A* to
    # begin at nothing is how this returns a 500 instead of an explanation.
    if not any(isinstance(n, int) and f == here_id for n, f in node_floor.items()):
        return JsonResponse({'success': False, 'error': 'No waypoints configured for this floor plan'})

    def nearest_waypoint(x, y, floor_id):
        """Nearest real waypoint on one floor. Stair nodes and the spliced start
        node are keyed by string, so the integer keys are exactly the waypoints."""
        best_id, best_d = None, None
        for wid, (wx, wy) in coords.items():
            if not isinstance(wid, int) or node_floor.get(wid) != floor_id:
                continue
            d = math.hypot(wx - x, wy - y)
            if best_d is None or d < best_d:
                best_d, best_id = d, wid
        return best_id

    # The corridor the librarian actually drew is a better start than whichever
    # end of it happens to be nearest: a patron standing mid-corridor snapped to
    # one endpoint used to route them there first, and as they kept walking the
    # snap would flip to the other endpoint and the drawn route would visibly
    # jump between two different paths for what was smooth, continuous motion.
    # Projecting onto the nearest edge fixes both -- the route starts from
    # wherever they are actually standing along it.
    START_SENTINEL = '__start__'
    EDGE_SNAP_EPSILON = 1e-6   # a projection this close to an endpoint IS that endpoint

    def nearest_point_on_edges(x, y):
        best = None   # (distance, a, b, proj_x, proj_y, t)
        for a, b in edges:
            ax, ay = coords[a]
            bx, by = coords[b]
            dx, dy = bx - ax, by - ay
            seg_len2 = dx * dx + dy * dy
            if seg_len2 <= EDGE_SNAP_EPSILON:
                t = 0.0   # a and b coincide -- treat the edge as a point
            else:
                t = ((x - ax) * dx + (y - ay) * dy) / seg_len2
                t = max(0.0, min(1.0, t))
            px, py = ax + t * dx, ay + t * dy
            d = math.hypot(x - px, y - py)
            if best is None or d < best[0]:
                best = (d, a, b, px, py, t)
        return best

    start_wp = nearest_waypoint(start_x, start_y, here_id)
    edge_hit = nearest_point_on_edges(start_x, start_y) if edges else None
    if edge_hit is not None:
        _dist, a, b, px, py, t = edge_hit
        if t > EDGE_SNAP_EPSILON and t < 1 - EDGE_SNAP_EPSILON:
            # Genuinely mid-edge: splice a start node into the graph at the
            # projected point. Split distances come from the live coordinates
            # rather than prorating WaypointConnection.distance, which is set
            # once when the connection is drawn and never recalculated if a
            # waypoint is moved afterward -- it can quietly drift from the
            # true geometry, while re-deriving from coords cannot.
            coords[START_SENTINEL] = (px, py)
            node_floor[START_SENTINEL] = here_id
            ax, ay = coords[a]
            bx, by = coords[b]
            adjacency[START_SENTINEL] = [
                (a, math.hypot(px - ax, py - ay)),
                (b, math.hypot(px - bx, py - by)),
            ]
            start_wp = START_SENTINEL
        elif t <= EDGE_SNAP_EPSILON:
            start_wp = a
        else:
            start_wp = b

    # -- The goal, which now sits on the target floor rather than this one --
    target_shelf = None
    if target_waypoint_id:
        try:
            goal_wp = int(target_waypoint_id)
        except ValueError:
            return JsonResponse({'success': False, 'error': 'Invalid target_waypoint_id'})
        if goal_wp not in coords:
            return JsonResponse({'success': False, 'error': 'Target waypoint not found on this floor plan'})
    else:
        # Already looked up above to decide which floor this route belongs to.
        target_shelf = resolving_shelf
        if target_shelf is None:
            return JsonResponse({'success': False, 'error': 'Target shelf not found'})
        linked = Waypoint.objects.filter(floor_plan=target_floor, linked_shelf=target_shelf).first()
        if linked is not None:
            goal_wp = linked.waypoint_id
        elif target_shelf.map_x is None or target_shelf.map_y is None:
            return JsonResponse({
                'success': False,
                'error': "This book's shelf has not been placed on the floor plan yet. "
                         "Please ask library staff for directions.",
            })
        else:
            goal_wp = nearest_waypoint(target_shelf.map_x, target_shelf.map_y,
                                       target_floor.floor_plan_id)
        if goal_wp is None:
            return JsonResponse({
                'success': False,
                'error': 'No waypoints have been drawn on the %s yet.' % target_floor.floor_label,
            })

    path, total = _astar(start_wp, goal_wp, coords, adjacency)

    # No continuous path across the floors, so the two are two islands in the
    # graph. Usually the staircases were drawn but never linked to each other;
    # sometimes there is simply no corridor drawn upstairs yet. Either way,
    # walking the patron to real stairs and telling them what to do at the top
    # beats refusing to help -- the same guidance this endpoint gave before the
    # graph spanned floors. `stairs_only` says that is what came back.
    stairs_only = False
    fallback_stairway = None
    if path is None and crossing_floors:
        options = list(Stairway.objects.select_related('connects_to').filter(
            floor_plan=floor_plan, is_active=True, connects_to=target_floor))
        if not options:
            options = list(Stairway.objects.select_related('connects_to').filter(
                floor_plan=floor_plan, is_active=True))
        if options:
            fallback_stairway = min(
                options, key=lambda st: math.hypot(st.map_x - start_x, st.map_y - start_y))
            stair_goal = nearest_waypoint(
                fallback_stairway.map_x, fallback_stairway.map_y, here_id)
            if stair_goal is not None:
                path, total = _astar(start_wp, stair_goal, coords, adjacency)
                if path is not None:
                    stairs_only = True
                    goal_wp = stair_goal
                    target_shelf = None
        if path is None:
            return JsonResponse({
                'success': False,
                'cross_floor': True,
                'target_floor': {'floor_plan_id': target_floor.floor_plan_id,
                                 'label': target_floor.floor_label},
                'error': 'That shelf is on the %s, and no route reaches it from here. '
                         'Ask an Administrator to draw a stairway on each floor and '
                         'link them to one another.' % target_floor.floor_label,
            })

    if path is None:
        return JsonResponse({'success': False, 'error': 'No path found between your position and the target'})

    def point(node):
        x, y = coords[node]
        st = stair_nodes.get(node)
        return {
            'waypoint_id': (None if isinstance(node, str) else node),
            'stairway_id': (st.stairway_id if st is not None else None),
            'floor_plan_id': node_floor.get(node),
            'x': x, 'y': y,
        }

    points = [point(n) for n in path]

    # One map draws one floor, so the path is cut where it changes floor. Both
    # ends of a stair link survive the cut: the leg below finishes at the foot of
    # the stairs and the leg above begins at the landing, which is what somebody
    # reading either map needs to see.
    label_of = {f.floor_plan_id: f.floor_label for f in floors}
    legs = []
    for pt in points:
        if not legs or legs[-1]['floor_plan_id'] != pt['floor_plan_id']:
            legs.append({
                'floor_plan_id': pt['floor_plan_id'],
                'floor_label': label_of.get(pt['floor_plan_id'], ''),
                'points': [],
                'distance': 0.0,
            })
        leg = legs[-1]
        if leg['points']:
            prev = leg['points'][-1]
            leg['distance'] += math.hypot(pt['x'] - prev['x'], pt['y'] - prev['y'])
        leg['points'].append(pt)
    for leg in legs:
        leg['distance'] = round(leg['distance'], 2)

    here_leg = next((l for l in legs if l['floor_plan_id'] == here_id), legs[0])

    response = {
        'success': True,
        # This floor's leg, under the name every existing caller already draws.
        'route': here_leg['points'],
        # `distance` is the whole walk, and carries the stair-climb allowance
        # that made A* prefer one staircase over another; `leg_distance` is the
        # part of it drawn on this map.
        'distance': total,
        'leg_distance': here_leg['distance'],
        'legs': legs,
        'start_waypoint_id': None if start_wp == START_SENTINEL else start_wp,
        'start_snapped_to_edge': start_wp == START_SENTINEL,
        'goal_waypoint_id': None if isinstance(goal_wp, str) else goal_wp,
    }
    if target_shelf is not None:
        response['target_shelf'] = {
            'shelf_id': target_shelf.shelf_id,
            'name': target_shelf.name,
            'x': target_shelf.map_x,
            'y': target_shelf.map_y,
        }

    if crossing_floors:
        # The staircase A* chose -- the first one the path meets on this floor.
        via_stairway = fallback_stairway or next(
            (stair_nodes[n] for n in path
             if n in stair_nodes and node_floor.get(n) == here_id), None)

        response['cross_floor'] = True
        response['floors_crossed'] = len(legs)
        response['stairs_only'] = stairs_only
        response['leg'] = 'to_stairs' if stairs_only else 'full'
        response['target_floor'] = {
            'floor_plan_id': target_floor.floor_plan_id,
            'label': target_floor.floor_label,
        }
        if via_stairway is not None:
            response['via_stairway'] = {
                'stairway_id': via_stairway.stairway_id,
                'kind': via_stairway.kind,
                'label': via_stairway.label,
                'x': via_stairway.map_x,
                'y': via_stairway.map_y,
                'linked': via_stairway.connects_to_id == target_floor.floor_plan_id,
            }
            verb = 'Take the lift' if via_stairway.kind == 'Elevator' else (
                'Take the ramp' if via_stairway.kind == 'Ramp' else 'Take the stairs')
            arrow = {'up': 'up', 'down': 'down', 'both': ''}.get(via_stairway.direction, '')
            # Only name it when it has been given a name. An unnamed lift produced
            # "Take the lift at Lift", because label falls back to the kind.
            where = ' at %s' % via_stairway.label if (via_stairway.name or '').strip() else ''
            going = ' %s' % arrow if arrow else ''
            response['instruction'] = (
                '%s%s%s to the %s, then follow the map from there.'
                % (verb, where, going, target_floor.floor_label)
            )
    return JsonResponse(response)

# ─── ENTRY/EXIT LOGGING & PATRON REGISTRATION (Admin Log Management) ──
# NOTE: The Patron model stores a single `fullname` (no firstname/lastname
# columns) and requires `password_hash`. The registration form collects a
# first/last name which are combined into `fullname`, and desk-created
# patrons get an unusable password (they can set one later via the portal).
def _patron_brief(patron):
    return {
        'patron_id': patron.patron_id,
        'fullname': patron.fullname,
        'patron_type': patron.patron_type,
        'account_status': patron.account_status,
    }


def _session_brief(log):
    entry = timezone.localtime(log.entry_time) if log.entry_time else None
    exit_dt = timezone.localtime(log.exit_time) if log.exit_time else None
    duration = ''
    if entry and exit_dt:
        secs = max(int((exit_dt - entry).total_seconds()), 0)
        hours, minutes = secs // 3600, (secs % 3600) // 60
        duration = (f'{hours}h ' if hours else '') + f'{minutes}m'
    return {
        'log_id': log.log_id,
        'school': log.school or '',
        'purpose_of_visit': log.purpose_of_visit or '',
        'entry_time': entry.strftime('%b %d, %Y · %I:%M %p') if entry else '',
        'exit_time': exit_dt.strftime('%b %d, %Y · %I:%M %p') if exit_dt else '',
        'duration': duration,
    }


@granted_module_required('logs')
def entry_log_start(request):
    """Entry: verify the patron by name + email, then open a visit session.

    Patrons are identified by their (unique) email, so two people with the same
    name are disambiguated. Unknown emails fall through to registration.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    name = (request.POST.get('name') or '').strip()
    email = (request.POST.get('email') or '').strip()
    school = (request.POST.get('school') or '').strip()
    purpose = (request.POST.get('purpose_of_visit') or '').strip()

    if not name or not email:
        return JsonResponse({'success': False, 'error': 'Name and email are required.'})

    patron = Patron.objects.filter(email__iexact=email).first()
    if patron is None:
        # Not registered -> let the desk register them (form is prefilled).
        return JsonResponse({'success': True, 'found': False})

    open_session = PatronLog.objects.filter(patron=patron, exit_time__isnull=True).first()
    if open_session is not None:
        return JsonResponse({
            'success': False,
            'error': f'{patron.fullname} already has an active entry with no exit recorded.',
        })

    log = PatronLog.objects.create(
        patron=patron,
        school=school or None,
        purpose_of_visit=purpose or None,
        entry_time=timezone.now(),
    )
    return JsonResponse({
        'success': True, 'found': True, 'action': 'entry',
        'patron': _patron_brief(patron), 'log': _session_brief(log),
    })


@granted_module_required('logs')
def entry_log_register(request):
    """Register a new patron, then open their first visit session (entry)."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    firstname = ' '.join((request.POST.get('firstname') or '').split())
    middlename = ' '.join((request.POST.get('middlename') or '').split())
    lastname = ' '.join((request.POST.get('lastname') or '').split())
    email = (request.POST.get('email') or '').strip()
    contact_number = (request.POST.get('contact_number') or '').strip()
    address = (request.POST.get('address') or '').strip()
    patron_type = (request.POST.get('patron_type') or '').strip()
    school = (request.POST.get('school') or '').strip()
    purpose = (request.POST.get('purpose_of_visit') or '').strip()

    # On-site identity validation (Ch.1 ¶242, Fig. 5): the patron presents a
    # physical ID and the desk verifies it on the spot. Nothing about the
    # document is stored - only that a named staff member checked it.
    id_confirmed = (request.POST.get('id_confirmed') or '').strip() in ('1', 'true', 'on', 'yes')

    valid_types = [choice[0] for choice in Patron.PATRON_TYPE_CHOICES]
    if not all([firstname, lastname, email, contact_number, address, patron_type]):
        return JsonResponse({'success': False, 'error': 'All fields are required.'})
    if patron_type not in valid_types:
        return JsonResponse({'success': False, 'error': 'Please choose a valid patron type.'})
    if not id_confirmed:
        return JsonResponse({'success': False,
                             'error': 'Confirm that you checked the physical ID before registering.'})

    from django.core.validators import validate_email as _validate_email
    from django.core.exceptions import ValidationError as _ValidationError
    try:
        _validate_email(email)
    except _ValidationError:
        return JsonResponse({'success': False, 'error': 'Please enter a valid email address.'})

    if Patron.objects.filter(email__iexact=email).exists():
        return JsonResponse({'success': False, 'error': 'A patron with this email already exists.'})

    verifier = User.objects.filter(admin_id=request.session.get('admin_id')).first()
    patron = Patron.objects.create(
        fullname=compose_name(firstname, middlename, lastname),
        first_name=firstname,
        middle_name=middlename,
        last_name=lastname,
        email=email,
        contact_number=contact_number,
        address=address,
        patron_type=patron_type,
        # Active on the spot because the ID was checked in person, and the
        # check is now on the record rather than merely assumed.
        account_status='Active',
        password_hash=hash_password(None),  # unusable until set via the portal
        registration_channel='On-site',
        qr_code=str(uuid4()),
        otp_verified=True,
        identity_verified_by=verifier,
        identity_verified_at=timezone.now(),
    )
    log_admin_action(request, 'Create', 'Patron', patron.patron_id,
                     f'On-site registration of "{patron.fullname}" — physical ID '
                     f'presented and checked at the desk')
    log = PatronLog.objects.create(
        patron=patron,
        school=school or None,
        purpose_of_visit=purpose or None,
        entry_time=timezone.now(),
    )
    return JsonResponse({
        'success': True, 'registered': True, 'action': 'entry',
        'patron': _patron_brief(patron), 'log': _session_brief(log),
    })


@granted_module_required('logs')
def entry_log_exit(request):
    """Exit: find the patron by name + email and close their open session."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    name = (request.POST.get('name') or '').strip()
    email = (request.POST.get('email') or '').strip()
    if not name or not email:
        return JsonResponse({'success': False, 'error': 'Name and email are required.'})

    patron = Patron.objects.filter(email__iexact=email).first()
    if patron is None:
        return JsonResponse({'success': False, 'error': 'No registered patron found with that email.'})

    open_session = PatronLog.objects.filter(
        patron=patron, exit_time__isnull=True
    ).order_by('-entry_time').first()
    if open_session is None:
        return JsonResponse({'success': False, 'error': f'No active entry found for {patron.fullname}.'})

    open_session.exit_time = timezone.now()
    open_session.save()
    return JsonResponse({
        'success': True, 'action': 'exit',
        'patron': _patron_brief(patron), 'log': _session_brief(open_session),
    })


@granted_module_required('logs')
def edit_patron_log(request):
    """Edit a visit log's school, purpose, and entry/exit times."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    log = PatronLog.objects.filter(log_id=request.POST.get('log_id')).first()
    if log is None:
        return JsonResponse({'success': False, 'error': 'Log not found'})

    school = (request.POST.get('school') or '').strip()
    purpose = (request.POST.get('purpose_of_visit') or '').strip()
    entry_raw = (request.POST.get('entry_time') or '').strip()
    exit_raw = (request.POST.get('exit_time') or '').strip()

    if not entry_raw:
        return JsonResponse({'success': False, 'error': 'Entry time is required.'})

    from datetime import datetime

    def _parse_local(value):
        # <input type="datetime-local"> sends 'YYYY-MM-DDThh:mm' in the admin's
        # local (PH) wall-clock time; make it timezone-aware.
        for fmt in ('%Y-%m-%dT%H:%M', '%Y-%m-%dT%H:%M:%S'):
            try:
                naive = datetime.strptime(value, fmt)
            except ValueError:
                continue
            return timezone.make_aware(naive) if timezone.is_naive(naive) else naive
        raise ValueError('Invalid datetime')

    try:
        entry_dt = _parse_local(entry_raw)
        exit_dt = _parse_local(exit_raw) if exit_raw else None
    except ValueError:
        return JsonResponse({'success': False, 'error': 'Invalid date/time format.'})

    if exit_dt is not None and exit_dt < entry_dt:
        return JsonResponse({'success': False, 'error': 'Exit time cannot be earlier than entry time.'})

    log.school = school or None
    log.purpose_of_visit = purpose or None
    log.entry_time = entry_dt
    log.exit_time = exit_dt
    log.save()
    return JsonResponse({'success': True})


@granted_module_required('logs')
def delete_patron_log(request):
    """Delete a visit log (form POST from the Log Management table)."""
    if request.method == 'POST':
        PatronLog.objects.filter(log_id=request.POST.get('log_id')).delete()
    return portal_redirect(request, 'admin_log_management')


# ─── USER MANAGEMENT (Admin-only: manage Library Staff accounts) ───────────
@admin_only_required
def user_management(request):
    """List all non-patron accounts (Admin + Library Staff)."""
    q = (request.GET.get('q') or '').strip()
    role = (request.GET.get('role') or '').strip()
    status = (request.GET.get('status') or '').strip()

    users = User.objects.all().order_by('fullname')
    if q:
        users = users.filter(Q(fullname__icontains=q) | Q(email__icontains=q))
    if role in dict(User.ROLE_CHOICES):
        users = users.filter(role=role)
    if status in dict(User.STATUS_CHOICES):
        users = users.filter(account_status=status)

    context = {
        'users': users,
        'total_users': User.objects.count(),
        'total_staff': User.objects.filter(role='Staff').count(),
        'total_admins': User.objects.filter(role='Admin').count(),
        'active_users': User.objects.filter(account_status='Active').count(),
        'q': q,
        'role_filter': role,
        'status_filter': status,
        'role_choices': User.ROLE_CHOICES,
        'status_choices': User.STATUS_CHOICES,
        'staff_module_choices': STAFF_MODULES,
    }
    return render(request, 'admin/usermanagement.html', context)


@admin_only_required
def create_staff(request):
    """Create a new staff/admin account."""
    if request.method != 'POST':
        return redirect('user_management')

    fullname = (request.POST.get('fullname') or '').strip()
    email = (request.POST.get('email') or '').strip().lower()
    password = request.POST.get('password') or ''
    role = request.POST.get('role') or 'Staff'
    status = request.POST.get('account_status') or 'Active'

    if not fullname or not email or not password:
        messages.error(request, 'Full name, email, and password are required.')
        return redirect('user_management')
    if role not in dict(User.ROLE_CHOICES):
        role = 'Staff'
    if status not in dict(User.STATUS_CHOICES):
        status = 'Active'
    if User.objects.filter(email=email).exists():
        messages.error(request, 'An account with that email already exists.')
        return redirect('user_management')

    # Both roles carry module grants. Staff need at least one or the account
    # can do nothing; an Administrator may hold none, which is the default and
    # leaves them the governing and oversight pages.
    module_keys = clean_module_keys(request.POST.get('modules', ''))
    if role == 'Staff' and not module_keys:
        messages.error(request, 'Select at least one module for this staff account.')
        return redirect('user_management')

    user = User.objects.create(
        fullname=fullname,
        email=email,
        password_hash=hash_password(password),
        role=role,
        account_status=status,
        modules=','.join(module_keys),
    )
    detail = f'Created {role} account {email}'
    if role == 'Staff':
        detail += f' with modules: {", ".join(user.module_labels)}'
    log_admin_action(request, 'Create', 'User', user.admin_id, detail)
    messages.success(request, f'{role} account for {fullname} created.')
    return redirect('user_management')


@admin_only_required
def edit_staff(request, user_id):
    """Edit name/email/role/status of an existing account."""
    user = get_object_or_404(User, admin_id=user_id)
    if request.method != 'POST':
        return redirect('user_management')

    fullname = (request.POST.get('fullname') or '').strip()
    email = (request.POST.get('email') or '').strip().lower()
    role = request.POST.get('role') or user.role
    status = request.POST.get('account_status') or user.account_status

    if not fullname or not email:
        messages.error(request, 'Full name and email are required.')
        return redirect('user_management')
    if role not in dict(User.ROLE_CHOICES):
        role = user.role
    if status not in dict(User.STATUS_CHOICES):
        status = user.account_status
    if User.objects.filter(email=email).exclude(admin_id=user.admin_id).exists():
        messages.error(request, 'Another account already uses that email.')
        return redirect('user_management')

    # Guard: don't allow removing/deactivating the last active admin.
    is_self = (user.admin_id == request.session.get('admin_id'))
    demoting = (user.role == 'Admin' and (role != 'Admin' or status != 'Active'))
    if demoting:
        other_active_admins = User.objects.filter(role='Admin', account_status='Active').exclude(admin_id=user.admin_id).count()
        if other_active_admins == 0:
            messages.error(request, 'Cannot change this account — it is the last active administrator.')
            return redirect('user_management')

    module_keys = clean_module_keys(request.POST.get('modules', ''))
    if role == 'Staff' and not module_keys:
        messages.error(request, 'Select at least one module for this staff account.')
        return redirect('user_management')

    user.fullname = fullname
    user.email = email
    user.role = role
    user.account_status = status
    user.modules = ','.join(module_keys)
    user.save()
    if is_self:
        request.session['admin_role'] = user.role
        request.session['admin_fullname'] = user.fullname
    detail = f'Updated account {email} (role={role}, status={status})'
    if role == 'Staff':
        detail += f' modules: {", ".join(user.module_labels)}'
    log_admin_action(request, 'Update', 'User', user.admin_id, detail)
    messages.success(request, f'Account for {fullname} updated.')
    return redirect('user_management')


@admin_only_required
def reset_staff_password(request, user_id):
    """Set a new password for an account."""
    user = get_object_or_404(User, admin_id=user_id)
    if request.method != 'POST':
        return redirect('user_management')
    password = request.POST.get('password') or ''
    # Same policy the account holder would face changing it themselves. A
    # password set for someone else is not a lesser password.
    policy_error = password_length_error(password, current_hash=user.password_hash)
    if policy_error:
        messages.error(request, policy_error)
        return redirect('user_management')
    user.password_hash = hash_password(password)
    user.save(update_fields=['password_hash'])
    log_admin_action(request, 'Update', 'User', user.admin_id, f'Reset password for {user.email}')
    messages.success(request, f'Password reset for {user.fullname}.')
    return redirect('user_management')


@admin_only_required
def toggle_staff_status(request, user_id):
    """Activate / deactivate (Inactive) an account."""
    user = get_object_or_404(User, admin_id=user_id)
    if request.method != 'POST':
        return redirect('user_management')

    new_status = 'Inactive' if user.account_status == 'Active' else 'Active'
    # Guard: never deactivate the last active admin (or yourself into lockout).
    if new_status != 'Active' and user.role == 'Admin':
        other_active_admins = User.objects.filter(role='Admin', account_status='Active').exclude(admin_id=user.admin_id).count()
        if other_active_admins == 0:
            messages.error(request, 'Cannot deactivate the last active administrator.')
            return redirect('user_management')

    user.account_status = new_status
    user.save(update_fields=['account_status'])
    log_admin_action(request, 'Update', 'User', user.admin_id, f'Set {user.email} status to {new_status}')
    messages.success(request, f'{user.fullname} is now {new_status}.')
    return redirect('user_management')



# ─── INVENTORY MANAGEMENT (Administrator-only) ─────────────────────────────
# Copy-level stock control, distinct from the Book catalogue (Ch.1 ¶268). This
# module never catalogues a title and never assigns a shelf — a received copy
# may sit in inventory before it is catalogued or shelved.

def _record_movement(record, action, request, reason='', source='',
                     before=None, after=None):
    """Log one stock movement. Every status-changing action goes through here."""
    admin_id = request.session.get('admin_id')
    actor = User.objects.filter(admin_id=admin_id).first() if admin_id else None
    return StockMovement.objects.create(
        inventory_record=record,
        action=action,
        actor=actor,
        actor_name=actor.fullname if actor else (request.session.get('admin_fullname') or 'System'),
        reason=(reason or '')[:500],
        source=(source or '')[:100],
        condition_before=before,
        condition_after=after,
    )


def _inventory_stats():
    qs = InventoryRecord.objects.all()
    in_stock = qs.filter(status='In Stock')
    return {
        'total_copies': qs.count(),
        'in_stock': in_stock.count(),
        'good_count': in_stock.filter(condition='Good').count(),
        'damaged_count': in_stock.filter(condition='Damaged').count(),
        'lost_count': in_stock.filter(condition='Lost').count(),
        'withdrawn_count': in_stock.filter(condition='Withdrawn').count(),
        'removed_count': qs.filter(status='Removed').count(),
        'uncatalogued': qs.filter(book__isnull=True, status='In Stock').count(),
    }


@admin_only_required
def inventory_management(request):
    """Inventory list, stock-audit workspace, and movement history."""
    tab = (request.GET.get('tab') or 'stock').strip()
    q = (request.GET.get('q') or '').strip()
    condition = (request.GET.get('condition') or '').strip()
    source = (request.GET.get('source') or '').strip()

    records = InventoryRecord.objects.select_related(
        'book', 'book__shelf_level', 'book__shelf_level__shelf', 'received_by'
    )
    if q:
        records = records.filter(
            Q(book__title__icontains=q) | Q(title_hint__icontains=q)
            | Q(book__author__icontains=q) | Q(book__ISBN__icontains=q)
            | Q(qr_label__icontains=q) | Q(supplier__icontains=q)
            | Q(po_number__icontains=q) | Q(donor_name__icontains=q)
        )
    if condition in dict(InventoryRecord.CONDITION_CHOICES):
        records = records.filter(condition=condition)
    if source in dict(InventoryRecord.SOURCE_CHOICES):
        records = records.filter(source=source)

    paginator = Paginator(records, 20)
    page_obj = paginator.get_page(request.GET.get('page', 1))

    movements = (StockMovement.objects
                 .select_related('inventory_record', 'inventory_record__book', 'actor')
                 .all())
    move_paginator = Paginator(movements, 25)
    move_page = move_paginator.get_page(request.GET.get('mpage', 1))

    # Copies a count could not find, oldest absence first — the ones most
    # likely to be genuinely gone rather than merely mislaid.
    missing_copies = (InventoryRecord.objects
                      .select_related('book', 'book__shelf_level', 'book__shelf_level__shelf')
                      .filter(status='Missing')
                      .order_by('missing_since', 'inventory_id'))
    today = timezone.localdate()
    for copy in missing_copies:
        copy.days_missing = (today - copy.missing_since).days if copy.missing_since else 0

    audits = (StockAudit.objects.select_related('shelf', 'audited_by').all())
    audit_page = Paginator(audits, 15).get_page(request.GET.get('apage', 1))

    # When each shelf was last counted — the question a stock-take exists to answer.
    last_audited = {}
    for row in StockAudit.objects.values('shelf_id').annotate(last=Max('audited_at')):
        last_audited[row['shelf_id']] = row['last']
    shelf_list = list(Shelf.objects.filter(is_active=True).select_related('room').order_by('name'))
    for shelf in shelf_list:
        shelf.last_audited = last_audited.get(shelf.shelf_id)

    context = {
        'tab': tab,
        'records': page_obj,
        'paginator': paginator,
        'movements': move_page,
        'missing_copies': missing_copies,
        'missing_count': len(missing_copies),
        'audits': audit_page,
        'q': q,
        'condition_filter': condition,
        'source_filter': source,
        'condition_choices': InventoryRecord.CONDITION_CHOICES,
        'source_choices': InventoryRecord.SOURCE_CHOICES,
        'stage_choices': InventoryRecord.STAGE_CHOICES,
        'today': timezone.localdate().isoformat(),
        'books': Book.objects.order_by('title'),
        'shelves': shelf_list,
    }
    context.update(_inventory_stats())
    return render(request, 'admin/inventoryadmin.html', context)


def _sync_donation_row(record):
    """Keep a donated copy and its accessioning row in step.

    Inventory is the single intake point (Ch.1 ¶268, Fig. 9, Fig. 78); the
    Donations page then tracks the copy through Received → Processing → Shelved
    (¶258, Fig. 7). Donation requires a Book, so an uncatalogued donated copy
    has no row until it is catalogued — this is called again at that point.
    """
    if record.source != 'Donation' or record.book is None:
        return None
    stage = record.processing_stage or 'Received'
    if record.donation is None:
        record.donation = Donation.objects.create(
            book=record.book,
            donor_name=record.donor_name or 'Unknown donor',
            date_donated=record.donated_date or timezone.localdate(),
            status=stage,
        )
        record.save(update_fields=['donation'])
    elif record.donation.status != stage:
        record.donation.status = stage
        record.donation.save(update_fields=['status'])
    return record.donation


def _receiving_redirect(request):
    """Staff land back on their receiving page, Administrators on the module."""
    if request.session.get('admin_role') == 'Staff':
        return redirect('staff_inventory_receive')
    return redirect('inventory_management')


def _resolve_intake_book(item, source):
    """Return the catalogue record a received line belongs to, creating it if new.

    Book details are entered at the receiving desk, so they have to land
    somewhere usable: the catalogue is the only place that holds title, author,
    ISBN, year and genre, and putting them there means nobody retypes the
    delivery note later. An existing title is matched on ISBN first (the only
    real identifier a book carries) and on title + author otherwise, so a second
    box of the same book adds copies instead of a duplicate catalogue entry.

    Raises ValueError with a message meant for the operator.
    """
    book_id = str(item.get('book_id') or '').strip()
    if book_id:
        book = Book.objects.filter(book_id=book_id).first()
        if book is None:
            raise ValueError('One of the titles is no longer in the catalogue.')
        return book, False

    title = (item.get('title') or '').strip()
    if not title:
        raise ValueError('Every line needs either a catalogued title or a new title.')
    author = (item.get('author') or '').strip()
    isbn = (item.get('isbn') or '').strip()
    genre = (item.get('genre') or '').strip()
    material_type = (item.get('material_type') or 'Book').strip()
    if material_type not in {c[0] for c in Book.MATERIAL_TYPE_CHOICES}:
        material_type = 'Book'
    year = None
    raw_year = str(item.get('publication_year') or '').strip()
    if raw_year:
        try:
            year = int(raw_year)
        except ValueError:
            raise ValueError('Publication year must be a number (got "' + raw_year + '").')
        if year < 1000 or year > timezone.localdate().year + 1:
            raise ValueError('Publication year ' + raw_year + ' is out of range.')

    existing = None
    if isbn:
        existing = Book.objects.filter(ISBN__iexact=isbn).first()
    if existing is None and author:
        existing = Book.objects.filter(title__iexact=title, author__iexact=author).first()
    if existing is not None:
        return existing, False

    book = Book.objects.create(
        title=title,
        author=author or 'Unknown',
        ISBN=isbn or None,
        genre=genre or None,
        material_type=material_type,
        publication_year=year,
        # A donated title is not lendable until accessioning reaches Shelved,
        # which is where the Donations page flips it to Available.
        status='Donated' if source == 'Donation' else 'Available',
        qr_code=str(uuid4()),
        shelf_level=None,           # shelving is a separate, deliberate step
    )
    return book, True


@admin_or_module_required('inventory')
def receive_stock(request):
    """Intake a delivery: one source, one or many titles, many copies each.

    A delivery arrives as a box, not as a single book, so the source details are
    entered once and every title in that box is added to a list before anything
    is written. Shipments and donations remain separate intakes with their own
    fields: a shipment records supplier and PO number, a donation records donor
    and date plus the Received/Processing/Shelved accessioning stage (Ch.1 ¶258).
    """
    if request.method != 'POST':
        return _receiving_redirect(request)

    source = (request.POST.get('source') or 'Purchase').strip()
    if source not in dict(InventoryRecord.SOURCE_CHOICES):
        source = 'Purchase'
    notes = (request.POST.get('notes') or '').strip()

    try:
        items = json.loads(request.POST.get('items') or '[]')
    except ValueError:
        items = None
    if not isinstance(items, list) or not items:
        messages.error(request, 'Add at least one title to the delivery before receiving it.')
        return _receiving_redirect(request)
    if len(items) > 50:
        messages.error(request, 'That is more than 50 titles — split it into two deliveries.')
        return _receiving_redirect(request)

    # Only the fields belonging to the chosen intake are kept, so a donation
    # can never carry a PO number and a shipment can never carry a donor.
    supplier = po_number = donor_name = processing_stage = None
    donated_date = None
    if source == 'Donation':
        donor_name = (request.POST.get('donor_name') or '').strip()
        if not donor_name:
            messages.error(request, 'A donation needs the donor name for the record.')
            return _receiving_redirect(request)
        raw_date = (request.POST.get('donated_date') or '').strip()
        if raw_date:
            try:
                donated_date = datetime.strptime(raw_date, '%Y-%m-%d').date()
            except ValueError:
                messages.error(request, 'Donation date must be a valid date.')
                return _receiving_redirect(request)
        else:
            donated_date = timezone.localdate()
        processing_stage = (request.POST.get('processing_stage') or 'Received').strip()
        if processing_stage not in dict(InventoryRecord.STAGE_CHOICES):
            processing_stage = 'Received'
    else:
        supplier = (request.POST.get('supplier') or '').strip() or None
        po_number = (request.POST.get('po_number') or '').strip() or None

    # Validate the whole delivery before writing any of it — a bad line halfway
    # down should not leave the first half already received.
    parsed = []
    total_copies = 0
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            messages.error(request, 'The delivery list was malformed. Please rebuild it.')
            return _receiving_redirect(request)
        raw_quantity = item.get('quantity')
        try:
            # Not `or 1`: a submitted 0 is falsy and would silently become one
            # copy instead of being rejected.
            quantity = 1 if raw_quantity in (None, '') else int(raw_quantity)
        except (TypeError, ValueError):
            quantity = 0
        if quantity < 1 or quantity > 100:
            messages.error(request, 'Line ' + str(index) + ': quantity must be between 1 and 100.')
            return _receiving_redirect(request)
        condition = (item.get('condition') or 'Good').strip()
        if condition not in dict(InventoryRecord.CONDITION_CHOICES):
            condition = 'Good'
        parsed.append((item, quantity, condition))
        total_copies += quantity

    if total_copies > 500:
        messages.error(request, 'That is ' + str(total_copies) + ' copies in one go — '
                                'split it into smaller deliveries.')
        return _receiving_redirect(request)

    admin = User.objects.filter(admin_id=request.session.get('admin_id')).first()
    titles_received = []
    new_titles = 0
    try:
        with transaction.atomic():
            for item, quantity, condition in parsed:
                book, was_created = _resolve_intake_book(item, source)
                if was_created:
                    new_titles += 1
                # Four copies of one donated title are one thing to accession,
                # not four, so every copy of a title shares its Donation row.
                donation_row = None
                for _ in range(quantity):
                    record = InventoryRecord.objects.create(
                        book=book,
                        source=source,
                        supplier=supplier,
                        po_number=po_number,
                        donor_name=donor_name,
                        donated_date=donated_date,
                        processing_stage=processing_stage,
                        donation=donation_row,
                        condition=condition,
                        status='In Stock',
                        qr_label=str(uuid4()),   # copy-level label, not the catalogue QR
                        received_by=admin,
                        notes=notes or None,
                    )
                    _record_movement(record, 'Received', request,
                                     reason='Received in ' + condition.lower() + ' condition',
                                     source=record.source_detail or source, after=condition)
                    if source == 'Donation' and donation_row is None:
                        # Opens the accessioning row on the first copy; the rest
                        # were created already pointing at it.
                        donation_row = _sync_donation_row(record)
                titles_received.append((book.title, quantity))
    except ValueError as exc:
        messages.error(request, str(exc))
        return _receiving_redirect(request)

    label = 'donation' if source == 'Donation' else 'shipment'
    summary = ('Received ' + str(total_copies) + ' copy(ies) across '
               + str(len(titles_received)) + ' title(s) by ' + label)
    log_admin_action(request, 'Create', 'Inventory', None,
                     summary + ': ' + ', '.join(t + ' x' + str(q) for t, q in titles_received))
    if new_titles:
        summary += ' (' + str(new_titles) + ' new to the catalogue)'
    messages.success(request, summary + '. Print the QR labels from the list.')
    return _receiving_redirect(request)


@module_required('inventory')
def staff_inventory_receive(request):
    """Library Staff receiving desk — intake only, per Ch.1 ¶268.

    Deliberately not the full module: no audit, no deaccession, no condition
    changes and no movement history, all of which stay with the Administrator.
    """
    mine = (InventoryRecord.objects
            .select_related('book', 'received_by')
            .filter(received_by__admin_id=request.session.get('admin_id'))
            .order_by('-inventory_id')[:25])
    return render(request, 'library_staff/inventoryreceive.html', {
        'recent': mine,
        'shelf_levels_available': _shelf_capacity(),
        'books': Book.objects.order_by('title'),
        'condition_choices': InventoryRecord.CONDITION_CHOICES,
        'stage_choices': InventoryRecord.STAGE_CHOICES,
        'today': timezone.localdate().isoformat(),
    })


@admin_only_required
def update_copy_condition(request):
    """Record a copy as damaged, lost, withdrawn, or back in good order."""
    if request.method != 'POST':
        return redirect('inventory_management')

    record = InventoryRecord.objects.filter(
        inventory_id=request.POST.get('inventory_id')).first()
    if record is None:
        messages.error(request, 'Inventory record not found.')
        return redirect('inventory_management')

    new_condition = (request.POST.get('condition') or '').strip()
    reason = (request.POST.get('reason') or '').strip()
    if new_condition not in dict(InventoryRecord.CONDITION_CHOICES):
        messages.error(request, 'Pick a valid condition.')
        return redirect('inventory_management')
    if not reason:
        messages.error(request, 'A reason is required so the movement history stays meaningful.')
        return redirect('inventory_management')

    before = record.condition
    if before == new_condition:
        messages.warning(request, 'That copy is already recorded as ' + new_condition + '.')
        return redirect('inventory_management')

    record.condition = new_condition
    record.save(update_fields=['condition'])
    _record_movement(record, 'ConditionChange', request, reason=reason,
                     source='Staff inspection', before=before, after=new_condition)
    log_admin_action(request, 'Update', 'Inventory', record.inventory_id,
                     '"' + record.display_title + '" condition ' + before + ' to ' + new_condition)
    messages.success(request, record.display_title + ': ' + before + ' to ' + new_condition + '.')
    return redirect('inventory_management')


@admin_only_required
def update_inventory_record(request):
    """Edit a copy's descriptive metadata, independently of its quantity."""
    if request.method != 'POST':
        return redirect('inventory_management')

    record = InventoryRecord.objects.filter(
        inventory_id=request.POST.get('inventory_id')).first()
    if record is None:
        messages.error(request, 'Inventory record not found.')
        return redirect('inventory_management')

    book_id = (request.POST.get('book_id') or '').strip()
    record.book = Book.objects.filter(book_id=book_id).first() if book_id else None
    record.title_hint = (request.POST.get('title_hint') or '').strip() or None
    source = (request.POST.get('source') or record.source).strip()
    if source in dict(InventoryRecord.SOURCE_CHOICES):
        record.source = source
    if record.source == 'Donation':
        record.donor_name = (request.POST.get('donor_name') or '').strip() or None
        record.supplier = record.po_number = None
        stage = (request.POST.get('processing_stage') or '').strip()
        record.processing_stage = stage if stage in dict(InventoryRecord.STAGE_CHOICES) else record.processing_stage
    else:
        record.supplier = (request.POST.get('supplier') or '').strip() or None
        record.po_number = (request.POST.get('po_number') or '').strip() or None
        record.donor_name = None
        record.donated_date = None
        record.processing_stage = None
    record.notes = (request.POST.get('notes') or '').strip() or None
    record.save(update_fields=['book', 'title_hint', 'source', 'supplier', 'po_number',
                               'donor_name', 'donated_date', 'processing_stage', 'notes'])
    # Catalogue a donated copy here and it joins the accessioning queue; change
    # its stage here and the Donations page follows.
    _sync_donation_row(record)

    _record_movement(record, 'Correction', request,
                     reason=(request.POST.get('reason') or 'Metadata corrected')[:500],
                     source=record.source)
    log_admin_action(request, 'Update', 'Inventory', record.inventory_id,
                     'Updated details for "' + record.display_title + '"')
    messages.success(request, 'Updated ' + record.display_title + '.')
    return redirect('inventory_management')


@admin_only_required
def deaccession_copy(request):
    """Remove a record created in error.

    Reserved for data-entry mistakes — routine stock reduction is a condition
    change (damaged / lost / withdrawn), not a deaccession.
    """
    if request.method != 'POST':
        return redirect('inventory_management')

    record = InventoryRecord.objects.filter(
        inventory_id=request.POST.get('inventory_id')).first()
    if record is None:
        messages.error(request, 'Inventory record not found.')
        return redirect('inventory_management')

    reason = (request.POST.get('reason') or '').strip()
    if not reason:
        messages.error(request, 'Deaccession needs a reason — it is an audited correction.')
        return redirect('inventory_management')
    if record.status == 'Removed':
        messages.warning(request, 'That copy has already been removed.')
        return redirect('inventory_management')

    record.status = 'Removed'
    record.save(update_fields=['status'])
    _record_movement(record, 'Deaccession', request, reason=reason, source=record.source,
                     before=record.condition, after=record.condition)
    log_admin_action(request, 'Delete', 'Inventory', record.inventory_id,
                     'Deaccessioned "' + record.display_title + '" - ' + reason)
    messages.success(request, record.display_title + ' removed from inventory.')
    return redirect('inventory_management')


@admin_only_required
def search_inventory_by_qr(request):
    """Resolve a scanned copy label — used by condition updates and audits."""
    qr_label = (request.GET.get('qr_label') or '').strip()
    if not qr_label:
        return JsonResponse({'success': False, 'error': 'QR label is required'})

    record = (InventoryRecord.objects
              .select_related('book', 'book__shelf_level', 'book__shelf_level__shelf')
              .filter(qr_label=qr_label).first())
    if record is None:
        return JsonResponse({'success': False, 'error': 'No inventory copy matches that label'})

    return JsonResponse({'success': True, 'record': {
        'inventory_id': record.inventory_id,
        'title': record.display_title,
        'condition': record.condition,
        'status': record.status,
        'source': record.source,
        'location': record.shelf_location or 'Not shelved',
        'catalogued': record.book is not None,
    }})


def _expected_copies_for_shelf(shelf_id):
    """Copies the shelf should be able to account for.

    Includes copies already flagged Missing: a stock-take is exactly when a
    mislaid book turns up again, and leaving them out would mean never
    recovering one.
    """
    return (InventoryRecord.objects
            .select_related('book', 'book__shelf_level', 'book__shelf_level__shelf')
            .filter(status__in=['In Stock', 'Missing'],
                    condition__in=['Good', 'Damaged'],
                    book__shelf_level__shelf__shelf_id=shelf_id))


def _open_loans_by_book(book_ids):
    """How many copies of each title are out on loan right now.

    Loans are recorded against the title, not the individual copy, so the
    stock-take cannot know *which* copy a patron is holding — only how many are
    legitimately off the shelf. Without this, every borrowed book is reported
    missing, and a library with twenty books out would write off twenty books
    on its first count.
    """
    counts = {}
    rows = (Transaction.objects
            .filter(book_id__in=book_ids, transaction_type='Borrow', return_date__isnull=True)
            .values('book_id')
            .annotate(n=Count('transaction_id')))
    for row in rows:
        counts[row['book_id']] = row['n']
    return counts


@admin_only_required
def stock_audit_progress(request):
    """Group a running list of scans by the shelf each copy belongs to.

    The shelf-at-a-time audit asks which shelf you are on before you scan a
    single book. That is backwards from how a stock-take is actually done --
    you walk the room with a scanner and the shelves arrive in whatever order
    the room is laid out. Every label already identifies its own copy, and the
    copy knows its shelf, so the grouping is derivable rather than something to
    be asked for.

    Deliberately cheap: no loan reconciliation, no discrepancy report. It runs
    after every few scans to keep the progress counts live, and the expensive
    reasoning stays in stock_audit_compare, which is called once per shelf when
    the count is done.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    labels = []
    seen = set()
    for raw in request.POST.getlist('scanned'):
        label = (raw or '').strip()
        if label and label not in seen:
            seen.add(label)
            labels.append(label)

    if len(labels) > MAX_AUDIT_SCANS:
        return JsonResponse({
            'success': False,
            'error': f'That is {len(labels)} scans in one sweep. File the shelves '
                     f'you have counted, then carry on.'})

    records = (InventoryRecord.objects
               .select_related('book', 'book__shelf_level', 'book__shelf_level__shelf')
               .filter(qr_label__in=labels))
    by_label = {r.qr_label: r for r in records}

    # shelf_id -> what has been seen there so far
    shelves = {}
    unshelved, unknown = [], []
    for label in labels:
        record = by_label.get(label)
        if record is None:
            unknown.append(label)
            continue
        level = record.book.shelf_level if record.book_id else None
        shelf = level.shelf if level and level.shelf_id else None
        if shelf is None:
            unshelved.append({'qr_label': label, 'title': record.display_title})
            continue
        bucket = shelves.setdefault(shelf.shelf_id, {
            'shelf_id': shelf.shelf_id,
            'shelf_name': shelf.name,
            'found': [],
        })
        bucket['found'].append(label)

    # Expected totals, counted once per shelf rather than per scan.
    totals = {}
    if shelves:
        for row in (InventoryRecord.objects
                    .filter(status__in=['In Stock', 'Missing'],
                            condition__in=['Good', 'Damaged'],
                            book__shelf_level__shelf__shelf_id__in=list(shelves))
                    .values('book__shelf_level__shelf__shelf_id')
                    .annotate(n=Count('inventory_id'))):
            totals[row['book__shelf_level__shelf__shelf_id']] = row['n']

    groups = []
    for shelf_id, bucket in shelves.items():
        expected = totals.get(shelf_id, 0)
        found = len(bucket['found'])
        groups.append({
            'shelf_id': shelf_id,
            'shelf_name': bucket['shelf_name'],
            'expected_count': expected,
            'found_count': found,
            'scanned': bucket['found'],
            # A shelf can read over 100% when a book from elsewhere has been
            # put away here; the count is still the honest one to show.
            'percent': round(100.0 * found / expected) if expected else None,
            'complete': bool(expected) and found >= expected,
        })
    groups.sort(key=lambda g: g['shelf_name'].lower())

    return JsonResponse({
        'success': True,
        'total_scanned': len(labels),
        'groups': groups,
        'unshelved': unshelved,
        'unknown': unknown,
    })


@admin_only_required
def stock_audit_compare(request):
    """Compare a shelf's expected holdings against what was physically scanned.

    Produces the discrepancy report only — nothing is written until the
    Administrator confirms it via stock_audit_apply.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    shelf_id = (request.POST.get('shelf_id') or '').strip()
    shelf = Shelf.objects.filter(shelf_id=shelf_id).first() if shelf_id else None
    if shelf is None:
        return JsonResponse({'success': False, 'error': 'Pick a shelf to audit'})

    scanned_labels = [s.strip() for s in request.POST.getlist('scanned') if s.strip()]
    expected = list(_expected_copies_for_shelf(shelf.shelf_id))
    expected_by_label = {r.qr_label: r for r in expected}

    found, unexpected, recovered = [], [], []
    seen = set()
    for label in scanned_labels:
        if label in seen:
            continue
        seen.add(label)
        record = expected_by_label.get(label)
        if record is not None:
            found.append(record)
            if record.status == 'Missing':
                # Turned up. Worth calling out: it is the good news in a count,
                # and it is what undoes an earlier miss.
                recovered.append({
                    'inventory_id': record.inventory_id,
                    'title': record.display_title,
                    'missing_since': record.missing_since.isoformat() if record.missing_since else None,
                })
        else:
            other = InventoryRecord.objects.select_related('book').filter(qr_label=label).first()
            unexpected.append({
                'qr_label': label,
                'inventory_id': other.inventory_id if other else None,
                'title': other.display_title if other else 'Unknown label',
                'belongs_to': (other.shelf_location or 'Not shelved') if other else '-',
            })

    # Anything not scanned is unaccounted for until a loan explains it.
    found_ids = {r.inventory_id for r in found}
    unaccounted = [r for r in expected if r.inventory_id not in found_ids]

    loans = _open_loans_by_book([r.book_id for r in unaccounted if r.book_id])
    on_loan, missing = [], []
    for record in unaccounted:
        remaining = loans.get(record.book_id, 0)
        if remaining > 0:
            # A copy of this title is with a patron, so one absence is expected.
            loans[record.book_id] = remaining - 1
            on_loan.append({
                'inventory_id': record.inventory_id,
                'title': record.display_title,
            })
            continue
        missing.append({
            'inventory_id': record.inventory_id,
            'qr_label': record.qr_label,
            'title': record.display_title,
            'condition': record.condition,
            'already_missing': record.status == 'Missing',
            'missing_since': record.missing_since.isoformat() if record.missing_since else None,
            'audit_misses': record.audit_misses,
        })

    return JsonResponse({
        'success': True,
        'shelf': shelf.name,
        'expected_count': len(expected),
        'scanned_count': len(seen),
        'found_count': len(found),
        'on_loan': on_loan,
        'on_loan_count': len(on_loan),
        'missing': missing,
        'recovered': recovered,
        'unexpected': unexpected,
        'reconciled': not missing and not unexpected,
    })


@admin_only_required
def stock_audit_apply(request):
    """Close a stock-take: record it, flag what is missing, recover what turned up.

    Nothing is written off here. A copy that could not be found is flagged
    Missing and its miss counted; declaring it lost is a separate, later
    decision made against how long it has been gone (see write_off_missing).
    A shelf-read finds mislaid books more often than it finds thefts, and a
    process that goes straight to "lost" on one pass would destroy that.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    if (request.POST.get('confirm') or '').strip() != 'yes':
        return JsonResponse({'success': False,
                             'error': 'Confirm the discrepancy report before it is filed'})

    shelf_id = (request.POST.get('shelf_id') or '').strip()
    shelf = Shelf.objects.filter(shelf_id=shelf_id).first() if shelf_id.isdigit() else None
    shelf_name = (request.POST.get('shelf_name') or '').strip() or (shelf.name if shelf else '')
    reason = ((request.POST.get('reason') or '').strip()
              or 'Not found during stock audit')
    source = ('Stock audit - ' + shelf_name) if shelf_name else 'Stock audit'

    missing_ids = [int(i) for i in request.POST.getlist('missing_ids') if str(i).strip().isdigit()]
    found_ids = [int(i) for i in request.POST.getlist('found_ids') if str(i).strip().isdigit()]

    def _int(name):
        raw = (request.POST.get(name) or '0').strip()
        return int(raw) if raw.lstrip('-').isdigit() else 0

    today = timezone.localdate()
    flagged = recovered = 0
    with transaction.atomic():
        for record in InventoryRecord.objects.filter(inventory_id__in=missing_ids):
            if record.status == 'Removed':
                continue
            before = record.status
            record.status = 'Missing'
            record.audit_misses = (record.audit_misses or 0) + 1
            if record.missing_since is None:
                record.missing_since = today
            record.save(update_fields=['status', 'audit_misses', 'missing_since'])
            _record_movement(record, 'AuditAdjustment', request, reason=reason, source=source,
                             before=before, after='Missing')
            flagged += 1

        # Anything scanned that had been written down as missing is back.
        for record in InventoryRecord.objects.filter(inventory_id__in=found_ids,
                                                     status='Missing'):
            gone_since = record.missing_since
            record.status = 'In Stock'
            record.missing_since = None
            record.audit_misses = 0
            record.save(update_fields=['status', 'missing_since', 'audit_misses'])
            _record_movement(
                record, 'Found', request,
                reason=('Found on the shelf during stock audit'
                        + (' — missing since ' + gone_since.strftime('%b %d, %Y')
                           if gone_since else '')),
                source=source, before='Missing', after='In Stock')
            recovered += 1

        audit = StockAudit.objects.create(
            shelf=shelf,
            shelf_name=shelf_name,
            audited_by=User.objects.filter(admin_id=request.session.get('admin_id')).first(),
            expected_count=_int('expected_count'),
            scanned_count=_int('scanned_count'),
            found_count=_int('found_count'),
            on_loan_count=_int('on_loan_count'),
            missing_count=flagged,
            recovered_count=recovered,
            unexpected_count=_int('unexpected_count'),
            notes=(request.POST.get('notes') or '').strip() or None,
        )

    log_admin_action(request, 'Create', 'Inventory', audit.audit_id,
                     'Stock audit of ' + (shelf_name or 'shelf') + ': '
                     + str(flagged) + ' flagged missing, '
                     + str(recovered) + ' recovered')
    return JsonResponse({'success': True, 'flagged': flagged, 'recovered': recovered,
                         'audit_id': audit.audit_id})


@admin_only_required
def write_off_missing(request):
    """Declare copies that have stayed missing to be lost.

    Deliberately separate from the stock-take. A book absent from one count is
    usually mislaid; a book absent from several counts over months is gone, and
    only a person looking at how long it has been missing should be the one to
    say so.
    """
    if request.method != 'POST':
        return redirect('inventory_management')

    ids = [int(i) for i in request.POST.getlist('inventory_ids') if str(i).strip().isdigit()]
    reason = (request.POST.get('reason') or '').strip()
    if not ids:
        messages.error(request, 'Select at least one missing copy to write off.')
        return redirect('/admin-portal/inventory/?tab=missing')
    if not reason:
        messages.error(request, 'A write-off needs a reason — it is a permanent correction.')
        return redirect('/admin-portal/inventory/?tab=missing')

    written_off = 0
    with transaction.atomic():
        for record in InventoryRecord.objects.filter(inventory_id__in=ids, status='Missing'):
            since = record.missing_since
            record.condition = 'Lost'
            record.status = 'Removed'
            record.save(update_fields=['condition', 'status'])
            _record_movement(
                record, 'Deaccession', request,
                reason=(reason + (' (missing since ' + since.strftime('%b %d, %Y') + ')'
                                  if since else '')),
                source='Write-off of missing stock',
                before='Missing', after='Lost')
            written_off += 1

    log_admin_action(request, 'Update', 'Inventory', None,
                     str(written_off) + ' missing copy(ies) written off as lost — ' + reason)
    messages.success(request, str(written_off) + ' copy(ies) written off as lost.')
    return redirect('/admin-portal/inventory/?tab=missing')


def flag_inventory_copy_lost(request, book, reason):
    """Mark one in-stock copy of `book` lost when a loan is written off.

    Called from the Transactions module's Mark Lost action, so a copy lost at
    the desk shows up in inventory without a second manual step (Ch.1 ¶268).
    Returns the record it touched, or None when the title has no copy on record.
    """
    if book is None:
        return None
    record = (InventoryRecord.objects
              .filter(book=book, status='In Stock')
              .exclude(condition='Lost')
              .order_by('condition', 'inventory_id')
              .first())
    if record is None:
        return None
    before = record.condition
    record.condition = 'Lost'
    record.save(update_fields=['condition'])
    _record_movement(record, 'ConditionChange', request, reason=reason,
                     source='Transactions module', before=before, after='Lost')
    return record

# ─── PATRON LIBRARY CARD ──────────────────────────────────────────────────
# The printable card the patron carries. Its QR is the same Patron.qr_code the
# desk scans for borrowing, returning and entry logging, so the card is the
# physical form of the identity check.

def _qr_data_uri(payload, box_size=10, border=2):
    """Render `payload` as a QR PNG and return it as a data: URI.

    Generated here rather than fetched from an image service so a card still
    prints correctly with no internet, which is the normal state of a library
    front desk mid-brownout.
    """
    import base64
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=border,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color='black', back_color='white')
    buffer = BytesIO()
    img.save(buffer, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode()


def _library_card_context(patron):
    active_borrows = Transaction.objects.filter(
        patron=patron, transaction_type='Borrow', return_date__isnull=True).count()
    return {
        'patron': patron,
        'qr_data_uri': _qr_data_uri(patron.qr_code) if patron.qr_code else None,
        'library_name': getattr(settings, 'LIBRARY_NAME', 'Ayla Public Library'),
        'library_location': 'Brgy. Sala, Cabuyao, Laguna',
        'active_borrows': active_borrows,
        'issued_on': timezone.localdate(),
    }


@granted_module_required('patrons')
def serve_patron_credential(request, path):
    """A patron's uploaded ID, served only to staff who hold the patrons module.

    These are photographs of government identity documents. They used to sit
    under /media/, which is served by django.views.static.serve with no
    authentication at all -- an anonymous request for the file returned it in
    full. The random filename was the only thing standing in the way, and an
    unguessable URL is not access control: it leaks through browser history on a
    shared desk machine, through a referrer header, through any backup.

    Gated on the patrons module rather than on being any logged-in user, because
    reviewing an applicant's ID is exactly what that module is for.
    """
    from django.utils._os import safe_join
    from django.views.static import serve as static_serve

    base = os.path.join(settings.MEDIA_ROOT, 'credentials')
    name = os.path.basename(path or '')
    if not name:
        raise Http404('No such document.')
    try:
        # safe_join raises rather than escaping the directory, which is what
        # stops ../../ from walking out of the credentials folder.
        safe_join(base, name)
    except (ValueError, SuspiciousFileOperation):
        raise Http404('No such document.')

    log_admin_action(request, 'View', 'Patron credential', detail=f'Opened ID document {name}')
    return static_serve(request, name, document_root=base)


@granted_module_required('patrons')
def patron_library_card(request, patron_id):
    """The card for any patron — printed at the desk by Staff or the Administrator."""
    patron = get_object_or_404(Patron, patron_id=patron_id)
    context = _library_card_context(patron)
    context['printed_by_staff'] = True
    return render(request, 'admin/librarycard.html', context)


@patron_login_required
def my_library_card(request):
    """The signed-in patron's own card, so they can print it themselves."""
    patron = get_object_or_404(Patron, patron_id=request.session.get('patron_id'))
    context = _library_card_context(patron)
    context['printed_by_staff'] = False
    return render(request, 'admin/librarycard.html', context)
