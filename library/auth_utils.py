from django.contrib.auth.hashers import check_password as django_check_password, make_password
from django.shortcuts import redirect


def hash_password(password):
    return make_password(password)


def check_password(plain_password, hashed_password):
    return django_check_password(plain_password, hashed_password)


def patron_login_required(view_func):
    def _wrapped_view(request, *args, **kwargs):
        if 'patron_id' not in request.session:
            return redirect('/patron/login/')
        return view_func(request, *args, **kwargs)
    return _wrapped_view


def admin_login_required(view_func):
    """Any authenticated portal user (Admin or Library Staff)."""
    def _wrapped_view(request, *args, **kwargs):
        if 'admin_id' not in request.session:
            return redirect('/admin-portal/login/')
        return view_func(request, *args, **kwargs)
    return _wrapped_view


def admin_only_required(view_func):
    """Admin-only views. Library Staff are bounced to their own portal."""
    def _wrapped_view(request, *args, **kwargs):
        if 'admin_id' not in request.session:
            return redirect('/admin-portal/login/')
        if request.session.get('admin_role') == 'Staff':
            return redirect('/library-staff/dashboard/')
        return view_func(request, *args, **kwargs)
    return _wrapped_view


def staff_only_required(view_func):
    """Library-Staff-only views. Admins are bounced to the admin dashboard."""
    def _wrapped_view(request, *args, **kwargs):
        if 'admin_id' not in request.session:
            return redirect('/library-staff/login/')
        if request.session.get('admin_role') != 'Staff':
            return redirect('/admin-portal/dashboard/')
        return view_func(request, *args, **kwargs)
    return _wrapped_view
