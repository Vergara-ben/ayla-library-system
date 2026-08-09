"""Request-level guards."""

from django.shortcuts import redirect

from .desk import DESK_SESSION_KEY


class DeskModeMiddleware:
    """While the desk is armed, the portal is unreachable in this browser.

    Without this the separation would be cosmetic: the attendance screen would
    be one tab among several, and a patron left alone at the desk could switch
    back into a live administrator session. Arming desk mode suspends the
    portal for that browser until someone types the desk PIN — the session
    itself survives, so staff land back where they were instead of signing in
    again a hundred times a day.
    """

    PORTAL_PREFIXES = ('/admin-portal/', '/library-staff/')

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (request.session.get(DESK_SESSION_KEY)
                and request.path.startswith(self.PORTAL_PREFIXES)):
            return redirect('/desk/')
        return self.get_response(request)
