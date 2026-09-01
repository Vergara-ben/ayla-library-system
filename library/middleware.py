"""Request-level guards."""

from django.conf import settings
from django.shortcuts import redirect
from django.utils import timezone

from .audit import log_system_action
from .desk import DESK_SESSION_KEY, desk_log_path


class DeskModeMiddleware:
    """While the desk is armed, the portal is Log Management and nothing else.

    Without this the separation would be cosmetic: the sign-in table would be
    one page among many, and a patron left alone at the desk could click
    through to Manage Patrons or Transactions. Arming confines the browser to
    the one page attendance actually needs; the session itself survives, so
    staff land back where they were on entering their password instead of
    signing in again a hundred times a day.
    """

    PORTAL_PREFIXES = ('/admin-portal/', '/library-staff/')

    # Reachable while armed: the log page's own machinery, and the way out.
    ALLOWED_EXACT = {
        '/admin-portal/log-management/',
        '/library-staff/logs/',
        '/admin-portal/log-entry/',
        '/admin-portal/log-exit/',
        '/admin-portal/log-register/',
        '/admin-portal/log-detail/',
    }
    ALLOWED_PREFIXES = ('/desk/',)

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.session.get(DESK_SESSION_KEY):
            path = request.path
            if (path.startswith(self.PORTAL_PREFIXES)
                    and path not in self.ALLOWED_EXACT
                    and not path.startswith(self.ALLOWED_PREFIXES)):
                return redirect(desk_log_path(request))
        return self.get_response(request)


# How long a signed-in session may sit untouched before it is closed.
#
# SESSION_COOKIE_AGE already caps a session's total life, but that is not the
# question a library desk asks. The machine sits on a counter in a public room,
# and what matters is how long it has been *unattended* -- not how long ago the
# librarian signed in. A four-hour shift should never be interrupted; ten
# minutes at lunch should not leave the portal open to whoever walks past.
#
# Patrons get a longer leash: someone reading the catalogue on their own phone
# is not the same exposure as a staff terminal in a public room.
STAFF_IDLE_SECONDS = 15 * 60
PATRON_IDLE_SECONDS = 60 * 60
LAST_SEEN_KEY = '_last_seen'


class IdleSessionTimeoutMiddleware:
    """Close a session that has gone quiet; renew one that is being used."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        session = request.session
        is_staff_side = 'admin_id' in session
        is_patron_side = 'patron_id' in session

        if is_staff_side or is_patron_side:
            limit = STAFF_IDLE_SECONDS if is_staff_side else PATRON_IDLE_SECONDS
            now = timezone.now().timestamp()
            last = session.get(LAST_SEEN_KEY)

            if last is not None and (now - last) > limit:
                if is_staff_side:
                    # An abandoned terminal being closed is exactly the kind of
                    # event an audit trail exists to hold.
                    who = session.get('admin_fullname') or 'someone'
                    log_system_action('Session timeout', 'Auth', session.get('admin_id'),
                                      f'Idle session for {who} was closed')
                session.flush()
            else:
                # Touched on every request, so activity keeps the session alive.
                session[LAST_SEEN_KEY] = now

        return self.get_response(request)


class ContentSecurityPolicyMiddleware:
    """Attach the CSP header to every response.

    A header rather than a <meta> tag so it also covers responses that are not
    HTML -- the report PDFs, the JSON endpoints, the credential downloads.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.policy = getattr(settings, 'CONTENT_SECURITY_POLICY', '')

    def __call__(self, request):
        response = self.get_response(request)
        if self.policy and 'Content-Security-Policy' not in response:
            response['Content-Security-Policy'] = self.policy
        return response
