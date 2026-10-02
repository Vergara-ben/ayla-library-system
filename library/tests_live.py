"""Live updates: what a write announces, who hears it, and what a refresh leaves alone."""

from unittest import mock

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from channels.routing import URLRouter
from channels.security.websocket import AllowedHostsOriginValidator
from channels.sessions import SessionMiddlewareStack
from channels.testing import WebsocketCommunicator
from django.contrib import messages
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.middleware import SessionMiddleware
from django.db import connections
from django.http import HttpResponse
from django.test import RequestFactory, TestCase, TransactionTestCase
from django.urls import path

from ayla_library_system.asgi import application
from .auth_utils import hash_password
from .consumers import LiveConsumer
from .live import (
    LiveUpdatesMiddleware, PATRON_GROUP, PUBLIC_GROUP, STAFF_GROUP, _WriteRecorder,
    session_origin, topics_for_sql,
)
from .models import LibraryStatus, Patron, User


def _user(role='Admin', email='live-admin@example.invalid'):
    return User.objects.create(
        fullname='Live Admin', email=email, password_hash=hash_password('LiveTest123'),
        role=role, account_status='Active',
    )


class WriteTopicsTests(TestCase):

    def test_writes_map_to_topics(self):
        self.assertEqual(topics_for_sql('UPDATE "Books" SET "status" = %s'), ('books',))
        self.assertEqual(topics_for_sql('INSERT INTO "Transactions" ("book_id") VALUES (%s)'),
                         ('transactions',))
        self.assertEqual(topics_for_sql('DELETE FROM "Patron_Logs" WHERE 1'), ('visits',))

    def test_reads_and_bookkeeping_announce_nothing(self):
        self.assertEqual(topics_for_sql('SELECT * FROM "Books" FOR UPDATE'), ())
        self.assertEqual(topics_for_sql('UPDATE "django_session" SET "expire_date" = %s'), ())
        self.assertEqual(topics_for_sql('INSERT INTO "Login_Attempts" ("scope") VALUES (%s)'), ())

    def test_update_that_matched_nothing_is_not_a_change(self):
        recorder = _WriteRecorder()
        with connections['default'].execute_wrapper(recorder):
            LibraryStatus.objects.filter(pk=-1).update(is_open=False)
        self.assertEqual(recorder.topics, set())

        LibraryStatus.objects.get_or_create(pk=1)
        with connections['default'].execute_wrapper(recorder):
            LibraryStatus.objects.filter(pk=1).update(is_open=False)
        self.assertEqual(recorder.topics, {'library'})


class MiddlewareTests(TestCase):

    def setUp(self):
        self.user = _user()
        session = self.client.session
        session['admin_id'] = self.user.admin_id
        session['admin_role'] = 'Admin'
        session.save()

    def test_a_write_is_announced_with_its_session(self):
        with mock.patch('library.live.broadcast') as sent:
            self.client.post('/portal/library-status/', {'open': '0'})
        sent.assert_called_once()
        topics = sent.call_args.args[0]
        self.assertIn('library', topics)
        self.assertEqual(sent.call_args.kwargs['origin'],
                         session_origin(self.client.session))

    def test_a_read_announces_nothing(self):
        LibraryStatus.objects.get_or_create(pk=1)
        with mock.patch('library.live.broadcast') as sent:
            self.client.get('/admin-portal/dashboard/')
        sent.assert_not_called()

    def test_a_live_refresh_never_announces(self):
        with mock.patch('library.live.broadcast') as sent:
            self.client.post('/portal/library-status/', {'open': '0'}, HTTP_X_AYLA_LIVE='1')
        sent.assert_not_called()

    def test_pages_carry_the_live_script(self):
        page = self.client.get('/admin-portal/dashboard/')
        self.assertContains(page, 'js/live.js')
        self.assertContains(page, 'data-live="stats-1"')


class FlashMessageTests(TestCase):
    """A page's own refresh must not eat the flash meant for the next page load."""

    def _request(self, cookies, live=False):
        request = RequestFactory().get('/admin-portal/dashboard/',
                                       **({'HTTP_X_AYLA_LIVE': '1'} if live else {}))
        request.COOKIES.update(cookies)
        return request

    def _run(self, request, view, cookies):
        chain = SessionMiddleware(MessageMiddleware(LiveUpdatesMiddleware(view)))
        response = chain(request)
        # Carry the cookies forward, as a browser would.
        for name, morsel in response.cookies.items():
            if morsel.value:
                cookies[name] = morsel.value
            else:
                cookies.pop(name, None)
        return response

    def test_refresh_keeps_messages(self):
        def add(request):
            messages.success(request, 'Saved.')
            return HttpResponse()

        def show(request):
            return HttpResponse(' '.join(str(m) for m in messages.get_messages(request)))

        cookies = {}
        self._run(self._request(cookies), add, cookies)
        self._run(self._request(cookies, live=True), show, cookies)
        final = self._run(self._request(cookies), show, cookies)
        self.assertEqual(final.content, b'Saved.')

    def test_an_ordinary_page_load_still_uses_them_up(self):
        def add(request):
            messages.success(request, 'Saved.')
            return HttpResponse()

        def show(request):
            return HttpResponse(' '.join(str(m) for m in messages.get_messages(request)))

        cookies = {}
        self._run(self._request(cookies), add, cookies)
        self.assertEqual(self._run(self._request(cookies), show, cookies).content, b'Saved.')
        self.assertEqual(self._run(self._request(cookies), show, cookies).content, b'')


