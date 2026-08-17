from django.shortcuts import redirect, render, get_object_or_404
from django.contrib import messages
from django.db.models import Count, Max, Q
from django.db import transaction
from django.utils import timezone
from django.http import HttpResponse, JsonResponse
from django.conf import settings
from django.core.paginator import Paginator
from datetime import datetime, timedelta
from io import BytesIO
from decimal import Decimal, InvalidOperation
import json

from uuid import uuid4
import openpyxl
from openpyxl import Workbook
import qrcode
import os
import math
import heapq

from .auth_utils import (
    check_password,
    hash_password,
    patron_login_required,
    admin_login_required,
    admin_only_required,
    admin_module_required,
    staff_only_required,
    module_required,
    admin_or_module_required,
)
from .modules import STAFF_MODULES, clean_module_keys
from .names import compose_name, parse_name
from .desk import PURPOSE_CHOICES, close_stale_visits, desk_is_armed

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
from .audit import log_admin_action
from .eligibility import check_patron_eligibility
from .emails import (
    announcement_email,
    borrow_confirmation_email,
    bulk_connection,
    return_receipt_email,
    lost_book_email,
    otp_email,
    password_reset_otp_email,
    registration_approved_email,
    registration_rejected_email,
)
from .reports import REPORT_TYPES, parse_date_range, build_report, render_report_pdf, render_report_excel
from .models import Book, Patron, PatronLog, Transaction, User, ShelfLevel, Donation, Announcement, FloorPlan, Shelf, Room, Door, Waypoint, BLEBeacon, WaypointConnection, SystemLog, BorrowingRule, PasswordResetOTP, InventoryRecord, StockMovement, StockAudit

# Patron views
def patron_login(request):
    if request.method == 'POST':
        email = request.POST.get('email')
        password = request.POST.get('password')

        try:
            patron = Patron.objects.get(email=email)
        except Patron.DoesNotExist:
            return render(request, 'patron/patronlogin.html', {'error': 'Invalid email or password'})

        if patron.account_status == 'Pending':
            return render(request, 'patron/patronlogin.html',
                          {'error': 'Your registration is awaiting administrator approval. You will receive an email once it is approved.'})

        if patron.account_status != 'Active':
            return render(request, 'patron/patronlogin.html', {'error': 'Your account is suspended or inactive'})

        if not check_password(password, patron.password_hash):
            return render(request, 'patron/patronlogin.html', {'error': 'Invalid email or password'})

        request.session['patron_id'] = patron.patron_id
        request.session['patron_fullname'] = patron.fullname
        return redirect('/patron/dashboard/')

    return render(request, 'patron/patronlogin.html')


def patron_logout(request):
    request.session.flush()
    return redirect('/patron/login/')


@patron_login_required
def patron_dashboard(request):
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
    """6-digit numeric one-time password."""
    import random
    return f'{random.randint(0, 999999):06d}'


def _save_credential_document(uploaded, patron_email):
    """Store an uploaded ID / proof-of-residency file under media/credentials/.

    Returns the media-relative path, or None when nothing was uploaded."""
    if not uploaded:
        return None
    ext = os.path.splitext(uploaded.name)[1].lower()
    if ext not in ('.jpg', '.jpeg', '.png', '.pdf'):
        raise ValueError('Credential must be a JPG, PNG, or PDF file.')
    if uploaded.size > 5 * 1024 * 1024:
        raise ValueError('Credential file must be 5 MB or smaller.')
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
        if code != patron.otp_code:
            return render(request, 'patron/patronregister.html',
                          {'stage': 'otp', 'otp_email': email,
                           'error': 'Incorrect code. Please try again.'})
        patron.otp_verified = True
        patron.otp_code = None
        patron.otp_expires_at = None
        patron.save(update_fields=['otp_verified', 'otp_code', 'otp_expires_at'])
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
        patron.otp_code = _new_otp()
        patron.otp_expires_at = timezone.now() + timedelta(minutes=10)
        patron.save(update_fields=['otp_code', 'otp_expires_at'])
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



@patron_login_required
def patron_catalog(request):
    search_query = request.GET.get('search', '').strip()

    books = Book.objects.filter(status='Available').select_related(
        'shelf_level', 'shelf_level__shelf'
    ).order_by('title')

    if search_query:
        books = books.filter(
            Q(title__icontains=search_query) |
            Q(author__icontains=search_query) |
            Q(genre__icontains=search_query)
        )

    context = {
        'books': books,
        'search_query': search_query,
    }
    return render(request, 'patron/patroncatalog.html', context)


@patron_login_required
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


@patron_login_required
def patron_map(request):
    target = {}
    book_id = request.GET.get('book_id')
    if book_id:
        book = Book.objects.select_related('shelf_level__shelf').filter(book_id=book_id).first()
        if book:
            target['book_id'] = book.book_id
            target['book_title'] = book.title
            shelf = book.shelf_level.shelf if book.shelf_level else None
            if shelf:
                target['shelf_id'] = shelf.shelf_id
                target['shelf_name'] = shelf.name
    return render(request, 'patron/patronmap.html', {'target': target})


