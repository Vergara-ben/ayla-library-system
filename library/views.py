from django.shortcuts import redirect, render, get_object_or_404
from django.db.models import Count, Q
from django.utils import timezone
from django.http import HttpResponse, JsonResponse
from django.conf import settings
from django.core.paginator import Paginator
from datetime import timedelta
from urllib.parse import quote
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
)
from .models import Book, Patron, PatronLog, Transaction, User, Section, ShelfLevel, Donation, Announcement, FloorPlan, Shelf, Room, Waypoint, BLEBeacon, WaypointConnection

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

        patron = Patron.objects.create(
            fullname=fullname,
            email=email,
            password_hash=hashed_password,
            patron_type=patron_type,
            contact_number=contact_number,
            address=address,
            account_status='Active'
        )

        # Save to session and redirect
        request.session['patron_id'] = patron.patron_id
        request.session['patron_fullname'] = patron.fullname
        return redirect('/patron/dashboard/')

    return render(request, 'patron/patronregister.html')



@patron_login_required
def patron_catalog(request):
    search_query = request.GET.get('search', '').strip()

    books = Book.objects.filter(status='Available').select_related(
        'section', 'shelf_level'
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
            'section',
            'section__shelf',
            'section__shelf__room',
            'section__shelf__room__floor_plan',
            'shelf_level',
        ),
        book_id=book_id,
    )

    # Walk the location hierarchy: Section -> Shelf -> Room -> FloorPlan
    section = book.section
    shelf = section.shelf if section else None
    room = shelf.room if shelf else None
    floor_plan = room.floor_plan if room else None

    context = {
        'book': book,
        'section': section,
        'shelf_level': book.shelf_level,
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
        book = Book.objects.select_related('section__shelf').filter(book_id=book_id).first()
        if book:
            target['book_id'] = book.book_id
            target['book_title'] = book.title
            shelf = book.section.shelf if book.section else None
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


def admin_signin(request):
    return render(request, 'admin/signin.html')


@admin_login_required
def admin_management(request):
    books_queryset = Book.objects.select_related('section', 'shelf_level').order_by('title')
    total_books = books_queryset.count()
    total_copies = total_books
    available_count = books_queryset.filter(status='Available').count()
    borrowed_count = books_queryset.filter(status='Borrowed').count()
    sections = Section.objects.all()
    shelf_levels = ShelfLevel.objects.all()

    # Pagination
    page_number = request.GET.get('page', 1)
    paginator = Paginator(books_queryset, 15)  # 15 books per page
    books = paginator.get_page(page_number)

    # Pending transactions count for badge
    pending_transactions_count = Transaction.objects.filter(transaction_type='Borrow', return_date__isnull=True).count()

    context = {
        'books': books,
        'total_books': total_books,
        'total_copies': total_copies,
        'available_count': available_count,
        'borrowed_count': borrowed_count,
        'sections': sections,
        'shelf_levels': shelf_levels,
        'pending_transactions_count': pending_transactions_count,
        'paginator': paginator,
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
            Patron.objects.create(
                fullname=fullname,
                email=email,
                password_hash=hashed_password,
                patron_type=patron_type,
                contact_number=contact_number,
                address=address,
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

    total_patrons = patrons_queryset.count()
    active_patrons = Patron.objects.filter(account_status='Active').count()
    
    # Pagination
    page_number = request.GET.get('page', 1)
    paginator = Paginator(patrons_queryset, 15)  # 15 patrons per page
    patrons = paginator.get_page(page_number)
    
    patrons_with_borrows = patrons_queryset.filter(active_borrows__gt=0).count()
    patrons_overdue = patrons_queryset.filter(overdue_count__gt=0).count()

    # Pending transactions count for badge
    pending_transactions_count = Transaction.objects.filter(transaction_type='Borrow', return_date__isnull=True).count()

    context = {
        'patrons': patrons,
        'total_patrons': total_patrons,
        'active_patrons': active_patrons,
        'patrons_with_borrows': patrons_with_borrows,
        'patrons_overdue': patrons_overdue,
        'pending_transactions_count': pending_transactions_count,
        'paginator': paginator,
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
def admin_transaction(request):
    transactions_queryset = Transaction.objects.select_related('patron', 'book').order_by('-transaction_date')

    total_borrowed = Transaction.objects.filter(transaction_type='Borrow').count()
    total_returned = Transaction.objects.filter(transaction_type='Return').count()
    currently_out = Transaction.objects.filter(transaction_type='Borrow', return_date__isnull=True).count()
    overdue_count = Transaction.objects.filter(overdue_flag=True).count()

    transaction_count = Transaction.objects.count()
    
    # Pagination
    page_number = request.GET.get('page', 1)
    paginator = Paginator(transactions_queryset, 20)  # 20 transactions per page
    transactions = paginator.get_page(page_number)
    
    # Pending transactions count for badge
    pending_transactions_count = Transaction.objects.filter(transaction_type='Borrow', return_date__isnull=True).count()

    context = {
        'transactions': transactions,
        'total_borrowed': total_borrowed,
        'total_returned': total_returned,
        'currently_out': currently_out,
        'overdue_count': overdue_count,
        'transaction_count': transaction_count,
        'pending_transactions_count': pending_transactions_count,
        'paginator': paginator,
    }
    return render(request, 'admin/transaction.html', context)


@admin_login_required
def admin_indoor_map(request):
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
        # Construct full image URL with MEDIA_URL prefix
        image_url = floorplan.image_url
        if image_url:
            # Remove leading slash if present to avoid double slashes
            if image_url.startswith('/'):
                image_url = image_url[1:]
            # URL-encode the filename to handle spaces and special characters
            image_url = quote(image_url)
            # Always prepend MEDIA_URL to ensure absolute path
            image_url = f'{settings.MEDIA_URL}{image_url}'
        else:
            image_url = ''
        
        floorplan_data = {
            'id': floorplan.floor_plan_id,
            'image_url': image_url,
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
            'image_url': fp.image_url,
            'is_active': fp.is_active,
            'uploaded_at': fp.uploaded_at.isoformat() if fp.uploaded_at else None
        })
    
    # Pending transactions count for badge
    pending_transactions_count = Transaction.objects.filter(transaction_type='Borrow', return_date__isnull=True).count()
    
    context = {
        'floorplan': json.dumps(floorplan_data) if floorplan_data else 'null',
        'floorplans': json.dumps(floorplans_list),
        'rooms': json.dumps(rooms_data),
        'shelves': json.dumps(shelves_data),
        'waypoints': json.dumps(waypoints_data),
        'no_floorplans': no_floorplans,
        'pending_transactions_count': pending_transactions_count,
    }
    
    return render(request, 'admin/indoormap.html', context)


@admin_login_required
def admin_log_management(request):
    today = timezone.localdate()
    logs_qs = PatronLog.objects.select_related('patron').filter(entry_time__date=today).order_by('-entry_time')
    logs = logs_qs[:50]
    todays_entries = logs_qs.count()
    todays_exits = PatronLog.objects.filter(exit_time__date=today).count()
    # Open sessions (entered, no exit yet) = people currently inside.
    currently_inside = PatronLog.objects.filter(exit_time__isnull=True).count()
    log_count = logs_qs.count()

    context = {
        'logs': logs,
        'todays_entries': todays_entries,
        'todays_exits': todays_exits,
        'currently_inside': currently_inside,
        'log_count': log_count,
        'today': today,
        'patron_types': [choice[0] for choice in Patron.PATRON_TYPE_CHOICES],
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
            return render(request, 'admin/donationadmin.html', {'donations': donations_queryset, 'error': error})
        
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
    
    # Pagination
    page_number = request.GET.get('page', 1)
    paginator = Paginator(donations_queryset, 15)  # 15 donations per page
    donations = paginator.get_page(page_number)
    
    # Pending transactions count for badge
    pending_transactions_count = Transaction.objects.filter(transaction_type='Borrow', return_date__isnull=True).count()
    
    return render(request, 'admin/donationadmin.html', {'donations': donations, 'paginator': paginator, 'pending_transactions_count': pending_transactions_count})


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
    announcements_queryset = Announcement.objects.select_related('posted_by').order_by('-created_at')
    
    if request.method == 'POST':
        title = request.POST.get('title', '').strip()
        message = request.POST.get('message', '').strip()
        
        if not all([title, message]):
            error = 'Title and message are required.'
            return render(request, 'admin/announcementadmin.html', {'announcements': announcements_queryset, 'error': error})
        
        admin_id = request.session.get('admin_id')
        admin = User.objects.filter(admin_id=admin_id).first()
        
        Announcement.objects.create(
            posted_by=admin,
            title=title,
            message=message,
            is_active=True
        )
        
        return redirect('announcement_management')
    
    # Pagination
    page_number = request.GET.get('page', 1)
    paginator = Paginator(announcements_queryset, 10)  # 10 announcements per page
    announcements = paginator.get_page(page_number)
    
    # Pending transactions count for badge
    pending_transactions_count = Transaction.objects.filter(transaction_type='Borrow', return_date__isnull=True).count()
    
    return render(request, 'admin/announcementadmin.html', {'announcements': announcements, 'paginator': paginator, 'pending_transactions_count': pending_transactions_count})


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
    floorplans = FloorPlan.objects.prefetch_related('room_set__shelf_set__section_set__shelflevel_set').order_by('-uploaded_at')
    
    if request.method == 'POST':
        image = request.FILES.get('image')
        
        if not image:
            error = 'Image file is required.'
            return render(request, 'admin/floorplanadmin.html', {'floorplans': floorplans, 'error': error})
        
        # Validate file type
        allowed_extensions = ['.png', '.jpg', '.jpeg']
        if not any(image.name.lower().endswith(ext) for ext in allowed_extensions):
            error = 'Only PNG, JPG, and JPEG files are allowed.'
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


@admin_login_required
def export_logs(request):
    logs = PatronLog.objects.select_related('patron').order_by('-entry_time')

    wb = Workbook()
    ws = wb.active
    ws.title = "Logs Export"

    headers = ['log_id', 'patron_id', 'patron_name', 'school', 'purpose_of_visit', 'entry_time', 'exit_time']
    ws.append(headers)

    for log in logs:
        ws.append([
            log.log_id,
            log.patron.patron_id,
            log.patron.fullname,
            log.school or '',
            log.purpose_of_visit or '',
            timezone.localtime(log.entry_time).strftime('%Y-%m-%d %H:%M:%S') if log.entry_time else '',
            timezone.localtime(log.exit_time).strftime('%Y-%m-%d %H:%M:%S') if log.exit_time else '',
        ])
    
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename=logs_export.xlsx'
    wb.save(response)
    return response


# ─── SHELF MANAGEMENT VIEWS ──────────────────────────────────────
@admin_login_required
def shelf_manager(request):
    """IDE-style hierarchical manager for shelves, sections, levels and books."""
    pending_transactions_count = Transaction.objects.filter(
        transaction_type='Borrow', return_date__isnull=True
    ).count()
    return render(request, 'admin/shelfmanager.html', {
        'pending_transactions_count': pending_transactions_count,
    })


@admin_login_required
def get_shelf_tree(request):
    """Returns full nested hierarchy FloorPlan > Room > Shelf > Section > ShelfLevel > Books"""
    floor_plans = FloorPlan.objects.filter(is_active=True).prefetch_related(
        'room_set__shelf_set__section_set__shelflevel_set__book_set'
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
                
                for section in shelf.section_set.all():
                    section_node = {
                        'type': 'section',
                        'id': section.section_id,
                        'name': section.name,
                        'description': section.description,
                        'is_active': section.is_active,
                        'children': []
                    }
                    
                    for shelf_level in section.shelflevel_set.all():
                        books = shelf_level.book_set.all()
                        book_count = books.count()
                        shelf_level_node = {
                            'type': 'shelflevel',
                            'id': shelf_level.shelf_level_id,
                            'name': f'Level {shelf_level.level_number}',
                            'label': shelf_level.label or '',
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
                        
                        section_node['children'].append(shelf_level_node)
                    
                    shelf_node['children'].append(section_node)
                
                room_node['children'].append(shelf_node)
            
            fp_node['children'].append(room_node)
        
        tree_data.append(fp_node)
    
    return JsonResponse({'tree': tree_data})


@admin_login_required
def get_shelf_levels_flat(request):
    """Returns flattened list of all ShelfLevels with breadcrumb path"""
    shelf_levels = ShelfLevel.objects.select_related(
        'section__shelf__room__floor_plan'
    ).all()
    
    flat_data = []
    for sl in shelf_levels:
        path_parts = []
        if sl.section.shelf.room.floor_plan:
            path_parts.append(f'Floor Plan {sl.section.shelf.room.floor_plan.floor_plan_id}')
        if sl.section.shelf.room:
            path_parts.append(sl.section.shelf.room.name)
        if sl.section.shelf:
            path_parts.append(sl.section.shelf.name)
        if sl.section:
            path_parts.append(sl.section.name)
        
        path = ' / '.join(path_parts)
        book_count = sl.book_set.count()
        
        flat_data.append({
            'id': sl.shelf_level_id,
            'path': path,
            'label': sl.label or f'Level {sl.level_number}',
            'level_number': sl.level_number,
            'book_count': book_count,
            'is_active': sl.is_active,
            'section_id': sl.section.section_id,
            'shelf_id': sl.section.shelf.shelf_id,
            'room_id': sl.section.shelf.room.room_id if sl.section.shelf.room else None,
            'floor_plan_id': sl.section.shelf.room.floor_plan.floor_plan_id if sl.section.shelf.room and sl.section.shelf.room.floor_plan else None
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
    
    try:
        floor_plan = FloorPlan.objects.get(floor_plan_id=floor_plan_id)
        room = Room.objects.create(
            floor_plan=floor_plan,
            name=name,
            map_x=float(map_x) if map_x else 0,
            map_y=float(map_y) if map_y else 0,
            description=description
        )
        return JsonResponse({'success': True, 'room_id': room.room_id})
    except FloorPlan.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Floor plan not found'})


@admin_login_required
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
    
    try:
        room = Room.objects.get(room_id=room_id)
        if name:
            room.name = name
        if map_x is not None:
            room.map_x = float(map_x)
        if map_y is not None:
            room.map_y = float(map_y)
        if description is not None:
            room.description = description
        room.save()
        return JsonResponse({'success': True})
    except Room.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Room not found'})


@admin_login_required
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
    
    try:
        room = Room.objects.get(room_id=room_id)
        shelf = Shelf.objects.create(
            room=room,
            name=name,
            map_x=float(map_x) if map_x else 0,
            map_y=float(map_y) if map_y else 0,
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
def add_section(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})
    
    shelf_id = request.POST.get('shelf_id')
    name = request.POST.get('name')
    description = request.POST.get('description', '')
    
    if not shelf_id or not name:
        return JsonResponse({'success': False, 'error': 'shelf_id and name are required'})
    
    try:
        shelf = Shelf.objects.get(shelf_id=shelf_id)
        section = Section.objects.create(
            shelf=shelf,
            name=name,
            description=description
        )
        return JsonResponse({'success': True, 'section_id': section.section_id})
    except Shelf.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Shelf not found'})


@admin_login_required
def edit_section(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})
    
    section_id = request.POST.get('section_id')
    name = request.POST.get('name')
    description = request.POST.get('description')
    
    if not section_id:
        return JsonResponse({'success': False, 'error': 'section_id is required'})
    
    try:
        section = Section.objects.get(section_id=section_id)
        if name:
            section.name = name
        if description is not None:
            section.description = description
        section.save()
        return JsonResponse({'success': True})
    except Section.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Section not found'})


@admin_login_required
def delete_section(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})
    
    section_id = request.POST.get('section_id')
    if not section_id:
        return JsonResponse({'success': False, 'error': 'section_id is required'})
    
    Section.objects.filter(section_id=section_id).delete()
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'success': True})
    return redirect('floorplan_management')


@admin_login_required
def add_shelf_level(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})
    
    section_id = request.POST.get('section_id')
    level_number = request.POST.get('level_number')
    label = request.POST.get('label', '')
    
    if not section_id or not level_number:
        return JsonResponse({'success': False, 'error': 'section_id and level_number are required'})
    
    try:
        section = Section.objects.get(section_id=section_id)
        shelf_level = ShelfLevel.objects.create(
            section=section,
            level_number=int(level_number),
            label=label
        )
        return JsonResponse({'success': True, 'shelf_level_id': shelf_level.shelf_level_id})
    except Section.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Section not found'})


@admin_login_required
def edit_shelf_level(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})
    
    shelf_level_id = request.POST.get('shelf_level_id')
    level_number = request.POST.get('level_number')
    label = request.POST.get('label')
    
    if not shelf_level_id:
        return JsonResponse({'success': False, 'error': 'shelf_level_id is required'})
    
    try:
        shelf_level = ShelfLevel.objects.get(shelf_level_id=shelf_level_id)
        if level_number:
            shelf_level.level_number = int(level_number)
        if label is not None:
            shelf_level.label = label
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
        'section': Section,
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
def _floorplan_media_url(floor_plan):
    """Build the public MEDIA URL for a floor plan image (matches admin_indoor_map)."""
    image_url = floor_plan.image_url or ''
    if not image_url:
        return ''
    if image_url.startswith('/'):
        image_url = image_url[1:]
    return f'{settings.MEDIA_URL}{quote(image_url)}'


def _floorplan_image_size(floor_plan):
    """Return (width, height) in pixels of the floor plan image, or (None, None)."""
    if not floor_plan.image_url:
        return None, None
    rel = floor_plan.image_url.lstrip('/')
    path = os.path.join(settings.MEDIA_ROOT, rel)
    try:
        from PIL import Image
        with Image.open(path) as img:
            return img.width, img.height
    except Exception:
        return None, None


@admin_login_required
def get_map_data(request):
    """Return the active floor plan plus all its beacons, waypoints, connections
    and the list of shelves (for the waypoint-link dropdown) as JSON."""
    floor_plan = FloorPlan.objects.filter(is_active=True).first()
    if floor_plan is None:
        return JsonResponse({'success': False, 'has_active': False, 'error': 'No active floor plan'})

    width, height = _floorplan_image_size(floor_plan)

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

    rooms = [
        {'room_id': r.room_id, 'name': r.name, 'map_x': r.map_x, 'map_y': r.map_y}
        for r in Room.objects.filter(floor_plan=floor_plan)
    ]

    shelves = [
        {
            'shelf_id': s.shelf_id, 'name': s.name,
            'map_x': s.map_x, 'map_y': s.map_y, 'room_id': s.room_id,
        }
        for s in Shelf.objects.filter(room__floor_plan=floor_plan).order_by('name')
    ]

    return JsonResponse({
        'success': True,
        'has_active': True,
        'floor_plan_id': floor_plan.floor_plan_id,
        'image_url': _floorplan_media_url(floor_plan),
        'image_width': width,
        'image_height': height,
        'beacons': beacons,
        'waypoints': waypoints,
        'connections': connections,
        'rooms': rooms,
        'shelves': shelves,
    })


@admin_login_required
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


@admin_login_required
def delete_beacon(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})

    beacon_id = request.POST.get('beacon_id')
    if not beacon_id:
        return JsonResponse({'success': False, 'error': 'beacon_id is required'})

    BLEBeacon.objects.filter(beacon_id=beacon_id).delete()
    return JsonResponse({'success': True})


@admin_login_required
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


@admin_login_required
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


@admin_login_required
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


@admin_login_required
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

    width, height = _floorplan_image_size(floor_plan)

    shelves = [
        {'shelf_id': s.shelf_id, 'name': s.name, 'x': s.map_x, 'y': s.map_y}
        for s in Shelf.objects.filter(room__floor_plan=floor_plan, is_active=True)
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
        'image_url': _floorplan_media_url(floor_plan),
        'image_width': width,
        'image_height': height,
        'renovation_notice': floor_plan.renovation_notice or '',
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
        account_status='Active',
        password_hash=hash_password(None),  # unusable until set via the portal
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
    return redirect('admin_log_management')