class SocketTests(TransactionTestCase):

    def _connect(self, session=None):
        headers = [(b'origin', b'http://testserver')]
        if session is not None:
            headers.append((b'cookie', f'sessionid={session.session_key}'.encode()))
        return WebsocketCommunicator(application, '/ws/live/', headers=headers)

    def _session(self, **values):
        session = SessionStore()
        for key, value in values.items():
            session[key] = value
        session.create()
        return session

    def test_staff_hear_everything_and_their_own_echo_is_marked(self):
        user = _user()
        session = self._session(admin_id=user.admin_id)

        async def scenario():
            socket = self._connect(session)
            connected, _ = await socket.connect()
            self.assertTrue(connected)
            hello = await socket.receive_json_from()
            self.assertIn('activity', hello['hello'])

            layer = get_channel_layer()
            await layer.group_send(STAFF_GROUP, {'type': 'live.changed', 'topics': ['books'],
                                                 'origin': session_origin(session)})
            self.assertEqual(await socket.receive_json_from(), {'topics': ['books'], 'self': True})
            await layer.group_send(STAFF_GROUP, {'type': 'live.changed', 'topics': ['books'],
                                                 'origin': 'someone-else'})
            self.assertEqual(await socket.receive_json_from(), {'topics': ['books']})
            await socket.disconnect()

        async_to_sync(scenario)()

    def test_visitors_hear_only_public_topics(self):
        async def scenario():
            socket = self._connect()
            connected, _ = await socket.connect()
            self.assertTrue(connected)
            hello = await socket.receive_json_from()
            self.assertEqual(set(hello['hello']), {'books', 'announcements', 'map', 'library'})
            # Staff-only news does not reach them.
            await get_channel_layer().group_send(
                STAFF_GROUP, {'type': 'live.changed', 'topics': ['activity'], 'origin': None})
            self.assertTrue(await socket.receive_nothing(timeout=0.2))
            await socket.disconnect()

        async_to_sync(scenario)()

    def test_patrons_hear_their_topics_not_staff_ones(self):
        patron = Patron.objects.create(first_name='Live', last_name='Patron',
                                       email='live-patron@example.invalid',
                                       account_status='Active')
        session = self._session(patron_id=patron.patron_id)

        async def scenario():
            socket = self._connect(session)
            await socket.connect()
            hello = await socket.receive_json_from()
            self.assertIn('transactions', hello['hello'])
            self.assertNotIn('activity', hello['hello'])
            self.assertNotIn('visits', hello['hello'])
            await socket.disconnect()

        async_to_sync(scenario)()

    def test_a_deactivated_account_is_not_staff(self):
        user = _user()
        user.account_status = 'Inactive'
        user.save()
        session = self._session(admin_id=user.admin_id)

        async def scenario():
            socket = self._connect(session)
            await socket.connect()
            hello = await socket.receive_json_from()
            self.assertNotIn('activity', hello['hello'])
            await socket.disconnect()

        async_to_sync(scenario)()

    def test_portal_address_rule_applies_to_the_socket(self):
        user = _user()
        session = self._session(admin_id=user.admin_id)

        async def scenario():
            socket = self._connect(session)
            await socket.connect()
            hello = await socket.receive_json_from()
            self.assertNotIn('activity', hello['hello'])
            await socket.disconnect()

        with self.settings(PORTAL_ALLOWED_IPS=['203.0.113.0/24']):
            async_to_sync(scenario)()

    def test_cross_site_pages_cannot_connect(self):
        # The validator reads ALLOWED_HOSTS when the app is built, as it is in production.
        with self.settings(ALLOWED_HOSTS=['ayla.example']):
            guarded = AllowedHostsOriginValidator(
                SessionMiddlewareStack(URLRouter([path('ws/live/', LiveConsumer.as_asgi())])))

            async def scenario():
                for origin, expected in ((b'https://evil.example', False),
                                         (b'https://ayla.example', True)):
                    socket = WebsocketCommunicator(guarded, '/ws/live/', headers=[(b'origin', origin)])
                    connected, _ = await socket.connect()
                    self.assertEqual(connected, expected, origin)
                    await socket.disconnect()

            async_to_sync(scenario)()


# Groups exist under these names; a rename would silently stop every page updating.
assert {STAFF_GROUP, PATRON_GROUP, PUBLIC_GROUP} == {'live.staff', 'live.patron', 'live.public'}
