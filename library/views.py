from django.shortcuts import redirect, render
from django.db.models import Count, Q
from django.utils import timezone
from django.http import HttpResponse, JsonResponse
from django.conf import settings
from datetime import timedelta

from uuid import uuid4
import openpyxl
from openpyxl import Workbook
import qrcode
import os

from .auth_utils import (
    check_password,
    hash_password,
    patron_login_required,
    admin_login_required,
)
from .models import Book, Patron, PatronLog, Transaction, User, Section, ShelfLevel, Donation, Announcement, FloorPlan, Shelf

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
    sections = Section.objects.all()
    shelf_levels = ShelfLevel.objects.all()

    context = {
        'books': books,
        'total_books': total_books,
        'total_copies': total_copies,
        'available_count': available_count,
        'borrowed_count': borrowed_count,
        'sections': sections,
        'shelf_levels': shelf_levels,
    }
    return render(request, 'admin/managebooks.html', context)


@admin_login_required
def admin_add_book(request):
    error = None

    if request.method == 'POST':
        title = request.POST.get('title', '').strip()
        author = request.POST.get('author', '').strip()
        isbn = request.POST.get('ISBN', '').strip()
        genre = request.POST.get('genre', '').strip()
        status = request.POST.get('status', 'Available').strip()
        section_id = request.POST.get('section', '').strip()
        shelf_level_id = request.POST.get('shelf_level', '').strip()
        publication_year = request.POST.get('publication_year', '').strip()
        cover_img_url = request.POST.get('cover_img_url', '').strip()

        if not title or not author:
            error = 'Title and author are required.'
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                from django.http import JsonResponse
                return JsonResponse({'success': False, 'error': error})
        else:
            section = None
            shelf_level = None

            if section_id:
                section = Section.objects.filter(section_id=section_id).first()
            if shelf_level_id:
                shelf_level = ShelfLevel.objects.filter(shelf_level_id=shelf_level_id).first()

            qr_code = str(uuid4())

            Book.objects.create(
                title=title,
                author=author,
                ISBN=isbn,
                genre=genre,
                status=status or 'Available',
                section=section,
                shelf_level=shelf_level,
                publication_year=int(publication_year) if publication_year else None,
                cover_img_url=cover_img_url if cover_img_url else None,
                qr_code=qr_code
            )

            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                from django.http import JsonResponse
                return JsonResponse({'success': True, 'message': 'Book added successfully.'})
            return redirect('admin_management')

    # Get books list context for managebooks.html
    books = Book.objects.select_related('section', 'shelf_level').order_by('title')
    total_books = books.count()
    total_copies = total_books
    available_count = books.filter(status='Available').count()
    borrowed_count = books.filter(status='Borrowed').count()
    sections = Section.objects.all()
    shelf_levels = ShelfLevel.objects.all()

    context = {
        'books': books,
        'total_books': total_books,
        'total_copies': total_copies,
        'available_count': available_count,
        'borrowed_count': borrowed_count,
        'sections': sections,
        'shelf_levels': shelf_levels,
        'error': error,
    }
    return render(request, 'admin/managebooks.html', context)


@admin_login_required
def admin_add_patron(request):
    error = None
    initial = {}

    if request.method == 'POST':
        fullname = request.POST.get('fullname', '').strip()
        patron_type = request.POST.get('patron_type', '').strip()
        email = request.POST.get('email', '').strip()
        contact_number = request.POST.get('contact_number', '').strip()
        address = request.POST.get('address', '').strip()
        qr_code = request.POST.get('qr_code', '').strip()
        password = request.POST.get('password', '').strip()
        account_status = request.POST.get('account_status', 'Active').strip()

        initial = {
            'fullname': fullname,
            'patron_type': patron_type,
            'email': email,
            'contact_number': contact_number,
            'address': address,
            'qr_code': qr_code,
            'account_status': account_status,
        }

        if not all([fullname, patron_type, email, password]):
            error = 'Full name, patron type, email, and password are required.'
        elif Patron.objects.filter(email=email).exists():
            error = 'A patron with that email already exists.'
        else:
            hashed_password = hash_password(password)
            if not qr_code:
                qr_code = str(uuid4())
            Patron.objects.create(
                fullname=fullname,
                email=email,
                password_hash=hashed_password,
                patron_type=patron_type,
                contact_number=contact_number,
                address=address,
                qr_code=qr_code,
                account_status=account_status or 'Active',
            )
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


