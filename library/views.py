from django.shortcuts import redirect, render
from django.db.models import Count, Q
from django.utils import timezone

from uuid import uuid4

from .auth_utils import (
    check_password,
    hash_password,
    patron_login_required,
    admin_login_required,
)
from .models import Book, Patron, PatronLog, Transaction, User

# Patron views
def patron_login(request):
    if request.method == 'POST':
        email = request.POST.get('email')
        password = request.POST.get('password')

        try:
            patron = Patron.objects.get(email=email)
        except Patron.DoesNotExist:
            return render(request, 'patron/patronlogin.html', {'error': 'Invalid email or password'})

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
    patron = Patron.objects.filter(patron_id=patron_id).first()
    return render(request, 'patron/patrondashboard.html', {'patron': patron})


def patron_register(request):
    if request.method == 'POST':
        fullname = request.POST.get('fullname')
        email = request.POST.get('email')
        password = request.POST.get('password')
        confirm_password = request.POST.get('confirm_password')
        patron_type = request.POST.get('patron_type')
        contact_number = request.POST.get('contact_number')
        address = request.POST.get('address')

        # Validate all required fields are filled
        if not all([fullname, email, password, confirm_password, patron_type, contact_number, address]):
            return render(request, 'patron/patronregister.html',
                         {'error': 'All fields are required'})

        # Validate password match
        if password != confirm_password:
            return render(request, 'patron/patronregister.html',
                         {'error': 'Passwords do not match'})

        # Validate email doesn't exist
        if Patron.objects.filter(email=email).exists():
            return render(request, 'patron/patronregister.html',
                         {'error': 'Email already exists'})

        # Hash password and create patron
        hashed_password = hash_password(password)
        qr_code = str(uuid4())

        patron = Patron.objects.create(
            fullname=fullname,
            email=email,
            password_hash=hashed_password,
            patron_type=patron_type,
            contact_number=contact_number,
            address=address,
            qr_code=qr_code,
            account_status='Active'
        )

        # Save to session and redirect
        request.session['patron_id'] = patron.patron_id
        request.session['patron_fullname'] = patron.fullname
        return redirect('/patron/dashboard/')

    return render(request, 'patron/patronregister.html')



def patron_catalog(request):
    return render(request, 'patron/patroncatalog.html')


def patron_book_details(request, book_id):
    context = {'book_id': book_id}
    return render(request, 'patron/patronbook-details.html', context)


def patron_map(request):
    return render(request, 'patron/patronmap.html')


def patron_announcements(request):
    return render(request, 'patron/patronannouncements.html')


def patron_account(request):
    return render(request, 'patron/patronaccount.html')

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

        request.session['admin_id'] = admin.admin_id
        request.session['admin_fullname'] = admin.fullname
        return redirect('/admin-portal/dashboard/')

    return render(request, 'admin/signin.html')


def admin_logout(request):
    request.session.flush()
    return redirect('/admin-portal/login/')


@admin_login_required
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

    # PatronLog entry count for today
    visitors_today = PatronLog.objects.filter(timestamp__date=today, log_type='Entry').count()

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


def admin_signin(request):
    return render(request, 'admin/signin.html')


@admin_login_required
def admin_management(request):
    books = Book.objects.select_related('section', 'shelf_level').order_by('title')
    total_books = books.count()
    total_copies = total_books
    available_count = books.filter(status='Available').count()
    borrowed_count = books.filter(status='Borrowed').count()

    context = {
        'books': books,
        'total_books': total_books,
        'total_copies': total_copies,
        'available_count': available_count,
        'borrowed_count': borrowed_count,
    }
    return render(request, 'admin/managebooks.html', context)


@admin_login_required
def admin_add_patron(request):
    error = None
    initial = {}

    if request.method == 'POST':
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        patron_type = request.POST.get('patron_type', '').strip()
        email = request.POST.get('email', '').strip()
        contact_number = request.POST.get('contact', '').strip()
        course_department = request.POST.get('course_department', '').strip()
        status = request.POST.get('status', 'Active').strip()

        initial = {
            'first_name': first_name,
            'last_name': last_name,
            'patron_type': patron_type,
            'email': email,
            'contact': contact_number,
            'course_department': course_department,
            'status': status,
        }

        if not all([first_name, last_name, patron_type, email]):
            error = 'First name, last name, patron type, and email are required.'
        elif Patron.objects.filter(email=email).exists():
            error = 'A patron with that email already exists.'
        else:
            fullname = f"{first_name} {last_name}"
            hashed_password = hash_password('AylaDefault123!')
            Patron.objects.create(
                fullname=fullname,
                email=email,
                password_hash=hashed_password,
                patron_type=patron_type,
                contact_number=contact_number,
                address=course_department,
                qr_code=str(uuid4()),
                account_status=status or 'Active',
            )
            return redirect('admin_manage_patron')

    return render(request, 'admin/addpatron.html', {'error': error, 'initial': initial})


@admin_login_required
def admin_manage_patron(request):
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
    }
    return render(request, 'admin/managepatron.html', context)