@patron_login_required
def patron_announcements(request):
    announcements = Announcement.objects.filter(
        is_active=True
    ).order_by('-created_at')

    context = {
        'announcements': announcements,
    }
    return render(request, 'patron/patronannouncements.html', context)


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
    active_loans = transactions.filter(
        transaction_type='Borrow', return_date__isnull=True
    )
    history = transactions.filter(return_date__isnull=False)

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
    return JsonResponse({'success': True, 'message': 'Profile updated.'})


@patron_login_required
def patron_change_password(request):
    """Change the login password (requires the current password)."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})
    patron = get_object_or_404(Patron, patron_id=request.session.get('patron_id'))

    current = request.POST.get('current_password') or ''
    new = request.POST.get('new_password') or ''
    if len(new) < 8:
        return JsonResponse({'success': False, 'error': 'New password must be at least 8 characters.'})
    if not check_password(current, patron.password_hash):
        return JsonResponse({'success': False, 'error': 'Current password is incorrect.'})

    patron.password_hash = hash_password(new)
    patron.save(update_fields=['password_hash'])
    return JsonResponse({'success': True, 'message': 'Password changed.'})


# Admin views
def admin_login(request):
    if request.method == 'POST':
        email = request.POST.get('email')
        password = request.POST.get('password')

        try:
            admin = User.objects.get(email=email)
        except User.DoesNotExist:
            return render(request, 'admin/signin.html', {'error': 'Invalid email or password'})

        if admin.account_status != 'Active':
            return render(request, 'admin/signin.html', {'error': 'Your account is suspended or inactive'})

        if not check_password(password, admin.password_hash):
            return render(request, 'admin/signin.html', {'error': 'Invalid email or password'})

        if admin.role != 'Admin':
            return render(request, 'admin/signin.html', {'error': 'This is the administrator portal. Please use the Library Staff login.'})

        request.session['admin_id'] = admin.admin_id
        request.session['admin_fullname'] = admin.fullname
        request.session['admin_role'] = admin.role
        log_admin_action(request, 'Login', 'Auth', admin.admin_id, f'{admin.fullname} (Admin) logged in')
        return redirect('/admin-portal/dashboard/')

    return render(request, 'admin/signin.html')


def admin_logout(request):
    request.session.flush()
    return redirect('/admin-portal/login/')


# Library Staff portal authentication
def staff_login(request):
    if request.method == 'POST':
        email = request.POST.get('email')
        password = request.POST.get('password')

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            return render(request, 'library_staff/signin.html', {'error': 'Invalid email or password'})

        if user.account_status != 'Active':
            return render(request, 'library_staff/signin.html', {'error': 'Your account is suspended or inactive'})

        if not check_password(password, user.password_hash):
            return render(request, 'library_staff/signin.html', {'error': 'Invalid email or password'})

        if user.role != 'Staff':
            return render(request, 'library_staff/signin.html', {'error': 'This portal is for library staff. Please use the administrator login.'})

        request.session['admin_id'] = user.admin_id
        request.session['admin_fullname'] = user.fullname
        request.session['admin_role'] = user.role
        log_admin_action(request, 'Login', 'Auth', user.admin_id, f'{user.fullname} (Staff) logged in')
        return redirect('/library-staff/dashboard/')

    return render(request, 'library_staff/signin.html')


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
        if account is not None:
            fullname = getattr(account, 'fullname', '') or 'there'
            if not _issue_reset_code(account_type, email, fullname, cfg['role_label']):
                return _render('request', email=email,
                               error='We could not send the code right now. '
                                     'Please try again in a moment.')
        # No account: fall through and show the same screen anyway.
        note = ('A new code has been sent if the account exists.'
                if action == 'resend' else _RESET_SENT_NOTE)
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
        if len(new_password) < 8:
            return _render('otp', email=email, error='New password must be at least 8 characters.')

        reset = (PasswordResetOTP.objects
                 .filter(account_type=account_type, email__iexact=email, used_at__isnull=True)
                 .order_by('-created_at').first())
        if reset is None or not reset.is_usable:
            return _render('otp', email=email,
                           error='That code is no longer valid. Request a new one.')

        if code != reset.code:
            reset.attempts += 1
            reset.save(update_fields=['attempts'])
            remaining = PasswordResetOTP.MAX_ATTEMPTS - reset.attempts
            if remaining <= 0:
                return _render('request',
                               error='Too many incorrect codes. Request a new one to try again.')
            return _render('otp', email=email,
                           error=f'Incorrect code. {remaining} attempt(s) left.')

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

        if account_type == 'Patron':
            log_admin_action(request, 'Update', 'Patron', account.patron_id,
                             f'Self-service password reset via OTP for {account.email}')
        else:
            log_admin_action(request, 'Update', 'User', account.admin_id,
                             f'Self-service password reset via OTP for {account.email}')

        return _render('done')

    return _render('request')


def patron_forgot_password(request):
    return _password_reset_view(request, 'patron')


def staff_forgot_password(request):
    return _password_reset_view(request, 'staff')


def admin_forgot_password(request):
    return _password_reset_view(request, 'admin')


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

    # Pending transactions count for badge
    pending_transactions_count = Transaction.objects.filter(transaction_type='Borrow', return_date__isnull=True).count()

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
        'pending_transactions_count': pending_transactions_count,
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
    pending_transactions_count = Transaction.objects.filter(transaction_type='Borrow', return_date__isnull=True).count()

    context = {
        'admin': admin,
        'books_available': books_available,
        'books_borrowed': books_borrowed,
        'books_overdue': books_overdue,
        'books_being_read': books_being_read,
        'recent_transactions': recent_transactions,
        'visitors_today': visitors_today,
        'pending_transactions_count': pending_transactions_count,
    }
    return render(request, 'library_staff/dashboard.html', context)


def admin_signin(request):
    return render(request, 'admin/signin.html')


def _books_page(request, template):
    from urllib.parse import urlencode
    q = (request.GET.get('q') or '').strip()
    status = (request.GET.get('status') or '').strip()
    genre = (request.GET.get('genre') or '').strip()

    books_queryset = Book.objects.select_related('shelf_level', 'shelf_level__shelf')
    if q:
        books_queryset = books_queryset.filter(
            Q(title__icontains=q) | Q(author__icontains=q) | Q(ISBN__icontains=q)
        )
    valid_status = [choice[0] for choice in Book.STATUS_CHOICES]
    if status in valid_status:
        books_queryset = books_queryset.filter(status=status)
    if genre:
        books_queryset = books_queryset.filter(genre=genre)
    books_queryset = books_queryset.order_by('title')

    # Global stats (independent of the filters above).
    total_books = Book.objects.count()
    total_copies = total_books
    available_count = Book.objects.filter(status='Available').count()
    borrowed_count = Book.objects.filter(status='Borrowed').count()
    shelf_levels = ShelfLevel.objects.select_related('shelf').all()
    genres = list(
        Book.objects.exclude(genre__isnull=True).exclude(genre='')
        .order_by('genre').values_list('genre', flat=True).distinct()
    )

    paginator = Paginator(books_queryset, 15)  # 15 books per page
    books = paginator.get_page(request.GET.get('page', 1))

    pending_transactions_count = Transaction.objects.filter(transaction_type='Borrow', return_date__isnull=True).count()

    params = {}
    for key, value in (('q', q), ('status', status), ('genre', genre)):
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
        'status_choices': valid_status,
        'q': q,
        'status': status,
        'genre': genre,
        'querystring': urlencode(params),
        'pending_transactions_count': pending_transactions_count,
        'paginator': paginator,
    }
    return render(request, template, context)


@admin_module_required('books')
def admin_management(request):
    return _books_page(request, 'admin/managebooks.html')


@module_required('books')
def staff_manage_books(request):
    return _books_page(request, 'library_staff/managebooks.html')


@admin_login_required
def admin_add_book(request):
    error = None

    if request.method == 'POST':
        title = request.POST.get('title', '').strip()
        author = request.POST.get('author', '').strip()
        isbn = request.POST.get('ISBN', '').strip()
        genre = request.POST.get('genre', '').strip()
        status = request.POST.get('status', 'Available').strip()
        shelf_level_id = request.POST.get('shelf_level', '').strip()
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
                status=status or 'Available',
                shelf_level=shelf_level,
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
            Q(fullname__icontains=search_query) | Q(email__icontains=search_query)
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

    # Apply the search filter to the table list.
    if search_query:
        patrons_queryset = patrons_queryset.filter(
            Q(fullname__icontains=search_query) | Q(email__icontains=search_query)
        )

    page_number = request.GET.get('page', 1)
    paginator = Paginator(patrons_queryset, 15)  # 15 patrons per page
    patrons = paginator.get_page(page_number)

    # Pending transactions count for badge
    pending_transactions_count = Transaction.objects.filter(transaction_type='Borrow', return_date__isnull=True).count()

    from urllib.parse import urlencode
    context = {
        'patrons': patrons,
        'total_patrons': total_patrons,
        'active_patrons': active_patrons,
        'patrons_with_borrows': patrons_with_borrows,
        'patrons_overdue': patrons_overdue,
        'pending_patrons': pending_patrons,
        'visitor_count': visitor_count,
        'show_visitors': show_visitors,
        'pending_transactions_count': pending_transactions_count,
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


@admin_login_required
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


@admin_login_required
def admin_delete_book(request, book_id):
    if request.method == 'POST':
        book = Book.objects.filter(book_id=book_id).first()
        if book:
            title = book.title
            book.delete()
            log_admin_action(request, 'Delete', 'Book', book_id, f'Deleted "{title}"')
    return portal_redirect(request, 'admin_management')


@admin_login_required
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


@admin_login_required
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
                tx.book.status = 'Available'
                tx.book.save()
            log_admin_action(request, 'Process', 'Transaction', tx.transaction_id,
                             f'Returned "{tx.book.title if tx.book else ""}"')
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
                             + (' — inventory copy flagged' if flagged else ''))
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
    pending_transactions_count = currently_out

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
        'pending_transactions_count': pending_transactions_count,
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
    # Fetch all floor plans for dropdown selector
    floorplans = FloorPlan.objects.all().order_by('-uploaded_at')
    
    # Determine which floor plan to display
    floorplan = None
    no_floorplans = False
    
    if floorplans.exists():
        # Check if a specific floor plan was requested via URL parameter
        requested_floorplan_id = request.GET.get('floorplan')
        if requested_floorplan_id:
            try:
                floorplan = FloorPlan.objects.filter(floor_plan_id=int(requested_floorplan_id)).first()
            except (ValueError, TypeError):
                pass
        
        # If no specific floor plan requested or not found, try to get active floor plan
        if not floorplan:
            floorplan = FloorPlan.objects.filter(is_active=True).first()
        
        # Fallback to most recent if still no floor plan
        if not floorplan:
            floorplan = floorplans.first()
    else:
        no_floorplans = True
    
    # Fetch related entities with coordinates if floor plan exists
    rooms_data = []
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
    
    # Serialize all floor plans for dropdown
    floorplans_list = []
    for fp in floorplans:
        floorplans_list.append({
            'id': fp.floor_plan_id,
            'name': fp.name,
            'is_active': fp.is_active,
            'uploaded_at': fp.uploaded_at.isoformat() if fp.uploaded_at else None
        })
    
    # Pending transactions count for badge
    pending_transactions_count = Transaction.objects.filter(transaction_type='Borrow', return_date__isnull=True).count()

    # A specific shelf to land on, e.g. arriving from "Locate on Map" on a
    # book's detail modal. `book` is a display label only (not looked up).
    target_shelf = None
    shelf_param = request.GET.get('shelf')
    if shelf_param:
        shelf = Shelf.objects.filter(shelf_id=shelf_param, room__floor_plan=floorplan).select_related('room').first()
        if shelf:
            target_shelf = {
                'id': shelf.shelf_id,
                'name': shelf.name,
                'room_name': shelf.room.name if shelf.room else None,
                'book': request.GET.get('book') or '',
            }

    context = {
        'floorplan': json.dumps(floorplan_data) if floorplan_data else 'null',
        'floorplans': json.dumps(floorplans_list),
        'rooms': json.dumps(rooms_data),
        'shelves': json.dumps(shelves_data),
        'waypoints': json.dumps(waypoints_data),
        'beacons': json.dumps(beacons_data),
        'show_beacons': is_admin_view,
        'no_floorplans': no_floorplans,
        'pending_transactions_count': pending_transactions_count,
        'target_shelf': json.dumps(target_shelf) if target_shelf else 'null',
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

    logs_qs = PatronLog.objects.select_related('patron').filter(entry_time__date=sel_date)
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
    desk_mode = desk_is_armed(request)
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


@admin_module_required('logs')
def admin_log_management(request):
    return _logs_page(request, 'admin/logmanagement.html')


@module_required('logs')
def staff_logs(request):
    return _logs_page(request, 'library_staff/logmanagement.html')


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
        'shelf_level': (f'Level {book.shelf_level.level_number}' if book.shelf_level else 'N/A'),
        'copies': Book.objects.filter(title=book.title, author=book.author).count(),
    }
    return JsonResponse(book_data)


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
            'shelf_level': (f'Level {book.shelf_level.level_number}' if book.shelf_level else 'N/A'),
        }
    }
    return JsonResponse(book_data)


@admin_login_required
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


@admin_login_required
def download_book_template(request):
    wb = Workbook()
    ws = wb.active
    ws.title = "Book Import Template"
    
    # Matched by name, so order does not matter and extra columns are ignored.
    headers = ['Title', 'Author', 'Publication Year', 'ISBN', 'Genre',
               'Quantity', 'Storage Area']
    ws.append(headers)
    ws.append(['Example Book Title', 'Surname, First', 2019, '9780000000000',
               'Fiction', 2, 'Zone 3'])
    
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename=book_import_template.xlsx'
    wb.save(response)
    return response


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


@admin_login_required
def import_books(request):
    """Bulk-import books from a spreadsheet.

    Columns are matched by *header name*, not position, so a sheet recorded in
    a different order still imports. Aliases cover what libraries actually
    write in their own sheets ("Barcode" for the ISBN, "Year Publish" for the
    publication year, and so on).

    Quantity creates that many physical copies — the catalogue stores one row
    per copy — and Storage Area is resolved to a shelf level by name so a sheet
    can carry its own locations.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    if 'excel_file' not in request.FILES:
        return JsonResponse({'success': False, 'error': 'No file uploaded'})

    excel_file = request.FILES['excel_file']
    if not excel_file.name.endswith('.xlsx'):
        return JsonResponse({'success': False, 'error': 'Only .xlsx files are allowed'})

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
        'quantity': 'quantity', 'qty': 'quantity', 'copies': 'quantity',
        'numberofcopies': 'quantity',
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
        unmatched_areas = set()

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

            for copy_no in range(quantity):
                # Only the first copy carries the ISBN: it is unique to the
                # title, and the duplicate check above relies on that.
                book = Book.objects.create(
                    title=title,
                    author=author,
                    publication_year=publication_year,
                    ISBN=isbn if copy_no == 0 else None,
                    genre=field(row, 'genre') or None,
                    shelf_level=shelf_level,
                    status='Available',
                    qr_code=str(uuid4()),
                )
                imported += 1
                if copy_no > 0:
                    copies_created += 1

        parts = [f'Imported {imported} book record(s)']
        if copies_created:
            parts.append(f'including {copies_created} extra copy/copies from Quantity')
        if skipped_dup:
            parts.append(f'skipped {skipped_dup} row(s) whose ISBN already exists')
        if skipped_blank:
            parts.append(f'skipped {skipped_blank} row(s) with no title or author')
        if unmatched_areas:
            parts.append('could not match storage area: ' + ', '.join(sorted(unmatched_areas)))

        return JsonResponse({
            'success': True,
            'message': '. '.join(parts) + '.',
            'imported': imported,
            'matched_columns': sorted(columns),
        })

    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})


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
    
    if not excel_file.name.endswith('.xlsx'):
        return JsonResponse({'success': False, 'error': 'Only .xlsx files are allowed'})
    
    try:
        wb = openpyxl.load_workbook(excel_file)
        ws = wb.active
        
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
        
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})


