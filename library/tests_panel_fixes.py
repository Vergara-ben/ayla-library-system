"""Fixes from the panel review: loan extensions, and what a received copy is recorded as."""

import io
import json
from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.utils import timezone

from .auth_utils import hash_password
from .models import (
    Book, BorrowingRule, DueDateExtension, InventoryRecord, Patron, Transaction, User,
)
from .views import isbn_error


def _staff(role='Staff', modules='transactions,books,inventory,donations'):
    return User.objects.create(fullname='Desk', email='desk-%s@example.invalid' % role.lower(),
                               password_hash=hash_password('DeskTest123'),
                               role=role, account_status='Active', modules=modules)


def _client(**session_values):
    c = Client()
    s = c.session
    for key, value in session_values.items():
        s[key] = value
    s.save()
    return c


class ExtensionTests(TestCase):

    def setUp(self):
        rule = BorrowingRule.current()
        rule.loan_period_days = 2
        rule.save()
        self.today = timezone.localdate()
        self.patron = Patron.objects.create(first_name='Ana', last_name='Cruz',
                                            email='ana@example.invalid', account_status='Active')
        self.book = Book.objects.create(title='Noli', author='Rizal', status='Borrowed')
        self.loan = Transaction.objects.create(
            book=self.book, patron=self.patron, transaction_type='Borrow',
            due_date=self.today + timedelta(days=2))
        self.patron_client = _client(patron_id=self.patron.patron_id)
        self.staff = _staff()
        self.staff_client = _client(admin_id=self.staff.admin_id, admin_role='Staff')

    def _request(self, reason='Still reading'):
        return self.patron_client.post('/patron/request-extension/',
                                       {'transaction_id': self.loan.transaction_id,
                                        'reason': reason}).json()

    def _respond(self, extension, action='approve'):
        return self.staff_client.post('/admin-portal/respond-to-extension/',
                                      {'extension_id': extension.extension_id,
                                       'action': action},
                                      HTTP_X_REQUESTED_WITH='XMLHttpRequest').json()

    def test_borrowed_today_and_extended_today_moves_the_due_date(self):
        # The panel's case: due in 2 days, extended the same day, still due in 2 days.
        self.assertTrue(self._request()['success'])
        ext = DueDateExtension.objects.get(transaction=self.loan)
        self.assertEqual(ext.requested_due_date, self.today + timedelta(days=4))
        self.assertTrue(self._respond(ext)['success'])
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.due_date, self.today + timedelta(days=4))

    def test_an_overdue_loan_cannot_be_extended(self):
        self.loan.due_date = self.today - timedelta(days=1)
        self.loan.overdue_flag = True
        self.loan.save()
        result = self._request()
        self.assertFalse(result['success'])
        self.assertIn('overdue', result['error'])
        self.assertFalse(DueDateExtension.objects.exists())

    def test_a_request_made_before_the_fix_still_adds_a_period_when_approved(self):
        stale = DueDateExtension.objects.create(
            transaction=self.loan, requested_by_patron=True,
            previous_due_date=self.loan.due_date, requested_due_date=self.loan.due_date)
        self.assertTrue(self._respond(stale)['success'])
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.due_date, self.today + timedelta(days=4))



class ReceivingConditionTests(TestCase):

    def setUp(self):
        self.client = _client(admin_id=_staff(role='Admin').admin_id)

    def _receive(self, items, source='Purchase', **extra):
        data = {'source': source, 'items': json.dumps(items)}
        data.update(extra)
        return self.client.post('/admin-portal/inventory/receive/', data, follow=True)

    def test_condition_defaults_to_good(self):
        self._receive([{'title': 'Ibong Adarna', 'author': 'Anon', 'quantity': 1}])
        self.assertEqual(InventoryRecord.objects.get().condition, 'Good')

    def test_lost_or_withdrawn_is_not_a_way_to_arrive(self):
        for condition in ('Lost', 'Withdrawn'):
            self._receive([{'title': 'Ibong Adarna', 'author': 'Anon', 'quantity': 1,
                            'condition': condition}])
        self.assertFalse(Book.objects.filter(title='Ibong Adarna').exists())

    def test_worn_arrives_as_worn_on_the_copy_and_the_book(self):
        self._receive([{'title': 'Ibong Adarna', 'author': 'Anon', 'quantity': 2, 'condition': 'Worn'}])
        books = Book.objects.filter(title='Ibong Adarna')
        self.assertEqual(set(books.values_list('condition', flat=True)), {'Worn'})
        self.assertEqual(set(InventoryRecord.objects.filter(book__in=books)
                             .values_list('condition', flat=True)), {'Worn'})

    def test_worn_copies_are_still_on_hand(self):
        self._receive([{'title': 'Ibong Adarna', 'author': 'Anon', 'quantity': 1, 'condition': 'Worn'}])
        self.assertTrue(InventoryRecord.objects.get().counts_as_held)

    def test_a_mistyped_isbn_is_refused(self):
        page = self._receive([{'title': 'Ibong Adarna', 'author': 'Anon', 'quantity': 1,
                               'condition': 'Good', 'isbn': '978-971-23-1234-0'}])
        self.assertContains(page, 'wrong check digit')
        self.assertFalse(Book.objects.filter(title='Ibong Adarna').exists())

    def test_the_form_offers_only_arrival_conditions(self):
        page = self.client.get('/admin-portal/inventory/').content.decode()
        picker = page.split('id="rcvCondition"')[1].split('</select>')[0]
        self.assertIn('Worn', picker)
        self.assertNotIn('Lost', picker)
        self.assertNotIn('Withdrawn', picker)


