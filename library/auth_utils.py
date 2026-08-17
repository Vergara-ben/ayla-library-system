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


def admin_module_required(module_key):
    """Administrator-only view covering operational rather than governing work.

    Governance and oversight use admin_only_required and are always available.
    This covers the desk screens an Administrator sees only where the module has
    been granted, so the role's default surface is administration without the
    account being unable to help at the desk when staffing requires it.
    """
    def decorator(view_func):
        def _wrapped_view(request, *args, **kwargs):
            from django.contrib import messages
            from .models import User
            from .modules import MODULE_LABELS

            if 'admin_id' not in request.session:
                return redirect('/admin-portal/login/')
            if request.session.get('admin_role') == 'Staff':
                return redirect('/library-staff/dashboard/')

            # Read per request, not from the session, so a grant or a
            # withdrawal takes effect immediately rather than at next login.
            user = User.objects.filter(admin_id=request.session['admin_id']).first()
            if user is None:
                request.session.flush()
                return redirect('/admin-portal/login/')
            if not user.has_module(module_key):
                messages.error(
                    request,
                    MODULE_LABELS.get(module_key, module_key)
                    + ' is a Library Staff function. Grant it to this account in '
                      'User Management if an Administrator needs to work the desk.'
                )
                return redirect('/admin-portal/dashboard/')
            return view_func(request, *args, **kwargs)
        return _wrapped_view
    return decorator


def granted_module_required(module_key):
    """Either role, but the module must actually be granted to the account.

    For pages both portals share, where holding the module is the question and
    the role only decides which dashboard a refusal returns to.
    """
    def decorator(view_func):
        def _wrapped_view(request, *args, **kwargs):
            from django.contrib import messages
            from .models import User
            from .modules import MODULE_LABELS

            is_staff = request.session.get('admin_role') == 'Staff'
            home = '/library-staff/dashboard/' if is_staff else '/admin-portal/dashboard/'
            login = '/library-staff/login/' if is_staff else '/admin-portal/login/'

            if 'admin_id' not in request.session:
                return redirect(login)
            user = User.objects.filter(admin_id=request.session['admin_id']).first()
            if user is None:
                request.session.flush()
                return redirect(login)
            if not user.has_module(module_key):
                messages.error(
                    request,
                    'You do not have access to '
                    + MODULE_LABELS.get(module_key, module_key)
                    + '. Ask an administrator to grant it.'
                )
                return redirect(home)
            return view_func(request, *args, **kwargs)
        return _wrapped_view
    return decorator


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