@admin_login_required
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


@admin_login_required
def import_donations(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})
    
    if 'excel_file' not in request.FILES:
        return JsonResponse({'success': False, 'error': 'No file uploaded'})
    
    excel_file = request.FILES['excel_file']
    
    if not excel_file.name.endswith('.xlsx'):
        return JsonResponse({'success': False, 'error': 'Only .xlsx files are allowed'})
    
    try:
        wb = openpyxl.load_workbook(excel_file)
        ws = wb.active
        
        imported_count = 0
        skipped_count = 0
        
        for row in ws.iter_rows(min_row=2):
            donor_name = row[0].value
            date_donated = row[1].value
            title = row[2].value
            author = row[3].value
            isbn = row[4].value
            genre = row[5].value
            publication_year = row[6].value
            
            if not donor_name or not title or not author:
                continue
            
            book = Book.objects.create(
                title=title,
                author=author,
                ISBN=isbn,
                genre=genre,
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
        
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})


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
    
    if not excel_file.name.endswith('.xlsx'):
        return JsonResponse({'success': False, 'error': 'Only .xlsx files are allowed'})
    
    try:
        wb = openpyxl.load_workbook(excel_file)
        ws = wb.active
        
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
        
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})


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


@admin_login_required
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
    if transaction_type == 'Borrow':
        eligible, violations = check_patron_eligibility(patron)
        if not eligible:
            return JsonResponse({
                'success': False,
                'error': f'{patron.fullname} is not eligible to borrow',
                'errors': violations,
            })

        # Enforce the configured borrowing limit.
        active_borrows = Transaction.objects.filter(
            patron=patron, transaction_type='Borrow', return_date__isnull=True
        ).count()
        if active_borrows + len(book_ids) > rule.max_books_per_patron:
            return JsonResponse({
                'success': False,
                'error': f'{patron.fullname} is not eligible to borrow',
                'errors': [
                    f'Borrowing limit is {rule.max_books_per_patron} book(s). '
                    f'This patron already has {active_borrows} active borrow(s).'
                ],
            })

    # Validate books and process transaction
    errors = []
    processed_books = []
    borrowed_books = []
    borrow_due_date = None
    returned_books = []
    returned_overdue = False
    today = timezone.localdate()
    
    for book_id_str in book_ids:
        try:
            book_id_int = int(book_id_str)
        except ValueError:
            errors.append(f'Invalid book_id: {book_id_str}')
            continue
        
        book = Book.objects.filter(book_id=book_id_int).first()
        if book is None:
            errors.append(f'Book not found: {book_id_str}')
            continue
        
        # Validate book status based on transaction type
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
        
        # Process transaction
        if transaction_type == 'Borrow':
            due_date = today + timedelta(days=rule.loan_period_days)
            book.status = 'Borrowed'
            book.save()
            Transaction.objects.create(
                patron=patron,
                book=book,
                processed_by=admin,
                transaction_type='Borrow',
                due_date=due_date
            )
            borrowed_books.append(book)
            borrow_due_date = due_date
        elif transaction_type == 'Return':
            book.status = 'Available'
            book.save()
            # Find the active borrow transaction for this book
            tx = Transaction.objects.filter(
                book=book,
                transaction_type='Borrow',
                return_date__isnull=True
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
                due_date=None
            )
        
        processed_books.append(book.title)

    if errors:
        return JsonResponse({
            'success': False,
            'error': 'Transaction validation failed',
            'errors': errors,
            'processed': processed_books
        })

    patron_label = f' for {patron.fullname}' if patron else ''
    log_admin_action(
        request, 'Process', 'Transaction', None,
        f'{transaction_type}: {len(processed_books)} book(s){patron_label}{verification}'
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

    # Pending transactions count for badge
    pending_transactions_count = Transaction.objects.filter(transaction_type='Borrow', return_date__isnull=True).count()

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
        'pending_transactions_count': pending_transactions_count,
    })