@admin_login_required
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
    initial = {
        'fullname': patron.fullname,
        'patron_type': patron.patron_type,
        'email': patron.email,
        'contact_number': patron.contact_number,
        'address': patron.address,
        'qr_code': patron.qr_code,
        'account_status': patron.account_status,
    }

    if request.method == 'POST':
        fullname = request.POST.get('fullname', '').strip()
        patron_type = request.POST.get('patron_type', '').strip()
        email = request.POST.get('email', '').strip()
        contact_number = request.POST.get('contact_number', '').strip()
        address = request.POST.get('address', '').strip()
        qr_code = request.POST.get('qr_code', '').strip()
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
            patron.qr_code = qr_code if qr_code else patron.qr_code
            patron.account_status = account_status or 'Active'
            if password:
                patron.password_hash = hash_password(password)
            patron.save()
            success = 'Patron updated successfully.'
            initial.update({
                'fullname': fullname,
                'patron_type': patron_type,
                'email': email,
                'contact_number': contact_number,
                'address': address,
                'qr_code': qr_code,
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
        'ISBN': book.ISBN,
        'publication_year': book.publication_year,
        'genre': book.genre,
        'cover_img_url': book.cover_img_url,
        'status': book.status,
        'section_id': book.section.section_id if book.section else '',
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
        section_id = request.POST.get('section', '').strip()
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
            if section_id:
                book.section = Section.objects.filter(section_id=section_id).first()
            if shelf_level_id:
                book.shelf_level = ShelfLevel.objects.filter(shelf_level_id=shelf_level_id).first()
            book.save()
            success = 'Book details updated successfully.'
            initial.update({
                'title': title,
                'author': author,
                'ISBN': isbn,
                'publication_year': publication_year,
                'genre': genre,
                'cover_img_url': cover_img_url,
                'status': status,
                'section_id': section_id,
                'shelf_level_id': shelf_level_id,
            })
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                from django.http import JsonResponse
                return JsonResponse({'success': True, 'message': success})

    # Get books list context for managebooks.html
    books = Book.objects.select_related('section', 'shelf_level').order_by('title')
    total_books = books.count()
    total_copies = total_books
    available_count = books.filter(status='Available').count()
    borrowed_count = books.filter(status='Borrowed').count()
    sections = Section.objects.all()
    shelf_levels = ShelfLevel.objects.all()

    context = {
        'books': books,
        'total_books': total_books,
        'total_copies': total_copies,
        'available_count': available_count,
        'borrowed_count': borrowed_count,
        'sections': sections,
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


@admin_login_required
def admin_book_details_ajax(request, book_id):
    from django.http import JsonResponse
    book = Book.objects.filter(book_id=book_id).select_related('section', 'shelf_level').first()
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
        'section': book.section.name if book.section else 'N/A',
        'shelf_level': book.shelf_level.label if book.shelf_level else 'N/A',
        'copies': Book.objects.filter(title=book.title, author=book.author).count(),
    }
    return JsonResponse(book_data)


@admin_login_required
def search_book_by_qr(request):
    qr_code = request.GET.get('qr_code', '').strip()
    if not qr_code:
        return JsonResponse({'success': False, 'error': 'QR code is required'})
    
    book = Book.objects.filter(qr_code=qr_code).select_related('section', 'shelf_level').first()
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
            'section': book.section.name if book.section else 'N/A',
            'shelf_level': book.shelf_level.label if book.shelf_level else 'N/A',
        }
    }
    return JsonResponse(book_data)


