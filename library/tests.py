"""Smoke tests.

Not a full suite -- this is the thin layer that catches the class of breakage
that actually happens while a system this size is being changed: a page that
stopped rendering, a decorator that slid onto the wrong function, a helper whose
signature drifted, a guard that quietly stopped guarding.

Every test here runs in a second and needs no fixtures beyond what it creates.
Run before every deployment:

    python manage.py test library
"""

from datetime import timedelta
import json
from uuid import uuid4

from django.test import TestCase, Client
from django.utils import timezone

from .auth_utils import hash_password, password_length_error
from .models import (
    BLEBeacon, Book, BorrowingRule, Door, FloorPlan, LoginAttempt, Obstacle, Patron, Room,
    InventoryRecord, Obstacle, Shelf, ShelfLevel, Stairway, Transaction, User,
    Waypoint,
    WaypointConnection,
)


def _admin(email='smoke-admin@example.invalid', modules=''):
    return User.objects.create(
        fullname='Smoke Admin', email=email,
        password_hash=hash_password('SmokeTest123'),
        role='Admin', account_status='Active', modules=modules,
    )


def _signed_in(user):
    c = Client()
    s = c.session
    s['admin_id'] = user.admin_id
    s['admin_fullname'] = user.fullname
    s['admin_role'] = user.role
    s.save()
    return c


class PagesRenderTests(TestCase):
    """Every admin page answers 200 for someone allowed to see it.

    The cheapest possible regression net: a template that stops compiling, a
    context variable that stops existing, or a view that starts raising all show
    up here as a 500 instead of being found by hand.
    """

    def setUp(self):
        self.user = _admin(modules='transactions,books,logs,patrons,shelf,'
                                   'donations,inventory')
        self.client = _signed_in(self.user)

    def test_admin_pages_render(self):
        pages = [
            '/admin-portal/dashboard/',
            '/admin-portal/management/',
            '/admin-portal/transaction/',
            '/admin-portal/log-management/',
            '/admin-portal/manage-patron/',
            '/admin-portal/shelf-manager/',
            '/admin-portal/floorplan-management/',
            '/admin-portal/indoor-map/',
            '/admin-portal/reports/',
            '/admin-portal/users/',
            '/admin-portal/activity-logs/',
            '/admin-portal/announcement-management/',
            '/admin-portal/donation-management/',
            '/admin-portal/inventory/',
            '/admin-portal/borrowing-rules/',
        ]
        for url in pages:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_every_report_builds_and_exports(self):
        from .reports import REPORT_TYPES, build_report, render_report_pdf, render_report_excel
        start = timezone.localdate() - timedelta(days=30)
        end = timezone.localdate()
        for key, _label in REPORT_TYPES:
            with self.subTest(report=key):
                report = build_report(key, start, end)
                self.assertIn('columns', report)
                # Both exporters have broken on a new report key before.
                self.assertTrue(render_report_pdf(report).getvalue())
                self.assertTrue(render_report_excel(report).getvalue())


class AuthGuardTests(TestCase):
    """The guards that a misplaced decorator silently removes."""

    def setUp(self):
        self.user = _admin(modules='patrons')

    def test_admin_pages_reject_anonymous(self):
        anon = Client()
        for url in ['/admin-portal/dashboard/', '/admin-portal/shelf-manager/',
                    '/admin-portal/announcement-management/',
                    '/admin-portal/reports/']:
            with self.subTest(url=url):
                self.assertEqual(anon.get(url).status_code, 302)

    def test_credential_documents_are_not_public(self):
        """Scanned IDs were once served to anyone who had the URL."""
        anon = Client()
        self.assertEqual(anon.get('/media/credentials/anything.png').status_code, 404)
        self.assertEqual(anon.get('/patron-id/anything.png').status_code, 302)

    def test_credential_route_refuses_path_traversal(self):
        client = _signed_in(self.user)
        for probe in ['../../db.sqlite3', '../../../manage.py', 'nope.png']:
            with self.subTest(probe=probe):
                self.assertEqual(client.get('/patron-id/' + probe).status_code, 404)


class LoginSecurityTests(TestCase):
    """Throttling, session rotation, and the absence of an enumeration oracle."""

    def setUp(self):
        self.user = _admin()

    def test_lockout_after_repeated_failures(self):
        client = Client()
        for _ in range(LoginAttempt.MAX_FAILURES):
            client.post('/admin-portal/login/',
                        {'email': self.user.email, 'password': 'wrong'})
        response = client.post('/admin-portal/login/',
                               {'email': self.user.email, 'password': 'wrong'})
        self.assertContains(response, 'Too many failed')

    def test_correct_password_clears_the_run(self):
        client = Client()
        client.post('/admin-portal/login/', {'email': self.user.email, 'password': 'wrong'})
        client.post('/admin-portal/login/',
                    {'email': self.user.email, 'password': 'SmokeTest123'})
        self.assertFalse(
            LoginAttempt.objects.filter(scope='admin',
                                        identifier=self.user.email.lower()).exists())

    def test_session_id_rotates_on_login(self):
        client = Client()
        client.get('/admin-portal/login/')
        session = client.session
        session['planted'] = 'attacker'
        session.save()
        before = session.session_key

        client.post('/admin-portal/login/',
                    {'email': self.user.email, 'password': 'SmokeTest123'})

        self.assertNotEqual(before, client.session.session_key)
        self.assertNotIn('planted', client.session)
        self.assertEqual(client.session.get('admin_id'), self.user.admin_id)

    def test_unknown_and_known_addresses_answer_identically(self):
        client = Client()
        unknown = client.post('/admin-portal/login/',
                              {'email': 'nobody@example.invalid', 'password': 'x'})
        known = Client().post('/admin-portal/login/',
                              {'email': self.user.email, 'password': 'x'})
        self.assertContains(unknown, 'Invalid email or password')
        self.assertContains(known, 'Invalid email or password')
        self.assertNotContains(known, 'suspended')


class PasswordPolicyTests(TestCase):
    def test_weak_passwords_are_refused(self):
        for weak in ['Ab1', 'aaaaaaaaaa', '12345678', 'password1']:
            with self.subTest(password=weak):
                self.assertIsNotNone(password_length_error(weak))

    def test_a_reasonable_password_passes(self):
        self.assertIsNone(password_length_error('Kalachuchi77'))

    def test_the_current_password_cannot_be_reused(self):
        current = hash_password('Kalachuchi77')
        self.assertIsNotNone(
            password_length_error('Kalachuchi77', current_hash=current))


class BorrowingTests(TestCase):
    """The basket either commits whole or not at all, and a copy goes out once."""

    def setUp(self):
        self.admin = _admin(modules='transactions')
        self.client = _signed_in(self.admin)
        self.patron = Patron.objects.create(
            fullname='Smoke Patron', email='smoke-patron@example.invalid',
            password_hash=hash_password('SmokeTest123'),
            patron_type='Student', account_status='Active',
        )
        self.rule = BorrowingRule.current()
        self.rule.max_books_per_patron = 5
        self.rule.save()
        self.free = [Book.objects.create(title=f'Smoke Book {i}', author='Tester',
                                         status='Available') for i in range(2)]
        self.taken = Book.objects.create(title='Smoke Taken', author='Tester',
                                         status='Borrowed')

    def _post(self, kind, books):
        return self.client.post('/admin-portal/process-transaction/', {
            'transaction_type': kind,
            'patron_id': self.patron.patron_id,
            'book_ids': [b.book_id for b in books],
        }).json()

    def test_borrow_and_return_round_trip(self):
        self.assertTrue(self._post('Borrow', self.free)['success'])
        for book in self.free:
            book.refresh_from_db()
            self.assertEqual(book.status, 'Borrowed')

        self.assertTrue(self._post('Return', self.free)['success'])
        for book in self.free:
            book.refresh_from_db()
            self.assertEqual(book.status, 'Available')

    def test_a_failing_book_rolls_the_whole_basket_back(self):
        before = Transaction.objects.count()
        result = self._post('Borrow', [self.free[0], self.taken, self.free[1]])

        self.assertFalse(result['success'])
        for book in self.free:
            book.refresh_from_db()
            self.assertEqual(book.status, 'Available',
                             'a partial basket was committed')
        self.assertEqual(Transaction.objects.count(), before,
                         'transaction rows survived a rolled-back basket')

    def test_a_borrowed_copy_cannot_be_borrowed_again(self):
        self.assertFalse(self._post('Borrow', [self.taken])['success'])
        self.assertEqual(
            Transaction.objects.filter(book=self.taken,
                                       return_date__isnull=True).count(), 0)


