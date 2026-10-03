"""Live updates: tell open pages which kinds of data just changed."""

import hashlib
import logging
import re

from asgiref.sync import async_to_sync
from django.db import connections

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
STAFF_GROUP = 'live.staff'
PATRON_GROUP = 'live.patron'
PUBLIC_GROUP = 'live.public'
GROUP_TOPICS = {
    STAFF_GROUP: ALL_TOPICS,
    PATRON_GROUP: frozenset({'books', 'transactions', 'patrons', 'chat', 'announcements', 'map', 'library'}),
    PUBLIC_GROUP: frozenset({'books', 'announcements', 'map', 'library'}),
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


def broadcast(topics, origin=None):
    """Send the changed topics to every group allowed to hear them; origin tags the acting session."""
    topics = set(topics) & ALL_TOPICS
    if not topics:
        return
    try:
        from channels.layers import get_channel_layer
        layer = get_channel_layer()
        if layer is None:
            return
        send = async_to_sync(layer.group_send)
        for group, allowed in GROUP_TOPICS.items():
            heard = sorted(topics & allowed)
            if heard:
                send(group, {'type': 'live.changed', 'topics': heard, 'origin': origin})
    except Exception:
        # A missed nudge only delays a screen; it must never fail the request.
        logger.exception('Live update broadcast failed for %s', sorted(topics))


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
