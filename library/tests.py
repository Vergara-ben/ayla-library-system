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

from django.test import TestCase, Client
from django.utils import timezone

from .auth_utils import hash_password, password_length_error
from .models import (
    Book, BorrowingRule, LoginAttempt, Patron, PatronLog, Transaction, User,
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