@admin_module_required('donations')
def donation_management(request):
    return _donations_page(request, 'admin/donationadmin.html')


@module_required('donations')
def staff_donations(request):
    return _donations_page(request, 'library_staff/donationadmin.html')


@admin_login_required
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


@admin_login_required
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
@admin_only_required
def announcement_management(request):
    announcements_queryset = Announcement.objects.select_related('posted_by').order_by('-created_at')
    
    if request.method == 'POST':
        title = request.POST.get('title', '').strip()
        message = request.POST.get('message', '').strip()
        
        if not all([title, message]):
            error = 'Title and message are required.'
            return render(request, 'admin/announcementadmin.html', {'announcements': announcements_queryset, 'error': error})
        
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
    
    # Pending transactions count for badge
    pending_transactions_count = Transaction.objects.filter(transaction_type='Borrow', return_date__isnull=True).count()
    
    return render(request, 'admin/announcementadmin.html', {'announcements': announcements, 'paginator': paginator, 'pending_transactions_count': pending_transactions_count})


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
            if floorplan.is_active:
                # Toggle off: deactivate this floor plan.
                floorplan.is_active = False
                floorplan.save()
            else:
                # Activate this one (only one active at a time).
                FloorPlan.objects.all().update(is_active=False)
                floorplan.is_active = True
                floorplan.save()

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
@admin_login_required
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


