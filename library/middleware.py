"""Request-level guards."""

import logging
import time

from django.conf import settings
from django.shortcuts import redirect

from .desk import DESK_SESSION_KEY, desk_log_path


logger = logging.getLogger(__name__)

# Every page in this app answers well under a second on a laptop.
SLOW_REQUEST_SECONDS = 1.5


class SlowRequestLoggingMiddleware:
    """Write a line for any request that took unreasonably long."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        started = time.monotonic()
        response = self.get_response(request)
        elapsed = time.monotonic() - started
        if elapsed >= SLOW_REQUEST_SECONDS:
            logger.warning('Slow request: %s %s took %.2fs (status %s)',
                           request.method, request.path, elapsed,
                           response.status_code)
        return response


class DeskModeMiddleware:
    """While the desk is armed, the portal is Log Management and nothing else."""

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


class NoStoreSignedInPagesMiddleware:
    """Keep signed-in pages out of the browser cache, so Back after logout reloads them."""

    SESSION_KEYS = ('admin_id', 'patron_id')

    def __init__(self, get_response):
        self.get_response = get_response

    def _signed_in(self, request):
        session = getattr(request, 'session', None)
        return bool(session) and any(session.get(key) for key in self.SESSION_KEYS)

    def __call__(self, request):
        was_signed_in = self._signed_in(request)
        response = self.get_response(request)
        if was_signed_in or self._signed_in(request):
            response['Cache-Control'] = 'no-cache, no-store, must-revalidate, private'
            response['Pragma'] = 'no-cache'
            response['Expires'] = '0'
        return response


class ContentSecurityPolicyMiddleware:
    """Attach the CSP header to every response."""

    def __init__(self, get_response):
        self.get_response = get_response
        self.policy = getattr(settings, 'CONTENT_SECURITY_POLICY', '')

    def __call__(self, request):
        response = self.get_response(request)
        if self.policy and 'Content-Security-Policy' not in response:
            response['Content-Security-Policy'] = self.policy
        return response