class DonationImportConditionTests(TestCase):

    def setUp(self):
        self.client = _client(admin_id=_staff(role='Admin').admin_id)

    def _import(self, rows):
        import openpyxl
        wb = openpyxl.Workbook()
        wb.active.append(['Donor', 'Title', 'Author', 'Condition'])
        for row in rows:
            wb.active.append(row)
        buf = io.BytesIO()
        wb.save(buf)
        upload = SimpleUploadedFile('d.xlsx', buf.getvalue(), content_type=(
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'))
        return self.client.post('/admin-portal/import-donations/',
                                {'excel_file': upload}, HTTP_X_REQUESTED_WITH='XMLHttpRequest').json()

    def test_imported_condition_is_kept(self):
        self._import([['Barangay', 'Florante at Laura', 'Balagtas', 'Damaged']])
        self.assertEqual(InventoryRecord.objects.get().condition, 'Damaged')
        self.assertEqual(Book.objects.get(title='Florante at Laura').condition, 'Damaged')

    def test_a_blank_condition_is_good_and_a_wrong_one_is_reported(self):
        result = self._import([['Barangay', 'Florante at Laura', 'Balagtas', ''],
                               ['Barangay', 'Noli Me Tangere', 'Rizal', 'Lost']])
        self.assertIn('condition must be Good, Worn or Damaged (got "Lost")', result['message'])
        self.assertEqual(InventoryRecord.objects.get().condition, 'Good')


class IsbnTests(TestCase):

    def test_valid_isbns(self):
        for isbn in ('978-0-306-40615-7', '0306406152', '0-8044-2957-X', '9780306406157'):
            self.assertIsNone(isbn_error(isbn), isbn)

    def test_invalid_isbns(self):
        self.assertIn('check digit', isbn_error('9780306406158'))
        self.assertIn('10 or 13 digits', isbn_error('12345'))
        self.assertIn('10 or 13 digits', isbn_error('ABCDEFGHIJ'))


class BeaconMinorTests(TestCase):
    UUID = 'FDA50693-A4E2-4FB1-AFCF-C6EB07647825'

    def setUp(self):
        from .models import FloorPlan
        self.plan = FloorPlan.objects.create(name='Ground', floor_number=1, is_active=True)
        self.client = _client(admin_id=_staff(role='Admin').admin_id, admin_role='Admin')

    def _place(self, **extra):
        data = {'floor_plan_id': self.plan.floor_plan_id, 'beacon_uuid': self.UUID,
                'map_x': 10, 'map_y': 10}
        data.update(extra)
        return self.client.post('/admin-portal/add-beacon/', data).json()

    def test_minor_and_label_are_filled_in(self):
        first = self._place()
        second = self._place()
        self.assertEqual((first['beacon']['minor'], second['beacon']['minor']), (1, 2))
        self.assertEqual(second['beacon']['label'], 'Beacon 2')
        self.assertEqual(second['beacon']['major'], 1)

    def test_a_freed_minor_is_offered_again(self):
        self._place(minor=1)
        self._place(minor=3)
        r = self.client.get('/admin-portal/next-beacon-minor/', {'uuid': self.UUID.lower(), 'major': 1})
        self.assertEqual(r.json()['minor'], 2)

    def test_a_duplicate_minor_is_refused(self):
        self._place(minor=5)
        result = self._place(minor=5, label='Other')
        self.assertFalse(result['success'])
        self.assertIn('already uses these identifiers', result['error'])
