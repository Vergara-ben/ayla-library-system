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


class LabelTests(TestCase):

    def setUp(self):
        from .models import FloorPlan, Room, Shelf, ShelfLevel
        plan = FloorPlan.objects.create(name='Ground', floor_number=1, is_active=True)
        room = Room.objects.create(floor_plan=plan, name='Main', map_x=0, map_y=0)
        shelf = Shelf.objects.create(room=room, name='Shelf A', map_x=0, map_y=0)
        self.level = ShelfLevel.objects.create(shelf=shelf, level_number=1)
        self.shelved = Book.objects.create(title='Shelved', author='A', shelf_level=self.level, qr_code='q1')
        self.loose = Book.objects.create(title='Loose', author='B', qr_code='q2')
        self.client = _client(admin_id=_staff(modules='books').admin_id, admin_role='Staff')

    def _print(self, *books):
        return self.client.get('/admin-portal/book-qr-labels/',
                               {'ids': ','.join(str(b.book_id) for b in books)})

    def test_only_shelved_books_are_printed_and_marked(self):
        response = self._print(self.shelved, self.loose)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertEqual(response['X-Labels-Skipped'], '1')
        self.shelved.refresh_from_db()
        self.loose.refresh_from_db()
        self.assertIsNotNone(self.shelved.label_printed_at)
        self.assertIsNone(self.loose.label_printed_at)

    def test_a_sheet_of_only_unshelved_books_is_refused(self):
        result = self._print(self.loose).json()
        self.assertFalse(result['success'])
        self.assertIn('Shelf Manager', result['error'])

    def test_books_needing_a_label_are_counted_and_listed(self):
        page = self.client.get('/library-staff/books/')
        self.assertEqual(page.context['unlabelled_count'], 1)
        self.assertEqual(page.context['unshelved_count'], 1)
        ids = self.client.get('/library-staff/books/', {'label': 'missing', 'ids': 'all'}).json()['ids']
        self.assertEqual(ids, [self.shelved.book_id])
        self._print(self.shelved)
        self.assertEqual(self.client.get('/library-staff/books/').context['unlabelled_count'], 0)


class RegistrationFixTests(TestCase):

    def setUp(self):
        from django.core import mail  # noqa: F401
        self.applicant = Patron.objects.create(
            first_name='Ana', last_name='Cruz', email='ana@example.invalid', account_status='Pending',
            registration_channel='Online', credential_document='credentials/x.png', otp_verified=True)
        self.client = _client(admin_id=_staff(modules='patrons').admin_id, admin_role='Staff')

    def _reject(self, **data):
        return self.client.post('/admin-portal/reject-patron/%d/' % self.applicant.patron_id, data)

    def test_a_reason_is_required(self):
        self._reject(mode='reject')
        self.applicant.refresh_from_db()
        self.assertIsNone(self.applicant.archived_at)

    def test_ask_to_fix_keeps_it_pending_and_the_link_updates_it(self):
        self._reject(mode='fix', reason='The ID has expired')
        self.applicant.refresh_from_db()
        self.assertEqual(self.applicant.account_status, 'Pending')
        token = self.applicant.fix_token
        self.assertTrue(token)
        page = Client().get('/patron/registration/fix/%s/' % token)
        self.assertContains(page, 'The ID has expired')
        Client().post('/patron/registration/fix/%s/' % token,
                      {'first_name': 'Ana', 'last_name': 'Santos', 'patron_type': 'Student'})
        self.applicant.refresh_from_db()
        self.assertEqual(self.applicant.last_name, 'Santos')
        self.assertIsNone(self.applicant.fix_token)
        self.assertIsNotNone(self.applicant.resubmitted_at)
        self.assertEqual(Client().get('/patron/registration/fix/%s/' % token).status_code, 404)

    def test_reject_completely_with_a_reason(self):
        self._reject(mode='reject', reason='This is not a valid ID')
        self.assertFalse(Patron.objects.filter(patron_id=self.applicant.patron_id).exists())


class AlertTests(TestCase):

    def _alerts(self, user):
        c = _client(admin_id=user.admin_id, admin_role=user.role)
        return {i['key']: i for i in c.get('/portal/alerts/').json()['items']}

    def test_staff_get_alerts_for_their_modules_only(self):
        desk = _staff(modules='transactions')
        items = self._alerts(desk)
        self.assertEqual(set(items), {'extensions', 'overdue'})

    def test_a_new_registration_is_reported_but_not_one_waiting_on_the_applicant(self):
        admin = _staff(role='Admin', modules='')
        Patron.objects.create(first_name='Ana', last_name='Cruz', email='a@example.invalid',
                              account_status='Pending', otp_verified=True)
        Patron.objects.create(first_name='Ben', last_name='Lim', email='b@example.invalid',
                              account_status='Pending', otp_verified=True, fix_token='t')
        item = self._alerts(admin)['registrations']
        self.assertEqual(item['count'], 1)
        self.assertIn('Ana', item['text'])

    def test_signed_out_gets_nothing(self):
        self.assertEqual(Client().get('/portal/alerts/').status_code, 403)


