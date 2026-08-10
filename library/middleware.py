"""Request-level guards."""

from django.shortcuts import redirect

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
