from django.contrib.auth.hashers import check_password as django_check_password, make_password
from django.shortcuts import redirect


def hash_password(password):
    return make_password(password)


def check_password(plain_password, hashed_password):
    return django_check_password(plain_password, hashed_password)


# Login throttling.

def _throttle_row(scope, identifier):
    from .models import LoginAttempt
    key = (identifier or '').strip().lower()[:255]
    if not key:
        return None
    row, _created = LoginAttempt.objects.get_or_create(scope=scope, identifier=key)
    return row


def login_locked_message(scope, identifier):
    """The refusal to show, or None when this identity may still try."""
    from django.utils import timezone
    from .models import LoginAttempt

    row = _throttle_row(scope, identifier)
    if row is None or not row.locked_until:
        return None
    if row.locked_until <= timezone.now():
        # Expired: clear it here so the next failure starts a fresh run.
        row.failures = 0
        row.locked_until = None
        row.first_failure_at = None
        row.save(update_fields=['failures', 'locked_until', 'first_failure_at'])
        return None
    minutes = max(1, int((row.locked_until - timezone.now()).total_seconds() // 60) + 1)
    return (f'Too many failed sign-in attempts. Try again in about {minutes} minute'
            f'{"s" if minutes != 1 else ""}.')


def record_login_failure(scope, identifier):
    """Count one miss, and lock the identity once the run is long enough."""
    from datetime import timedelta
    from django.utils import timezone
    from .models import LoginAttempt

    row = _throttle_row(scope, identifier)
    if row is None:
        return None
    now = timezone.now()

    # A run that went quiet for longer than the window is over, not continuing.
    if row.last_failure_at and (now - row.last_failure_at) > timedelta(minutes=LoginAttempt.WINDOW_MINUTES):
        row.failures = 0
        row.first_failure_at = None

    row.failures += 1
    row.last_failure_at = now
    if row.first_failure_at is None:
        row.first_failure_at = now
    if row.failures >= LoginAttempt.MAX_FAILURES:
        row.locked_until = now + timedelta(minutes=LoginAttempt.LOCKOUT_MINUTES)
    row.save(update_fields=['failures', 'first_failure_at', 'last_failure_at', 'locked_until'])
    return LoginAttempt.MAX_FAILURES - row.failures


def clear_login_failures(scope, identifier):
    """A correct password ends the run."""
    from .models import LoginAttempt
    key = (identifier or '').strip().lower()[:255]
    if key:
        LoginAttempt.objects.filter(scope=scope, identifier=key).delete()


# A hash of a value nobody can supply.
_DUMMY_HASH = None


def waste_password_time():
    """Spend the same time on a missing account as on a real one."""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = make_password('no-such-account-placeholder')
    django_check_password('no-such-account-placeholder-attempt', _DUMMY_HASH)


# One sentence for every way a sign-in can fail.
LOGIN_FAILED_TEXT = 'Invalid email or password.'


def patron_login_required(view_func):
    def _wrapped_view(request, *args, **kwargs):
        if 'patron_id' not in request.session:
            return redirect('/patron/login/')
        from .models import Patron
        # An archived patron is signed out.
        if not Patron.objects.filter(patron_id=request.session['patron_id']).exists():
            request.session.flush()
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
    """Administrator-only view covering operational rather than governing work."""
    def decorator(view_func):
        def _wrapped_view(request, *args, **kwargs):
            from django.contrib import messages
            from .models import User
            from .modules import MODULE_LABELS

            if 'admin_id' not in request.session:
                return redirect('/admin-portal/login/')
            if request.session.get('admin_role') == 'Staff':
                return redirect('/library-staff/dashboard/')

            # Read module access on every request.
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
    """Either role, but the module must actually be granted to the account."""
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
    """Administrators always; Library Staff only with `module_key` granted."""
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


def admin_or_any_module_required(*module_keys):
    """Administrators always; Library Staff holding any one of `module_keys`."""
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
            if not any(user.has_module(key) for key in module_keys):
                messages.error(
                    request,
                    'You do not have access to '
                    + ' or '.join(MODULE_LABELS.get(k, k) for k in module_keys)
                    + '. Ask an administrator to grant it.'
                )
                return redirect('/library-staff/dashboard/')
            return view_func(request, *args, **kwargs)
        return _wrapped_view
    return decorator


def module_required(module_key):
    """Staff-only view that also requires a module granted by an Administrator."""
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

# Password policy.
MIN_PASSWORD_LENGTH = 8

PASSWORD_RULE_TEXT = (
    f'Password must be at least {MIN_PASSWORD_LENGTH} characters and include '
    'both a letter and a number.'
)


def password_length_error(password, user=None, current_hash=None):
    """The message to show for an unacceptable password, or None if it passes."""
    password = password or ''

    if len(password) < MIN_PASSWORD_LENGTH:
        return PASSWORD_RULE_TEXT

    has_letter = any(c.isalpha() for c in password)
    has_digit = any(c.isdigit() for c in password)
    if not (has_letter and has_digit):
        return PASSWORD_RULE_TEXT

    try:
        from django.contrib.auth.password_validation import validate_password
        from django.core.exceptions import ValidationError
        try:
            validate_password(password, user=user)
        except ValidationError as exc:
            # Show one message at a time.
            return exc.messages[0]
    except ImportError:            # pragma: no cover - Django is always present
        pass

    if current_hash and django_check_password(password, current_hash):
        return 'That is your current password. Please choose a different one.'

    return None