class DonorTests(TestCase):

    def setUp(self):
        self.client = _client(admin_id=_staff(role='Admin').admin_id, admin_role='Admin')

    def _give(self, donor, day, title):
        return self.client.post('/admin-portal/inventory/receive/', {
            'source': 'Donation', 'donor_name': donor, 'donated_date': day,
            'items': json.dumps([{'title': title, 'author': 'Anon', 'quantity': 2}])})

    def test_a_donor_is_saved_once_and_matched_however_it_is_typed(self):
        from .models import Donor
        self._give('Brgy. Sala Council', '2026-06-01', 'Noli')
        self._give('  brgy. sala   council ', '2026-09-01', 'Fili')
        self.assertEqual(Donor.objects.count(), 1)
        page = self.client.get('/admin-portal/donors/')
        donor = page.context['donors'][0]
        self.assertEqual((donor.gifts, donor.titles, donor.copies), (2, 2, 4))
        self.assertEqual(donor.every_days, 92)

    def test_donor_details_can_be_saved_and_history_shown(self):
        from .models import Donor
        self._give('Rotary Club', '2026-09-01', 'Noli')
        donor = Donor.objects.get()
        self.client.post('/admin-portal/donors/', {'donor_id': donor.donor_id, 'name': 'Rotary Club',
                                                   'donor_type': 'Organization', 'contact_number': '0917'})
        donor.refresh_from_db()
        self.assertEqual((donor.donor_type, donor.contact_number), ('Organization', '0917'))
        page = self.client.get('/admin-portal/donors/', {'d': donor.donor_id})
        self.assertContains(page, 'Noli')

    def test_the_donors_report(self):
        from datetime import date
        from .reports import build_report
        self._give('Rotary Club', '2026-09-01', 'Noli')
        report = build_report('donors', date(2026, 1, 1), date(2026, 12, 31))
        self.assertEqual(report['rows'][0][0], 'Rotary Club')
        self.assertEqual(report['rows'][0][5], 2)

    def test_receiving_suggests_past_donors(self):
        self._give('Rotary Club', '2026-09-01', 'Noli')
        page = self.client.get('/admin-portal/inventory/')
        self.assertContains(page, '<script id="donorNamesData" type="application/json">["Rotary Club"]</script>', html=False)


class IsbnLookupTests(TestCase):

    def setUp(self):
        self.client = _client(admin_id=_staff(modules='books').admin_id, admin_role='Staff')

    def _lookup(self, isbn):
        return self.client.get('/admin-portal/isbn-lookup/', {'isbn': isbn}).json()

    def test_a_book_we_hold_is_filled_from_the_catalogue(self):
        Book.objects.create(title='Noli Me Tangere', author='Rizal, Jose', ISBN='978-0-306-40615-7',
                            genre='Fiction', publication_year=1887)
        result = self._lookup('9780306406157')
        self.assertEqual(result['source'], 'catalogue')
        self.assertEqual(result['book']['title'], 'Noli Me Tangere')
        self.assertEqual(result['copies'], 1)

    def test_otherwise_open_library_is_asked(self):
        from unittest import mock
        found = {'title': 'Opticks', 'author': 'Isaac Newton', 'publication_year': '1979',
                 'cover_img_url': ''}
        with mock.patch('library.views._open_library', return_value=found) as asked:
            result = self._lookup('0-306-40615-2')
        asked.assert_called_once_with('0306406152')
        self.assertEqual(result['source'], 'openlibrary')
        self.assertEqual(result['book']['author'], 'Isaac Newton')

    def test_a_bad_isbn_is_not_looked_up(self):
        from unittest import mock
        with mock.patch('library.views._open_library') as asked:
            result = self._lookup('12345')
        asked.assert_not_called()
        self.assertFalse(result['success'])

    def test_the_add_form_shelves_the_book_and_skips_status(self):
        page = self.client.get('/library-staff/books/').content.decode()
        form = page.split('id="addBookForm"')[1].split('</form>')[0]
        self.assertIn('name="shelf_level"', form)
        self.assertIn('<input type="hidden" name="status" value="Available">', form)
        self.assertNotIn('cover_img_url', form)
        self.assertLess(form.index('id="addIsbn"'), form.index('id="addTitle"'))