class DeskModeTests(TestCase):
    """Arming the desk confines the browser to the log page."""

    def setUp(self):
        from .desk import DESK_SESSION_KEY
        self.user = _admin(modules='logs,patrons')
        self.client = _signed_in(self.user)
        session = self.client.session
        session[DESK_SESSION_KEY] = True
        session.save()

    def test_other_portal_pages_redirect_to_the_log(self):
        response = self.client.get('/admin-portal/manage-patron/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('log-management', response['Location'])


class IdleTimeoutTests(TestCase):
    def test_an_idle_session_is_closed(self):
        from .middleware import LAST_SEEN_KEY, STAFF_IDLE_SECONDS
        user = _admin()
        client = _signed_in(user)
        self.assertEqual(client.get('/admin-portal/dashboard/').status_code, 200)

        session = client.session
        session[LAST_SEEN_KEY] = timezone.now().timestamp() - (STAFF_IDLE_SECONDS + 60)
        session.save()

        self.assertEqual(client.get('/admin-portal/dashboard/').status_code, 302)
        self.assertNotIn('admin_id', client.session)


class ImportGuardTests(TestCase):
    def test_only_real_workbooks_are_parsed(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from .views import check_import_upload

        self.assertIsNotNone(check_import_upload(None))
        self.assertIsNotNone(check_import_upload(
            SimpleUploadedFile('x.csv', b'PK\x03\x04')))
        self.assertIsNotNone(check_import_upload(
            SimpleUploadedFile('x.xlsx', b'a,b,c')))


class ContentSecurityPolicyTests(TestCase):
    def test_the_header_is_sent(self):
        response = Client().get('/admin-portal/login/')
        policy = response.headers.get('Content-Security-Policy', '')
        self.assertIn("frame-ancestors 'none'", policy)
        self.assertIn("connect-src 'self'", policy)


class DailyMaintenanceTests(TestCase):
    """The two daily jobs run from one scheduled-task slot."""

    def test_both_jobs_run(self):
        from io import StringIO
        from django.core.management import call_command

        out = StringIO()
        call_command('daily_maintenance', dry_run=True, stdout=out)
        printed = out.getvalue()
        self.assertIn('Overdue loans', printed)
        self.assertIn('Open visits', printed)

    def test_one_job_failing_does_not_stop_the_other(self):
        from io import StringIO
        from unittest.mock import patch
        from django.core.management import call_command

        real = call_command

        def flaky(name, *args, **kwargs):
            if name == 'send_overdue_notifications':
                raise RuntimeError('simulated mail failure')
            return real(name, *args, **kwargs)

        out, err = StringIO(), StringIO()
        with patch('library.management.commands.daily_maintenance.call_command',
                   side_effect=flaky):
            with self.assertRaises(SystemExit):
                call_command('daily_maintenance', stdout=out, stderr=err)

        # The second job still ran, and the failure was reported rather than
        # swallowed -- a scheduler needs a non-zero exit to show a red task.
        self.assertIn('Open visits', out.getvalue())
        self.assertIn('simulated mail failure', err.getvalue())


class EmailTransportTests(TestCase):
    """Mail can leave over SMTP or over an HTTPS API without the code changing."""

    def test_send_goes_through_whatever_backend_is_configured(self):
        from django.core import mail
        from .emails import send_email

        with self.settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend'):
            mail.outbox = []
            self.assertTrue(send_email('Subject', 'Body', 'someone@example.invalid'))
            self.assertEqual(len(mail.outbox), 1)
            self.assertEqual(mail.outbox[0].subject, 'Subject')

    def test_a_send_failure_never_raises_into_the_caller(self):
        """Mail is best-effort by contract: a broken mail server must not stop a
        librarian processing a loan."""
        from .emails import send_email

        with self.settings(EMAIL_BACKEND='django.core.mail.backends.smtp.EmailBackend',
                           EMAIL_HOST='127.0.0.1', EMAIL_PORT=1, EMAIL_TIMEOUT=1):
            self.assertFalse(send_email('Subject', 'Body', 'someone@example.invalid'))


class CrossFloorRoutingTests(TestCase):
    """Routing to a shelf that is not on the floor the patron is standing on.

    The building here is deliberately the simplest one that can go wrong: two
    floors, a corridor of three waypoints on each, and two staircases per floor
    so there is a genuinely wrong answer available. The near stairs on floor 1
    are linked to floor 2; the far ones are not. A route that picks the far
    stairs, or that quietly stays on one floor, fails.

        floor 1:  W1 --- W2 --- W3          floor 2:  U1 --- U2 --- U3
                  |             |                     |             |
              NEAR STAIRS   FAR STAIRS            LANDING       (far, unlinked)
                  |
                  +--------- linked --------->  LANDING
    """

    def setUp(self):
        self.f1 = FloorPlan.objects.create(
            name='Ground', floor_number=1, is_active=True, pixels_per_meter=10)
        self.f2 = FloorPlan.objects.create(
            name='Upper', floor_number=2, is_active=True, pixels_per_meter=10)

        def corridor(plan, y):
            wps = [Waypoint.objects.create(floor_plan=plan, map_x=x, map_y=y,
                                           label='%s-%d' % (plan.name, x))
                   for x in (100, 300, 500)]
            for a, b in zip(wps, wps[1:]):
                WaypointConnection.objects.create(
                    waypoint_from=a, waypoint_to=b,
                    distance=abs(b.map_x - a.map_x))
            return wps

        self.lower = corridor(self.f1, 200)
        self.upper = corridor(self.f2, 200)

        # The book, upstairs at the far end of the corridor.
        room = Room.objects.create(floor_plan=self.f2, name='Upstairs Reading',
                                   map_x=500, map_y=200)
        self.shelf = Shelf.objects.create(room=room, name='Shelf U3',
                                          map_x=500, map_y=210)

        self.near = Stairway.objects.create(
            floor_plan=self.f1, kind='Stairs', name='North Stairs',
            geometry=[[80, 240], [120, 240], [120, 300], [80, 300]],
            map_x=100, map_y=270, direction='up', connects_to=self.f2)
        self.landing = Stairway.objects.create(
            floor_plan=self.f2, kind='Stairs', name='North Stairs',
            geometry=[[80, 240], [120, 240], [120, 300], [80, 300]],
            map_x=100, map_y=270, direction='down', connects_to=self.f1)
        # Real stairs, never linked -- the wrong answer that has to be avoided.
        self.far = Stairway.objects.create(
            floor_plan=self.f1, kind='Stairs', name='South Stairs',
            geometry=[[480, 240], [520, 240], [520, 300], [480, 300]],
            map_x=500, map_y=270, direction='up')

        self.client = Client()

    def _route(self, **params):
        params.setdefault('start_x', 100)
        params.setdefault('start_y', 200)
        r = self.client.get('/patron/navigation-route/', params)
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_same_floor_route_is_untouched(self):
        """The floor the stairs are on still routes as it always did."""
        room = Room.objects.create(floor_plan=self.f1, name='Ground Reading',
                                   map_x=500, map_y=200)
        shelf = Shelf.objects.create(room=room, name='Shelf G3',
                                     map_x=500, map_y=210)
        data = self._route(target_shelf_id=shelf.shelf_id,
                           floor=self.f1.floor_plan_id)

        self.assertTrue(data['success'])
        self.assertNotIn('cross_floor', data)
        # Three waypoints, no stairway spliced in, and the plain corridor length.
        self.assertEqual([p['waypoint_id'] for p in data['route']],
                         [w.waypoint_id for w in self.lower])
        self.assertEqual(data['distance'], 400)
        self.assertEqual(len(data['legs']), 1)

    def test_route_crosses_the_floor_and_reaches_the_shelf(self):
        data = self._route(target_shelf_id=self.shelf.shelf_id,
                           from_floor=self.f1.floor_plan_id)

        self.assertTrue(data['success'])
        self.assertTrue(data['cross_floor'])
        self.assertFalse(data['stairs_only'])
        self.assertEqual(data['leg'], 'full')
        self.assertEqual(data['floors_crossed'], 2)

        # One continuous path: down the graph, up the stairs, on to the shelf.
        floors = [l['floor_plan_id'] for l in data['legs']]
        self.assertEqual(floors, [self.f1.floor_plan_id, self.f2.floor_plan_id])
        self.assertEqual(data['legs'][-1]['points'][-1]['waypoint_id'],
                         self.upper[-1].waypoint_id)
        self.assertEqual(data['target_shelf']['shelf_id'], self.shelf.shelf_id)

    def test_it_uses_the_staircase_that_is_actually_linked(self):
        """Starting beside the unlinked far stairs must not tempt it.

        The far stairs are 400 units closer, so a router that picked the nearest
        staircase would choose them -- and they go nowhere.
        """
        data = self._route(target_shelf_id=self.shelf.shelf_id,
                           from_floor=self.f1.floor_plan_id,
                           start_x=500, start_y=200)

        self.assertTrue(data['success'])
        self.assertEqual(data['via_stairway']['stairway_id'], self.near.stairway_id)
        self.assertTrue(data['via_stairway']['linked'])

    def test_route_is_the_leg_for_the_floor_being_shown(self):
        """`route` follows the map, so the same request answers each floor."""
        below = self._route(target_shelf_id=self.shelf.shelf_id,
                            from_floor=self.f1.floor_plan_id)
        above = self._route(target_shelf_id=self.shelf.shelf_id,
                            from_floor=self.f2.floor_plan_id,
                            start_x=100, start_y=200)

        self.assertTrue(all(p['floor_plan_id'] == self.f1.floor_plan_id
                            for p in below['route']))
        self.assertTrue(all(p['floor_plan_id'] == self.f2.floor_plan_id
                            for p in above['route']))
        # Standing on the target floor is not a crossing at all.
        self.assertNotIn('cross_floor', above)

    def test_total_distance_includes_the_climb(self):
        """The walk is longer than the two floors laid end to end."""
        data = self._route(target_shelf_id=self.shelf.shelf_id,
                           from_floor=self.f1.floor_plan_id)
        flat = sum(l['distance'] for l in data['legs'])
        self.assertGreater(data['distance'], flat)
        self.assertEqual(data['leg_distance'], data['legs'][0]['distance'])

    def test_instruction_names_the_stairs_and_the_floor(self):
        data = self._route(target_shelf_id=self.shelf.shelf_id,
                           from_floor=self.f1.floor_plan_id)
        self.assertEqual(
            data['instruction'],
            'Take the stairs at North Stairs up to the %s, then follow the map '
            'from there.' % self.f2.floor_label)

    def test_unlinked_stairs_still_walk_the_patron_to_them(self):
        """With nothing joining the floors, guidance beats an error."""
        self.near.connects_to = None
        self.near.save(update_fields=['connects_to'])
        self.landing.connects_to = None
        self.landing.save(update_fields=['connects_to'])

        data = self._route(target_shelf_id=self.shelf.shelf_id,
                           from_floor=self.f1.floor_plan_id)

        self.assertTrue(data['success'])
        self.assertTrue(data['stairs_only'])
        self.assertEqual(data['leg'], 'to_stairs')
        self.assertEqual(data['via_stairway']['stairway_id'], self.near.stairway_id)
        self.assertFalse(data['via_stairway']['linked'])
        self.assertEqual(data['target_floor']['floor_plan_id'], self.f2.floor_plan_id)

    def test_linking_only_the_upward_flight_is_enough(self):
        """The landing forgot to name the floor below. Still one staircase.

        This is the ordinary setup mistake -- the flight up gets linked, the one
        coming down does not -- and it used to leave the two floors as separate
        islands with no route between them.
        """
        self.landing.connects_to = None
        self.landing.save(update_fields=['connects_to'])

        data = self._route(target_shelf_id=self.shelf.shelf_id,
                           from_floor=self.f1.floor_plan_id)

        self.assertTrue(data['success'])
        self.assertFalse(data['stairs_only'])
        self.assertEqual(data['floors_crossed'], 2)
        self.assertEqual(data['via_stairway']['stairway_id'], self.near.stairway_id)

    def test_a_distant_stairway_is_not_the_same_staircase(self):
        """Half a floor away is a different stairwell, not the other end."""
        self.landing.connects_to = None
        self.landing.map_x = 900          # nowhere near the flight below
        self.landing.save(update_fields=['connects_to', 'map_x'])

        data = self._route(target_shelf_id=self.shelf.shelf_id,
                           from_floor=self.f1.floor_plan_id)

        # No pairing, so no through route -- and the honest fallback instead.
        self.assertTrue(data['success'])
        self.assertTrue(data['stairs_only'])
        self.assertEqual(data['leg'], 'to_stairs')

    def test_no_stairs_at_all_says_so_plainly(self):
        Stairway.objects.all().delete()
        data = self._route(target_shelf_id=self.shelf.shelf_id,
                           from_floor=self.f1.floor_plan_id)

        self.assertFalse(data['success'])
        self.assertTrue(data['cross_floor'])
        self.assertIn('stairway', data['error'].lower())
        self.assertEqual(data['target_floor']['label'], self.f2.floor_label)

    def test_a_floor_with_stairs_but_no_corridor_explains_itself(self):
        """Half-drawn upstairs: stairs placed, waypoints not.

        The stairway is a node in the same graph as the waypoints, so a floor
        that has one and no waypoints looks populated to a careless check and
        then has nowhere for a route to begin. It has to answer, not raise.
        """
        Waypoint.objects.filter(floor_plan=self.f2).delete()

        data = self._route(target_shelf_id=self.shelf.shelf_id,
                           from_floor=self.f1.floor_plan_id)
        self.assertFalse(data['success'])
        self.assertIn('waypoint', data['error'].lower())

        # And from the other side: standing on the floor that has none.
        blank = self._route(target_shelf_id=self.shelf.shelf_id,
                            from_floor=self.f2.floor_plan_id)
        self.assertFalse(blank['success'])
        self.assertIn('waypoint', blank['error'].lower())

    def test_a_lift_is_preferred_over_stairs_when_it_is_nearer(self):
        """Stairs and lift are one graph, so the shorter walk simply wins."""
        Stairway.objects.create(
            floor_plan=self.f1, kind='Elevator', name='West Lift',
            geometry=[[480, 240], [520, 240], [520, 280], [480, 280]],
            map_x=500, map_y=260, direction='both', connects_to=self.f2)
        Stairway.objects.create(
            floor_plan=self.f2, kind='Elevator', name='West Lift',
            geometry=[[480, 240], [520, 240], [520, 280], [480, 280]],
            map_x=500, map_y=260, direction='both', connects_to=self.f1)

        data = self._route(target_shelf_id=self.shelf.shelf_id,
                           from_floor=self.f1.floor_plan_id,
                           start_x=500, start_y=200)

        self.assertTrue(data['success'])
        self.assertEqual(data['via_stairway']['kind'], 'Elevator')
        self.assertTrue(data['instruction'].startswith('Take the lift at West Lift'))


class QRLabelSheetTests(TestCase):
    """The printable QR label sheet.

    These labels get stuck onto physical books, so the two things that must not
    go wrong are the identity on each sticker and the size it prints at.
    """

    def setUp(self):
        self.user = _admin(modules='books')
        self.client = _signed_in(self.user)
        self.copies = [
            Book.objects.create(title='Noli Me Tangere', author='Jose Rizal',
                                qr_code=str(uuid4()), status='Available')
            for _ in range(3)
        ]
        self.single = Book.objects.create(title='Florante at Laura',
                                          author='Francisco Balagtas',
                                          qr_code=str(uuid4()), status='Available')

    def _sheet(self, ids, **params):
        params['ids'] = ','.join(str(i) for i in ids)
        return self.client.get('/admin-portal/book-qr-labels/', params)

    def test_sheet_is_a_pdf(self):
        r = self._sheet([b.book_id for b in self.copies])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r['Content-Type'], 'application/pdf')
        self.assertIn('attachment', r['Content-Disposition'])
        self.assertTrue(r.content.startswith(b'%PDF'))

    def test_every_copy_gets_its_own_code(self):
        """Three copies of one title are three different stickers."""
        codes = {b.qr_code for b in self.copies}
        self.assertEqual(len(codes), 3)

    def test_copy_numbers_count_the_whole_catalogue(self):
        """Printing two of three copies still reads 2 of 3, not 1 of 2.

        The numbers go onto physical books, so they have to agree with the
        shelf rather than with whatever subset was ticked in the table.
        """
        from .labels import copy_numbers
        subset = self.copies[1:]
        numbers = copy_numbers(subset)
        self.assertEqual(numbers[subset[0].book_id], (2, 3))
        self.assertEqual(numbers[subset[1].book_id], (3, 3))

    def test_a_single_copy_is_not_numbered(self):
        from .labels import copy_numbers
        self.assertNotIn(self.single.book_id, copy_numbers([self.single]))

    def test_a_book_with_no_qr_gets_one_minted(self):
        """Otherwise it is silently left off the sheet and nobody notices."""
        bare = Book.objects.create(title='Ibong Adarna', author='Anonymous',
                                   qr_code=None, status='Available')
        r = self._sheet([bare.book_id])
        self.assertEqual(r.status_code, 200)
        bare.refresh_from_db()
        self.assertTrue(bare.qr_code)

    def test_requested_size_is_honoured_and_clamped(self):
        from .labels import build_label_sheet, MAX_QR_MM, MIN_QR_MM
        _pdf, big = build_label_sheet(self.copies, qr_mm=9999)
        _pdf, small = build_label_sheet(self.copies, qr_mm=0.1)
        self.assertEqual(big['qr_mm'], MAX_QR_MM)
        self.assertEqual(small['qr_mm'], MIN_QR_MM)

    def test_a_smaller_label_fits_more_on_the_page(self):
        from .labels import build_label_sheet
        _pdf, small = build_label_sheet(self.copies, qr_mm=15)
        _pdf, large = build_label_sheet(self.copies, qr_mm=40)
        self.assertGreater(small['per_page'], large['per_page'])
        self.assertGreater(small['per_page'], 50)      # the grid is worth having

    def test_skip_pushes_the_first_label_down_the_sheet(self):
        from .labels import build_label_sheet
        _pdf, plain = build_label_sheet(self.copies, qr_mm=25)
        _pdf, offset = build_label_sheet(self.copies, qr_mm=25,
                                         skip=plain['per_page'])
        self.assertEqual(plain['pages'], 1)
        self.assertEqual(offset['pages'], 2)

    def test_no_selection_is_refused(self):
        r = self.client.get('/admin-portal/book-qr-labels/')
        self.assertFalse(r.json()['success'])

    def test_too_many_is_refused(self):
        from .views import MAX_LABELS_PER_SHEET
        r = self._sheet(range(1, MAX_LABELS_PER_SHEET + 5))
        self.assertFalse(r.json()['success'])

    def test_it_is_gated(self):
        stranger = Client()
        r = stranger.get('/admin-portal/book-qr-labels/',
                         {'ids': str(self.single.book_id)})
        self.assertNotEqual(r.status_code, 200)

    def test_import_reports_what_it_created(self):
        """So the label sheet can target exactly that delivery."""
        import openpyxl
        from io import BytesIO
        from django.core.files.uploadedfile import SimpleUploadedFile

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(['Title', 'Author', 'Quantity'])
        ws.append(['Mga Ibong Mandaragit', 'Amado V. Hernandez', 2])
        buf = BytesIO()
        wb.save(buf)

        upload = SimpleUploadedFile(
            'delivery.xlsx', buf.getvalue(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        r = self.client.post('/admin-portal/import-books/', {'excel_file': upload})
        data = r.json()
        self.assertTrue(data['success'], data)
        self.assertEqual(len(data['created_ids']), 2)
        self.assertFalse(data['created_truncated'])

        sheet = self._sheet(data['created_ids'])
        self.assertTrue(sheet.content.startswith(b'%PDF'))


class EveryAdminEndpointIsGuardedTests(TestCase):
    """No /admin-portal/ view is missing its sign-in guard.

    Inserting a new view directly above an existing one strands that view's
    decorator on the newcomer and leaves the original wide open, with nothing
    about the file looking wrong -- it has happened twice here. Neither reading
    the source nor eyeballing the decorators catches it, because both keep
    looking at a decorator that is still present, just attached to the wrong
    function. So this asks the two questions from outside instead: is every
    routed admin view decorated at all, and can a stranger get a 200 out of it?
    """

    # Reached before sign-in by design. Logging out is on the list because
    # requiring a session in order to end one is how a half-broken session
    # becomes a trap the user cannot get out of.
    PUBLIC = {
        'admin_login', 'admin_signin', 'admin_forgot_password', 'admin_logout',
        'staff_login', 'staff_forgot_password',
        'desk_sign', 'desk_sign_out', 'desk_scan', 'desk_unlock', 'desk_arm',
    }
    GUARDS = {
        'admin_login_required', 'admin_only_required', 'admin_module_required',
        'granted_module_required', 'admin_or_module_required',
        'patron_login_required',
    }

    def _admin_routes(self):
        """(route, name, view callable) for every admin-portal URL."""
        from library import urls as library_urls

        found = []
        for pattern in library_urls.urlpatterns:
            route = str(getattr(pattern, 'pattern', ''))
            name = getattr(pattern, 'name', None)
            if not route.startswith('admin-portal/') or name in self.PUBLIC:
                continue
            found.append((route, name, getattr(pattern, 'callback', None)))
        return found

    def test_every_admin_view_carries_a_guard(self):
        """Checked on the decorated object, so a stranded decorator shows up."""
        import ast
        from pathlib import Path

        here = Path(__file__).resolve().parent
        decorated = {}
        for module in ('views.py', 'desk.py', 'chat.py'):
            tree = ast.parse((here / module).read_text(encoding='utf-8'))
            for node in tree.body:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                # A view may guard itself by decorating an inner function and
                # calling it -- close_open_visits_now does exactly that -- so
                # decorators anywhere inside the view count as the view's own.
                names = set()
                for inner in ast.walk(node):
                    if not isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    for dec in inner.decorator_list:
                        target = dec.func if isinstance(dec, ast.Call) else dec
                        names.add(getattr(target, 'id', None)
                                  or getattr(target, 'attr', None))
                decorated[node.name] = names

        routes = self._admin_routes()
        self.assertGreater(len(routes), 30, 'the sweep found almost no URLs')

        unguarded = [
            '%s -> %s()' % (route, view.__name__)
            for route, _name, view in routes
            if view is not None
            and view.__name__ in decorated
            and not (decorated[view.__name__] & self.GUARDS)
        ]
        self.assertEqual(unguarded, [], 'no sign-in guard: %s' % unguarded)

    def test_a_stranger_gets_no_admin_page(self):
        anonymous = Client()
        open_doors = []
        for route, name, _view in self._admin_routes():
            if '<' in route:
                continue          # needs an id; a 404 would prove nothing
            url = '/' + route
            for response in (anonymous.get(url), anonymous.post(url, {})):
                if response.status_code == 200:
                    open_doors.append('%s (%s)' % (url, name))
                    break
        self.assertEqual(open_doors, [],
                         'reachable without signing in: %s' % open_doors)


class LabelPickerTests(TestCase):
    """The picker's feed, and printing in shelf order.

    Two shelves, two levels each, plus books left unplaced -- the smallest
    library where "sorted by where it lives" can be wrong.
    """

    def setUp(self):
        self.user = _admin(modules='books')
        self.client = _signed_in(self.user)

        plan = FloorPlan.objects.create(name='Ground', floor_number=1, is_active=True)
        room = Room.objects.create(floor_plan=plan, name='Main', map_x=10, map_y=10)
        self.shelf_a = Shelf.objects.create(room=room, name='Shelf A', map_x=1, map_y=1)
        self.shelf_b = Shelf.objects.create(room=room, name='Shelf B', map_x=2, map_y=2)
        self.a1 = ShelfLevel.objects.create(shelf=self.shelf_a, level_number=1,
                                            category='Fiction')
        self.a2 = ShelfLevel.objects.create(shelf=self.shelf_a, level_number=2,
                                            category='Filipiniana')
        self.b1 = ShelfLevel.objects.create(shelf=self.shelf_b, level_number=1,
                                            category='Reference')

        def book(title, level):
            return Book.objects.create(title=title, author='A. Writer',
                                       qr_code=str(uuid4()), status='Available',
                                       shelf_level=level)

        # Deliberately created out of shelf order.
        self.unplaced = book('Zebra Book', None)
        self.b1_book = book('Beta', self.b1)
        self.a2_book = book('Alpha', self.a2)
        self.a1_two = book('Second', self.a1)
        self.a1_one = book('First', self.a1)

    def test_the_feed_reports_where_each_book_lives(self):
        data = self.client.get('/admin-portal/book-label-picker/').json()
        self.assertTrue(data['success'])
        self.assertEqual(len(data['books']), 5)
        self.assertFalse(data['truncated'])
        self.assertEqual(data['max_per_sheet'], 500)

        by_id = {b['book_id']: b for b in data['books']}
        placed = by_id[self.a2_book.book_id]
        self.assertEqual(placed['location'], 'Shelf A · L2')
        self.assertEqual(placed['shelf_name'], 'Shelf A')
        self.assertEqual(placed['level_number'], 2)
        self.assertEqual(placed['level_category'], 'Filipiniana')

        # An unplaced book says so rather than inventing a location.
        self.assertEqual(by_id[self.unplaced.book_id]['location'], '')
        self.assertIsNone(by_id[self.unplaced.book_id]['level_id'])

    def test_the_feed_is_gated(self):
        r = Client().get('/admin-portal/book-label-picker/')
        self.assertNotEqual(r.status_code, 200)

    def test_printing_order_walks_the_shelves(self):
        """Shelf, then level, then title -- with the unplaced ones last."""
        from .labels import sort_for_printing

        ordered = sort_for_printing([
            self.unplaced, self.b1_book, self.a2_book, self.a1_two, self.a1_one])
        self.assertEqual([b.title for b in ordered],
                         ['First', 'Second', 'Alpha', 'Beta', 'Zebra Book'])

    def test_the_sheet_is_returned_in_that_order(self):
        ids = ','.join(str(b.book_id) for b in
                       [self.unplaced, self.b1_book, self.a1_one])
        r = self.client.get('/admin-portal/book-qr-labels/', {'ids': ids})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.content.startswith(b'%PDF'))

    def test_sort_by_id_is_still_available(self):
        from .labels import sort_for_printing
        by_shelf = sort_for_printing([self.unplaced, self.a1_one])
        self.assertEqual(by_shelf[0].title, 'First')     # shelf order
        ids = ','.join(str(b.book_id) for b in [self.unplaced, self.a1_one])
        r = self.client.get('/admin-portal/book-qr-labels/',
                            {'ids': ids, 'sort': 'id'})
        self.assertTrue(r.content.startswith(b'%PDF'))

    def test_groups_do_not_get_their_own_page(self):
        """Packed tight: five books across three shelf levels is still one page.

        This is the decision the sheet is built on -- a group boundary must not
        cost a page, or a twenty-shelf job becomes twenty sheets of mostly
        blank paper.
        """
        from .labels import build_label_sheet, sort_for_printing

        books = sort_for_printing(list(Book.objects.select_related(
            'shelf_level', 'shelf_level__shelf').all()))
        _pdf, layout = build_label_sheet(books, qr_mm=25,
                                         show={'title': True, 'location': True})
        self.assertEqual(layout['pages'], 1)
        self.assertGreater(layout['per_page'], 5)

    def test_location_reads_as_shelf_and_level(self):
        from .labels import location_of
        self.assertEqual(location_of(self.a1_one), 'Shelf A · L1')
        self.assertEqual(location_of(self.unplaced), '')


class ShelfShapeTests(TestCase):
    """Shelves that are not rectangles, tops, and tables that hold books."""

    def setUp(self):
        self.user = _admin(modules='shelf')
        self.client = _signed_in(self.user)
        self.plan = FloorPlan.objects.create(name='Ground', floor_number=1,
                                             is_active=True)
        self.room = Room.objects.create(floor_plan=self.plan, name='Main',
                                        map_x=100, map_y=100)

    def test_a_rectangular_shelf_still_derives_its_corners(self):
        """Everything drawn before geometry existed must not move."""
        shelf = Shelf.objects.create(room=self.room, name='Shelf A',
                                     map_x=100, map_y=100,
                                     width=40, depth=10, rotation=0)
        self.assertEqual(shelf.footprint(),
                         [[80.0, 95.0], [120.0, 95.0], [120.0, 105.0], [80.0, 105.0]])

    def test_rotation_still_turns_the_rectangle(self):
        shelf = Shelf.objects.create(room=self.room, name='Turned',
                                     map_x=0, map_y=0, width=20, depth=10,
                                     rotation=90)
        xs = [p[0] for p in shelf.footprint()]
        ys = [p[1] for p in shelf.footprint()]
        # A quarter turn swaps the run and the depth.
        self.assertAlmostEqual(max(xs) - min(xs), 10.0, places=4)
        self.assertAlmostEqual(max(ys) - min(ys), 20.0, places=4)

    def test_a_traced_shelf_keeps_the_shape_it_was_drawn_as(self):
        """A corner unit is a polygon, not a rectangle with a rotation."""
        chevron = [[0, 0], [60, 0], [60, 12], [12, 12], [12, 60], [0, 60]]
        shelf = Shelf.objects.create(room=self.room, name='Corner',
                                     map_x=20, map_y=20, geometry=chevron)
        self.assertEqual(shelf.footprint(), [[float(x), float(y)] for x, y in chevron])
        self.assertEqual(len(shelf.footprint()), 6)

    def test_an_unplaced_shelf_has_no_footprint(self):
        shelf = Shelf.objects.create(room=self.room, name='Unplaced')
        self.assertEqual(shelf.footprint(), [])

    def test_drawing_a_shelf_saves_the_outline_and_centres_on_it(self):
        chevron = [[0, 0], [60, 0], [60, 12], [12, 12], [12, 60], [0, 60]]
        r = self.client.post('/admin-portal/add-shelf/', {
            'room_id': self.room.room_id, 'name': 'Corner Unit',
            'kind': 'Shelf', 'geometry': json.dumps(chevron),
        })
        data = r.json()
        self.assertTrue(data['success'], data)
        shelf = Shelf.objects.get(shelf_id=data['shelf_id'])
        self.assertEqual(len(shelf.geometry), 6)
        # Positioned by its own outline, not left at the default 0,0 -- the
        # label and the route target both hang off map_x/map_y.
        self.assertIsNotNone(shelf.map_x)
        self.assertGreater(shelf.map_x, 0)
        self.assertGreater(shelf.map_y, 0)

    def test_the_top_of_a_case_is_not_a_numbered_level(self):
        shelf = Shelf.objects.create(room=self.room, name='Shelf A',
                                     map_x=10, map_y=10)
        ShelfLevel.objects.create(shelf=shelf, level_number=1)
        r = self.client.post('/admin-portal/add-shelf-level/',
                             {'shelf_id': shelf.shelf_id, 'is_top': '1'})
        self.assertTrue(r.json()['success'], r.json())
        top = ShelfLevel.objects.get(shelf=shelf, is_top=True)
        self.assertEqual(top.label, 'Top')
        self.assertEqual(top.short_label, 'Top')
        # It sorts above every real shelf, which is where it physically is.
        self.assertGreater(top.level_number, 1)

    def test_a_case_gets_only_one_top(self):
        shelf = Shelf.objects.create(room=self.room, name='Shelf A',
                                     map_x=10, map_y=10)
        self.client.post('/admin-portal/add-shelf-level/',
                         {'shelf_id': shelf.shelf_id, 'is_top': '1'})
        again = self.client.post('/admin-portal/add-shelf-level/',
                                 {'shelf_id': shelf.shelf_id, 'is_top': '1'})
        self.assertFalse(again.json()['success'])
        self.assertEqual(ShelfLevel.objects.filter(shelf=shelf, is_top=True).count(), 1)

    def test_a_label_says_top_rather_than_a_level_number(self):
        """L5 would send someone looking inside for a book sitting on top."""
        from .labels import location_of

        shelf = Shelf.objects.create(room=self.room, name='Shelf A',
                                     map_x=10, map_y=10)
        inside = ShelfLevel.objects.create(shelf=shelf, level_number=2)
        on_top = ShelfLevel.objects.create(shelf=shelf, level_number=5, is_top=True)

        a = Book.objects.create(title='Inside', author='X', qr_code=str(uuid4()),
                                shelf_level=inside)
        b = Book.objects.create(title='On top', author='X', qr_code=str(uuid4()),
                                shelf_level=on_top)
        self.assertEqual(location_of(a), 'Shelf A · L2')
        self.assertEqual(location_of(b), 'Shelf A · Top')

    def test_furniture_that_holds_books_is_saved_as_a_shelf(self):
        """A book points at a ShelfLevel, so a table holding one must be a Shelf."""
        top = [[10, 10], [90, 10], [90, 50], [10, 50]]
        r = self.client.post('/admin-portal/add-obstacle/', {
            'floor_plan_id': self.plan.floor_plan_id, 'kind': 'Table',
            'name': 'New Arrivals', 'geometry': json.dumps(top),
            'holds_books': '1',
        })
        data = r.json()
        self.assertTrue(data['success'], data)
        self.assertTrue(data['as_shelf'])
        self.assertEqual(Obstacle.objects.count(), 0)

        shelf = Shelf.objects.get(shelf_id=data['shelf_id'])
        self.assertEqual(shelf.kind, 'Table')
        self.assertEqual(shelf.name, 'New Arrivals')
        self.assertEqual(len(shelf.geometry), 4)
        # One surface, and it is the top of it.
        level = ShelfLevel.objects.get(shelf=shelf)
        self.assertTrue(level.is_top)
        self.assertEqual(shelf.label, 'New Arrivals (table)')

    def test_furniture_that_holds_nothing_stays_an_obstacle(self):
        top = [[10, 10], [90, 10], [90, 50], [10, 50]]
        r = self.client.post('/admin-portal/add-obstacle/', {
            'floor_plan_id': self.plan.floor_plan_id, 'kind': 'Table',
            'name': 'Reading Table', 'geometry': json.dumps(top),
        })
        self.assertTrue(r.json()['success'])
        self.assertEqual(Obstacle.objects.count(), 1)
        self.assertEqual(Shelf.objects.filter(kind='Table').count(), 0)

    def test_a_book_table_needs_a_room_to_live_in(self):
        """It is catalogued inside one, so it cannot be saved without one."""
        Room.objects.all().delete()
        r = self.client.post('/admin-portal/add-obstacle/', {
            'floor_plan_id': self.plan.floor_plan_id, 'kind': 'Table',
            'geometry': json.dumps([[1, 1], [9, 1], [9, 5], [1, 5]]),
            'holds_books': '1',
        })
        data = r.json()
        self.assertFalse(data['success'])
        self.assertIn('room', data['error'].lower())
        self.assertEqual(Shelf.objects.count(), 0)

    def test_the_map_payload_carries_a_resolved_outline(self):
        """So no map has to know the difference between the two shapes."""
        Shelf.objects.create(room=self.room, name='Rect', map_x=50, map_y=50)
        Shelf.objects.create(room=self.room, name='Traced', map_x=50, map_y=50,
                             geometry=[[0, 0], [10, 0], [10, 10]])
        data = self.client.get('/admin-portal/map-data/').json()
        shelves = {s['name']: s for s in data.get('shelves', [])}
        self.assertGreaterEqual(len(shelves['Rect']['footprint']), 4)
        self.assertEqual(len(shelves['Traced']['footprint']), 3)
        self.assertIsNone(shelves['Rect']['geometry'])


class ShelfPositionTests(TestCase):
    """Underneath, columns, and units that are off the floor."""

    def setUp(self):
        self.user = _admin(modules='shelf')
        self.client = _signed_in(self.user)
        self.plan = FloorPlan.objects.create(name='Ground', floor_number=1,
                                             is_active=True)
        self.room = Room.objects.create(floor_plan=self.plan, name='Main',
                                        map_x=100, map_y=100)
        self.shelf = Shelf.objects.create(room=self.room, name='Shelf A',
                                          map_x=10, map_y=10)

    def _level(self, **params):
        params.setdefault('shelf_id', self.shelf.shelf_id)
        return self.client.post('/admin-portal/add-shelf-level/', params).json()

    def test_underneath_is_not_a_numbered_shelf(self):
        r = self._level(is_under='1')
        self.assertTrue(r['success'], r)
        under = ShelfLevel.objects.get(shelf=self.shelf, is_under=True)
        self.assertEqual(under.label, 'Underneath')
        self.assertEqual(under.short_label, 'Under')
        # Below everything, so it sorts first.
        self.assertEqual(under.level_number, 0)

    def test_a_level_cannot_be_both_top_and_underneath(self):
        r = self._level(is_top='1', is_under='1')
        self.assertFalse(r['success'])
        self.assertEqual(ShelfLevel.objects.count(), 0)

    def test_a_column_addresses_a_bay(self):
        r = self._level(level_number='2', column_number='3')
        self.assertTrue(r['success'], r)
        lv = ShelfLevel.objects.get(shelf_level_id=r['shelf_level_id'])
        self.assertEqual(lv.label, 'Level 2, Column 3')
        self.assertEqual(lv.short_label, 'L2C3')

    def test_a_level_without_a_column_is_unchanged(self):
        r = self._level(level_number='2')
        lv = ShelfLevel.objects.get(shelf_level_id=r['shelf_level_id'])
        self.assertIsNone(lv.column_number)
        self.assertEqual(lv.label, 'Level 2')
        self.assertEqual(lv.short_label, 'L2')

    def test_a_bad_column_is_refused_rather_than_guessed(self):
        self.assertFalse(self._level(level_number='1', column_number='abc')['success'])

    def test_labels_walk_a_case_bottom_to_top_and_left_to_right(self):
        from .labels import sort_for_printing

        under = ShelfLevel.objects.create(shelf=self.shelf, level_number=0, is_under=True)
        l1c2 = ShelfLevel.objects.create(shelf=self.shelf, level_number=1, column_number=2)
        l1c1 = ShelfLevel.objects.create(shelf=self.shelf, level_number=1, column_number=1)
        top = ShelfLevel.objects.create(shelf=self.shelf, level_number=9, is_top=True)

        def book(name, level):
            return Book.objects.create(title=name, author='X',
                                       qr_code=str(uuid4()), shelf_level=level)
        made = [book('D top', top), book('C l1c2', l1c2),
                book('A under', under), book('B l1c1', l1c1)]
        self.assertEqual([b.title for b in sort_for_printing(made)],
                         ['A under', 'B l1c1', 'C l1c2', 'D top'])

    def test_an_elevated_unit_says_so_and_is_flagged(self):
        wall = Shelf.objects.create(room=self.room, name='Corner Ledge',
                                    map_x=5, map_y=5, mount='Ceiling',
                                    mount_height_m=2.4)
        floor = Shelf.objects.create(room=self.room, name='Stack 1',
                                     map_x=6, map_y=6)
        self.assertTrue(wall.is_elevated)
        self.assertFalse(floor.is_elevated)
        self.assertEqual(wall.label, 'Corner Ledge (ceiling-mounted)')
        self.assertEqual(floor.label, 'Stack 1')

    def test_drawing_an_elevated_shelf_keeps_its_mount(self):
        r = self.client.post('/admin-portal/add-shelf/', {
            'room_id': self.room.room_id, 'name': 'Ceiling Corner',
            'mount': 'Ceiling', 'mount_height_m': '2.4',
            'geometry': json.dumps([[0, 0], [40, 0], [40, 20]]),
        }).json()
        self.assertTrue(r['success'], r)
        shelf = Shelf.objects.get(shelf_id=r['shelf_id'])
        self.assertEqual(shelf.mount, 'Ceiling')
        self.assertEqual(shelf.mount_height_m, 2.4)

    def test_an_impossible_mounting_height_is_refused(self):
        r = self.client.post('/admin-portal/add-shelf/', {
            'room_id': self.room.room_id, 'name': 'Too High',
            'mount': 'Wall', 'mount_height_m': '95',
        }).json()
        self.assertFalse(r['success'])
        self.assertIn('metres', r['error'])

    def test_the_map_payload_flags_an_elevated_unit(self):
        """So a map can draw it hollow instead of as floor furniture."""
        Shelf.objects.create(room=self.room, name='Hung', map_x=5, map_y=5,
                             mount='Ceiling')
        data = self.client.get('/admin-portal/map-data/').json()
        by_name = {s['name']: s for s in data.get('shelves', [])}
        self.assertTrue(by_name['Hung']['elevated'])
        self.assertFalse(by_name['Shelf A']['elevated'])


class AuditSweepTests(TestCase):
    """Scanning a room without saying which shelf you are standing at."""

    def setUp(self):
        self.user = _admin(modules='inventory')
        self.client = _signed_in(self.user)
        plan = FloorPlan.objects.create(name='G', floor_number=1, is_active=True)
        room = Room.objects.create(floor_plan=plan, name='Main', map_x=1, map_y=1)
        self.a = Shelf.objects.create(room=room, name='Shelf A', map_x=1, map_y=1)
        self.b = Shelf.objects.create(room=room, name='Shelf B', map_x=2, map_y=2)
        self.la = ShelfLevel.objects.create(shelf=self.a, level_number=1)
        self.lb = ShelfLevel.objects.create(shelf=self.b, level_number=1)

        self.labels = {}
        for shelf_key, level, n in (('a', self.la, 3), ('b', self.lb, 2)):
            for i in range(n):
                book = Book.objects.create(title=f'{shelf_key}{i}', author='X',
                                           qr_code=str(uuid4()), shelf_level=level)
                rec = InventoryRecord.objects.create(book=book, status='In Stock',
                                                     condition='Good',
                                                     qr_label=f'{shelf_key.upper()}-{i}')
                self.labels.setdefault(shelf_key, []).append(rec.qr_label)

    def _sweep(self, scanned):
        return self.client.post('/admin-portal/inventory/audit/progress/',
                                {'scanned': scanned}).json()

    def test_scans_sort_themselves_into_shelves(self):
        """No shelf is chosen up front; each label says where it belongs."""
        data = self._sweep([self.labels['a'][0], self.labels['b'][0],
                            self.labels['a'][1]])
        self.assertTrue(data['success'])
        groups = {g['shelf_name']: g for g in data['groups']}
        self.assertEqual(groups['Shelf A']['found_count'], 2)
        self.assertEqual(groups['Shelf A']['expected_count'], 3)
        self.assertEqual(groups['Shelf B']['found_count'], 1)
        self.assertEqual(groups['Shelf B']['expected_count'], 2)

    def test_progress_says_when_a_shelf_is_done(self):
        data = self._sweep(self.labels['a'])
        shelf_a = data['groups'][0]
        self.assertEqual(shelf_a['found_count'], 3)
        self.assertEqual(shelf_a['percent'], 100)
        self.assertTrue(shelf_a['complete'])

    def test_a_half_counted_shelf_is_not_complete(self):
        data = self._sweep(self.labels['a'][:1])
        self.assertFalse(data['groups'][0]['complete'])
        self.assertEqual(data['groups'][0]['percent'], 33)

    def test_duplicate_scans_count_once(self):
        """Waving the scanner twice at one book must not invent a copy."""
        one = self.labels['a'][0]
        data = self._sweep([one, one, one])
        self.assertEqual(data['total_scanned'], 1)
        self.assertEqual(data['groups'][0]['found_count'], 1)

    def test_an_unknown_label_is_reported_not_dropped(self):
        data = self._sweep([self.labels['a'][0], 'NOT-A-REAL-LABEL'])
        self.assertEqual(data['unknown'], ['NOT-A-REAL-LABEL'])
        self.assertEqual(len(data['groups']), 1)

    def test_a_copy_with_no_shelf_is_kept_separate(self):
        loose = Book.objects.create(title='Loose', author='X', qr_code=str(uuid4()))
        InventoryRecord.objects.create(book=loose, status='In Stock',
                                       condition='Good', qr_label='LOOSE-1')
        data = self._sweep(['LOOSE-1'])
        self.assertEqual(len(data['unshelved']), 1)
        self.assertEqual(data['groups'], [])

    def test_it_is_gated(self):
        r = Client().post('/admin-portal/inventory/audit/progress/', {'scanned': ['x']})
        self.assertNotEqual(r.status_code, 200)


class BulkReshelvingTests(TestCase):
    """A trolley of returns goes back in one action, not thirty."""

    def setUp(self):
        self.user = _admin(modules='books')
        self.client = _signed_in(self.user)
        self.books = [Book.objects.create(title=f'B{i}', author='X',
                                          qr_code=str(uuid4()),
                                          status='For Reshelving')
                      for i in range(4)]

    def _post(self, ids):
        return self.client.post('/admin-portal/reshelving/',
                                {'book_id': [str(i) for i in ids]}).json()

    def test_many_at_once(self):
        ids = [b.book_id for b in self.books[:3]]
        data = self._post(ids)
        self.assertTrue(data['success'])
        self.assertEqual(data['shelved_count'], 3)
        self.assertEqual(data['remaining'], 1)
        self.assertEqual(Book.objects.filter(status='Available').count(), 3)

    def test_one_still_works_unchanged(self):
        data = self._post([self.books[0].book_id])
        self.assertTrue(data['success'])
        self.assertEqual(data['shelved_count'], 1)
        self.assertEqual(data['title'], 'B0')

    def test_a_book_someone_else_already_shelved_is_skipped_not_fatal(self):
        """Two people working the trolley must not error each other out."""
        self.books[0].status = 'Available'
        self.books[0].save(update_fields=['status'])
        data = self._post([self.books[0].book_id, self.books[1].book_id])
        self.assertTrue(data['success'])
        self.assertEqual(data['shelved_count'], 1)
        self.assertEqual(data['skipped_count'], 1)

    def test_nothing_selected_is_refused(self):
        self.assertFalse(self.client.post('/admin-portal/reshelving/', {}).json()['success'])

    def test_all_already_shelved_reports_it(self):
        Book.objects.update(status='Available')
        data = self._post([b.book_id for b in self.books])
        self.assertFalse(data['success'])
        self.assertIn('already', data['error'].lower())


class DoorPlacementTests(TestCase):
    """Where a door lands, and how wide it is once it is there."""

    def setUp(self):
        self.user = _admin(modules='shelf')
        self.client = _signed_in(self.user)
        self.plan = FloorPlan.objects.create(name='Ground', floor_number=1,
                                             is_active=True)
        # A wide, short room, so "nearest wall" differs per click rather than
        # every point being equidistant from two of them.
        self.room = Room.objects.create(
            floor_plan=self.plan, name='Hallway', map_x=0, map_y=0,
            geometry=[[100, 100], [500, 100], [500, 200], [100, 200]])

    def _add(self, x, y, **extra):
        params = {'room_id': self.room.room_id, 'map_x': x, 'map_y': y}
        params.update(extra)
        return self.client.post('/admin-portal/add-door/', params).json()

    def _edit(self, door_id, **params):
        params['door_id'] = door_id
        return self.client.post('/admin-portal/edit-door/', params).json()

    def test_the_door_lands_on_the_wall_nearest_the_click(self):
        """Each click belongs to one wall, and it is the one clicked."""
        for label, (cx, cy), expect in (
                ('top', (300, 110), (300, 100)),
                ('bottom', (300, 190), (300, 200)),
                ('left', (110, 150), (100, 150)),
                ('right', (490, 150), (500, 150))):
            r = self._add(cx, cy)
            self.assertTrue(r['success'], (label, r))
            self.assertAlmostEqual(r['door']['x'], expect[0], places=3, msg=label)
            self.assertAlmostEqual(r['door']['y'], expect[1], places=3, msg=label)

    def test_a_new_door_takes_the_default_width(self):
        r = self._add(300, 110)
        self.assertEqual(r['door']['width'], 28)

    def test_width_can_be_changed_after_placement(self):
        """The gap this closes: before edit_door existed, 28 was for ever."""
        door_id = self._add(300, 110)['door']['door_id']
        r = self._edit(door_id, width=90)
        self.assertTrue(r['success'], r)
        self.assertEqual(r['door']['width'], 90)
        self.assertEqual(Door.objects.get(pk=door_id).width, 90)

    def test_an_impossible_width_is_refused(self):
        door_id = self._add(300, 110)['door']['door_id']
        for bad in (0, 5, 401, 'wide'):
            r = self._edit(door_id, width=bad)
            self.assertFalse(r['success'], bad)
        self.assertEqual(Door.objects.get(pk=door_id).width, 28)

    def test_resizing_from_one_end_leaves_the_other_end_alone(self):
        """Dragging an end grip sends a new width AND a new centre together.

        The far jamb is what the Administrator is holding still, so it is the
        thing that must not move; the centre shifting by half is the correct
        consequence, not a bug.
        """
        door_id = self._add(300, 110)['door']['door_id']
        door = Door.objects.get(pk=door_id)
        self.assertAlmostEqual(door.rotation, 0.0, places=3)   # along the top wall
        left_end = door.map_x - door.width / 2                 # the end held still

        new_width = 100
        r = self._edit(door_id, width=new_width,
                       map_x=left_end + new_width / 2, map_y=door.map_y)
        self.assertTrue(r['success'], r)

        door.refresh_from_db()
        self.assertEqual(door.width, new_width)
        self.assertAlmostEqual(door.map_x - door.width / 2, left_end, places=3)
        # And it is still on the wall it was cut into.
        self.assertAlmostEqual(door.map_y, 100.0, places=3)

    def test_a_moved_door_is_re_snapped_to_the_wall(self):
        """A centre sent from a drag is never trusted as final."""
        door_id = self._add(300, 110)['door']['door_id']
        r = self._edit(door_id, width=40, map_x=320, map_y=137)
        self.assertTrue(r['success'], r)
        door = Door.objects.get(pk=door_id)
        self.assertAlmostEqual(door.map_y, 100.0, places=3)    # pulled back onto the wall
        self.assertAlmostEqual(door.map_x, 320.0, places=3)

    def test_editing_nothing_is_refused_rather_than_silently_accepted(self):
        door_id = self._add(300, 110)['door']['door_id']
        r = self._edit(door_id)
        self.assertFalse(r['success'])


class WallCrossingTests(TestCase):
    """A connection asserts someone can walk straight between two points."""

    def setUp(self):
        self.user = _admin(modules='shelf')
        self.client = _signed_in(self.user)
        self.plan = FloorPlan.objects.create(name='Ground', floor_number=1,
                                             is_active=True, pixels_per_meter=100)
        # Two rooms sharing the wall x = 300.
        self.left = Room.objects.create(
            floor_plan=self.plan, name='Left', map_x=200, map_y=200,
            geometry=[[100, 100], [300, 100], [300, 300], [100, 300]])
        self.right = Room.objects.create(
            floor_plan=self.plan, name='Right', map_x=400, map_y=200,
            geometry=[[300, 100], [500, 100], [500, 300], [300, 300]])

    def _wp(self, x, y):
        return Waypoint.objects.create(floor_plan=self.plan, map_x=x, map_y=y)

    def _connect(self, a, b):
        return self.client.post('/admin-portal/add-waypoint-connection/', {
            'waypoint_from_id': a.waypoint_id, 'waypoint_to_id': b.waypoint_id}).json()

    def test_a_connection_through_a_solid_wall_is_refused(self):
        r = self._connect(self._wp(200, 200), self._wp(400, 200))
        self.assertFalse(r['success'])
        self.assertIn('wall', r['error'].lower())
        self.assertEqual(WaypointConnection.objects.count(), 0)

    def test_the_refusal_names_the_room_whose_wall_it_is(self):
        r = self._connect(self._wp(200, 200), self._wp(400, 200))
        self.assertTrue('Left' in r['error'] or 'Right' in r['error'], r['error'])

    def test_the_same_connection_is_allowed_once_a_door_is_there(self):
        """The whole point: a door is what makes a crossing legal."""
        Door.objects.create(room=self.left, map_x=300, map_y=200,
                            width=60, rotation=90)
        r = self._connect(self._wp(200, 200), self._wp(400, 200))
        self.assertTrue(r['success'], r)
        self.assertEqual(WaypointConnection.objects.count(), 1)

    def test_a_door_elsewhere_on_the_wall_does_not_help(self):
        """A doorway 200 units up the wall is not the one being walked through."""
        Door.objects.create(room=self.left, map_x=300, map_y=110,
                            width=20, rotation=90)
        r = self._connect(self._wp(200, 250), self._wp(400, 250))
        self.assertFalse(r['success'], r)

    def test_two_waypoints_in_one_room_connect_freely(self):
        r = self._connect(self._wp(150, 150), self._wp(250, 250))
        self.assertTrue(r['success'], r)

    def test_a_connection_running_along_a_wall_is_not_a_crossing(self):
        """Sitting on the wall, or tracking it, is not passing through it."""
        r = self._connect(self._wp(300, 120), self._wp(300, 280))
        self.assertTrue(r['success'], r)

    def test_floors_are_joined_by_stairs_not_by_connections(self):
        upstairs = FloorPlan.objects.create(name='First', floor_number=2)
        far = Waypoint.objects.create(floor_plan=upstairs, map_x=200, map_y=200)
        r = self._connect(self._wp(150, 150), far)
        self.assertFalse(r['success'])
        self.assertIn('floor', r['error'].lower())


class DoorResnapTests(TestCase):
    """Reshaping a room used to leave its doors floating in mid-air."""

    def setUp(self):
        self.user = _admin(modules='shelf')
        self.client = _signed_in(self.user)
        self.plan = FloorPlan.objects.create(name='Ground', floor_number=1,
                                             is_active=True, pixels_per_meter=100)
        self.room = Room.objects.create(
            floor_plan=self.plan, name='Hall', map_x=200, map_y=200,
            geometry=[[100, 100], [300, 100], [300, 300], [100, 300]])
        self.door = Door.objects.create(room=self.room, map_x=200, map_y=100,
                                        width=40, rotation=0)

    def _reshape(self, geometry):
        import json
        return self.client.post('/admin-portal/edit-room/', {
            'room_id': self.room.room_id,
            'geometry': json.dumps(geometry)}).json()

    def test_a_door_follows_the_wall_it_was_cut_into(self):
        # Push the top wall down from y=100 to y=160.
        r = self._reshape([[100, 160], [300, 160], [300, 300], [100, 300]])
        self.assertTrue(r['success'], r)
        self.door.refresh_from_db()
        self.assertAlmostEqual(self.door.map_y, 160.0, places=3)
        self.assertAlmostEqual(self.door.map_x, 200.0, places=3)

    def test_the_move_is_reported_rather_than_silent(self):
        """Nearest-edge is a guess, so it has to be visible to be correctable."""
        r = self._reshape([[100, 160], [300, 160], [300, 300], [100, 300]])
        self.assertEqual(len(r['doors_moved']), 1)
        self.assertEqual(r['doors_moved'][0]['door_id'], self.door.door_id)
        self.assertAlmostEqual(r['doors_moved'][0]['moved_by'], 60.0, places=1)

    def test_a_door_already_on_the_wall_is_left_alone(self):
        r = self._reshape([[100, 100], [300, 100], [300, 400], [100, 400]])
        self.assertEqual(r['doors_moved'], [])
        self.door.refresh_from_db()
        self.assertAlmostEqual(self.door.map_y, 100.0, places=3)

    def test_renaming_a_room_does_not_disturb_its_doors(self):
        r = self.client.post('/admin-portal/edit-room/', {
            'room_id': self.room.room_id, 'name': 'Lobby'}).json()
        self.assertTrue(r['success'], r)
        self.assertEqual(r.get('doors_moved'), [])


class FloorPlanReadinessTests(TestCase):
    """Everything this reports is something that otherwise fails silently."""

    def setUp(self):
        self.user = _admin(modules='shelf')
        self.client = _signed_in(self.user)
        self.plan = FloorPlan.objects.create(name='Ground', floor_number=1)

    def _check(self):
        return self.client.get('/admin-portal/floor-plan-readiness/',
                               {'floor_plan_id': self.plan.floor_plan_id}).json()

    def _texts(self, data):
        return [i['text'] for i in data['issues']]

    def test_a_bare_plan_reports_its_blockers(self):
        data = self._check()
        self.assertFalse(data['ready'])
        joined = ' | '.join(self._texts(data))
        self.assertIn('No scale set', joined)
        self.assertIn('No rooms drawn', joined)
        self.assertIn('No waypoints placed', joined)

    def test_walls_that_almost_touch_are_flagged(self):
        """A tenth of a unit apart looks joined at every zoom level."""
        self.plan.pixels_per_meter = 100
        self.plan.save()
        Room.objects.create(floor_plan=self.plan, name='A', map_x=0, map_y=0,
                            geometry=[[0, 0], [100, 0], [100, 100], [0, 100]])
        Room.objects.create(floor_plan=self.plan, name='B', map_x=0, map_y=0,
                            geometry=[[100.5, 0], [200, 0], [200, 100], [100.5, 100]])
        joined = ' | '.join(self._texts(self._check()))
        self.assertIn('almost touch', joined)

    def test_rooms_that_genuinely_touch_are_not_flagged(self):
        Room.objects.create(floor_plan=self.plan, name='A', map_x=0, map_y=0,
                            geometry=[[0, 0], [100, 0], [100, 100], [0, 100]])
        Room.objects.create(floor_plan=self.plan, name='B', map_x=0, map_y=0,
                            geometry=[[100, 0], [200, 0], [200, 100], [100, 100]])
        joined = ' | '.join(self._texts(self._check()))
        self.assertNotIn('almost touch', joined)

    def test_an_unreachable_room_is_named(self):
        Room.objects.create(floor_plan=self.plan, name='Store', map_x=0, map_y=0,
                            geometry=[[0, 0], [100, 0], [100, 100], [0, 100]])
        Room.objects.create(floor_plan=self.plan, name='Lobby', map_x=0, map_y=0,
                            geometry=[[200, 0], [300, 0], [300, 100], [200, 100]])
        Waypoint.objects.create(floor_plan=self.plan, map_x=250, map_y=50)
        joined = ' | '.join(self._texts(self._check()))
        self.assertIn('No waypoint inside "Store"', joined)
        self.assertNotIn('No waypoint inside "Lobby"', joined)

    def test_a_split_route_network_is_reported(self):
        w = [Waypoint.objects.create(floor_plan=self.plan, map_x=x, map_y=0)
             for x in (0, 10, 100, 110)]
        WaypointConnection.objects.create(waypoint_from=w[0], waypoint_to=w[1], distance=10)
        WaypointConnection.objects.create(waypoint_from=w[2], waypoint_to=w[3], distance=10)
        joined = ' | '.join(self._texts(self._check()))
        self.assertIn('2 separate pieces', joined)

    def test_orphan_waypoints_are_counted(self):
        Waypoint.objects.create(floor_plan=self.plan, map_x=0, map_y=0)
        joined = ' | '.join(self._texts(self._check()))
        self.assertIn('1 waypoint(s) connected to nothing', joined)

    def test_a_finished_plan_reports_ready(self):
        self.plan.pixels_per_meter = 100
        self.plan.is_active = True
        self.plan.save()
        room = Room.objects.create(
            floor_plan=self.plan, name='Main', map_x=50, map_y=50,
            geometry=[[0, 0], [100, 0], [100, 100], [0, 100]])
        Door.objects.create(room=room, map_x=50, map_y=0, width=30, rotation=0)
        for i in range(3):
            BLEBeacon.objects.create(floor_plan=self.plan, beacon_uuid='u%d' % i,
                                     map_x=i * 10, map_y=i * 7, tx_power=-59, height=2.4)
        a = Waypoint.objects.create(floor_plan=self.plan, map_x=30, map_y=30)
        b = Waypoint.objects.create(floor_plan=self.plan, map_x=70, map_y=70)
        WaypointConnection.objects.create(waypoint_from=a, waypoint_to=b, distance=56)
        data = self._check()
        self.assertTrue(data['ready'], data['issues'])
        self.assertEqual(data['blockers'], 0)


class SharedDoorTests(TestCase):
    """A doorway joins two rooms, and that is what makes a plan connected."""

    def setUp(self):
        self.user = _admin(modules='shelf')
        self.client = _signed_in(self.user)
        self.plan = FloorPlan.objects.create(name='Ground', floor_number=1,
                                             is_active=True, pixels_per_meter=100)
        # Left and Right share the wall x = 300. Far is nowhere near either.
        self.left = Room.objects.create(
            floor_plan=self.plan, name='Left', map_x=200, map_y=200,
            geometry=[[100, 100], [300, 100], [300, 300], [100, 300]])
        self.right = Room.objects.create(
            floor_plan=self.plan, name='Right', map_x=400, map_y=200,
            geometry=[[300, 100], [500, 100], [500, 300], [300, 300]])
        self.far = Room.objects.create(
            floor_plan=self.plan, name='Far', map_x=900, map_y=200,
            geometry=[[800, 100], [1000, 100], [1000, 300], [800, 300]])

    def _add(self, room, x, y):
        return self.client.post('/admin-portal/add-door/', {
            'room_id': room.room_id, 'map_x': x, 'map_y': y}).json()

    def _edit(self, door_id, **params):
        params['door_id'] = door_id
        return self.client.post('/admin-portal/edit-door/', params).json()

    def test_placing_a_door_on_a_shared_wall_suggests_the_other_room(self):
        r = self._add(self.left, 295, 200)
        self.assertTrue(r['success'], r)
        self.assertIsNotNone(r['suggested_room_b'])
        self.assertEqual(r['suggested_room_b']['name'], 'Right')

    def test_the_suggestion_is_only_a_suggestion(self):
        """Guessing outright would record adjacency nobody agreed to."""
        r = self._add(self.left, 295, 200)
        self.assertIsNone(Door.objects.get(pk=r['door']['door_id']).room_b)

    def test_a_door_with_nothing_on_the_far_side_suggests_nothing(self):
        r = self._add(self.left, 105, 200)     # the outer wall, x = 100
        self.assertTrue(r['success'], r)
        self.assertIsNone(r['suggested_room_b'])

    def test_rooms_that_only_nearly_touch_are_still_found(self):
        """Hand-drawn walls miss by a fraction; that must not hide adjacency."""
        gap = Room.objects.create(
            floor_plan=self.plan, name='Gap', map_x=200, map_y=500,
            geometry=[[100, 300.4], [300, 300.4], [300, 500], [100, 500]])
        r = self._add(self.left, 200, 295)     # the wall at y = 300
        self.assertEqual(r['suggested_room_b']['name'], gap.name)

    def test_a_wall_far_away_is_not_offered(self):
        r = self._add(self.far, 805, 200)
        self.assertIsNone(r['suggested_room_b'])

    def test_the_far_room_can_be_set_and_cleared(self):
        door_id = self._add(self.left, 295, 200)['door']['door_id']
        r = self._edit(door_id, room_b=self.right.room_id)
        self.assertTrue(r['success'], r)
        self.assertEqual(r['door']['room_b'], self.right.room_id)
        self.assertEqual(r['door']['room_b_name'], 'Right')

        r = self._edit(door_id, room_b='')
        self.assertTrue(r['success'], r)
        self.assertIsNone(r['door']['room_b'])

    def test_a_door_cannot_join_a_room_to_itself(self):
        door_id = self._add(self.left, 295, 200)['door']['door_id']
        r = self._edit(door_id, room_b=self.left.room_id)
        self.assertFalse(r['success'])
        self.assertIn('itself', r['error'])

    def test_the_two_rooms_must_be_on_the_same_floor(self):
        upstairs = FloorPlan.objects.create(name='First', floor_number=2)
        elsewhere = Room.objects.create(floor_plan=upstairs, name='Up',
                                        map_x=0, map_y=0,
                                        geometry=[[0, 0], [10, 0], [10, 10], [0, 10]])
        door_id = self._add(self.left, 295, 200)['door']['door_id']
        r = self._edit(door_id, room_b=elsewhere.room_id)
        self.assertFalse(r['success'])
        self.assertIn('same floor', r['error'])

    def test_deleting_the_far_room_leaves_the_door_standing(self):
        """SET_NULL, not CASCADE: the doorway is still a hole in a wall."""
        door_id = self._add(self.left, 295, 200)['door']['door_id']
        self._edit(door_id, room_b=self.right.room_id)
        self.right.delete()
        door = Door.objects.filter(pk=door_id).first()
        self.assertIsNotNone(door, 'the door was deleted with the far room')
        self.assertIsNone(door.room_b)

    def test_adjacency_comes_from_the_doors(self):
        from library.views import _room_adjacency
        self.assertEqual(_room_adjacency(self.plan), set())
        door_id = self._add(self.left, 295, 200)['door']['door_id']
        self._edit(door_id, room_b=self.right.room_id)
        self.assertEqual(
            _room_adjacency(self.plan),
            {(min(self.left.room_id, self.right.room_id),
              max(self.left.room_id, self.right.room_id))})

    def test_a_linked_door_excuses_a_crossing_between_its_own_two_rooms(self):
        door_id = self._add(self.left, 295, 200)['door']['door_id']
        self._edit(door_id, room_b=self.right.room_id, width=60)
        a = Waypoint.objects.create(floor_plan=self.plan, map_x=200, map_y=200)
        b = Waypoint.objects.create(floor_plan=self.plan, map_x=400, map_y=200)
        r = self.client.post('/admin-portal/add-waypoint-connection/', {
            'waypoint_from_id': a.waypoint_id, 'waypoint_to_id': b.waypoint_id}).json()
        self.assertTrue(r['success'], r)

    def test_a_door_linked_to_other_rooms_excuses_nothing_here(self):
        """The precision the second room buys: a doorway belongs to one wall."""
        # A door sitting exactly where the crossing happens, but declared to
        # join two rooms that have nothing to do with it.
        stray = Door.objects.create(room=self.far, room_b=self.left,
                                    map_x=300, map_y=200, width=80, rotation=90)
        self.assertEqual(stray.room_b_id, self.left.room_id)
        a = Waypoint.objects.create(floor_plan=self.plan, map_x=200, map_y=200)
        b = Waypoint.objects.create(floor_plan=self.plan, map_x=400, map_y=200)
        r = self.client.post('/admin-portal/add-waypoint-connection/', {
            'waypoint_from_id': a.waypoint_id, 'waypoint_to_id': b.waypoint_id}).json()
        # It crosses Right's wall, and this door says nothing about Right.
        self.assertFalse(r['success'], r)

    def test_reshaping_away_from_the_far_room_drops_the_link(self):
        """The link was true of the old wall; it has to be re-earned."""
        import json
        door_id = self._add(self.left, 295, 200)['door']['door_id']
        self._edit(door_id, room_b=self.right.room_id)
        # Pull Left's right-hand wall back to x = 200, well away from Right.
        r = self.client.post('/admin-portal/edit-room/', {
            'room_id': self.left.room_id,
            'geometry': json.dumps([[100, 100], [200, 100], [200, 300], [100, 300]])}).json()
        self.assertTrue(r['success'], r)
        door = Door.objects.get(pk=door_id)
        self.assertIsNone(door.room_b)
        self.assertEqual(r['doors_moved'][0]['unlinked_from'], 'Right')

    def test_readiness_names_rooms_no_door_reaches(self):
        # With no doors at all the panel says so instead, which is the more
        # useful message; this is the case where doors exist but link nothing.
        self._add(self.left, 105, 200)
        data = self.client.get('/admin-portal/floor-plan-readiness/',
                               {'floor_plan_id': self.plan.floor_plan_id}).json()
        joined = ' | '.join(i['text'] for i in data['issues'])
        self.assertIn('not joined to any other by a door', joined)

    def test_readiness_points_out_doors_that_look_shared(self):
        self._add(self.left, 295, 200)
        data = self.client.get('/admin-portal/floor-plan-readiness/',
                               {'floor_plan_id': self.plan.floor_plan_id}).json()
        joined = ' | '.join(i['text'] for i in data['issues'])
        self.assertIn('look shared but are not linked', joined)


class GeneratedRouteTests(TestCase):
    """Deriving the walkable network instead of clicking it out by hand."""

    def setUp(self):
        self.user = _admin(modules='shelf')
        self.client = _signed_in(self.user)
        self.plan = FloorPlan.objects.create(name='Ground', floor_number=1,
                                             is_active=True, pixels_per_meter=100)
        # Two rooms sharing the wall x = 300, joined by a door in the middle.
        self.left = Room.objects.create(
            floor_plan=self.plan, name='Left', map_x=200, map_y=200,
            geometry=[[100, 100], [300, 100], [300, 300], [100, 300]])
        self.right = Room.objects.create(
            floor_plan=self.plan, name='Right', map_x=400, map_y=200,
            geometry=[[300, 100], [500, 100], [500, 300], [300, 300]])
        self.door = Door.objects.create(room=self.left, room_b=self.right,
                                        map_x=300, map_y=200, width=70, rotation=90)

    def _generate(self):
        return self.client.post('/admin-portal/generate-waypoints/',
                                {'floor_plan_id': self.plan.floor_plan_id}).json()

    def _links(self):
        wps = Waypoint.objects.filter(floor_plan=self.plan)
        return WaypointConnection.objects.filter(waypoint_from__in=wps,
                                                 waypoint_to__in=wps)

    def test_it_places_and_joins_something(self):
        r = self._generate()
        self.assertTrue(r['success'], r)
        self.assertGreater(r['placed'], 0)
        self.assertGreater(r['connections'], 0)
        self.assertEqual(r['unreachable'], [])

    def test_nothing_it_draws_goes_through_a_wall(self):
        """The property that matters, checked independently of the generator."""
        from library.views import _walls_crossed
        self._generate()
        for c in self._links().select_related('waypoint_from', 'waypoint_to'):
            a, b = c.waypoint_from, c.waypoint_to
            self.assertEqual(
                _walls_crossed(self.plan, a.map_x, a.map_y, b.map_x, b.map_y), [],
                'a generated connection crosses a wall')

    def test_the_two_rooms_end_up_joined(self):
        self._generate()
        wps = {w.waypoint_id: w for w in Waypoint.objects.filter(floor_plan=self.plan)}
        parent = dict((i, i) for i in wps)

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for c in self._links():
            a, b = find(c.waypoint_from_id), find(c.waypoint_to_id)
            if a != b:
                parent[a] = b
        self.assertEqual(len(set(find(i) for i in wps)), 1,
                         'the generated network is not one connected piece')

    def test_a_second_run_replaces_only_what_it_made(self):
        """The whole reason generated waypoints are marked as such."""
        mine = Waypoint.objects.create(floor_plan=self.plan, map_x=150, map_y=150,
                                       label='mine by hand')
        first = self._generate()
        second = self._generate()
        self.assertEqual(second['replaced'], first['placed'])
        self.assertEqual(second['kept_manual'], 1)
        self.assertTrue(Waypoint.objects.filter(pk=mine.waypoint_id).exists())

    def test_a_room_with_no_door_is_reported_not_silently_skipped(self):
        Room.objects.create(
            floor_plan=self.plan, name='Cupboard', map_x=900, map_y=200,
            geometry=[[800, 100], [1000, 100], [1000, 300], [800, 300]])
        r = self._generate()
        # It still gets its own middle waypoint, but nothing connects to it.
        self.assertTrue(r['success'], r)
        cupboard = [w for w in Waypoint.objects.filter(floor_plan=self.plan)
                    if w.label == 'Cupboard']
        self.assertEqual(len(cupboard), 1)
        self.assertFalse(
            WaypointConnection.objects.filter(waypoint_from=cupboard[0]).exists()
            or WaypointConnection.objects.filter(waypoint_to=cupboard[0]).exists())

    def test_it_refuses_when_there_is_nothing_drawn(self):
        empty = FloorPlan.objects.create(name='Blank', floor_number=9)
        r = self.client.post('/admin-portal/generate-waypoints/',
                             {'floor_plan_id': empty.floor_plan_id}).json()
        self.assertFalse(r['success'])
        self.assertIn('room', r['error'].lower())

    def test_furniture_is_walked_around_not_through(self):
        from library.views import _crosses_obstacle
        Obstacle.objects.create(
            floor_plan=self.plan, kind='Table', name='Long table',
            map_x=200, map_y=200,
            geometry=[[120, 190], [280, 190], [280, 210], [120, 210]])
        self._generate()
        for c in self._links().select_related('waypoint_from', 'waypoint_to'):
            a, b = c.waypoint_from, c.waypoint_to
            if a.label == 'Doorway' and b.label == 'Doorway':
                continue          # the step through the door is exempt
            self.assertFalse(
                _crosses_obstacle(self.plan, a.map_x, a.map_y, b.map_x, b.map_y),
                'a generated connection walks through the table')


class BookLocationTests(TestCase):
    """Where a copy is, to the space on the board it sits on."""

    def setUp(self):
        self.user = _admin(modules='books,shelf')
        self.client = _signed_in(self.user)
        self.plan = FloorPlan.objects.create(name='Ground', floor_number=1,
                                             is_active=True, pixels_per_meter=100)
        self.room = Room.objects.create(floor_plan=self.plan, name='Main',
                                        map_x=200, map_y=200,
                                        geometry=[[0, 0], [400, 0], [400, 400], [0, 400]])
        self.shelf = Shelf.objects.create(room=self.room, name='Shelf A',
                                          map_x=200, map_y=200, width=200, depth=40,
                                          rotation=0)
        self.level = ShelfLevel.objects.create(shelf=self.shelf, level_number=3,
                                               category='Fiction')

    def _book(self, **kw):
        kw.setdefault('title', 'A Book')
        kw.setdefault('author', 'Someone')
        kw.setdefault('shelf_level', self.level)
        return Book.objects.create(**kw)

    def test_a_blank_slot_is_not_position_one(self):
        """Not recorded and 'first on the shelf' must stay different."""
        from library.views import parse_shelf_slot
        for blank in ('', '   ', None, 'four', '0', '-2', '10000'):
            self.assertIsNone(parse_shelf_slot(blank), repr(blank))
        self.assertEqual(parse_shelf_slot('1'), 1)
        self.assertEqual(parse_shelf_slot(' 12 '), 12)

    def test_the_location_reads_in_walking_order(self):
        from library.views import book_location_words
        for i in range(1, 4):
            self._book(title='Filler %d' % i, shelf_slot=i, status='Available')
        book = self._book(shelf_slot=4, status='Available')
        words = book_location_words(book)
        self.assertIn('Shelf A', words)
        self.assertIn('Level 3', words)
        # Counted among the copies on the shelf, not read off the stored slot.
        self.assertIn('4th book along', words)
        self.assertLess(words.index('Shelf A'), words.index('Level 3'))

    def test_precision_nobody_entered_is_not_claimed(self):
        from library.views import book_location_words
        book = self._book()
        self.assertNotIn('from the left', book_location_words(book))
        self.assertEqual(book_location_words(self._book(shelf_level=None)), '')

    def test_ordinals_survive_the_teens(self):
        from library.views import _ordinal
        self.assertEqual([_ordinal(n) for n in (1, 2, 3, 11, 12, 13, 21, 22)],
                         ['1st', '2nd', '3rd', '11th', '12th', '13th', '21st', '22nd'])

    def test_the_slot_can_be_set_and_cleared_by_editing(self):
        book = self._book()
        base = {'title': book.title, 'author': book.author,
                'shelf_level': self.level.shelf_level_id}
        r = self.client.post('/admin-portal/edit-book/%d/' % book.book_id,
                             dict(base, shelf_slot='7'),
                             HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        book.refresh_from_db()
        self.assertEqual(book.shelf_slot, 7)

        self.client.post('/admin-portal/edit-book/%d/' % book.book_id,
                         dict(base, shelf_slot=''),
                         HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        book.refresh_from_db()
        self.assertIsNone(book.shelf_slot)

    def test_the_map_is_told_where_on_the_shelf_to_look(self):
        self._book(title='First', shelf_slot=1, status='Available')
        book = self._book(shelf_slot=2, status='Available')
        self._book(title='Another', shelf_slot=5, status='Available')
        r = self.client.get('/patron/map/', {'book_id': book.book_id})
        target = r.context['target']
        self.assertEqual(target['shelf_slot'], 2)
        self.assertEqual(target['level_number'], 3)
        # The marker is placed against the copies on the shelf, so it is the
        # 2nd of 3 rather than anything to do with the stored slot numbers.
        self.assertEqual(target['position'], 2)
        self.assertEqual(target['position_of'], 3)
        self.assertIn('2nd book along', target['location'])

    def test_a_book_with_no_shelf_tells_the_map_nothing_extra(self):
        book = self._book(shelf_level=None)
        r = self.client.get('/patron/map/', {'book_id': book.book_id})
        self.assertNotIn('shelf_slot', r.context['target'])


class BookHistoryTests(TestCase):
    """Who has had this copy, without leaving the book you are looking at."""

    def setUp(self):
        self.user = _admin(modules='books')
        self.client = _signed_in(self.user)
        self.book = Book.objects.create(title='A Book', author='Someone')
        self.patron = Patron.objects.create(fullname='Ben Vergara',
                                            email='ben@example.invalid',
                                            account_status='Active')

    def _loan(self, **kw):
        kw.setdefault('patron', self.patron)
        kw.setdefault('book', self.book)
        kw.setdefault('transaction_type', 'Borrow')
        return Transaction.objects.create(**kw)

    def _details(self):
        return self.client.get('/admin-portal/book-details/%d/' % self.book.book_id).json()

    def test_a_never_borrowed_copy_says_so_rather_than_erroring(self):
        d = self._details()
        self.assertTrue(d['success'])
        self.assertEqual(d['history'], [])
        self.assertEqual(d['history_total'], 0)

    def test_each_loan_reports_its_state(self):
        import datetime
        today = datetime.date.today()
        self._loan(due_date=today + datetime.timedelta(days=7))
        self._loan(due_date=today - datetime.timedelta(days=1), overdue_flag=True)
        self._loan(due_date=today, return_date=today)
        states = sorted(t['state'] for t in self._details()['history'])
        self.assertEqual(states, ['Out', 'Overdue', 'Returned'])

    def test_the_borrower_is_named(self):
        self._loan()
        self.assertEqual(self._details()['history'][0]['patron'], 'Ben Vergara')

    def test_a_long_history_is_capped_but_the_total_is_honest(self):
        from library.views import BOOK_HISTORY_LIMIT
        for _ in range(BOOK_HISTORY_LIMIT + 6):
            self._loan()
        d = self._details()
        self.assertEqual(len(d['history']), BOOK_HISTORY_LIMIT)
        self.assertEqual(d['history_total'], BOOK_HISTORY_LIMIT + 6)

    def test_history_is_staff_side_only(self):
        """The patron endpoint must not say who has had a book."""
        self._loan()
        anon = Client()
        r = anon.get('/admin-portal/book-details/%d/' % self.book.book_id)
        self.assertNotEqual(r.status_code, 200)

        patron_view = anon.get('/patron/book-details/%d/' % self.book.book_id)
        body = patron_view.content.decode('utf-8', 'replace')
        self.assertNotIn('Ben Vergara', body)


class LivePositionTests(TestCase):
    """The number shown is worked out from what is on the shelf right now."""

    def setUp(self):
        self.user = _admin(modules='books,shelf')
        self.client = _signed_in(self.user)
        plan = FloorPlan.objects.create(name='Ground', floor_number=1,
                                        is_active=True, pixels_per_meter=100)
        room = Room.objects.create(floor_plan=plan, name='Main', map_x=0, map_y=0,
                                   geometry=[[0, 0], [400, 0], [400, 400], [0, 400]])
        self.shelf = Shelf.objects.create(room=room, name='Shelf A', map_x=200,
                                          map_y=200, width=200, depth=40, rotation=0)
        self.level = ShelfLevel.objects.create(shelf=self.shelf, level_number=3)
        self.titles = ['Algorithms', 'Botany', 'Calculus', 'Data Structures',
                       'Ecology', 'Fractals', 'Geology', 'History', 'Immunology', 'Jazz']
        for i, t in enumerate(self.titles, start=1):
            Book.objects.create(title=t, author='A', shelf_level=self.level,
                                shelf_slot=i, status='Available')

    def _book(self, title):
        return Book.objects.get(title=title, shelf_level=self.level)

    def _focus(self, title):
        from library.views import level_layout
        return level_layout(self.level, self._book(title))['focus']

    def test_with_nothing_borrowed_the_count_matches_the_slot(self):
        f = self._focus('Immunology')
        self.assertEqual(f['position'], 9)
        self.assertEqual(f['space'], 9)

    def test_a_borrowed_neighbour_moves_the_count_but_not_the_space(self):
        """The case that started this: slot 9 with slot 6 out on loan."""
        Book.objects.filter(title='Fractals').update(status='Borrowed')
        f = self._focus('Immunology')
        self.assertEqual(f['position'], 8, 'the count should skip the borrowed copy')
        self.assertEqual(f['space'], 9, 'its place in the recorded order is unchanged')

    def test_every_way_of_being_off_the_shelf_counts(self):
        for status in ('Borrowed', 'Overdue', 'Lost', 'Being Read', 'For Reshelving'):
            Book.objects.filter(shelf_level=self.level).update(status='Available')
            Book.objects.filter(title='Botany').update(status=status)
            self.assertEqual(self._focus('Calculus')['position'], 2,
                             '%s should not be counted as on the shelf' % status)

    def test_the_neighbours_are_the_ones_actually_there(self):
        Book.objects.filter(title='History').update(status='Borrowed')
        f = self._focus('Immunology')
        self.assertEqual(f['before'], 'Geology')
        self.assertEqual(f['after'], 'Jazz')

    def test_a_copy_that_is_out_reports_no_position(self):
        Book.objects.filter(title='Immunology').update(status='Borrowed')
        f = self._focus('Immunology')
        self.assertFalse(f['on_shelf'])
        self.assertIsNone(f['position'])
        self.assertIn('not on the shelf', book_location_words_for(self._book('Immunology')))

    def test_the_map_marker_follows_the_live_count(self):
        book = self._book('Immunology')
        before = self.client.get('/patron/map/', {'book_id': book.book_id}).context['target']
        Book.objects.filter(title='Fractals').update(status='Borrowed')
        after = self.client.get('/patron/map/', {'book_id': book.book_id}).context['target']
        self.assertEqual(before['position'], 9)
        self.assertEqual(after['position'], 8)
        self.assertEqual(after['position_of'], 9)

    def test_the_layout_marks_the_gaps(self):
        from library.views import level_layout
        Book.objects.filter(title='Calculus').update(status='Borrowed')
        rows = level_layout(self.level, self._book('Immunology'))['rows']
        out = [r for r in rows if not r['on_shelf']]
        self.assertEqual([r['title'] for r in out], ['Calculus'])
        self.assertIsNone(out[0]['position'])
        self.assertEqual(sum(1 for r in rows if r['is_focus']), 1)


def book_location_words_for(book):
    from library.views import book_location_words
    return book_location_words(book)


class ReorderBooksTests(TestCase):
    """Dragging a book into place, and dragging one in from elsewhere."""

    def setUp(self):
        self.user = _admin(modules='shelf')
        self.client = _signed_in(self.user)
        plan = FloorPlan.objects.create(name='Ground', floor_number=1, is_active=True)
        room = Room.objects.create(floor_plan=plan, name='Main', map_x=0, map_y=0)
        shelf = Shelf.objects.create(room=room, name='Shelf A', map_x=0, map_y=0)
        self.level = ShelfLevel.objects.create(shelf=shelf, level_number=1)
        self.other = ShelfLevel.objects.create(shelf=shelf, level_number=2)
        self.books = [Book.objects.create(title=t, author='A', shelf_level=self.level,
                                          shelf_slot=i, status='Available')
                      for i, t in enumerate(['One', 'Two', 'Three'], start=1)]

    def _reorder(self, level, ids):
        return self.client.post('/admin-portal/reorder-books-on-level/', {
            'shelf_level_id': level.shelf_level_id,
            'book_ids': ','.join(str(i) for i in ids)}).json()

    def test_the_new_order_is_written_as_one_two_three(self):
        a, b, c = self.books
        r = self._reorder(self.level, [c.book_id, a.book_id, b.book_id])
        self.assertTrue(r['success'], r)
        for book, expected in ((c, 1), (a, 2), (b, 3)):
            book.refresh_from_db()
            self.assertEqual(book.shelf_slot, expected, book.title)

    def test_a_book_dragged_from_another_level_moves_and_takes_its_place(self):
        stray = Book.objects.create(title='Stray', author='A',
                                    shelf_level=self.other, shelf_slot=1)
        a, b, c = self.books
        r = self._reorder(self.level, [a.book_id, stray.book_id, b.book_id, c.book_id])
        self.assertTrue(r['success'], r)
        self.assertEqual(r['moved'], 1)
        stray.refresh_from_db()
        self.assertEqual(stray.shelf_level_id, self.level.shelf_level_id)
        self.assertEqual(stray.shelf_slot, 2)

    def test_renumbering_closes_gaps_left_by_hand_entry(self):
        """Slots are an ordering key, so a tidy-up makes them dense again."""
        for book, slot in zip(self.books, (5, 40, 900)):
            book.shelf_slot = slot
            book.save()
        self._reorder(self.level, [b.book_id for b in self.books])
        slots = list(Book.objects.filter(shelf_level=self.level)
                     .order_by('shelf_slot').values_list('shelf_slot', flat=True))
        self.assertEqual(slots, [1, 2, 3])

    def test_an_order_naming_a_book_that_is_gone_changes_nothing(self):
        """All or nothing: half an ordering is worse than none."""
        a, b, c = self.books
        r = self._reorder(self.level, [c.book_id, 999999, a.book_id])
        self.assertFalse(r['success'])
        a.refresh_from_db()
        self.assertEqual(a.shelf_slot, 1)

    def test_an_empty_order_is_refused(self):
        self.assertFalse(self._reorder(self.level, [])['success'])

    def test_the_tree_returns_books_in_shelf_order(self):
        a, b, c = self.books
        self._reorder(self.level, [c.book_id, b.book_id, a.book_id])
        data = self.client.get('/admin-portal/get-shelf-tree/').json()

        def walk(nodes):
            for n in nodes:
                if n['type'] == 'shelflevel' and n['id'] == self.level.shelf_level_id:
                    return [k['name'] for k in n.get('children', [])]
                found = walk(n.get('children', []))
                if found:
                    return found
            return None

        self.assertEqual(walk(data['tree']), ['Three', 'Two', 'One'])


class TracedShelfTests(TestCase):
    """A shelf drawn corner by corner keeps that shape through every gesture."""

    def setUp(self):
        self.user = _admin(modules='shelf')
        self.client = _signed_in(self.user)
        plan = FloorPlan.objects.create(name='Ground', floor_number=1, is_active=True)
        self.room = Room.objects.create(floor_plan=plan, name='Main', map_x=0, map_y=0)
        # An L, so a rectangle could never stand in for it.
        self.shape = [[100, 100], [200, 100], [200, 140], [140, 140], [140, 200], [100, 200]]
        self.shelf = Shelf.objects.create(
            room=self.room, name='Corner unit', map_x=150, map_y=150,
            width=46, depth=14, rotation=0, geometry=self.shape)

    def _move(self, x, y):
        return self.client.post('/admin-portal/move-shelf/', {
            'shelf_id': self.shelf.shelf_id, 'map_x': x, 'map_y': y}).json()

    def _rotate(self, deg):
        return self.client.post('/admin-portal/rotate-shelf/', {
            'shelf_id': self.shelf.shelf_id, 'rotation': deg}).json()

    def test_moving_carries_the_outline_with_it(self):
        """The bug: the anchor moved and the drawn shape stayed behind."""
        r = self._move(200, 250)
        self.assertTrue(r['success'], r)
        self.shelf.refresh_from_db()
        self.assertEqual(self.shelf.geometry,
                         [[x + 50, y + 100] for x, y in self.shape])

    def test_moving_does_not_change_the_shape(self):
        import math
        def edges(geom):
            return [round(math.dist(geom[i], geom[(i + 1) % len(geom)]), 6)
                    for i in range(len(geom))]
        before = edges(self.shape)
        self._move(-30, 900)
        self.shelf.refresh_from_db()
        self.assertEqual(edges(self.shelf.geometry), before)
        self.assertEqual(len(self.shelf.geometry), len(self.shape))

    def test_turning_rotates_the_outline_about_the_anchor(self):
        r = self._rotate(90)
        self.assertTrue(r['success'], r)
        self.shelf.refresh_from_db()
        # (100,100) is (-50,-50) from the anchor; a quarter turn puts it at
        # (+50,-50) from it, which is (200,100).
        self.assertEqual(self.shelf.geometry[0], [200.0, 100.0])
        self.assertEqual(len(self.shelf.geometry), len(self.shape))

    def test_turning_twice_is_the_same_as_turning_once_by_the_sum(self):
        """Rotation is applied as a delta, so it must not accumulate wrongly."""
        self._rotate(45)
        self._rotate(90)
        once = list(self.shelf.geometry)
        self.shelf.refresh_from_db()
        turned = list(self.shelf.geometry)

        other = Shelf.objects.create(room=self.room, name='Twin', map_x=150,
                                     map_y=150, rotation=0, geometry=self.shape)
        self.client.post('/admin-portal/rotate-shelf/', {
            'shelf_id': other.shelf_id, 'rotation': 90})
        other.refresh_from_db()
        for a, b in zip(turned, other.geometry):
            self.assertAlmostEqual(a[0], b[0], places=1)
            self.assertAlmostEqual(a[1], b[1], places=1)
        del once

    def test_a_sized_shelf_is_untouched_by_any_of_this(self):
        plain = Shelf.objects.create(room=self.room, name='Plain', map_x=10,
                                     map_y=10, width=46, depth=14, rotation=0)
        r = self.client.post('/admin-portal/move-shelf/', {
            'shelf_id': plain.shelf_id, 'map_x': 99, 'map_y': 99}).json()
        self.assertTrue(r['success'], r)
        plain.refresh_from_db()
        self.assertIsNone(plain.geometry)
        self.assertEqual((plain.map_x, plain.map_y), (99, 99))

    def test_placing_an_unplaced_shelf_does_not_shift_its_outline(self):
        """A shelf with no position yet has nothing to be offset from."""
        fresh = Shelf.objects.create(room=self.room, name='Fresh', geometry=self.shape)
        self.assertIsNone(fresh.map_x)
        r = self.client.post('/admin-portal/place-shelf/', {
            'shelf_id': fresh.shelf_id, 'map_x': 500, 'map_y': 500}).json()
        self.assertTrue(r['success'], r)
        fresh.refresh_from_db()
        self.assertEqual(fresh.geometry, self.shape)

    def test_the_move_response_carries_the_new_outline(self):
        """The editor takes the server's answer so the two cannot drift."""
        r = self._move(160, 160)
        self.assertEqual(r['geometry'], [[x + 10, y + 10] for x, y in self.shape])


class ShelfGridTests(TestCase):
    """A bay's boards, made once instead of one dialog at a time."""

    def setUp(self):
        self.user = _admin(modules='shelf')
        self.client = _signed_in(self.user)
        plan = FloorPlan.objects.create(name='Ground', floor_number=1, is_active=True)
        self.room = Room.objects.create(floor_plan=plan, name='Main', map_x=0, map_y=0)

    def _add(self, **extra):
        params = {'room_id': self.room.room_id, 'name': 'Bay A',
                  'map_x': 10, 'map_y': 10}
        params.update(extra)
        return self.client.post('/admin-portal/add-shelf/', params).json()

    def _grid(self, shelf_id, **params):
        params['shelf_id'] = shelf_id
        return self.client.post('/admin-portal/set-shelf-grid/', params).json()

    def _labels(self, shelf_id):
        return [lv.label for lv in ShelfLevel.objects
                .filter(shelf_id=shelf_id).order_by('level_number', 'column_number')]

    def test_a_new_shelf_gets_the_levels_it_was_asked_for(self):
        r = self._add(levels=5, columns=1)
        self.assertTrue(r['success'], r)
        self.assertEqual(r['levels_created'], 5)
        self.assertEqual(self._labels(r['shelf_id']),
                         ['Level 1', 'Level 2', 'Level 3', 'Level 4', 'Level 5'])

    def test_a_single_column_bay_is_not_labelled_column_one(self):
        """"Level 3, Column 1" is noise on a bookcase that has no bays."""
        r = self._add(levels=2, columns=1)
        for label in self._labels(r['shelf_id']):
            self.assertNotIn('Column', label)

    def test_a_divided_bay_gets_every_cell(self):
        r = self._add(levels=3, columns=3)
        self.assertEqual(r['levels_created'], 9)
        labels = self._labels(r['shelf_id'])
        self.assertIn('Level 1, Column 1', labels)
        self.assertIn('Level 3, Column 3', labels)

    def test_asking_for_no_levels_places_an_empty_bay(self):
        r = self._add(levels=0)
        self.assertTrue(r['success'], r)
        self.assertEqual(ShelfLevel.objects.filter(shelf_id=r['shelf_id']).count(), 0)

    def test_growing_a_shelf_adds_only_what_is_missing(self):
        shelf_id = self._add(levels=2, columns=1)['shelf_id']
        r = self._grid(shelf_id, levels=4, columns=1)
        self.assertTrue(r['success'], r)
        self.assertEqual(r['added'], 2)
        self.assertEqual(len(self._labels(shelf_id)), 4)

    def test_running_the_same_grid_twice_changes_nothing(self):
        shelf_id = self._add(levels=3, columns=2)['shelf_id']
        before = self._labels(shelf_id)
        r = self._grid(shelf_id, levels=3, columns=2)
        self.assertEqual(r['added'], 0)
        self.assertEqual(self._labels(shelf_id), before)

    def test_shrinking_never_destroys_a_level_that_holds_books(self):
        """Reported, not deleted: a mistyped number must not lose books."""
        shelf_id = self._add(levels=4, columns=1)['shelf_id']
        top = ShelfLevel.objects.get(shelf_id=shelf_id, level_number=4)
        Book.objects.create(title='Kept', author='A', shelf_level=top)

        r = self._grid(shelf_id, levels=2, columns=1)
        self.assertTrue(r['success'], r)
        self.assertEqual(ShelfLevel.objects.filter(shelf_id=shelf_id).count(), 4)
        surplus = {row['label']: row['books'] for row in r['surplus']}
        self.assertIn('Level 4', surplus)
        self.assertEqual(surplus['Level 4'], 1)
        self.assertTrue(Book.objects.filter(title='Kept').exists())

    def test_absurd_counts_are_clamped_not_obeyed(self):
        r = self._add(levels=9999, columns=9999)
        self.assertTrue(r['success'], r)
        from library.views import MAX_SHELF_LEVELS, MAX_SHELF_COLUMNS
        self.assertEqual(ShelfLevel.objects.filter(shelf_id=r['shelf_id']).count(),
                         MAX_SHELF_LEVELS * MAX_SHELF_COLUMNS)

    def test_nonsense_counts_fall_back_rather_than_erroring(self):
        r = self._add(levels='four', columns='')
        self.assertTrue(r['success'], r)
        self.assertEqual(ShelfLevel.objects.filter(shelf_id=r['shelf_id']).count(), 1)


class LabelPickerGroupingTests(TestCase):
    """The picker groups by shelf and by level entirely client-side.

    That only works while the payload carries the keys to group on, so this
    pins them: dropping shelf_id from the endpoint would silently reduce
    "By shelf" to one bucket called "Not on a shelf yet".
    """

    def setUp(self):
        self.user = _admin(modules='books')
        self.client = _signed_in(self.user)
        plan = FloorPlan.objects.create(name='Ground', floor_number=1, is_active=True)
        room = Room.objects.create(floor_plan=plan, name='Main', map_x=0, map_y=0)
        self.bay = Shelf.objects.create(room=room, name='Bay A', map_x=0, map_y=0)
        self.level = ShelfLevel.objects.create(shelf=self.bay, level_number=2,
                                               category='Maths')
        Book.objects.create(title='On a shelf', author='A', shelf_level=self.level)
        Book.objects.create(title='Nowhere yet', author='B')

    def _rows(self):
        d = self.client.get('/admin-portal/book-label-picker/').json()
        self.assertTrue(d['success'], d)
        return {b['title']: b for b in d['books']}

    def test_a_shelved_book_carries_both_grouping_keys(self):
        row = self._rows()['On a shelf']
        self.assertEqual(row['shelf_id'], self.bay.shelf_id)
        self.assertEqual(row['shelf_name'], 'Bay A')
        self.assertEqual(row['level_id'], self.level.shelf_level_id)
        self.assertEqual(row['level_number'], 2)

    def test_an_unshelved_book_says_so_with_nulls(self):
        """Null rather than absent: the picker buckets on it."""
        row = self._rows()['Nowhere yet']
        self.assertIsNone(row['shelf_id'])
        self.assertIsNone(row['level_id'])

    def test_the_location_string_is_there_for_the_print_order(self):
        self.assertTrue(self._rows()['On a shelf']['location'])


class ReshapeByCornerTests(TestCase):
    """Dragging a corner is the one edit width and depth cannot express."""

    def setUp(self):
        self.user = _admin(modules='shelf')
        self.client = _signed_in(self.user)
        self.plan = FloorPlan.objects.create(name='Ground', floor_number=1, is_active=True)
        self.room = Room.objects.create(floor_plan=self.plan, name='Main', map_x=0, map_y=0)
        self.L = [[0, 0], [100, 0], [100, 40], [40, 40], [40, 100], [0, 100]]

    def _reshape_shelf(self, shelf, geom):
        import json
        return self.client.post('/admin-portal/edit-shelf/', {
            'shelf_id': shelf.shelf_id, 'geometry': json.dumps(geom)}).json()

    def test_a_sized_shelf_becomes_traced_when_its_corners_are_moved(self):
        """That is what dragging a corner means; refusing it would be worse."""
        shelf = Shelf.objects.create(room=self.room, name='Plain', map_x=50,
                                     map_y=50, width=46, depth=14)
        self.assertIsNone(shelf.geometry)
        r = self._reshape_shelf(shelf, self.L)
        self.assertTrue(r['success'], r)
        shelf.refresh_from_db()
        self.assertEqual(shelf.geometry, self.L)

    def test_the_anchor_follows_the_new_outline(self):
        """Otherwise the label and the route target sit off the shape."""
        shelf = Shelf.objects.create(room=self.room, name='Plain', map_x=999,
                                     map_y=999, width=46, depth=14)
        self._reshape_shelf(shelf, self.L)
        shelf.refresh_from_db()
        self.assertLess(shelf.map_x, 100)
        self.assertLess(shelf.map_y, 100)

    def test_a_broken_outline_is_refused_and_changes_nothing(self):
        shelf = Shelf.objects.create(room=self.room, name='Plain', map_x=50,
                                     map_y=50, geometry=self.L)
        import json
        r = self.client.post('/admin-portal/edit-shelf/', {
            'shelf_id': shelf.shelf_id,
            'geometry': json.dumps([[0, 0], [1, 1]])}).json()
        self.assertFalse(r['success'], r)
        shelf.refresh_from_db()
        self.assertEqual(shelf.geometry, self.L)

    def test_furniture_can_be_reshaped_too(self):
        import json
        o = Obstacle.objects.create(floor_plan=self.plan, kind='Table',
                                    name='Long table', map_x=0, map_y=0,
                                    geometry=[[0, 0], [10, 0], [10, 10], [0, 10]])
        r = self.client.post('/admin-portal/edit-obstacle/', {
            'obstacle_id': o.obstacle_id, 'geometry': json.dumps(self.L)}).json()
        self.assertTrue(r['success'], r)
        o.refresh_from_db()
        self.assertEqual(o.geometry, self.L)

    def test_renaming_a_shelf_without_geometry_leaves_the_outline_alone(self):
        shelf = Shelf.objects.create(room=self.room, name='Corner', map_x=50,
                                     map_y=50, geometry=self.L)
        r = self.client.post('/admin-portal/edit-shelf/', {
            'shelf_id': shelf.shelf_id, 'name': 'Renamed'}).json()
        self.assertTrue(r['success'], r)
        shelf.refresh_from_db()
        self.assertEqual(shelf.name, 'Renamed')
        self.assertEqual(shelf.geometry, self.L)


class ShelfTopSurfaceTests(TestCase):
    """Books stacked on top of a bay, and fixing a board recorded wrongly."""

    def setUp(self):
        self.user = _admin(modules='shelf')
        self.client = _signed_in(self.user)
        plan = FloorPlan.objects.create(name='Ground', floor_number=1, is_active=True)
        room = Room.objects.create(floor_plan=plan, name='Main', map_x=0, map_y=0)
        self.shelf = Shelf.objects.create(room=room, name='Bay A', map_x=0, map_y=0)

    def _grid(self, **params):
        params.setdefault('shelf_id', self.shelf.shelf_id)
        params.setdefault('levels', 3)
        params.setdefault('columns', 1)
        return self.client.post('/admin-portal/set-shelf-grid/', params).json()

    def _tops(self):
        return ShelfLevel.objects.filter(shelf=self.shelf, is_top=True)

    def test_ticking_it_adds_a_top_surface(self):
        r = self._grid(has_top='1')
        self.assertTrue(r['success'], r)
        self.assertTrue(r['has_top'])
        self.assertEqual(self._tops().count(), 1)
        self.assertEqual(self._tops().first().label, 'Top')

    def test_the_top_is_not_one_of_the_numbered_boards(self):
        self._grid(levels=3, has_top='1')
        numbered = ShelfLevel.objects.filter(shelf=self.shelf, is_top=False, is_under=False)
        self.assertEqual(numbered.count(), 3)
        self.assertEqual(ShelfLevel.objects.filter(shelf=self.shelf).count(), 4)

    def test_ticking_it_twice_does_not_stack_two_tops(self):
        self._grid(has_top='1')
        self._grid(has_top='1')
        self.assertEqual(self._tops().count(), 1)

    def test_unticking_removes_an_empty_top(self):
        self._grid(has_top='1')
        r = self._grid(has_top='0')
        self.assertFalse(r['has_top'])
        self.assertEqual(self._tops().count(), 0)
        self.assertIn('removed', r['top_note'])

    def test_unticking_keeps_a_top_that_holds_books(self):
        """The rule everywhere here: a number in a box never deletes books."""
        self._grid(has_top='1')
        Book.objects.create(title='On top', author='A', shelf_level=self._tops().first())
        r = self._grid(has_top='0')
        self.assertEqual(self._tops().count(), 1)
        self.assertIn('kept', r['top_note'])
        self.assertTrue(Book.objects.filter(title='On top').exists())

    def test_shrinking_the_grid_never_reports_the_top_as_surplus(self):
        """It has no level number to be outside the range."""
        self._grid(levels=4, has_top='1')
        r = self._grid(levels=2)
        labels = [row['label'] for row in r['surplus']]
        self.assertNotIn('Top', labels)

    def test_a_board_can_be_turned_into_the_top_afterwards(self):
        self._grid(levels=2)
        board = ShelfLevel.objects.filter(shelf=self.shelf, level_number=2).first()
        Book.objects.create(title='Stays put', author='A', shelf_level=board)
        r = self.client.post('/admin-portal/edit-shelf-level/', {
            'shelf_level_id': board.shelf_level_id,
            'level_number': board.level_number,
            'is_top': '1', 'is_under': '0'}).json()
        self.assertTrue(r['success'], r)
        board.refresh_from_db()
        self.assertTrue(board.is_top)
        self.assertEqual(board.label, 'Top')
        # And its books came with it, rather than being deleted and remade.
        self.assertEqual(board.book_set.count(), 1)

    def test_a_board_cannot_be_both_top_and_underneath(self):
        self._grid(levels=1)
        board = ShelfLevel.objects.filter(shelf=self.shelf).first()
        r = self.client.post('/admin-portal/edit-shelf-level/', {
            'shelf_level_id': board.shelf_level_id,
            'level_number': 1, 'is_top': '1', 'is_under': '1'}).json()
        self.assertFalse(r['success'])
        board.refresh_from_db()
        self.assertFalse(board.is_top)

    def test_clearing_both_returns_it_to_a_numbered_board(self):
        self._grid(has_top='1')
        top = self._tops().first()
        r = self.client.post('/admin-portal/edit-shelf-level/', {
            'shelf_level_id': top.shelf_level_id,
            'level_number': 9, 'is_top': '0', 'is_under': '0'}).json()
        self.assertTrue(r['success'], r)
        top.refresh_from_db()
        self.assertFalse(top.is_top)
        self.assertEqual(top.label, 'Level 9')


class CallNumberTests(TestCase):
    """The spine number: class, author mark, title letter, year."""

    def _n(self, genre, author, title, year=None):
        return Book(genre=genre, author=author, title=title,
                    publication_year=year).derive_call_number()

    def test_it_reads_in_the_house_format(self):
        self.assertEqual(self._n('Fiction', 'Austen, Jane', 'Pride and Prejudice', 1963),
                         'FIC A31p 1963')

    def test_the_mark_comes_from_the_surname_not_the_forename(self):
        """Austen is A, whichever way round the name was typed."""
        a = self._n('Fiction', 'Austen, Jane', 'Quiet Sea', 1963)
        b = self._n('Fiction', 'Jane Austen', 'Quiet Sea', 1963)
        self.assertEqual(a, b)
        self.assertTrue(a.split()[1].startswith('A'), a)

    def test_a_leading_article_does_not_decide_the_letter(self):
        """"The Quiet Sea" files under q, the way a shelf is ordered."""
        self.assertTrue(self._n('Fiction', 'Austen, Jane', 'The Quiet Sea').endswith('q'))
        self.assertTrue(self._n('Fiction', 'Austen, Jane', 'A Quiet Sea').endswith('q'))
        self.assertTrue(self._n('Fiction', 'Austen, Jane', 'An Ocean').endswith('o'))

    def test_the_class_is_the_first_three_letters_of_the_genre(self):
        self.assertTrue(self._n('Science', 'Hawking, S', 'Time', 1988).startswith('SCI '))
        self.assertTrue(self._n('History', 'Rizal, J', 'Noli', 1887).startswith('HIS '))

    def test_a_book_with_nothing_to_go_on_still_gets_something(self):
        self.assertEqual(self._n('', '', 'Untitled'), 'GEN')

    def test_the_year_is_left_off_when_unknown(self):
        self.assertNotIn('None', self._n('Fiction', 'Austen, Jane', 'Emma'))

    def test_it_is_filled_in_on_save_and_never_overwritten(self):
        book = Book.objects.create(title='Emma', author='Austen, Jane',
                                   genre='Fiction', publication_year=1815)
        self.assertEqual(book.call_number, 'FIC A31e 1815')

        book.call_number = 'FIC AUS 1815'      # a librarian's own number
        book.save()
        book.refresh_from_db()
        self.assertEqual(book.call_number, 'FIC AUS 1815')

        # And correcting the genre later does not move a spine already labelled.
        book.genre = 'Classics'
        book.save()
        book.refresh_from_db()
        self.assertEqual(book.call_number, 'FIC AUS 1815')

    def test_clearing_it_renumbers(self):
        book = Book.objects.create(title='Emma', author='Austen, Jane',
                                   genre='Fiction', publication_year=1815)
        book.call_number = ''
        book.genre = 'Classics'
        book.save()
        book.refresh_from_db()
        self.assertEqual(book.call_number, 'CLA A31e 1815')


class BookConditionTests(TestCase):
    """Three steps, and anything else means Good rather than a failed row."""

    def setUp(self):
        self.user = _admin(modules='books')
        self.client = _signed_in(self.user)

    def test_a_new_copy_is_good_until_said_otherwise(self):
        self.assertEqual(Book.objects.create(title='T', author='A').condition, 'Good')

    def test_the_three_values_are_the_whole_list(self):
        self.assertEqual([c[0] for c in Book.CONDITION_CHOICES],
                         ['Good', 'Worn', 'Damaged'])

    def test_editing_accepts_a_valid_value_and_ignores_a_bad_one(self):
        book = Book.objects.create(title='T', author='A')
        base = {'title': 'T', 'author': 'A'}
        self.client.post('/admin-portal/edit-book/%d/' % book.book_id,
                         dict(base, condition='Damaged'),
                         HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        book.refresh_from_db()
        self.assertEqual(book.condition, 'Damaged')

        self.client.post('/admin-portal/edit-book/%d/' % book.book_id,
                         dict(base, condition='Pristine'),
                         HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        book.refresh_from_db()
        self.assertEqual(book.condition, 'Damaged', 'a bad value must not stick')


class CatalogueImportTests(TestCase):
    """The template, the importer and the labels agree on the columns."""

    def setUp(self):
        self.user = _admin(modules='books')
        self.client = _signed_in(self.user)

    def _template(self):
        import io as _io
        import openpyxl
        r = self.client.get('/admin-portal/download-book-template/')
        self.assertEqual(r.status_code, 200)
        return list(openpyxl.load_workbook(_io.BytesIO(r.content)).active.iter_rows(values_only=True))

    def test_the_template_offers_both_new_columns(self):
        headers = self._template()[0]
        self.assertIn('Condition', headers)
        self.assertIn('Code Label', headers)

    def _import(self, rows):
        import io as _io
        import openpyxl
        from django.core.files.uploadedfile import SimpleUploadedFile
        wb = openpyxl.Workbook()
        for row in rows:
            wb.active.append(row)
        buf = _io.BytesIO()
        wb.save(buf)
        up = SimpleUploadedFile('b.xlsx', buf.getvalue(),
                                content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        return self.client.post('/admin-portal/import-books/', {'excel_file': up}).json()

    def test_the_template_can_be_filled_in_and_imported_straight_back(self):
        rows = self._template()
        r = self._import(rows)
        self.assertTrue(r['success'], r)
        self.assertEqual(Book.objects.filter(condition='Worn').count(), 1)
        self.assertTrue(Book.objects.filter(call_number='FIC A31p 1963').exists())

    def test_a_blank_code_label_is_worked_out(self):
        r = self._import([['Title', 'Author', 'Genre', 'Publication Year'],
                          ['Emma', 'Austen, Jane', 'Fiction', 1815]])
        self.assertTrue(r['success'], r)
        self.assertEqual(Book.objects.get(title='Emma').call_number, 'FIC A31e 1815')

    def test_an_unknown_condition_does_not_fail_the_row(self):
        r = self._import([['Title', 'Author', 'Condition'],
                          ['Emma', 'Austen, Jane', 'absolutely fine']])
        self.assertTrue(r['success'], r)
        self.assertEqual(Book.objects.get(title='Emma').condition, 'Good')

    def test_a_sheet_with_no_such_columns_still_imports(self):
        """Every sheet written before these columns existed must still work."""
        r = self._import([['Title', 'Author'], ['Emma', 'Austen, Jane']])
        self.assertTrue(r['success'], r)
        book = Book.objects.get(title='Emma')
        self.assertEqual(book.condition, 'Good')
        self.assertTrue(book.call_number)

    def test_the_label_sheet_prints_the_code_label(self):
        import io as _io
        from pypdf import PdfReader
        book = Book.objects.create(title='Emma', author='Austen, Jane',
                                   genre='Fiction', publication_year=1815)
        r = self.client.get('/admin-portal/book-qr-labels/', {'ids': str(book.book_id)})
        self.assertEqual(r['Content-Type'], 'application/pdf')
        text = PdfReader(_io.BytesIO(r.content)).pages[0].extract_text()
        self.assertIn('FIC A31e 1815', text)
        self.assertIn('Emma', text)


class BookLocationLabelTests(TestCase):
    """Shelf, then across, then up -- the order somebody walks it."""

    def setUp(self):
        plan = FloorPlan.objects.create(name='Ground', floor_number=1, is_active=True)
        room = Room.objects.create(floor_plan=plan, name='Main', map_x=0, map_y=0)
        self.shelf = Shelf.objects.create(room=room, name='Shelf A', map_x=0, map_y=0)

    def _book(self, **level_kw):
        level = ShelfLevel.objects.create(shelf=self.shelf, **level_kw)
        return Book.objects.create(title='T', author='A', shelf_level=level)

    def test_the_long_form_reads_as_a_sentence(self):
        b = self._book(level_number=2, column_number=1)
        self.assertEqual(b.location_label(), 'Shelf A Column 1 Level 2')

    def test_the_short_form_drops_the_word_shelf(self):
        b = self._book(level_number=2, column_number=1)
        self.assertEqual(b.location_label(short=True), 'A C1 L2')

    def test_a_bay_with_no_columns_says_nothing_about_them(self):
        b = self._book(level_number=3)
        self.assertEqual(b.location_label(), 'Shelf A Level 3')
        self.assertEqual(b.location_label(short=True), 'A L3')

    def test_a_top_is_named_not_numbered(self):
        """"L5" sends somebody to the fifth board of a case whose books are on its lid."""
        b = self._book(level_number=9, is_top=True)
        self.assertEqual(b.location_label(), 'Shelf A Top')
        self.assertNotIn('L9', b.location_label(short=True))

    def test_an_unshelved_copy_has_no_location_at_all(self):
        b = Book.objects.create(title='T', author='A')
        self.assertEqual(b.location_label(), '')
        self.assertEqual(b.location_label(short=True), '')


class LocationParsingTests(TestCase):
    """A sheet carries whichever form the person typing it preferred."""

    def setUp(self):
        self.user = _admin(modules='books')
        self.client = _signed_in(self.user)
        plan = FloorPlan.objects.create(name='Ground', floor_number=1, is_active=True)
        room = Room.objects.create(floor_plan=plan, name='Main', map_x=0, map_y=0)
        self.shelf = Shelf.objects.create(room=room, name='Shelf A', map_x=0, map_y=0)
        for lv in (1, 2, 3):
            for col in (1, 2):
                ShelfLevel.objects.create(shelf=self.shelf, level_number=lv,
                                          column_number=col)

    def test_both_forms_read_the_same(self):
        from library.views import parse_location
        self.assertEqual(parse_location('Shelf A Column 1 Level 2'),
                         ('Shelf A', 2, 1, False, False))
        self.assertEqual(parse_location('A C1 L2'), ('A', 2, 1, False, False))

    def test_separators_do_not_matter(self):
        from library.views import parse_location
        for text in ('Shelf A · Column 1 · Level 2',
                     'Shelf A, Column 1, Level 2',
                     'Shelf A/Column 1/Level 2'):
            self.assertEqual(parse_location(text), ('Shelf A', 2, 1, False, False), text)

    def test_a_plain_storage_area_still_means_nothing_positional(self):
        """Old sheets said "Zone 3" and must keep working."""
        from library.views import parse_location
        self.assertEqual(parse_location('Zone 3'), ('Zone 3', None, None, False, False))

    def _import(self, rows):
        import io as _io
        import openpyxl
        from django.core.files.uploadedfile import SimpleUploadedFile
        wb = openpyxl.Workbook()
        for row in rows:
            wb.active.append(row)
        buf = _io.BytesIO()
        wb.save(buf)
        up = SimpleUploadedFile('b.xlsx', buf.getvalue(),
                                content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        return self.client.post('/admin-portal/import-books/', {'excel_file': up}).json()

    def test_importing_the_long_form_lands_on_the_right_board(self):
        r = self._import([['Title', 'Author', 'Location'],
                          ['Emma', 'Austen, Jane', 'Shelf A Column 1 Level 2']])
        self.assertTrue(r['success'], r)
        book = Book.objects.get(title='Emma')
        self.assertEqual(book.shelf_level.level_number, 2)
        self.assertEqual(book.shelf_level.column_number, 1)
        self.assertEqual(book.location_label(), 'Shelf A Column 1 Level 2')

    def test_importing_the_short_form_lands_on_the_same_board(self):
        r = self._import([['Title', 'Author', 'Location'],
                          ['Emma', 'Austen, Jane', 'A C1 L2']])
        self.assertTrue(r['success'], r)
        self.assertEqual(Book.objects.get(title='Emma').location_label(),
                         'Shelf A Column 1 Level 2')

    def test_a_board_that_does_not_exist_yet_is_made(self):
        """The location travels with the import rather than being dropped."""
        r = self._import([['Title', 'Author', 'Location'],
                          ['Emma', 'Austen, Jane', 'Shelf A Column 2 Level 9']])
        self.assertTrue(r['success'], r)
        book = Book.objects.get(title='Emma')
        self.assertEqual(book.shelf_level.level_number, 9)
        self.assertEqual(book.shelf_level.column_number, 2)

    def test_a_top_imports_as_a_top(self):
        r = self._import([['Title', 'Author', 'Location'],
                          ['Emma', 'Austen, Jane', 'Shelf A Top']])
        self.assertTrue(r['success'], r)
        self.assertTrue(Book.objects.get(title='Emma').shelf_level.is_top)

    def test_the_template_round_trips_with_its_own_locations(self):
        import io as _io
        import openpyxl
        tpl = self.client.get('/admin-portal/download-book-template/')
        rows = list(openpyxl.load_workbook(_io.BytesIO(tpl.content)).active.iter_rows(values_only=True))
        self.assertIn('Location', rows[0])
        r = self._import(rows)
        self.assertTrue(r['success'], r)
        # Both example rows name the same board, one long and one short.
        placed = [b.location_label() for b in Book.objects.all() if b.shelf_level]
        self.assertTrue(placed, 'nothing was shelved by the import')
        for where in placed:
            self.assertEqual(where, 'Shelf A Column 1 Level 2')


class ImportRepeatAndConditionTests(TestCase):
    """The two things a real sheet exposed: repeats, and unread conditions."""

    def setUp(self):
        self.user = _admin(modules='books')
        self.client = _signed_in(self.user)
        plan = FloorPlan.objects.create(name='Ground', floor_number=1, is_active=True)
        room = Room.objects.create(floor_plan=plan, name='Main', map_x=0, map_y=0)
        shelf = Shelf.objects.create(room=room, name='Shelf C', map_x=0, map_y=0)
        ShelfLevel.objects.create(shelf=shelf, level_number=3, column_number=1)

    def _import(self, rows):
        import io as _io
        import openpyxl
        from django.core.files.uploadedfile import SimpleUploadedFile
        wb = openpyxl.Workbook()
        for row in rows:
            wb.active.append(row)
        buf = _io.BytesIO()
        wb.save(buf)
        up = SimpleUploadedFile('b.xlsx', buf.getvalue(),
                                content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        return self.client.post('/admin-portal/import-books/', {'excel_file': up}).json()

    # Shaped like the real sheet: a blank ISBN column and "GOOD/BAD CONDITION".
    SHEET = [
        ['Title', 'Author', 'Publication Year', 'ISBN', 'Genre', 'Condition',
         'Code Label', 'Quantity', 'Location'],
        ['Communication for the Common Good', 'Florangel Rosario-Braid', 1990, '',
         'EDUCATION', 'BAD CONDITION', 'SR 001.54 C65 1990', 1,
         'Shelf C Column 1 Level 3'],
        ['A Course on Words', 'Waldo E. Sweet', 1989, '0-472-08101-2',
         'EDUCATION', 'GOOD CONDITION', 'P 305 S93 1982', 1,
         'Shelf C Column 1 Level 3'],
    ]

    def test_the_sheets_own_words_are_understood(self):
        from library.views import parse_condition
        self.assertEqual(parse_condition('GOOD CONDITION'), 'Good')
        self.assertEqual(parse_condition('BAD CONDITION'), 'Damaged')
        self.assertEqual(parse_condition('Fair'), 'Worn')
        self.assertIsNone(parse_condition('sparkly'))

    def test_a_bad_condition_row_is_not_filed_as_good(self):
        r = self._import(self.SHEET)
        self.assertTrue(r['success'], r)
        book = Book.objects.get(title='Communication for the Common Good')
        self.assertEqual(book.condition, 'Damaged')

    def test_importing_the_same_sheet_twice_adds_nothing(self):
        """The bug: 380 of 480 books had no ISBN, so every run copied them."""
        first = self._import(self.SHEET)
        self.assertEqual(first['imported'], 2, first)
        after_first = Book.objects.count()

        second = self._import(self.SHEET)
        self.assertTrue(second['success'], second)
        self.assertEqual(Book.objects.count(), after_first,
                         'a second run of the same sheet added books again')
        self.assertIn('already on the same shelf', second['message'])

    def test_two_rows_for_one_book_in_a_sheet_are_two_copies(self):
        """A sheet listing the same book twice is how a library says it holds two.

        Only a LATER import of the same book means nothing new; within one
        sheet, the second line is the second copy on the shelf.
        """
        rows = [self.SHEET[0],
                ['Twice Over', 'Someone', 1990, '', 'EDUCATION', 'GOOD CONDITION',
                 '', 1, 'Shelf C Column 1 Level 3'],
                ['Twice Over', 'Someone', 1990, '', 'EDUCATION', 'GOOD CONDITION',
                 '', 1, 'Shelf C Column 1 Level 3']]
        r = self._import(rows)
        self.assertTrue(r['success'], r)
        self.assertEqual(Book.objects.filter(title='Twice Over').count(), 2)

        # And running that same sheet again still adds nothing.
        self._import(rows)
        self.assertEqual(Book.objects.filter(title='Twice Over').count(), 2)

    def test_a_quantity_still_creates_that_many_copies(self):
        """The dedupe must not mistake a book's own copies for repeats."""
        rows = [self.SHEET[0],
                ['Three Copies', 'Someone', 1990, '', 'EDUCATION', 'GOOD CONDITION',
                 '', 3, 'Shelf C Column 1 Level 3']]
        r = self._import(rows)
        self.assertTrue(r['success'], r)
        self.assertEqual(Book.objects.filter(title='Three Copies').count(), 3)

    def test_the_same_book_on_a_different_shelf_is_a_different_copy(self):
        self._import(self.SHEET)
        rows = [self.SHEET[0],
                ['Communication for the Common Good', 'Florangel Rosario-Braid', 1990,
                 '', 'EDUCATION', 'BAD CONDITION', '', 1, 'Shelf C Column 1 Level 9']]
        r = self._import(rows)
        self.assertTrue(r['success'], r)
        self.assertEqual(
            Book.objects.filter(title='Communication for the Common Good').count(), 2)

    def test_an_unreadable_condition_is_named_in_the_report(self):
        rows = [self.SHEET[0],
                ['Odd One', 'Someone', 1990, '', 'EDUCATION', 'sparkly', '', 1, '']]
        r = self._import(rows)
        self.assertIn('could not read the condition', r['message'])
        self.assertIn('sparkly', r['message'])
        self.assertEqual(Book.objects.get(title='Odd One').condition, 'Good')


class ImportPreviewTests(TestCase):
    """A preview reports what happened, then puts it all back."""

    def setUp(self):
        self.user = _admin(modules='books')
        self.client = _signed_in(self.user)
        plan = FloorPlan.objects.create(name='Ground', floor_number=1, is_active=True)
        room = Room.objects.create(floor_plan=plan, name='Main', map_x=0, map_y=0)
        shelf = Shelf.objects.create(room=room, name='Shelf C', map_x=0, map_y=0)
        ShelfLevel.objects.create(shelf=shelf, level_number=3, column_number=1)

    SHEET = [
        ['Title', 'Author', 'Publication Year', 'ISBN', 'Genre', 'Condition',
         'Code Label', 'Quantity', 'Location'],
        ['One', 'Someone', 1990, '', 'EDUCATION', 'BAD CONDITION', '', 2,
         'Shelf C Column 1 Level 3'],
        ['Two', 'Another', 1991, '111-1', 'EDUCATION', 'GOOD CONDITION', '', 1,
         'Shelf C Column 1 Level 3'],
    ]

    def _post(self, rows, **extra):
        import io as _io
        import openpyxl
        from django.core.files.uploadedfile import SimpleUploadedFile
        wb = openpyxl.Workbook()
        for row in rows:
            wb.active.append(row)
        buf = _io.BytesIO()
        wb.save(buf)
        up = SimpleUploadedFile('b.xlsx', buf.getvalue(),
                                content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        data = {'excel_file': up}
        data.update(extra)
        return self.client.post('/admin-portal/import-books/', data).json()

    def test_a_preview_writes_nothing(self):
        before = Book.objects.count()
        r = self._post(self.SHEET, preview='1')
        self.assertTrue(r['success'], r)
        self.assertTrue(r['preview'])
        self.assertEqual(Book.objects.count(), before,
                         'the preview left books behind')

    def test_a_preview_counts_what_the_real_run_would_do(self):
        preview = self._post(self.SHEET, preview='1')
        real = self._post(self.SHEET)
        for key in ('imported', 'copies_created', 'skipped_dup',
                    'skipped_repeat', 'skipped_blank'):
            self.assertEqual(preview[key], real[key], key)
        self.assertEqual(Book.objects.count(), real['imported'])

    def test_a_preview_says_what_will_happen_not_what_did(self):
        preview = self._post(self.SHEET, preview='1')
        self.assertIn('will be imported', preview['message'])
        self.assertNotIn('Imported', preview['message'])
        self.assertIn('Imported', self._post(self.SHEET)['message'])

    def test_a_preview_does_not_leave_shelf_levels_behind_either(self):
        """It creates levels for locations it cannot find; those roll back too."""
        before = ShelfLevel.objects.count()
        self._post([self.SHEET[0],
                    ['Three', 'Someone', 1990, '', 'EDUCATION', '', '', 1,
                     'Shelf C Column 1 Level 8']], preview='1')
        self.assertEqual(ShelfLevel.objects.count(), before)

    def test_the_summary_breaks_the_numbers_out_for_the_confirm_box(self):
        r = self._post(self.SHEET, preview='1')
        for key in ('imported', 'copies_created', 'skipped_dup',
                    'skipped_repeat', 'skipped_blank'):
            self.assertIn(key, r)

    def test_a_preview_hands_out_no_book_ids(self):
        """They would be ids of rows that no longer exist."""
        r = self._post(self.SHEET, preview='1')
        self.assertEqual(r['created_ids'], [])
        self.assertTrue(self._post(self.SHEET)['created_ids'])

    def test_importing_without_asking_for_a_preview_still_writes(self):
        r = self._post(self.SHEET)
        self.assertFalse(r.get('preview'))
        self.assertEqual(Book.objects.count(), 3)


class CopiesColumnTests(TestCase):
    """The Copies column counts copies, and can be searched on."""

    def setUp(self):
        self.user = _admin(modules='books')
        self.client = _signed_in(self.user)
        for i in range(4):
            Book.objects.create(title='Four Of These', author='Ann Author',
                                genre='FICTION', status='Available')
        Book.objects.create(title='Only One', author='Bob Byline',
                            genre='FICTION', status='Available')
        for i in range(2):
            Book.objects.create(title='Two Of These', author='Cee Creator',
                                genre='FICTION', status='Available')
        # Same title, different author: a different book, not another copy.
        Book.objects.create(title='Only One', author='Dee Different',
                            genre='FICTION', status='Available')

    def _rows(self, query=''):
        r = self.client.get('/admin-portal/management/' + query)
        self.assertEqual(r.status_code, 200)
        return r.context['books'].paginator.count

    def test_the_column_counts_the_copies_rather_than_always_saying_one(self):
        r = self.client.get('/admin-portal/management/?q=Four Of These')
        counts = {b.copies for b in r.context['books']}
        self.assertEqual(counts, {4})

    def test_a_different_author_is_a_different_book(self):
        r = self.client.get('/admin-portal/management/?q=Only One')
        self.assertEqual({b.copies for b in r.context['books']}, {1})

    def test_searching_for_single_copies(self):
        self.assertEqual(self._rows('?copies=1'), 2)

    def test_searching_for_an_exact_number(self):
        self.assertEqual(self._rows('?copies=4'), 4)
        self.assertEqual(self._rows('?copies=2'), 2)

    def test_searching_for_that_many_or_more(self):
        self.assertEqual(self._rows('?copies=2%2B'), 6)
        self.assertEqual(self._rows('?copies=4%2B'), 4)

    def test_nonsense_is_ignored_rather_than_emptying_the_table(self):
        for bad in ('?copies=banana', '?copies=0', '?copies=-3', '?copies=%2B',
                    '?copies=999999'):
            self.assertEqual(self._rows(bad), 8, bad)

    def test_the_filter_survives_paging(self):
        r = self.client.get('/admin-portal/management/?copies=2%2B')
        self.assertIn('copies=2%2B', r.context['querystring'])

    def test_it_combines_with_the_other_filters(self):
        Book.objects.create(title='Four Of These', author='Ann Author',
                            genre='FICTION', status='Borrowed')
        # Five rows share the title now; only the four available ones match.
        self.assertEqual(self._rows('?copies=5&status=Available'), 4)

    def test_the_staff_page_can_search_on_copies_too(self):
        staff = User.objects.create(
            fullname='Smoke Staff', email='smoke-staff@example.invalid',
            password_hash=hash_password('SmokeTest123'),
            role='Staff', account_status='Active', modules='books')
        r = _signed_in(staff).get('/library-staff/books/?copies=4')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['books'].paginator.count, 4)


class ParseCopiesFilterTests(TestCase):
    """What the dropdown sends, and what someone types into the address bar."""

    def setUp(self):
        from library.views import COPIES_FILTER_CHOICES, parse_copies_filter
        self.parse = parse_copies_filter
        self.choices = COPIES_FILTER_CHOICES

    def test_a_bare_number_means_exactly_that_many(self):
        self.assertEqual(self.parse('3'), {'copies': 3})

    def test_a_trailing_plus_means_that_many_or_more(self):
        self.assertEqual(self.parse('3+'), {'copies__gte': 3})

    def test_whitespace_is_forgiven(self):
        self.assertEqual(self.parse('  2+  '), {'copies__gte': 2})

    def test_nothing_recognisable_means_no_filter(self):
        for raw in ('', None, 'banana', '0', '-1', '+', '1.5', '100', '3++'):
            self.assertIsNone(self.parse(raw), raw)

    def test_every_dropdown_option_parses(self):
        for value, label in self.choices:
            self.assertIsNotNone(self.parse(value), value)
