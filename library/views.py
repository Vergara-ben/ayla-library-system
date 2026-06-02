from django.shortcuts import render

# Patron views
def patron_login(request):
    return render(request, 'patron/patronlogin.html')

def patron_register(request):
    return render(request, 'patron/patronregister.html')

def patron_dashboard(request):
    return render(request, 'patron/patrondashboard.html')

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
def admin_signin(request):
    return render(request, 'admin/signinadmin.html')

def admin_dashboard(request):
    return render(request, 'admin/dashboardadmin.html')

def admin_management(request):
    return render(request, 'admin/managementadmin.html')

def admin_add_patron(request):
    return render(request, 'admin/addparton.html')

def admin_manage_patron(request):
    return render(request, 'admin/managepartonadmin.html')

def admin_edit_patron(request, patron_id):
    context = {'patron_id': patron_id}
    return render(request, 'admin/editparton.html', context)

def admin_book_detail(request):
    return render(request, 'admin/bookdetailadmin.html')

def admin_qr_scanner(request):
    return render(request, 'admin/qrscanneradmin.html')

def admin_transaction(request):
    return render(request, 'admin/transactionadmin.html')

def admin_indoor_map(request):
    return render(request, 'admin/indoormapadmin.html')

def admin_log_management(request):
    return render(request, 'admin/logmanagement.html')

