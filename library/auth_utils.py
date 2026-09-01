from django.contrib.auth.hashers import check_password as django_check_password, make_password
from django.shortcuts import redirect


def hash_password(password):
    return make_password(password)


def check_password(plain_password, hashed_password):
    return django_check_password(plain_password, hashed_password)


# ─── Login throttling ─────────────────────────────────────────────────────
# Password guessing was unlimited on all four sign-in doors. These helpers are
# deliberately tiny and synchronous: a capstone deployment has no Redis and no
# background worker, and a counter in the database that everyone actually calls
# beats a perfect design nobody wires up.

def _throttle_row(scope, identifier):
    from .models import LoginAttempt
    key = (identifier or '').strip().lower()[:255]
    if not key:
        return None
    row, _created = LoginAttempt.objects.get_or_create(scope=scope, identifier=key)
    return row


def login_locked_message(scope, identifier):
    """The refusal to show, or None when this identity may still try.

    Returns the message rather than a boolean so the caller cannot forget to
    explain itself: a locked-out librarian needs to know it is a lockout and not
    a wrong password, or they will keep typing.
    """
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


# A hash of a value nobody can supply. Verifying a password against it costs the
# same as verifying a real one, which is the point: without this, a missing user
# returns instantly while a real one runs PBKDF2, and the difference is a
# perfectly good answer to "does this address have an account?".
_DUMMY_HASH = None


def waste_password_time():
    """Spend the same time on a missing account as on a real one."""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = make_password('no-such-account-placeholder')
    django_check_password('no-such-account-placeholder-attempt', _DUMMY_HASH)


# One sentence for every way a sign-in can fail. Naming the reason -- no such
# user, wrong password, suspended -- tells an attacker which addresses are real
# without them ever needing a correct password.
LOGIN_FAILED_TEXT = 'Invalid email or password.'


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

    Only correct where the *page* is gated the same way. Guarding an endpoint
    with this while the page that calls it is admin_only_required locks an
    Administrator out of a screen they can still open: the request 302s to the
    dashboard, the fetch that expected JSON parses an HTML page instead, and the
    button appears to do nothing at all. Shelf Manager and Floor Plan Management
    were exactly that for any Administrator holding no modules -- both are
    admin_only_required pages -- so their endpoints use admin_or_module_required
    instead. Check the calling page before reaching for this one.
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

# ─── Password policy ──────────────────────────────────────────────────────
# One number, in one place. It had drifted: self-service changes and resets
# demanded 8 characters, an Administrator resetting a *staff* password demanded
# 6, and patron registration checked nothing at all -- so the account types with
# the most access had the weakest rule, and a one-character password could be
# set at sign-up.
MIN_PASSWORD_LENGTH = 8

PASSWORD_RULE_TEXT = (
    f'Password must be at least {MIN_PASSWORD_LENGTH} characters and include '
    'both a letter and a number.'
)


def password_length_error(password, user=None, current_hash=None):
    """The message to show for an unacceptable password, or None if it passes.

    Kept under its original name because every password path in the app already
    funnels through it -- registration, self-service change, admin reset, the
    OTP reset -- so strengthening it here strengthens all of them at once.

    Four checks, in the order a person meets them:

    Length, as before.

    Letter *and* number. The rule was length alone, so "aaaaaaaa" passed.

    Django's own validators, which were configured in settings from the day the
    project was generated and then never called -- nothing in the codebase ever
    invoked validate_password. That is 20,000 known-common passwords, a
    similar-to-your-own-email check, and a not-entirely-numeric check, all free.

    Reuse. "Change your password" that accepts the same password back is not a
    change, and it is the one people reach for when forced to rotate.
    """
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
            # One message, not the whole list: a wall of red is how people end
            # up picking the first thing that clears it.
            return exc.messages[0]
    except ImportError:            # pragma: no cover - Django is always present
        pass

    if current_hash and django_check_password(password, current_hash):
        return 'That is your current password. Please choose a different one.'

    return None