@admin_login_required
def import_transactions(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})
    
    if 'excel_file' not in request.FILES:
        return JsonResponse({'success': False, 'error': 'No file uploaded'})
    
    excel_file = request.FILES['excel_file']
    
    if not excel_file.name.endswith('.xlsx'):
        return JsonResponse({'success': False, 'error': 'Only .xlsx files are allowed'})
    
    try:
        wb = openpyxl.load_workbook(excel_file)
        ws = wb.active
        
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
        
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})


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
    
    if not excel_file.name.endswith('.xlsx'):
        return JsonResponse({'success': False, 'error': 'Only .xlsx files are allowed'})
    
    try:
        wb = openpyxl.load_workbook(excel_file)
        ws = wb.active
        
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
        
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})


# ─── REPORTS VIEWS ───────────────────────────────────────────────
@admin_only_required
def admin_reports(request):
    """Reports hub: on-screen preview of the selected report + date range."""
    report_type = request.GET.get('type', 'transactions')
    if report_type not in dict(REPORT_TYPES):
        report_type = 'transactions'
    start, end = parse_date_range(request.GET.get('start'), request.GET.get('end'))
    report = build_report(report_type, start, end)

    pending_transactions_count = Transaction.objects.filter(
        transaction_type='Borrow', return_date__isnull=True
    ).count()

    context = {
        'report': report,
        'report_types': REPORT_TYPES,
        'selected_type': report_type,
        'start_date': start.strftime('%Y-%m-%d'),
        'end_date': end.strftime('%Y-%m-%d'),
        'is_snapshot': report_type in {'books', 'patrons'},
        'pending_transactions_count': pending_transactions_count,
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

    pending_transactions_count = Transaction.objects.filter(
        transaction_type='Borrow', return_date__isnull=True
    ).count()
    return render(request, 'admin/borrowingrules.html', {
        'rule': rule,
        'saved': saved,
        'error': error,
        'pending_transactions_count': pending_transactions_count,
    })


# ─── SHELF MANAGEMENT VIEWS ──────────────────────────────────────
def _shelf_page(request, template):
    """IDE-style hierarchical manager for shelves, sections, levels and books."""
    pending_transactions_count = Transaction.objects.filter(
        transaction_type='Borrow', return_date__isnull=True
    ).count()
    return render(request, template, {
        'pending_transactions_count': pending_transactions_count,
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
def get_books_for_placement(request):
    """Searchable list of books for the shelf-level placement picker."""
    q = (request.GET.get('q') or '').strip()
    books = Book.objects.select_related('shelf_level').order_by('title')
    if q:
        books = books.filter(
            Q(title__icontains=q) | Q(author__icontains=q) | Q(ISBN__icontains=q)
        )
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


@admin_login_required
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


@admin_login_required
def get_shelf_tree(request):
    """Returns full nested hierarchy FloorPlan > Room > Shelf > ShelfLevel > Books"""
    floor_plans = FloorPlan.objects.filter(is_active=True).prefetch_related(
        'room_set__shelf_set__shelflevel_set__book_set'
    )
    
    tree_data = []
    for fp in floor_plans:
        fp_node = {
            'type': 'floorplan',
            'id': fp.floor_plan_id,
            'name': f'Floor Plan {fp.floor_plan_id}',
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
                    books = shelf_level.book_set.all()
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


@admin_login_required
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


@admin_login_required
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


@admin_only_required
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
        return JsonResponse({'success': True, 'geometry': room.geometry})
    except Room.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Room not found'})


@admin_only_required
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


@admin_login_required
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
        )
        return JsonResponse({'success': True, 'shelf_id': shelf.shelf_id})
    except Room.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Room not found'})


@admin_login_required
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
        shelf.save()
        return JsonResponse({'success': True})
    except Shelf.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Shelf not found'})


# ─── SHELF PLACEMENT ON THE FLOOR PLAN ────────────────────────────────
# One endpoint per Administrator action so each traces to its activity diagram:
# Place Shelf (Fig. 48), Move Shelf (Fig. 49), Rotate Shelf (Fig. 50) and
# Unplace Shelf (Fig. 51). Confirmation happens in the UI before these are hit.

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

    shelf.map_x, shelf.map_y = map_x, map_y
    shelf.save(update_fields=['map_x', 'map_y'])
    log_admin_action(request, action, 'Shelf', shelf.shelf_id,
                     f'{shelf.name} {action.lower()} at ({map_x:.0f}, {map_y:.0f})')
    return JsonResponse({'success': True, 'map_x': shelf.map_x, 'map_y': shelf.map_y})


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

    shelf.rotation = rotation % 360        # keep it in 0–359 whatever is sent
    shelf.save(update_fields=['rotation'])
    log_admin_action(request, 'Rotate', 'Shelf', shelf.shelf_id,
                     f'{shelf.name} rotated to {shelf.rotation:.0f}°')
    return JsonResponse({'success': True, 'rotation': shelf.rotation})


# ─── DOORS ON ROOM WALLS ──────────────────────────────────────────────
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
        width = float(request.POST.get('width') or 28)
    except (TypeError, ValueError):
        width = 28.0
    if not (6 <= width <= 400):
        return JsonResponse({'success': False, 'error': 'Door width must be between 6 and 400'})

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
    return JsonResponse({'success': True, 'door': _door_payload(door)})


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


@admin_login_required
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


@admin_login_required
def add_shelf_level(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    shelf_id = request.POST.get('shelf_id')
    level_number = request.POST.get('level_number')
    category = request.POST.get('category', '')

    if not shelf_id or not level_number:
        return JsonResponse({'success': False, 'error': 'shelf_id and level_number are required'})

    try:
        shelf = Shelf.objects.get(shelf_id=shelf_id)
        shelf_level = ShelfLevel.objects.create(
            shelf=shelf,
            level_number=int(level_number),
            category=category
        )
        return JsonResponse({'success': True, 'shelf_level_id': shelf_level.shelf_level_id})
    except Shelf.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Shelf not found'})


@admin_login_required
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
        shelf_level.save()
        return JsonResponse({'success': True})
    except ShelfLevel.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Shelf level not found'})


@admin_login_required
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


@admin_login_required
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


def _door_payload(door):
    return {
        'door_id': door.door_id,
        'room_id': door.room_id,
        'x': door.map_x,
        'y': door.map_y,
        'width': door.width or 28,
        'rotation': door.rotation or 0,
        'swing': door.swing if door.swing in (1, -1) else 1,
        'label': door.label or '',
    }


def _room_payload(room, doors_by_room=None):
    """Serialise a room including its drawn polygon (may be None if never drawn)."""
    doors = (doors_by_room or {}).get(room.room_id, [])
    return {
        'room_id': room.room_id,
        'name': room.name,
        'geometry': room.geometry or None,
        'map_x': room.map_x,
        'map_y': room.map_y,
        'doors': [_door_payload(d) for d in doors],
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
        _room_payload(r, doors_by_room)
        for r in Room.objects.filter(floor_plan=floor_plan)
    ]

    shelves = [
        {
            'shelf_id': s.shelf_id, 'name': s.name,
            'map_x': s.map_x, 'map_y': s.map_y, 'rotation': s.rotation or 0,
            'width': s.width or 46, 'depth': s.depth or 14,
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
        'beacons': beacons,
        'waypoints': waypoints,
        'connections': connections,
        'rooms': rooms,
        'shelves': shelves,
    })


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
    except ValueError as bad:
        return JsonResponse({'success': False, 'error': f'{bad} must be a number'})

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
    beacon.save()

    detail = f'{beacon.label or beacon.beacon_uuid[:8]} — {adv_type}'
    if before != adv_type:
        detail += f' (was {before})'
    log_admin_action(request, 'Update', 'BLEBeacon', beacon.beacon_id, detail)
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


@patron_login_required
def get_patron_map_data(request):
    """Read-only map payload for the patron navigation map: active floor plan,
    shelves, waypoints, beacons and connections."""
    floor_plan = FloorPlan.objects.filter(is_active=True).first()
    if floor_plan is None:
        return JsonResponse({'success': False, 'has_active': False, 'error': 'No active floor plan'})

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
            'x': s.map_x, 'y': s.map_y, 'rotation': s.rotation or 0,
            'width': s.width or 46, 'depth': s.depth or 14,
            # Feeds the "section labels" and "shelf level indicators" layers.
            'levels': [
                {'level_number': lv.level_number, 'category': lv.category or ''}
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
        'renovation_notice': floor_plan.renovation_notice or '',
        'rooms': rooms,
        'shelves': shelves,
        'waypoints': waypoints,
        'beacons': beacons,
        'connections': connections,
    })


@patron_login_required
def get_navigation_route(request):
    """Compute the A* route from the patron's current position to a target shelf
    (or waypoint) over the active floor plan's waypoint graph."""
    floor_plan = FloorPlan.objects.filter(is_active=True).first()
    if floor_plan is None:
        return JsonResponse({'success': False, 'error': 'No active floor plan'})

    try:
        start_x = float(request.GET.get('start_x'))
        start_y = float(request.GET.get('start_y'))
    except (TypeError, ValueError):
        return JsonResponse({'success': False, 'error': 'start_x and start_y are required'})

    target_shelf_id = request.GET.get('target_shelf_id')
    target_waypoint_id = request.GET.get('target_waypoint_id')
    if not target_shelf_id and not target_waypoint_id:
        return JsonResponse({'success': False, 'error': 'A target_shelf_id or target_waypoint_id is required'})

    waypoints = list(Waypoint.objects.filter(floor_plan=floor_plan))
    if not waypoints:
        return JsonResponse({'success': False, 'error': 'No waypoints configured for this floor plan'})

    coords = {w.waypoint_id: (w.map_x, w.map_y) for w in waypoints}
    wp_ids = set(coords)
    adjacency = {wid: [] for wid in coords}
    for c in WaypointConnection.objects.filter(
        waypoint_from_id__in=wp_ids, waypoint_to_id__in=wp_ids
    ):
        adjacency[c.waypoint_from_id].append((c.waypoint_to_id, c.distance))
        adjacency[c.waypoint_to_id].append((c.waypoint_from_id, c.distance))

    def nearest_waypoint(x, y):
        best_id, best_d = None, None
        for wid, (wx, wy) in coords.items():
            d = math.hypot(wx - x, wy - y)
            if best_d is None or d < best_d:
                best_d, best_id = d, wid
        return best_id

    start_wp = nearest_waypoint(start_x, start_y)

    target_shelf = None
    if target_waypoint_id:
        try:
            goal_wp = int(target_waypoint_id)
        except ValueError:
            return JsonResponse({'success': False, 'error': 'Invalid target_waypoint_id'})
        if goal_wp not in coords:
            return JsonResponse({'success': False, 'error': 'Target waypoint not found on this floor plan'})
    else:
        target_shelf = Shelf.objects.filter(shelf_id=target_shelf_id).first()
        if target_shelf is None:
            return JsonResponse({'success': False, 'error': 'Target shelf not found'})
        linked = Waypoint.objects.filter(floor_plan=floor_plan, linked_shelf=target_shelf).first()
        if linked is not None:
            goal_wp = linked.waypoint_id
        elif target_shelf.map_x is None or target_shelf.map_y is None:
            return JsonResponse({
                'success': False,
                'error': "This book's shelf has not been placed on the floor plan yet. "
                         "Please ask library staff for directions.",
            })
        else:
            goal_wp = nearest_waypoint(target_shelf.map_x, target_shelf.map_y)

    path, total = _astar(start_wp, goal_wp, coords, adjacency)
    if path is None:
        return JsonResponse({'success': False, 'error': 'No path found between your position and the target'})

    route = [
        {'waypoint_id': wid, 'x': coords[wid][0], 'y': coords[wid][1]}
        for wid in path
    ]
    response = {
        'success': True,
        'route': route,
        'distance': total,
        'start_waypoint_id': start_wp,
        'goal_waypoint_id': goal_wp,
    }
    if target_shelf is not None:
        response['target_shelf'] = {
            'shelf_id': target_shelf.shelf_id,
            'name': target_shelf.name,
            'x': target_shelf.map_x,
            'y': target_shelf.map_y,
        }
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


@admin_login_required
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


@admin_login_required
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


@admin_login_required
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


@admin_login_required
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


@admin_login_required
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
    if len(password) < 6:
        messages.error(request, 'Password must be at least 6 characters.')
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
        'pending_transactions_count': Transaction.objects.filter(
            transaction_type='Borrow', return_date__isnull=True).count(),
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
        'books': Book.objects.order_by('title'),
        'condition_choices': InventoryRecord.CONDITION_CHOICES,
        'stage_choices': InventoryRecord.STAGE_CHOICES,
        'today': timezone.localdate().isoformat(),
        'pending_transactions_count': Transaction.objects.filter(
            transaction_type='Borrow', return_date__isnull=True).count(),
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


@admin_login_required
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