@admin_login_required
def admin_edit_patron(request, patron_id):
    patron = Patron.objects.filter(patron_id=patron_id).first()
    if patron is None:
        return redirect('admin_manage_patron')

    error = None
    success = None
    first_name, last_name = patron.fullname.split(' ', 1) if ' ' in patron.fullname else (patron.fullname, '')
    initial = {
        'first_name': first_name,
        'last_name': last_name,
        'patron_type': patron.patron_type,
        'email': patron.email,
        'contact': patron.contact_number,
        'course_department': patron.address,
        'status': patron.account_status,
    }

    if request.method == 'POST':
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        patron_type = request.POST.get('patron_type', '').strip()
        email = request.POST.get('email', '').strip()
        contact_number = request.POST.get('contact', '').strip()
        course_department = request.POST.get('course_department', '').strip()
        status = request.POST.get('status', 'Active').strip()

        if not all([first_name, last_name, patron_type, email]):
            error = 'First name, last name, patron type, and email are required.'
        elif Patron.objects.exclude(patron_id=patron_id).filter(email=email).exists():
            error = 'A different patron already uses that email.'
        else:
            patron.fullname = f"{first_name} {last_name}"
            patron.patron_type = patron_type
            patron.email = email
            patron.contact_number = contact_number
            patron.address = course_department
            patron.account_status = status or 'Active'
            patron.save()
            success = 'Patron updated successfully.'
            initial.update({
                'first_name': first_name,
                'last_name': last_name,
                'patron_type': patron_type,
                'email': email,
                'contact': contact_number,
                'course_department': course_department,
                'status': status,
            })

    context = {
        'patron': patron,
        'error': error,
        'success': success,
        'initial': initial,
    }
    return render(request, 'admin/editpatron.html', context)


@admin_login_required
def admin_delete_patron(request, patron_id):
    if request.method == 'POST':
        Patron.objects.filter(patron_id=patron_id).delete()
    return redirect('admin_manage_patron')


@admin_login_required
def admin_edit_book(request, book_id):
    book = Book.objects.filter(book_id=book_id).first()
    if book is None:
        return redirect('admin_management')

    error = None
    success = None
    initial = {
        'title': book.title,
        'author': book.author,
        'isbn': book.ISBN,
        'genre': book.genre,
        'status': book.status,
        'section': book.section.name if book.section else '',
        'shelf_level': book.shelf_level.name if book.shelf_level else '',
    }

    if request.method == 'POST':
        title = request.POST.get('title', '').strip()
        author = request.POST.get('author', '').strip()
        isbn = request.POST.get('isbn', '').strip()
        genre = request.POST.get('genre', '').strip()
        status = request.POST.get('status', '').strip()

        if not title or not author:
            error = 'Title and author are required.'
        else:
            book.title = title
            book.author = author
            book.ISBN = isbn
            book.genre = genre
            book.status = status or book.status
            book.save()
            success = 'Book details updated successfully.'
            initial.update({
                'title': title,
                'author': author,
                'isbn': isbn,
                'genre': genre,
                'status': status,
            })

    context = {
        'book': book,
        'error': error,
        'success': success,
        'initial': initial,
    }
    return render(request, 'admin/editbook.html', context)


@admin_login_required
def admin_delete_book(request, book_id):
    if request.method == 'POST':
        Book.objects.filter(book_id=book_id).delete()
    return redirect('admin_management')


@admin_login_required
def admin_transaction_action(request, transaction_id):
    if request.method == 'POST':
        action = request.POST.get('action')
        tx = Transaction.objects.select_related('book').filter(transaction_id=transaction_id).first()
        if tx and action == 'return' and tx.return_date is None:
            tx.return_date = timezone.localdate()
            tx.overdue_flag = bool(tx.due_date and tx.return_date > tx.due_date)
            tx.save()
            if tx.book and tx.transaction_type == 'Borrow':
                tx.book.status = 'Available'
                tx.book.save()
    return redirect('admin_transaction')


@admin_login_required
def admin_book_detail(request):
    books = Book.objects.select_related('section', 'shelf_level').order_by('title')
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
    return render(request, 'admin/bookdetail.html', context)


@admin_login_required
def admin_qr_scanner(request):
    return render(request, 'admin/qrscanner.html')


@admin_login_required
def admin_transaction(request):
    transactions = Transaction.objects.select_related('patron', 'book').order_by('-transaction_date')[:100]

    total_borrowed = Transaction.objects.filter(transaction_type='Borrow').count()
    total_returned = Transaction.objects.filter(transaction_type='Return').count()
    currently_out = Transaction.objects.filter(transaction_type='Borrow', return_date__isnull=True).count()
    overdue_count = Transaction.objects.filter(overdue_flag=True).count()

    transaction_count = Transaction.objects.count()
    context = {
        'transactions': transactions,
        'total_borrowed': total_borrowed,
        'total_returned': total_returned,
        'currently_out': currently_out,
        'overdue_count': overdue_count,
        'transaction_count': transaction_count,
    }
    return render(request, 'admin/transaction.html', context)


@admin_login_required
def admin_indoor_map(request):
    return render(request, 'admin/indoormap.html')


@admin_login_required
def admin_log_management(request):
    today = timezone.localdate()
    logs_qs = PatronLog.objects.select_related('patron').filter(timestamp__date=today).order_by('-timestamp')
    logs = logs_qs[:50]
    todays_entries = logs_qs.filter(log_type='Entry').count()
    todays_exits = logs_qs.filter(log_type='Exit').count()
    currently_inside = max(todays_entries - todays_exits, 0)
    log_count = logs_qs.count()

    context = {
        'logs': logs,
        'todays_entries': todays_entries,
        'todays_exits': todays_exits,
        'currently_inside': currently_inside,
        'log_count': log_count,
        'today': today,
    }
    return render(request, 'admin/logmanagement.html', context)