@admin_login_required
def download_book_template(request):
    wb = Workbook()
    ws = wb.active
    ws.title = "Book Import Template"
    
    headers = ['title', 'author', 'publication_year', 'ISBN', 'genre', 'section_id', 'shelf_level_id']
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
            section_id = row[5].value
            shelf_level_id = row[6].value
            
            if not title or not author:
                continue
            
            if isbn and Book.objects.filter(ISBN=isbn).exists():
                skipped_count += 1
                continue
            
            section = None
            if section_id and str(section_id).strip() not in ['', 'N/A', 'n/a']:
                section = Section.objects.filter(section_id=section_id).first()
            
            shelf_level = None
            if shelf_level_id and str(shelf_level_id).strip() not in ['', 'N/A', 'n/a']:
                shelf_level = ShelfLevel.objects.filter(shelf_level_id=shelf_level_id).first()
            
            book = Book.objects.create(
                title=title,
                author=author,
                publication_year=int(publication_year) if publication_year else None,
                ISBN=isbn,
                genre=genre,
                section=section,
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


@admin_login_required
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


@admin_login_required
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
                registration_date=timezone.now()
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


@admin_login_required
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


@admin_login_required
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
    
    # Validate books and process transaction
    errors = []
    processed_books = []
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
            due_date = today + timedelta(days=14)
            book.status = 'Borrowed'
            book.save()
            Transaction.objects.create(
                patron=patron,
                book=book,
                processed_by=admin,
                transaction_type='Borrow',
                due_date=due_date
            )
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
                tx.save()
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
    
    return JsonResponse({
        'success': True,
        'message': f'Successfully processed {len(processed_books)} books',
        'processed_books': processed_books
    })


# Donation Management Views
@admin_login_required
def donation_management(request):
    donations = Donation.objects.select_related('book').order_by('-date_donated')
    
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
            return render(request, 'admin/donationadmin.html', {'donations': donations, 'error': error})
        
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
        
        return redirect('donation_management')
    
    return render(request, 'admin/donationadmin.html', {'donations': donations})


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
    
    return redirect('donation_management')


@admin_login_required
def delete_donation(request):
    if request.method == 'POST':
        donation_id = request.POST.get('donation_id')
        donation = Donation.objects.filter(donation_id=donation_id).first()
        if donation:
            # Delete the book as well since it's linked
            donation.book.delete()
    
    return redirect('donation_management')


# Announcement Management Views
@admin_login_required
def announcement_management(request):
    announcements = Announcement.objects.select_related('posted_by').order_by('-created_at')
    
    if request.method == 'POST':
        title = request.POST.get('title', '').strip()
        message = request.POST.get('message', '').strip()
        
        if not all([title, message]):
            error = 'Title and message are required.'
            return render(request, 'admin/announcementadmin.html', {'announcements': announcements, 'error': error})
        
        admin_id = request.session.get('admin_id')
        admin = User.objects.filter(admin_id=admin_id).first()
        
        Announcement.objects.create(
            posted_by=admin,
            title=title,
            message=message,
            is_active=True
        )
        
        return redirect('announcement_management')
    
    return render(request, 'admin/announcementadmin.html', {'announcements': announcements})


@admin_login_required
def toggle_announcement(request):
    if request.method == 'POST':
        announcement_id = request.POST.get('announcement_id')
        announcement = Announcement.objects.filter(announcement_id=announcement_id).first()
        if announcement:
            announcement.is_active = not announcement.is_active
            announcement.save()
    
    return redirect('announcement_management')


@admin_login_required
def delete_announcement(request):
    if request.method == 'POST':
        announcement_id = request.POST.get('announcement_id')
        Announcement.objects.filter(announcement_id=announcement_id).delete()
    
    return redirect('announcement_management')


# Floor Plan Management Views
@admin_login_required
def floorplan_management(request):
    floorplans = FloorPlan.objects.prefetch_related('shelf_set__section_set__shelflevel_set').order_by('-uploaded_at')
    
    if request.method == 'POST':
        image = request.FILES.get('image')
        
        if not image:
            error = 'Image file is required.'
            return render(request, 'admin/floorplanadmin.html', {'floorplans': floorplans, 'error': error})
        
        # Validate file type
        if not image.name.lower().endswith('.png'):
            error = 'Only PNG files are allowed.'
            return render(request, 'admin/floorplanadmin.html', {'floorplans': floorplans, 'error': error})
        
        # Save image to media/floorplans/
        floorplan_dir = os.path.join(settings.MEDIA_ROOT, 'floorplans')
        os.makedirs(floorplan_dir, exist_ok=True)
        
        image_filename = f'floorplan_{timezone.now().strftime("%Y%m%d_%H%M%S")}_{image.name}'
        image_path = os.path.join(floorplan_dir, image_filename)
        
        with open(image_path, 'wb+') as destination:
            for chunk in image.chunks():
                destination.write(chunk)
        
        FloorPlan.objects.create(
            image_url=f'floorplans/{image_filename}',
            is_active=False,
            uploaded_at=timezone.now()
        )
        
        return redirect('floorplan_management')
    
    return render(request, 'admin/floorplanadmin.html', {'floorplans': floorplans})


@admin_login_required
def set_active_floorplan(request):
    if request.method == 'POST':
        floorplan_id = request.POST.get('floorplan_id')
        
        # Set all floorplans to inactive
        FloorPlan.objects.all().update(is_active=False)
        
        # Set selected floorplan to active
        floorplan = FloorPlan.objects.filter(floor_plan_id=floorplan_id).first()
        if floorplan:
            floorplan.is_active = True
            floorplan.save()
    
    return redirect('floorplan_management')


@admin_login_required
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


@admin_login_required
def delete_floorplan(request):
    if request.method == 'POST':
        floorplan_id = request.POST.get('floorplan_id')
        FloorPlan.objects.filter(floor_plan_id=floorplan_id).delete()
    
    return redirect('floorplan_management')


@admin_login_required
def add_shelf(request):
    if request.method == 'POST':
        floorplan_id = request.POST.get('floorplan_id')
        name = request.POST.get('name', '').strip()
        map_x = request.POST.get('map_x', '0').strip()
        map_y = request.POST.get('map_y', '0').strip()
        description = request.POST.get('description', '').strip()
        
        if not all([floorplan_id, name]):
            return redirect('floorplan_management')
        
        floorplan = FloorPlan.objects.filter(floor_plan_id=floorplan_id).first()
        if floorplan:
            Shelf.objects.create(
                floor_plan=floorplan,
                name=name,
                map_x=float(map_x) if map_x else 0.0,
                map_y=float(map_y) if map_y else 0.0,
                description=description if description else None
            )
    
    return redirect('floorplan_management')


@admin_login_required
def delete_shelf(request):
    if request.method == 'POST':
        shelf_id = request.POST.get('shelf_id')
        Shelf.objects.filter(shelf_id=shelf_id).delete()
    
    return redirect('floorplan_management')


@admin_login_required
def add_section(request):
    if request.method == 'POST':
        shelf_id = request.POST.get('shelf_id')
        name = request.POST.get('name', '').strip()
        description = request.POST.get('description', '').strip()
        
        if not all([shelf_id, name]):
            return redirect('floorplan_management')
        
        shelf = Shelf.objects.filter(shelf_id=shelf_id).first()
        if shelf:
            from .models import Section
            Section.objects.create(
                shelf=shelf,
                name=name,
                description=description if description else None
            )
    
    return redirect('floorplan_management')


@admin_login_required
def delete_section(request):
    if request.method == 'POST':
        section_id = request.POST.get('section_id')
        from .models import Section
        Section.objects.filter(section_id=section_id).delete()
    
    return redirect('floorplan_management')


@admin_login_required
def add_shelf_level(request):
    if request.method == 'POST':
        section_id = request.POST.get('section_id')
        level_number = request.POST.get('level_number', '1').strip()
        label = request.POST.get('label', '').strip()
        
        if not all([section_id, level_number]):
            return redirect('floorplan_management')
        
        from .models import Section, ShelfLevel
        section = Section.objects.filter(section_id=section_id).first()
        if section:
            ShelfLevel.objects.create(
                section=section,
                level_number=int(level_number),
                label=label if label else None
            )
    
    return redirect('floorplan_management')


@admin_login_required
def delete_shelf_level(request):
    if request.method == 'POST':
        shelf_level_id = request.POST.get('shelf_level_id')
        ShelfLevel.objects.filter(shelf_level_id=shelf_level_id).delete()
    
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


@admin_login_required
def export_transactions(request):
    transactions = Transaction.objects.select_related('patron', 'book', 'processed_by').order_by('-transaction_date')
    
    wb = Workbook()
    ws = wb.active
    ws.title = "Transactions Export"
    
    headers = ['transaction_id', 'patron_id', 'patron_name', 'book_id', 'book_title', 'transaction_type', 'transaction_date', 'due_date', 'return_date', 'overdue_flag']
    ws.append(headers)
    
    for tx in transactions:
        ws.append([
            tx.transaction_id,
            tx.patron.patron_id if tx.patron else '',
            tx.patron.fullname if tx.patron else 'In-Library User',
            tx.book.book_id,
            tx.book.title,
            tx.transaction_type,
            tx.transaction_date.strftime('%Y-%m-%d') if tx.transaction_date else '',
            tx.due_date.strftime('%Y-%m-%d') if tx.due_date else '',
            tx.return_date.strftime('%Y-%m-%d') if tx.return_date else '',
            tx.overdue_flag
        ])
    
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename=transactions_export.xlsx'
    wb.save(response)
    return response


# Log Import/Export
@admin_login_required
def download_log_template(request):
    wb = Workbook()
    ws = wb.active
    ws.title = "Log Import Template"
    
    headers = ['patron_id', 'log_type', 'timestamp']
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
        
        for row in ws.iter_rows(min_row=2):
            patron_id = row[0].value
            log_type = row[1].value
            timestamp = row[2].value
            
            if not patron_id or not log_type:
                continue
            
            patron = Patron.objects.filter(patron_id=patron_id).first()
            if not patron:
                skipped_count += 1
                continue
            
            # Parse timestamp
            from datetime import datetime
            ts = None
            if timestamp:
                if isinstance(timestamp, datetime):
                    ts = timestamp
                else:
                    ts = datetime.strptime(str(timestamp), '%Y-%m-%d %H:%M:%S')
            else:
                ts = timezone.now()
            
            PatronLog.objects.create(
                patron=patron,
                log_type=log_type,
                timestamp=ts
            )
            
            imported_count += 1
        
        return JsonResponse({
            'success': True,
            'message': f'Successfully imported {imported_count} logs. Skipped {skipped_count} invalid entries.'
        })
        
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})


@admin_login_required
def export_logs(request):
    logs = PatronLog.objects.select_related('patron').order_by('-timestamp')
    
    wb = Workbook()
    ws = wb.active
    ws.title = "Logs Export"
    
    headers = ['log_id', 'patron_id', 'patron_name', 'log_type', 'timestamp']
    ws.append(headers)
    
    for log in logs:
        ws.append([
            log.log_id,
            log.patron.patron_id,
            log.patron.fullname,
            log.log_type,
            log.timestamp.strftime('%Y-%m-%d %H:%M:%S') if log.timestamp else ''
        ])
    
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename=logs_export.xlsx'
    wb.save(response)
    return response

