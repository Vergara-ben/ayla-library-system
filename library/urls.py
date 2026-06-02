from django.urls import path
from . import views

urlpatterns = [
    path('patron/login/', views.patron_login, name='patron_login'),
    path('patron/register/', views.patron_register, name='patron_register'),
    path('patron/dashboard/', views.patron_dashboard, name='patron_dashboard'),
    path('patron/catalog/', views.patron_catalog, name='patron_catalog'),
    path('patron/book-details/<int:book_id>/', views.patron_book_details, name='patron_book_details'),
    path('patron/map/', views.patron_map, name='patron_map'),
    path('patron/announcements/', views.patron_announcements, name='patron_announcements'),
    path('patron/account/', views.patron_account, name='patron_account'),
    path('admin/signin/', views.admin_signin, name='admin_signin'),
    path('admin/dashboard/', views.admin_dashboard, name='admin_dashboard'),
    path('admin/management/', views.admin_management, name='admin_management'),
    path('admin/add-patron/', views.admin_add_patron, name='admin_add_patron'),
    path('admin/manage-patron/', views.admin_manage_patron, name='admin_manage_patron'),
    path('admin/edit-patron/<int:patron_id>/', views.admin_edit_patron, name='admin_edit_patron'),
    path('admin/book-detail/', views.admin_book_detail, name='admin_book_detail'),
    path('admin/qr-scanner/', views.admin_qr_scanner, name='admin_qr_scanner'),
    path('admin/transaction/', views.admin_transaction, name='admin_transaction'),
    path('admin/indoor-map/', views.admin_indoor_map, name='admin_indoor_map'),
    path('admin/log-management/', views.admin_log_management, name='admin_log_management'),
]
