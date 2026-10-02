"""ASGI config for ayla_library_system project: pages over HTTP, live updates over WebSocket."""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ayla_library_system.settings')

# Load Django before anything that imports models.
django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.security.websocket import AllowedHostsOriginValidator  # noqa: E402
from channels.sessions import SessionMiddlewareStack  # noqa: E402
from django.urls import path  # noqa: E402

from library.consumers import LiveConsumer  # noqa: E402

application = ProtocolTypeRouter({
    'http': django_asgi_app,
    'websocket': AllowedHostsOriginValidator(
        SessionMiddlewareStack(
            URLRouter([path('ws/live/', LiveConsumer.as_asgi())])
        )
    ),
})
