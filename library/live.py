"""Live updates: tell open pages which kinds of data just changed."""

import hashlib
import ipaddress
import logging
import re
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.db import connections
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

logger = logging.getLogger(__name__)

# Topics a page can listen for, and the models whose rows feed them.
MODEL_TOPICS = {
    'Book': ('books',),
    'InventoryRecord': ('books',),
    'StockMovement': ('books',),
    'StockAudit': ('books',),
    'StockAuditLine': ('books',),
    'ShelfLevel': ('books', 'map'),
    'Shelf': ('books', 'map'),
    'Transaction': ('transactions',),
    'DueDateExtension': ('transactions',),
    'Patron': ('patrons',),
    'PatronCredential': ('patrons',),
    'PatronPhoto': ('patrons',),
    'ReactivationRequest': ('patrons',),
    'PatronLog': ('visits',),
    'Conversation': ('chat',),
    'ChatMessage': ('chat',),
    'Announcement': ('announcements',),
    'Donation': ('donations',),
    'Donor': ('donations',),
    'FloorPlan': ('map',),
    'BLEBeacon': ('map',),
    'Room': ('map',),
    'Door': ('map',),
    'Obstacle': ('map',),
    'Stairway': ('map',),
    'Waypoint': ('map',),
    'WaypointConnection': ('map',),
    'LibraryStatus': ('library',),
    'BorrowingRule': ('library',),
    'User': ('staff',),
    'SystemLog': ('activity',),
}

ALL_TOPICS = frozenset(t for topics in MODEL_TOPICS.values() for t in topics)

# Who hears what. The message names the topic only, never the data itself.
STAFF_CHANNEL = 'private-live-staff'
PATRON_CHANNEL = 'private-live-patron'
PUBLIC_CHANNEL = 'live-public'
CHANNEL_TOPICS = {
    STAFF_CHANNEL: ALL_TOPICS,
    PATRON_CHANNEL: frozenset({'books', 'transactions', 'patrons', 'chat', 'announcements', 'map', 'library'}),
    PUBLIC_CHANNEL: frozenset({'books', 'announcements', 'map', 'library'}),
}

# Sent by every fetch a live update triggers; such requests never broadcast,
# so a read that records itself (a message marked read) cannot echo forever.
REFRESH_HEADER = 'HTTP_X_AYLA_LIVE'


def session_origin(session):
    """A stable, non-secret tag for a session (never the key itself)."""
    key = getattr(session, 'session_key', None)
    if not key:
        return None
    return hashlib.sha256(key.encode()).hexdigest()[:16]


_WRITE_SQL = re.compile(r'^\s*(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+[`"]?(\w+)[`"]?', re.IGNORECASE)
_table_topics = None


def table_topics():
    """db_table -> topics, built once the app registry is ready."""
    global _table_topics
    if _table_topics is None:
        from django.apps import apps
        mapping = {}
        for model in apps.get_app_config('library').get_models():
            topics = MODEL_TOPICS.get(model.__name__)
            if topics:
                mapping[model._meta.db_table.lower()] = topics
        _table_topics = mapping
    return _table_topics


def topics_for_sql(sql):
    match = _WRITE_SQL.match(sql or '')
    if not match:
        return ()
    return table_topics().get(match.group(1).lower(), ())


_client = None
# One background thread, so a page never waits on Pusher to answer.
_sender = ThreadPoolExecutor(max_workers=1, thread_name_prefix='live')


def pusher_client():
    global _client
    if _client is None and settings.PUSHER_ENABLED:
        import pusher
        _client = pusher.Pusher(app_id=settings.PUSHER_APP_ID, key=settings.PUSHER_KEY,
                                secret=settings.PUSHER_SECRET, cluster=settings.PUSHER_CLUSTER,
                                ssl=True, timeout=5)
    return _client


def publish(topics, origin=None):
    """Send the changed topics to every channel allowed to hear them; origin tags the acting session."""
    client = pusher_client()
    if client is None:
        return
    for channel, allowed in CHANNEL_TOPICS.items():
        heard = sorted(set(topics) & allowed)
        if heard:
            try:
                client.trigger(channel, 'changed', {'topics': heard, 'origin': origin})
            except Exception:
                # A missed nudge only delays a screen; the page's own check catches up.
                logger.exception('Live update to %s failed for %s', channel, heard)


def broadcast(topics, origin=None):
    topics = set(topics) & ALL_TOPICS
    if topics and settings.PUSHER_ENABLED:
        _sender.submit(publish, topics, origin)


class _WriteRecorder:
    """execute_wrapper that notes the topics of every write statement."""

    def __init__(self):
        self.topics = set()

    def __call__(self, execute, sql, params, many, context):
        result = execute(sql, params, many, context)
        topics = topics_for_sql(sql)
        # An UPDATE or DELETE that matched nothing changed nothing.
        if topics and getattr(context.get('cursor'), 'rowcount', 1) != 0:
            self.topics.update(topics)
        return result


class LiveUpdatesMiddleware:
    """After a request that wrote data, nudge the open pages that show it."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.META.get(REFRESH_HEADER):
            response = self.get_response(request)
            # Leave flash messages for the page load they were meant for.
            storage = getattr(request, '_messages', None)
            if storage is not None:
                storage.used = False
            return response

        recorder = _WriteRecorder()
        with connections['default'].execute_wrapper(recorder):
            response = self.get_response(request)
        if recorder.topics:
            broadcast(recorder.topics, origin=session_origin(getattr(request, 'session', None)))
        return response


def _portal_address_allowed(request):
    """The PORTAL_ALLOWED_IPS rule, so staff news stays on the library's connection."""
    from .middleware import client_ip
    raw = tuple(getattr(settings, 'PORTAL_ALLOWED_IPS', ()) or ())
    if not raw:
        return True
    try:
        ip = ipaddress.ip_address(client_ip(request))
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    for entry in raw:
        try:
            if ip in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False


def channels_for(request):
    """The channels this visitor may listen on."""
    from .models import Patron, User
    channels = [PUBLIC_CHANNEL]
    session = request.session
    admin_id = session.get('admin_id')
    if (admin_id and _portal_address_allowed(request)
            and User.objects.filter(admin_id=admin_id, account_status='Active').exists()):
        channels.append(STAFF_CHANNEL)
    patron_id = session.get('patron_id')
    if patron_id and Patron.objects.filter(patron_id=patron_id, account_status='Active').exists():
        channels.append(PATRON_CHANNEL)
    return channels


# No CSRF check: the answer is only readable by this site's own pages, and it
# grants nothing beyond what this session may already hear.
@csrf_exempt
@require_POST
def pusher_auth(request):
    """Pusher asks here before letting a page join a private channel."""
    client = pusher_client()
    channel = request.POST.get('channel_name', '')
    socket_id = request.POST.get('socket_id', '')
    if client is None or channel not in channels_for(request) or channel == PUBLIC_CHANNEL:
        return JsonResponse({'error': 'Not allowed.'}, status=403)
    return JsonResponse(client.authenticate(channel=channel, socket_id=socket_id))
