"""Live updates: what a write announces, who hears it, and what a refresh leaves alone."""

from unittest import mock

from django.contrib import messages
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.db import connections
from django.http import HttpResponse
from django.test import RequestFactory, TestCase, override_settings

from .auth_utils import hash_password
from . import live
from .live import (
    LiveUpdatesMiddleware, PATRON_CHANNEL, PUBLIC_CHANNEL, STAFF_CHANNEL, _WriteRecorder,
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


@override_settings(PUSHER_ENABLED=True, PUSHER_APP_ID='1', PUSHER_KEY='key',
                   PUSHER_SECRET='secret', PUSHER_CLUSTER='ap1')
class PusherTests(TestCase):

    def setUp(self):
        live._client = None
        self.addCleanup(setattr, live, '_client', None)
        # Writes made while setting up must not reach the real Pusher.
        sender = mock.patch.object(live, '_sender')
        sender.start()
        self.addCleanup(sender.stop)

    def test_each_channel_hears_only_its_topics(self):
        client = mock.Mock()
        with mock.patch.object(live, 'pusher_client', return_value=client):
            live.publish({'books', 'activity', 'visits'}, origin='abc')
        sent = {c.args[0]: c.args[2]['topics'] for c in client.trigger.call_args_list}
        self.assertEqual(sent[STAFF_CHANNEL], ['activity', 'books', 'visits'])
        self.assertEqual(sent[PATRON_CHANNEL], ['books'])
        self.assertEqual(sent[PUBLIC_CHANNEL], ['books'])
        self.assertEqual(client.trigger.call_args_list[0].args[2]['origin'], 'abc')

    def test_a_pusher_outage_never_fails_the_request(self):
        client = mock.Mock()
        client.trigger.side_effect = OSError('unreachable')
        with mock.patch.object(live, 'pusher_client', return_value=client),                 self.assertLogs('library.live', 'ERROR'):
            live.publish({'books'})  # logged, not raised

    def _auth(self, channel, **session_values):
        session = self.client.session
        for key, value in session_values.items():
            session[key] = value
        session.save()
        return self.client.post('/live/auth/', {'channel_name': channel, 'socket_id': '123.456'})

    def test_staff_may_join_the_staff_channel(self):
        user = _user()
        response = self._auth(STAFF_CHANNEL, admin_id=user.admin_id)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['auth'].startswith('key:'))

    def test_patrons_and_guests_may_not(self):
        patron = Patron.objects.create(first_name='Live', last_name='Patron',
                                       email='live-patron@example.invalid', account_status='Active')
        self.assertEqual(self._auth(STAFF_CHANNEL, patron_id=patron.patron_id).status_code, 403)
        self.assertEqual(self._auth(PATRON_CHANNEL, patron_id=patron.patron_id).status_code, 200)
        self.client.session.flush()
        self.client.cookies.clear()
        self.assertEqual(self._auth(PATRON_CHANNEL).status_code, 403)

    def test_a_deactivated_account_is_refused(self):
        user = _user()
        user.account_status = 'Inactive'
        user.save()
        self.assertEqual(self._auth(STAFF_CHANNEL, admin_id=user.admin_id).status_code, 403)

    def test_portal_address_rule_applies(self):
        user = _user()
        with self.settings(PORTAL_ALLOWED_IPS=['203.0.113.0/24']):
            self.assertEqual(self._auth(STAFF_CHANNEL, admin_id=user.admin_id).status_code, 403)

    def test_page_is_given_its_channels(self):
        user = _user()
        session = self.client.session
        session['admin_id'] = user.admin_id
        session.save()
        page = self.client.get('/admin-portal/dashboard/')
        self.assertContains(page, 'data-channels="live-public private-live-staff"')
        self.assertContains(page, 'pusher.min.js')


class NoPusherTests(TestCase):

    def test_without_keys_pages_still_load_and_nothing_is_sent(self):
        with self.settings(PUSHER_ENABLED=False), mock.patch.object(live, '_sender') as sender:
            live.broadcast({'books'})
        sender.submit.assert_not_called()