class AuditTrailTests(TestCase):

    def setUp(self):
        from .models import FloorPlan, Room, Shelf, ShelfLevel
        plan = FloorPlan.objects.create(name='Ground', floor_number=1, is_active=True)
        room = Room.objects.create(floor_plan=plan, name='Main', map_x=0, map_y=0)
        shelf = Shelf.objects.create(room=room, name='Shelf A', map_x=0, map_y=0)
        self.level = ShelfLevel.objects.create(shelf=shelf, level_number=1)
        self.here = Book.objects.create(title='Here', author='A', shelf_level=self.level)
        self.gone = Book.objects.create(title='Gone', author='B', shelf_level=self.level)
        self.out = Book.objects.create(title='Out', author='C', shelf_level=self.level, status='Borrowed')
        self.reading = Book.objects.create(title='Reading', author='D', shelf_level=self.level,
                                           status='Being Read')
        reader = Patron.objects.create(first_name='Ana', last_name='Cruz', email='a@example.invalid')
        Transaction.objects.create(book=self.out, patron=reader, transaction_type='Borrow',
                                   due_date=timezone.localdate())
        self.client = _client(admin_id=_staff(role='Admin').admin_id, admin_role='Admin')

    def test_books_away_are_recorded_without_being_marked(self):
        r = self.client.post('/admin-portal/inventory/audit/file/', {
            'level': self.level.shelf_level_id,
            'found_ids': [self.here.book_id], 'missing_ids': [self.gone.book_id]}).json()
        self.assertTrue(r['success'], r)
        from .models import StockAuditLine
        results = {l.title: (l.result, l.status_then, l.detail) for l in StockAuditLine.objects.all()}
        self.assertEqual(results['Here'][0], 'Found')
        # "Then" is how the count left it, so the count's own change is not "changed since".
        self.assertEqual(results['Gone'][:2], ('Missing', 'Missing'))
        self.assertEqual(results['Out'][0], 'On loan')
        self.assertIn('Ana', results['Out'][2])
        self.assertEqual(results['Reading'][0], 'Being read')

        # Later the borrowed book comes back: the record shows then and now side by side.
        self.out.status = 'Available'
        self.out.save()
        detail = self.client.get('/admin-portal/inventory/audit/%d/' % r['audit_id']).json()
        out = next(l for l in detail['lines'] if l['title'] == 'Out')
        self.assertEqual((out['status_then'], out['status_now'], out['changed']), ('Borrowed', 'Available', True))


class ReviewFollowUpTests(TestCase):
    """Fixes from re-checking the panel work."""

    def test_an_accession_number_is_not_mistaken_for_a_bad_isbn(self):
        from .views import isbn_typo
        self.assertIsNone(isbn_typo('ACC-2026-0042'))
        self.assertIsNone(isbn_typo('12345'))
        self.assertIn('check digit', isbn_typo('9780306406158'))

    def test_books_can_be_marked_as_already_labelled(self):
        book = Book.objects.create(title='Old', author='A')
        c = _client(admin_id=_staff(modules='books').admin_id, admin_role='Staff')
        result = c.post('/admin-portal/mark-labelled/', {'ids': str(book.book_id)}).json()
        self.assertEqual(result['marked'], 1)
        book.refresh_from_db()
        self.assertIsNotNone(book.label_printed_at)

    def test_a_desk_sign_up_cannot_be_sent_a_fix_link(self):
        applicant = Patron.objects.create(first_name='Ben', last_name='Lim', account_status='Pending',
                                          registration_channel='On-site')
        c = _client(admin_id=_staff(modules='patrons').admin_id, admin_role='Staff')
        c.post('/admin-portal/reject-patron/%d/' % applicant.patron_id,
               {'mode': 'fix', 'reason': 'The ID has expired'})
        applicant.refresh_from_db()
        self.assertIsNone(applicant.fix_token)

    def test_a_resubmission_raises_an_alert_naming_that_applicant(self):
        admin = _staff(role='Admin', modules='')
        Patron.objects.create(first_name='Ana', last_name='Cruz', email='a@example.invalid',
                              account_status='Pending', otp_verified=True)
        back = Patron.objects.create(first_name='Ben', last_name='Lim', email='b@example.invalid',
                                     account_status='Pending', otp_verified=True,
                                     resubmitted_at=timezone.now())
        c = _client(admin_id=admin.admin_id, admin_role='Admin')
        item = {i['key']: i for i in c.get('/portal/alerts/').json()['items']}['registrations']
        self.assertIn('Ben', item['text'])
        self.assertIn('resubmitted', item['text'])
        self.assertEqual(item['stamp'], back.resubmitted_at.isoformat())

    def test_a_donor_can_be_added_before_their_first_gift(self):
        from .models import Donor
        c = _client(admin_id=_staff(role='Admin').admin_id, admin_role='Admin')
        c.post('/admin-portal/donors/', {'action': 'add', 'name': 'Rotary Club'})
        c.post('/admin-portal/donors/', {'action': 'add', 'name': 'rotary  club'})
        self.assertEqual(Donor.objects.count(), 1)

    def test_renaming_a_donated_copys_donor_moves_the_donation(self):
        from .models import Donor
        c = _client(admin_id=_staff(role='Admin').admin_id, admin_role='Admin')
        c.post('/admin-portal/inventory/receive/', {
            'source': 'Donation', 'donor_name': 'Rotary Clubb', 'donated_date': '2026-09-01',
            'items': json.dumps([{'title': 'Noli', 'author': 'Rizal', 'quantity': 1}])})
        record = InventoryRecord.objects.get()
        from .views import _sync_donation_row
        record.donor_name = 'Rotary Club'
        record.save()
        _sync_donation_row(record)
        record.donation.refresh_from_db()
        self.assertEqual(record.donation.donor.name, 'Rotary Club')


