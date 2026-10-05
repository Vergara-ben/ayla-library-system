"""The live-updates socket each open page connects to."""

import ipaddress

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.conf import settings

from .live import GROUP_TOPICS, PATRON_GROUP, PUBLIC_GROUP, STAFF_GROUP, session_origin


def _socket_address(scope):
    """Same rule as middleware.client_ip, read from the socket's scope."""
    headers = dict(scope.get('headers') or [])
    header = getattr(settings, 'CLIENT_IP_HEADER', '')
    if header:
        return headers.get(header.lower().encode('latin-1'), b'').decode('latin-1').strip()
    client = scope.get('client') or ('',)
    return (client[0] or '').strip()


def _portal_address_allowed(address):
    raw = tuple(getattr(settings, 'PORTAL_ALLOWED_IPS', ()) or ())
    if not raw:
        return True
    try:
        ip = ipaddress.ip_address(address)
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


@database_sync_to_async
def _groups_for(scope):
    from .models import Patron, User
    session = scope.get('session')
    admin_id = session.get('admin_id') if session is not None else None
    patron_id = session.get('patron_id') if session is not None else None
    groups = [PUBLIC_GROUP]
    if (admin_id and _portal_address_allowed(_socket_address(scope))
            and User.objects.filter(admin_id=admin_id, account_status='Active').exists()):
        groups.append(STAFF_GROUP)
    if patron_id and Patron.objects.filter(patron_id=patron_id, account_status='Active').exists():
        groups.append(PATRON_GROUP)
    return groups


class LiveConsumer(AsyncJsonWebsocketConsumer):
    """Joins the groups this visitor may hear and forwards topic names."""

    async def connect(self):
        self.origin = session_origin(self.scope.get('session'))
        self.groups_joined = await _groups_for(self.scope)
        for group in self.groups_joined:
            await self.channel_layer.group_add(group, self.channel_name)
        await self.accept()
        # Tell the page what it can expect, so it knows the socket is live.
        heard = set()
        for group in self.groups_joined:
            heard |= GROUP_TOPICS[group]
        await self.send_json({'hello': sorted(heard)})

    async def disconnect(self, code):
        for group in getattr(self, 'groups_joined', ()):
            await self.channel_layer.group_discard(group, self.channel_name)

    async def receive_json(self, content, **kwargs):
        # Keep-alive from the page; hosts drop sockets that stay silent.
        if content.get('ping'):
            await self.send_json({'pong': True})

    async def live_changed(self, event):
        message = {'topics': event['topics']}
        # Changes made from this same browser session (its own saves).
        if self.origin and event.get('origin') == self.origin:
            message['self'] = True
        await self.send_json(message)
