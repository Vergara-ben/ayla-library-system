from django.shortcuts import redirect, render, get_object_or_404
from django.contrib import messages
from django.db.models import Count, Q
from django.utils import timezone
from django.http import HttpResponse, JsonResponse
from django.conf import settings
from django.core.paginator import Paginator
from datetime import timedelta
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
    staff_only_required,
)

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
    return_receipt_email,
    lost_book_email,
    otp_email,
    registration_approved_email,
    registration_rejected_email,
)
from .reports import REPORT_TYPES, parse_date_range, build_report, render_report_pdf, render_report_excel
from .models import Book, Patron, PatronLog, Transaction, User, ShelfLevel, Donation, Announcement, FloorPlan, Shelf, Room, Door, Waypoint, BLEBeacon, WaypointConnection, SystemLog, BorrowingRule

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
        otp_email(patron.email, patron.fullname, patron.otp_code)
        return render(request, 'patron/patronregister.html',
                      {'stage': 'otp', 'otp_email': email,
                       'info': 'A new code has been sent to your email.'})

    # ── Step 1: submit the registration form ────────────────
    fullname = (request.POST.get('fullname') or '').strip()
    email = (request.POST.get('email') or '').strip()
    password = request.POST.get('password')
    confirm_password = request.POST.get('confirm_password')
    patron_type = request.POST.get('patron_type')
    contact_number = (request.POST.get('contact_number') or '').strip()
    address = (request.POST.get('address') or '').strip()
    pin = (request.POST.get('pin') or '').strip()

    def _form_error(msg):
        return render(request, 'patron/patronregister.html', {'stage': 'form', 'error': msg})

    if not all([fullname, email, password, confirm_password, patron_type, contact_number, address, pin]):
        return _form_error('All fields are required')
    if password != confirm_password:
        return _form_error('Passwords do not match')
    if not (pin.isdigit() and 4 <= len(pin) <= 6):
        return _form_error('PIN must be 4 to 6 digits')

    existing = Patron.objects.filter(email__iexact=email).first()
    if existing is not None:
        # A stale unverified application may be replaced; anything else is a duplicate.
        if existing.account_status == 'Pending' and not existing.otp_verified:
            existing.delete()
        else:
            return _form_error('Email already exists')

    try:
        credential_path = _save_credential_document(request.FILES.get('credential_document'), email)
    except ValueError as exc:
        return _form_error(str(exc))

    patron = Patron.objects.create(
        fullname=fullname,
        email=email,
        password_hash=hash_password(password),
        pin_hash=hash_password(pin),
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
    otp_email(patron.email, patron.fullname, patron.otp_code)
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

    fullname = (request.POST.get('fullname') or '').strip()
    email = (request.POST.get('email') or '').strip()
    contact_number = (request.POST.get('contact_number') or '').strip()
    address = (request.POST.get('address') or '').strip()

    if not fullname or not email:
        return JsonResponse({'success': False, 'error': 'Full name and email are required.'})
    if Patron.objects.exclude(patron_id=patron.patron_id).filter(email__iexact=email).exists():
        return JsonResponse({'success': False, 'error': 'That email is already in use by another account.'})

    patron.fullname = fullname
    patron.email = email
    patron.contact_number = contact_number or None
    patron.address = address or None
    patron.save(update_fields=['fullname', 'email', 'contact_number', 'address'])
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


@patron_login_required
def patron_change_pin(request):
    """Set or change the transaction PIN.

    When a PIN already exists the current PIN is required; patrons without
    one (e.g. registered on-site at the desk) can set it directly."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})
    patron = get_object_or_404(Patron, patron_id=request.session.get('patron_id'))

    current = (request.POST.get('current_pin') or '').strip()
    new = (request.POST.get('new_pin') or '').strip()
    if not (new.isdigit() and 4 <= len(new) <= 6):
        return JsonResponse({'success': False, 'error': 'PIN must be 4 to 6 digits.'})
    if patron.pin_hash and not check_password(current, patron.pin_hash):
        return JsonResponse({'success': False, 'error': 'Current PIN is incorrect.'})

    patron.pin_hash = hash_password(new)
    patron.save(update_fields=['pin_hash'])
    return JsonResponse({'success': True, 'message': 'PIN saved.'})


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


@admin_only_required
def admin_management(request):
    return _books_page(request, 'admin/managebooks.html')


@staff_only_required
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


@admin_only_required
def admin_add_patron(request):
    error = None
    initial = {}

    if request.method == 'POST':
        fullname = request.POST.get('fullname', '').strip()
        patron_type = request.POST.get('patron_type', '').strip()
        email = request.POST.get('email', '').strip()
        contact_number = request.POST.get('contact_number', '').strip()
        address = request.POST.get('address', '').strip()
        password = request.POST.get('password', '').strip()
        account_status = request.POST.get('account_status', 'Active').strip()

        initial = {
            'fullname': fullname,
            'patron_type': patron_type,
            'email': email,
            'contact_number': contact_number,
            'address': address,
            'account_status': account_status,
        }

        if not all([fullname, patron_type, email, password]):
            error = 'Full name, patron type, email, and password are required.'
        elif Patron.objects.filter(email=email).exists():
            error = 'A patron with that email already exists.'
        else:
            hashed_password = hash_password(password)
            patron = Patron.objects.create(
                fullname=fullname,
                email=email,
                password_hash=hashed_password,
                patron_type=patron_type,
                contact_number=contact_number,
                address=address,
                account_status=account_status or 'Active',
                registration_channel='On-site',
                qr_code=str(uuid4()),
                otp_verified=True,
            )
            log_admin_action(request, 'Create', 'Patron', patron.patron_id, f'Added "{patron.fullname}"')
            return redirect('admin_manage_patron')

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
    return render(request, 'admin/managepatron.html', context)


@admin_only_required
def approve_patron(request, patron_id):
    """Approve a pending registration: activate, generate the identity QR, email."""
    if request.method != 'POST':
        return redirect('admin_manage_patron')
    patron = Patron.objects.filter(patron_id=patron_id, account_status='Pending').first()
    if patron is None:
        messages.error(request, 'Pending patron not found.')
        return redirect('admin_manage_patron')

    patron.account_status = 'Active'
    if not patron.qr_code:
        patron.qr_code = str(uuid4())
    patron.save(update_fields=['account_status', 'qr_code'])
    log_admin_action(request, 'Update', 'Patron', patron.patron_id,
                     f'Approved registration of "{patron.fullname}"')
    registration_approved_email(patron)
    messages.success(request, f'{patron.fullname} approved. Their QR code is now active.')
    return redirect('admin_manage_patron')


@admin_only_required
def reject_patron(request, patron_id):
    """Reject a pending registration: notify the applicant and remove the row."""
    if request.method != 'POST':
        return redirect('admin_manage_patron')
    patron = Patron.objects.filter(patron_id=patron_id, account_status='Pending').first()
    if patron is None:
        messages.error(request, 'Pending patron not found.')
        return redirect('admin_manage_patron')

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
    return redirect('admin_manage_patron')


@admin_only_required
def admin_manage_patron(request):
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
    
    # Regular page load
    patrons_queryset = Patron.objects.annotate(
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

    # Online registrations awaiting Administrator approval.
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
        'pending_transactions_count': pending_transactions_count,
        'paginator': paginator,
        'search_query': search_query,
        'querystring': urlencode({'search': search_query}) if search_query else '',
    }
    return render(request, 'admin/managepatron.html', context)


@admin_only_required
def admin_edit_patron(request, patron_id):
    patron = Patron.objects.filter(patron_id=patron_id).first()
    if patron is None:
        return redirect('admin_manage_patron')

    error = None
    success = None
    initial = {
        'fullname': patron.fullname,
        'patron_type': patron.patron_type,
        'email': patron.email,
        'contact_number': patron.contact_number,
        'address': patron.address,
        'account_status': patron.account_status,
    }

    if request.method == 'POST':
        fullname = request.POST.get('fullname', '').strip()
        patron_type = request.POST.get('patron_type', '').strip()
        email = request.POST.get('email', '').strip()
        contact_number = request.POST.get('contact_number', '').strip()
        address = request.POST.get('address', '').strip()
        password = request.POST.get('password', '').strip()
        account_status = request.POST.get('account_status', 'Active').strip()

        if not all([fullname, patron_type, email]):
            error = 'Full name, patron type, and email are required.'
        elif Patron.objects.exclude(patron_id=patron_id).filter(email=email).exists():
            error = 'A different patron already uses that email.'
        else:
            patron.fullname = fullname
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


@admin_only_required
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
            log_admin_action(request, 'Process', 'Transaction', tx.transaction_id,
                             f'Marked "{tx.book.title if tx.book else ""}" lost (fine {tx.fine_amount})')
            if tx.patron and tx.book:
                lost_book_email(tx.patron, tx.book, tx.fine_amount)
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


@admin_only_required
def admin_book_detail(request):
    return _book_detail_page(request, 'admin/bookdetail.html')


@staff_only_required
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
    total_returned = Transaction.objects.filter(transaction_type='Return').count()
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


@admin_only_required
def admin_transaction(request):
    return _transaction_page(request, 'admin/transaction.html')


@staff_only_required
def staff_transaction(request):
    return _transaction_page(request, 'library_staff/transaction.html')


def _indoor_map_page(request, template):
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
        'no_floorplans': no_floorplans,
        'pending_transactions_count': pending_transactions_count,
        'target_shelf': json.dumps(target_shelf) if target_shelf else 'null',
    }

    return render(request, template, context)


@admin_only_required
def admin_indoor_map(request):
    return _indoor_map_page(request, 'admin/indoormap.html')


@staff_only_required
def staff_indoor_map(request):
    return _indoor_map_page(request, 'library_staff/indoormap.html')


def _logs_page(request, template):
    from datetime import datetime
    from urllib.parse import urlencode

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

    # Stat cards reflect today's overall activity, independent of the table filters.
    todays_entries = PatronLog.objects.filter(entry_time__date=today).count()
    todays_exits = PatronLog.objects.filter(exit_time__date=today).count()
    currently_inside = PatronLog.objects.filter(exit_time__isnull=True).count()

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


@admin_only_required
def admin_log_management(request):
    return _logs_page(request, 'admin/logmanagement.html')


@staff_only_required
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
def download_book_template(request):
    wb = Workbook()
    ws = wb.active
    ws.title = "Book Import Template"
    
    headers = ['title', 'author', 'publication_year', 'ISBN', 'genre', 'shelf_level_id']
    ws.append(headers)
    
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename=book_import_template.xlsx'
    wb.save(response)
    return response


@admin_login_required
def import_books(request):
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
            title = row[0].value
            author = row[1].value
            publication_year = row[2].value
            isbn = row[3].value
            genre = row[4].value
            shelf_level_id = row[5].value

            if not title or not author:
                continue

            if isbn and Book.objects.filter(ISBN=isbn).exists():
                skipped_count += 1
                continue

            shelf_level = None
            if shelf_level_id and str(shelf_level_id).strip() not in ['', 'N/A', 'n/a']:
                shelf_level = ShelfLevel.objects.filter(shelf_level_id=shelf_level_id).first()

            book = Book.objects.create(
                title=title,
                author=author,
                publication_year=int(publication_year) if publication_year else None,
                ISBN=isbn,
                genre=genre,
                shelf_level=shelf_level,
                status='Available'
            )
            
            qr = qrcode.QRCode(version=1, box_size=10, border=5)
            qr.add_data(str(book.book_id))
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            
            qr_dir = os.path.join(settings.MEDIA_ROOT, 'qrcodes', 'books')
            os.makedirs(qr_dir, exist_ok=True)
            
            qr_filename = f'book_qr_{book.book_id}.png'
            qr_path = os.path.join(qr_dir, qr_filename)
            img.save(qr_path)
            
            book.cover_img_url = f'qrcodes/books/{qr_filename}'
            book.save()
            
            imported_count += 1
        
        return JsonResponse({
            'success': True,
            'message': f'Successfully imported {imported_count} books. Skipped {skipped_count} duplicates.'
        })
        
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})


@admin_only_required
def download_patron_template(request):
    wb = Workbook()
    ws = wb.active
    ws.title = "Patron Import Template"
    
    headers = ['fullname', 'patron_type', 'email', 'contact_number', 'address', 'account_status']
    ws.append(headers)
    
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename=patron_import_template.xlsx'
    wb.save(response)
    return response


@admin_only_required
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
            fullname = row[0].value
            patron_type = row[1].value
            email = row[2].value
            contact_number = row[3].value
            address = row[4].value
            account_status = row[5].value
            
            if not fullname or not email:
                continue
            
            if Patron.objects.filter(email=email).exists():
                skipped_count += 1
                continue
            
            patron = Patron.objects.create(
                fullname=fullname,
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
            
            donation = Donation.objects.create(
                donor_name=donor_name,
                date_donated=date_donated if date_donated else timezone.now(),
                book=book,
                status='Received'
            )
            
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
    book_ids = request.POST.getlist('book_ids')
    
    if not transaction_type:
        return JsonResponse({'success': False, 'error': 'transaction_type is required'})
    
    if transaction_type not in ['Borrow', 'Return', 'In-Library Reading']:
        return JsonResponse({'success': False, 'error': 'Invalid transaction_type'})
    
    if not book_ids:
        return JsonResponse({'success': False, 'error': 'book_ids is required'})
    
    # Validate patron for Borrow and Return transactions
    patron = None
    if transaction_type in ['Borrow', 'Return']:
        if not patron_id:
            return JsonResponse({'success': False, 'error': 'patron_id is required for this transaction type'})
        try:
            patron_id_int = int(patron_id)
            patron = Patron.objects.filter(patron_id=patron_id_int).first()
            if patron is None:
                return JsonResponse({'success': False, 'error': 'Patron not found'})
        except ValueError:
            return JsonResponse({'success': False, 'error': 'Invalid patron_id format'})
    
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
        f'{transaction_type}: {len(processed_books)} book(s){patron_label}'
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
    donations_queryset = Donation.objects.select_related('book').order_by('-date_donated')
    
    if request.method == 'POST':
        donor_name = request.POST.get('donor_name', '').strip()
        date_donated = request.POST.get('date_donated', '').strip()
        title = request.POST.get('title', '').strip()
        author = request.POST.get('author', '').strip()
        isbn = request.POST.get('ISBN', '').strip()
        genre = request.POST.get('genre', '').strip()
        publication_year = request.POST.get('publication_year', '').strip()
        
        if not all([donor_name, date_donated, title, author]):
            error = 'Donor name, date, title, and author are required.'
            return render(request, template, {'donations': donations_queryset, 'error': error})
        
        # Create book with status Donated
        book = Book.objects.create(
            title=title,
            author=author,
            ISBN=isbn if isbn else None,
            genre=genre if genre else None,
            publication_year=int(publication_year) if publication_year else None,
            status='Donated'
        )
        
        # Generate QR code for the book
        qr = qrcode.QRCode(version=1, box_size=10, border=5)
        qr.add_data(str(book.book_id))
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        
        qr_dir = os.path.join(settings.MEDIA_ROOT, 'qrcodes', 'books')
        os.makedirs(qr_dir, exist_ok=True)
        
        qr_filename = f'book_qr_{book.book_id}.png'
        qr_path = os.path.join(qr_dir, qr_filename)
        img.save(qr_path)
        
        # Save QR code path to cover_img_url
        book.cover_img_url = f'qrcodes/books/{qr_filename}'
        book.save()
        
        # Create donation record
        from datetime import datetime
        donation = Donation.objects.create(
            book=book,
            donor_name=donor_name,
            date_donated=datetime.strptime(date_donated, '%Y-%m-%d').date() if date_donated else timezone.localdate(),
            status='Received'
        )
        log_admin_action(request, 'Create', 'Donation', donation.donation_id,
                         f'"{book.title}" from {donor_name}')

        return portal_redirect(request, 'donation_management')

    # Pagination
    page_number = request.GET.get('page', 1)
    paginator = Paginator(donations_queryset, 15)  # 15 donations per page
    donations = paginator.get_page(page_number)

    # Pending transactions count for badge
    pending_transactions_count = Transaction.objects.filter(transaction_type='Borrow', return_date__isnull=True).count()

    return render(request, template, {'donations': donations, 'paginator': paginator, 'pending_transactions_count': pending_transactions_count})


@admin_only_required
def donation_management(request):
    return _donations_page(request, 'admin/donationadmin.html')


@staff_only_required
def staff_donations(request):
    return _donations_page(request, 'library_staff/donationadmin.html')


@admin_login_required
def update_donation_status(request):
    if request.method == 'POST':
        donation_id = request.POST.get('donation_id')
        status = request.POST.get('status')
        
        donation = Donation.objects.filter(donation_id=donation_id).first()
        if donation:
            donation.status = status
            donation.save()

            # If status is Shelved, update book status to Available
            if status == 'Shelved':
                donation.book.status = 'Available'
                donation.book.save()
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
            # Delete the book as well since it's linked
            donation.book.delete()
            log_admin_action(request, 'Delete', 'Donation', donation_id, detail)

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

        # Optionally email the announcement to all active patrons.
        if request.POST.get('email_patrons'):
            recipients = Patron.objects.filter(account_status='Active').exclude(email='')
            sent = 0
            for p in recipients:
                if announcement_email(p, announcement):
                    sent += 1
            log_admin_action(request, 'Notify', 'Announcement', announcement.announcement_id,
                             f'Emailed "{title}" to {sent} patron(s)')

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

        FloorPlan.objects.create(
            name=name,
            canvas_width=canvas_width,
            canvas_height=canvas_height,
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
def shelf_manager(request):
    return _shelf_page(request, 'admin/shelfmanager.html')


@staff_only_required
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

    try:
        room = Room.objects.get(room_id=room_id)
        shelf = Shelf.objects.create(
            room=room,
            name=name,
            map_x=x,
            map_y=y,
            description=description
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

    beacons = [
        {
            'beacon_id': b.beacon_id,
            'beacon_uuid': b.beacon_uuid,
            'map_x': b.map_x,
            'map_y': b.map_y,
            'label': b.label or '',
        }
        for b in BLEBeacon.objects.filter(floor_plan=floor_plan)
    ]

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
        'beacons': beacons,
        'waypoints': waypoints,
        'connections': connections,
        'rooms': rooms,
        'shelves': shelves,
    })


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

    beacon = BLEBeacon.objects.create(
        floor_plan=floor_plan,
        beacon_uuid=beacon_uuid,
        map_x=map_x,
        map_y=map_y,
        label=label or None,
    )
    return JsonResponse({
        'success': True,
        'beacon': {
            'beacon_id': beacon.beacon_id,
            'beacon_uuid': beacon.beacon_uuid,
            'map_x': beacon.map_x,
            'map_y': beacon.map_y,
            'label': beacon.label or '',
        },
    })


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
        {'beacon_id': b.beacon_id, 'beacon_uuid': b.beacon_uuid, 'x': b.map_x, 'y': b.map_y, 'label': b.label or ''}
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

    firstname = (request.POST.get('firstname') or '').strip()
    lastname = (request.POST.get('lastname') or '').strip()
    email = (request.POST.get('email') or '').strip()
    contact_number = (request.POST.get('contact_number') or '').strip()
    address = (request.POST.get('address') or '').strip()
    patron_type = (request.POST.get('patron_type') or '').strip()
    school = (request.POST.get('school') or '').strip()
    purpose = (request.POST.get('purpose_of_visit') or '').strip()

    valid_types = [choice[0] for choice in Patron.PATRON_TYPE_CHOICES]
    if not all([firstname, lastname, email, contact_number, address, patron_type]):
        return JsonResponse({'success': False, 'error': 'All fields are required.'})
    if patron_type not in valid_types:
        return JsonResponse({'success': False, 'error': 'Please choose a valid patron type.'})

    from django.core.validators import validate_email as _validate_email
    from django.core.exceptions import ValidationError as _ValidationError
    try:
        _validate_email(email)
    except _ValidationError:
        return JsonResponse({'success': False, 'error': 'Please enter a valid email address.'})

    if Patron.objects.filter(email__iexact=email).exists():
        return JsonResponse({'success': False, 'error': 'A patron with this email already exists.'})

    patron = Patron.objects.create(
        fullname=f'{firstname} {lastname}',
        email=email,
        contact_number=contact_number,
        address=address,
        patron_type=patron_type,
        account_status='Active',   # desk staff verified identity on the spot
        password_hash=hash_password(None),  # unusable until set via the portal
        registration_channel='On-site',
        qr_code=str(uuid4()),
        otp_verified=True,
    )
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

    user = User.objects.create(
        fullname=fullname,
        email=email,
        password_hash=hash_password(password),
        role=role,
        account_status=status,
    )
    log_admin_action(request, 'Create', 'User', user.admin_id, f'Created {role} account {email}')
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

    user.fullname = fullname
    user.email = email
    user.role = role
    user.account_status = status
    user.save()
    if is_self:
        request.session['admin_role'] = user.role
        request.session['admin_fullname'] = user.fullname
    log_admin_action(request, 'Update', 'User', user.admin_id, f'Updated account {email} (role={role}, status={status})')
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

