"""Request-level guards."""

import ipaddress
import logging
import os
import time

from django.conf import settings
from django.http import Http404
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


class SignOutInactiveAccountsMiddleware:
    """Sign out an account that is deactivated, suspended or archived while signed in."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        session = getattr(request, 'session', None)
        if session is not None and (session.get('admin_id') or session.get('patron_id')):
            from .models import Patron, User
            admin_id = session.get('admin_id')
            patron_id = session.get('patron_id')
            ended = (
                (admin_id and not User.objects.filter(
                    admin_id=admin_id, account_status='Active').exists())
                or (patron_id and not Patron.objects.filter(
                    patron_id=patron_id, account_status='Active').exists())
            )
            if ended:
                session.flush()
                from django.contrib import messages
                messages.error(request, 'Your account is no longer active, so you have been signed out.')
        return self.get_response(request)

# Pages for the library's own people, as opposed to patrons.
PORTAL_PREFIXES = ('/admin-portal/', '/library-staff/', '/desk/', '/portal/', '/patron-id/')


def client_ip(request):
    """The visitor's internet address.

    On Render every request arrives through Cloudflare, which writes the real address into
    CF-Connecting-IP and replaces any value a visitor sends. X-Forwarded-For is not used,
    because a visitor can put any address at the front of it.
    """
    if os.environ.get('RENDER', '').lower() == 'true':
        return (request.META.get('HTTP_CF_CONNECTING_IP') or '').strip()
    return (request.META.get('REMOTE_ADDR') or '').strip()


class LibraryNetworkOnlyMiddleware:
    """The admin, staff and desk pages open only on the library's own internet connection."""

    def __init__(self, get_response):
        self.get_response = get_response
        self._parsed = ((), [])

    def _networks(self, raw):
        if self._parsed[0] != raw:
            networks = []
            for entry in raw:
                try:
                    networks.append(ipaddress.ip_network(entry, strict=False))
                except ValueError:
                    logger.warning('PORTAL_ALLOWED_IPS: ignoring %r, which is not an address or range', entry)
            self._parsed = (raw, networks)
        return self._parsed[1]

    def _allowed(self, address, raw):
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return any(ip in network for network in self._networks(raw))

    def __call__(self, request):
        raw = tuple(getattr(settings, 'PORTAL_ALLOWED_IPS', ()) or ())
        if raw and request.path.startswith(PORTAL_PREFIXES) and not self._allowed(client_ip(request), raw):
            # The ordinary "not found" page, so the portal's existence is not confirmed.
            raise Http404
        return self.get_response(request)