class OneExtensionPerLoanTests(TestCase):

    def setUp(self):
        rule = BorrowingRule.current()
        rule.loan_period_days = 7
        rule.save()
        self.today = timezone.localdate()
        self.patron = Patron.objects.create(first_name='Ana', last_name='Cruz',
                                            email='ana@example.invalid', account_status='Active')
        self.loans = []
        for title in ('Noli', 'Fili'):
            book = Book.objects.create(title=title, author='Rizal', status='Borrowed')
            self.loans.append(Transaction.objects.create(
                book=book, patron=self.patron, transaction_type='Borrow',
                due_date=self.today + timedelta(days=7)))
        self.client = _client(patron_id=self.patron.patron_id)
        self.staff = _client(admin_id=_staff().admin_id, admin_role='Staff')

    def _ask(self, loan):
        return self.client.post('/patron/request-extension/', {'transaction_id': loan.transaction_id}).json()

    def test_each_loan_can_be_extended_once(self):
        self.assertTrue(self._ask(self.loans[0])['success'])
        ext = DueDateExtension.objects.get()
        self.staff.post('/admin-portal/respond-to-extension/',
                        {'extension_id': ext.extension_id, 'action': 'decline'},
                        HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        again = self._ask(self.loans[0])
        self.assertFalse(again['success'])
        self.assertIn('once', again['error'])

    def test_a_staff_due_date_change_does_not_use_it_up(self):
        self.staff.post('/admin-portal/adjust-due-date/', {
            'transaction_id': self.loans[0].transaction_id,
            'new_due_date': (self.today + timedelta(days=10)).isoformat()})
        self.assertTrue(self._ask(self.loans[0])['success'])

    def test_returning_one_book_leaves_the_other_extendable(self):
        self.loans[0].return_date = timezone.now()
        self.loans[0].save()
        self.assertFalse(self._ask(self.loans[0])['success'])
        self.assertTrue(self._ask(self.loans[1])['success'])

    def test_all_loans_in_one_tap(self):
        page = self.client.get('/patron/account/')
        self.assertContains(page, 'Request extension for all (2)')
        result = self.client.post('/patron/request-extension/all/').json()
        self.assertEqual(len(result['requested']), 2)
        self.assertEqual(DueDateExtension.objects.count(), 2)
        again = self.client.post('/patron/request-extension/all/').json()
        self.assertFalse(again['success'])
        self.assertEqual(len(again['skipped']), 2)

    def test_extend_all_skips_an_overdue_book(self):
        self.loans[1].due_date = self.today - timedelta(days=1)
        self.loans[1].save()
        result = self.client.post('/patron/request-extension/all/').json()
        self.assertEqual([r['title'] for r in result['requested']], ['Noli'])
        self.assertEqual(result['skipped'][0]['title'], 'Fili')


class ReceivingPickerTests(TestCase):

    def test_the_receiving_forms_offer_search_instead_of_a_long_dropdown(self):
        from .models import Donor
        Donor.for_name('Rotary Club')
        Book.objects.create(title='Noli', author='Rizal')
        c = _client(admin_id=_staff(modules='inventory').admin_id, admin_role='Staff')
        page = c.get('/library-staff/receiving/').content.decode()
        self.assertIn('id="rcvBookSearch"', page)
        self.assertIn('js/picker.js', page)
        self.assertIn('<script id="donorNamesData" type="application/json">["Rotary Club"]</script>', page)
