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


def admin_or_module_required(module_key):
    """Administrators always; Library Staff only with `module_key` granted.

    Used where the manuscript splits a module by role — stock receiving is open
    to Staff who hold it, while the rest of Inventory stays Administrator-only.
    """
    def decorator(view_func):
        def _wrapped_view(request, *args, **kwargs):
            from django.contrib import messages
            from .models import User
            from .modules import MODULE_LABELS

            if 'admin_id' not in request.session:
                return redirect('/admin-portal/login/')
            if request.session.get('admin_role') != 'Staff':
                return view_func(request, *args, **kwargs)

            user = User.objects.filter(admin_id=request.session['admin_id']).first()
            if user is None:
                request.session.flush()
                return redirect('/library-staff/login/')
            if not user.has_module(module_key):
                messages.error(
                    request,
                    'You do not have access to '
                    + MODULE_LABELS.get(module_key, module_key)
                    + '. Ask an administrator to grant it.'
                )
                return redirect('/library-staff/dashboard/')
            return view_func(request, *args, **kwargs)
        return _wrapped_view
    return decorator


def module_required(module_key):
    """Staff-only view that also requires a module granted by an Administrator.

    The grant is read from the database on every request rather than cached in
    the session, so revoking a module takes effect immediately instead of at the
    staff member's next login.
    """
    def decorator(view_func):
        def _wrapped_view(request, *args, **kwargs):
            from django.contrib import messages
            from .models import User
            from .modules import MODULE_LABELS

            if 'admin_id' not in request.session:
                return redirect('/library-staff/login/')
            if request.session.get('admin_role') != 'Staff':
                return redirect('/admin-portal/dashboard/')

            user = User.objects.filter(admin_id=request.session['admin_id']).first()
            if user is None:
                request.session.flush()
                return redirect('/library-staff/login/')
            if not user.has_module(module_key):
                messages.error(
                    request,
                    f'You do not have access to the {MODULE_LABELS.get(module_key, module_key)}. '
                    'Ask an administrator to grant it.'
                )
                return redirect('/library-staff/dashboard/')
            return view_func(request, *args, **kwargs)
        return _wrapped_view
    return decorator
