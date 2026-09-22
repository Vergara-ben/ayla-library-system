"""Report generation for the admin panel."""

import math
from datetime import date, datetime, timedelta
from decimal import Decimal
from io import BytesIO

from django.db.models import Count, Q
from django.utils import timezone

from .models import (Transaction, Patron, PatronLog, Book, Donation,
                     InventoryRecord, StockMovement, BorrowingRule)


REPORT_TYPES = [
    ('transactions', 'Transactions Report'),
    ('patron_logs', 'Patron Logs Report'),
    ('books', 'Books Report'),
    ('patrons', 'Patrons Report'),
    ('donations', 'Donations Report'),
    ('stock_levels', 'Stock Levels Report'),
    ('stock_movement', 'Stock Movement Report'),
    ('unreturned', 'Unreturned Books Report'),
    ('penalties', 'Penalties Report'),
    ('analytics', 'Borrowing Analytics Report'),
]

# Reports that represent a real-time snapshot rather than a date range.
SNAPSHOT_REPORTS = {'books', 'patrons', 'stock_levels', 'unreturned'}

LIBRARY_NAME = 'Ayla Public Library'
LIBRARY_LOCATION = 'Brgy. Sala, Cabuyao, Laguna'
# Hours shown in the peak-hour statistics: 8 AM up to the 5 PM hour.
OPEN_HOURS = range(8, 18)


# Date handling
def parse_date_range(start_str, end_str):
    """Parse YYYY-MM-DD strings, falling back to a sensible 30-day window."""
    today = timezone.localdate()

    def _parse(value, default):
        if not value:
            return default
        try:
            return datetime.strptime(value, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            return default

    start = _parse(start_str, today - timedelta(days=30))
    end = _parse(end_str, today)
    if start > end:
        start, end = end, start
    return start, end


def _period_label(report_type, start, end):
    if report_type in SNAPSHOT_REPORTS:
        return f"As of {timezone.localdate().strftime('%B %d, %Y')}"
    return f"{start.strftime('%B %d, %Y')} — {end.strftime('%B %d, %Y')}"


def _fmt_date(value):
    return value.strftime('%Y-%m-%d') if value else '—'


def _fmt_dt(value):
    return timezone.localtime(value).strftime('%Y-%m-%d %H:%M') if value else '—'


# Chart geometry.

CHART_W, CHART_H = 720, 240
CHART_PAD_L, CHART_PAD_R, CHART_PAD_T, CHART_PAD_B = 46, 16, 20, 32


def _nice_max(n):
    """A round ceiling for the value axis, so gridlines land on whole units."""
    if n <= 5:
        return 5
    step = 5 if n <= 50 else 10 if n <= 100 else 25 if n <= 500 else 100
    return int(math.ceil(n / step) * step)


def _axis_label(value):
    return int(value) if float(value).is_integer() else round(value, 1)


def _chart(series, kind='line', title='', note='', empty_note='',
           highlight=None, axis_note='', total=None):
    """Geometry for one chart."""
    plot_w = CHART_W - CHART_PAD_L - CHART_PAD_R
    plot_h = CHART_H - CHART_PAD_T - CHART_PAD_B
    base_y = CHART_PAD_T + plot_h

    values = [v for _lbl, v in series]
    # Optional total for percentage shares.
    charted = sum(values)
    total = charted if total is None else total
    y_max = _nice_max(max(values)) if values else 5

    # Past a dozen labels they collide.
    count = len(series)
    label_every = 1 if count <= 12 else 2 if count <= 26 else max(1, count // 12)

    def _share(v):
        return round((v / total) * 100, 1) if total else 0

    points, bars = [], []

    if kind == 'line':
        for i, (label, value) in enumerate(series):
            x = (CHART_PAD_L + plot_w * i / (count - 1)) if count > 1 else CHART_PAD_L + plot_w / 2
            y = base_y - (value / y_max * plot_h if y_max else 0)
            points.append({
                'x': round(x, 2),
                'y': round(y, 2),
                'value_y': round(y - 14, 2),      # callout sits above the point
                'label': label,
                'axis_label': label,
                
                'value': value,
                'share': _share(value),
                'is_peak': (highlight is not None and label == highlight),
                'show_label': (i % label_every == 0) or i == count - 1
                              or (highlight is not None and label == highlight),
            })
    else:
        slot = (plot_w / count) if count else plot_w
        bar_w = min(slot * 0.6, 56)
        # Shorten long category names on the axis.
        max_chars = max(6, int(slot / 4.6))
        for i, (label, value) in enumerate(series):
            cx = CHART_PAD_L + slot * (i + 0.5)
            h = (value / y_max * plot_h) if y_max else 0
            bars.append({
                'x': round(cx - bar_w / 2, 2),
                'w': round(bar_w, 2),
                'y': round(base_y - h, 2),
                'h': round(h, 2),
                'cx': round(cx, 2),
                'value_y': round(base_y - h - 6, 2),
                'label': label,
                'axis_label': (label if len(label) <= max_chars
                               else label[:max_chars - 1].rstrip() + '…'),
                'value': value,
                'share': _share(value),
                'is_peak': (highlight is not None and label == highlight),
                'show_label': (i % label_every == 0) or i == count - 1
                              or (highlight is not None and label == highlight),
            })

    line = ' '.join(f"{p['x']},{p['y']}" for p in points)
    # Close the area along the baseline.
    area = ''
    if points:
        area = (f"M {points[0]['x']},{base_y} "
                + ' '.join(f"L {p['x']},{p['y']}" for p in points)
                + f" L {points[-1]['x']},{base_y} Z")

    gridlines = []
    for step in range(5):
        y = round(CHART_PAD_T + plot_h * step / 4, 2)
        gridlines.append({
            'y': y,
            # Compute offsets here; the add filter drops decimals.
            'label_y': round(y + 3.5, 2),
            'label': _axis_label(y_max * (4 - step) / 4),
        })

    marks = points if kind == 'line' else bars
    peak_mark = next((m for m in marks if m['is_peak']), None)

    return {
        'kind': kind,
        'title': title,
        'note': note if marks else (empty_note or 'Nothing to chart in this period.'),
        'axis_note': axis_note,
        'width': CHART_W,
        'height': CHART_H,
        'plot_left': CHART_PAD_L,
        'plot_right': CHART_W - CHART_PAD_R,
        'axis_label_x': CHART_PAD_L - 8,
        'baseline_y': base_y,
        'tick_label_y': base_y + 16,
        'y_max': y_max,
        'total': total,
        'line': line,
        'area': area,
        'gridlines': gridlines,
        'points': points,
        'bars': bars,
        'marks': marks,          # whichever of the two this chart actually uses
        'peak_mark': peak_mark,
        'has_data': bool(marks) and charted > 0,
    }


def _days_in(start, end):
    """Every date in the range, so a quiet day is a gap in the line, not absent."""
    out, cur = [], start
    while cur <= end:
        out.append(cur)
        cur += timedelta(days=1)
    return out


def _day_label(d, span):
    """Short enough to fit the axis; the year is in the period heading already."""
    return d.strftime('%b %d') if span > 31 else d.strftime('%d %b')


# Individual report builders

DASH = '—'
# Ids listed in one cell before the rest are counted instead.
MAX_IDS_LISTED = 12


def _text(value):
    """A cell that is never the word None."""
    value = '' if value is None else str(value).strip()
    return value or DASH


def _book_location(book):
    """Where a copy sits, named the way the shelf itself is: Shelf A Column 1 Level 2."""
    if book is None:
        return DASH
    return book.location_label() or 'Not shelved'


def _card(patron):
    return patron.card_display if (patron and patron.card_number) else DASH


def _id_list(ids):
    """Ids in one cell, with a count instead of a list once there are too many."""
    ids = sorted(ids)
    if not ids:
        return DASH
    if len(ids) <= MAX_IDS_LISTED:
        return ', '.join(str(i) for i in ids)
    listed = ', '.join(str(i) for i in ids[:MAX_IDS_LISTED])
    return '%s and %d more' % (listed, len(ids) - MAX_IDS_LISTED)


def _copy_id(book):
    """The inventory copy for a book: one each since every copy is its own record."""
    copies = list(book.inventory_records.all()) if book else []
    return copies[0].inventory_id if copies else DASH


def _minutes_label(minutes):
    if minutes is None:
        return DASH
    if minutes >= 60:
        return '%dh %dm' % (minutes // 60, minutes % 60)
    return '%dm' % minutes


def _transactions(start, end):
    # A loan borrowed earlier but returned in this period is part of this period too.
    txns = (Transaction.objects
            .select_related('patron', 'book', 'processed_by')
            .filter(Q(transaction_date__range=(start, end))
                    | Q(return_date__range=(start, end)))
            .order_by('-transaction_date', '-transaction_id'))

    rows = []
    borrows = returns = in_library = overdue = 0
    fines = Decimal('0.00')
    per_day = {}
    for tx in txns:
        started_here = bool(tx.transaction_date and start <= tx.transaction_date <= end)
        returned_here = bool(tx.return_date and start <= tx.return_date <= end)
        if started_here:
            if tx.transaction_type == 'Borrow':
                borrows += 1
            elif tx.transaction_type == 'In-Library Reading':
                in_library += 1
            per_day[tx.transaction_date] = per_day.get(tx.transaction_date, 0) + 1
        if returned_here:
            returns += 1
            per_day[tx.return_date] = per_day.get(tx.return_date, 0) + 1
        if tx.overdue_flag:
            overdue += 1
        fines += tx.fine_amount or Decimal('0')
        rows.append([
            tx.transaction_id,
            _fmt_date(tx.transaction_date),
            tx.transaction_type,
            tx.book_id or DASH,
            _text(tx.book.title if tx.book else None),
            _text(tx.book.author if tx.book else None),
            tx.patron_id or DASH,
            _card(tx.patron),
            _text(tx.patron.fullname if tx.patron else 'In-library reader'),
            _fmt_date(tx.due_date),
            _fmt_date(tx.return_date),
            'Yes' if tx.overdue_flag else 'No',
            f'{tx.fine_amount:.2f}' if tx.fine_amount else '0.00',
            _text(tx.processed_by.fullname if tx.processed_by else None),
        ])

    days = _days_in(start, end)
    busiest_day = max(per_day.items(), key=lambda kv: (kv[1], kv[0]))[0] if per_day else None

    return {
        'key': 'transactions',
        'title': 'Transactions Report',
        'subtitle': 'Borrowing, returning, and in-library reading transactions',
        'columns': ['Transaction ID', 'Transaction Date', 'Type', 'Book ID', 'Book Title',
                    'Author', 'Patron ID', 'Card Number', 'Patron', 'Due Date', 'Date Returned',
                    'Overdue', 'Fine (PHP)', 'Processed By'],
        'rows': rows,
        'summary': [
            ('Transactions Listed', len(rows)),
            ('Borrows Started', borrows),
            ('Returns Completed', returns),
            ('In-Library Reading', in_library),
            ('Flagged Overdue', overdue),
            ('Fines On These Loans', f'{fines:.2f}'),
            ('Busiest Day', _fmt_date(busiest_day)),
        ],
        'chart': _chart(
            [(_day_label(d, len(days)), per_day.get(d, 0)) for d in days],
            kind='line',
            title='Transactions per day',
            note='A borrow counts on the day it started, a return on the day it came back. '
                 'Busiest day marked.',
            empty_note='No transactions in this period.',
            highlight=_day_label(busiest_day, len(days)) if busiest_day else None,
            axis_note='Date · vertical axis is number of transactions',
        ),
        'breakdown': {
            'By type': [
                ('Borrows started', str(borrows)),
                ('Returns completed', str(returns)),
                ('In-library reading', str(in_library)),
            ],
        },
    }


def _patron_logs(start, end):
    logs = (PatronLog.objects
            .select_related('patron')
            .filter(entry_time__date__range=(start, end))
            .order_by('-entry_time'))

    rows = []
    signed_out = assumed = ongoing = 0
    stay_minutes = []
    for lg in logs:
        minutes = None
        if lg.exit_time:
            minutes = int((lg.exit_time - lg.entry_time).total_seconds() // 60)
            if lg.auto_closed:
                assumed += 1
                exit_record = 'Assumed at closing'
            else:
                signed_out += 1
                exit_record = 'Signed out'
                stay_minutes.append(minutes)
        else:
            ongoing += 1
            exit_record = 'Still inside'
        rows.append([
            lg.log_id,
            lg.patron_id or DASH,
            _card(lg.patron),
            _text(lg.patron.fullname if lg.patron else None),
            _text(lg.patron.patron_type if lg.patron else None),
            _text(lg.school),
            _text(lg.purpose_of_visit),
            _fmt_dt(lg.entry_time),
            _fmt_dt(lg.exit_time),
            exit_record,
            _minutes_label(minutes),
        ])

    per_day, per_purpose = {}, {}
    for lg in logs:
        d = timezone.localtime(lg.entry_time).date()
        per_day[d] = per_day.get(d, 0) + 1
        purpose = lg.purpose_of_visit or 'Not stated'
        per_purpose[purpose] = per_purpose.get(purpose, 0) + 1
    days = _days_in(start, end)
    busiest_day = max(per_day.items(), key=lambda kv: (kv[1], kv[0]))[0] if per_day else None
    average_stay = (sum(stay_minutes) / len(stay_minutes)) if stay_minutes else None

    return {
        'key': 'patron_logs',
        'title': 'Patron Logs Report',
        'subtitle': 'Library visit entry and exit records',
        'columns': ['Visit ID', 'Patron ID', 'Card Number', 'Patron', 'Patron Type', 'School',
                    'Purpose', 'Entry Time', 'Exit Time', 'Exit Record', 'Time Inside'],
        'rows': rows,
        'summary': [
            ('Total Visits', len(rows)),
            ('Signed Out', signed_out),
            ('Assumed Exits', assumed),
            ('Still Inside', ongoing),
            ('Busiest Day', _fmt_date(busiest_day)),
            ('Visits Per Day', f'{len(rows) / len(days):.1f}' if days else '0.0'),
            ('Average Stay', _minutes_label(int(average_stay)) if average_stay else DASH),
        ],
        'chart': _chart(
            [(_day_label(d, len(days)), per_day.get(d, 0)) for d in days],
            kind='line',
            title='Visits per day',
            note='Busiest day marked. A day with no entries sits on the baseline.',
            empty_note='No visits were logged in this period.',
            highlight=_day_label(busiest_day, len(days)) if busiest_day else None,
            axis_note='Date · vertical axis is number of library entries',
        ),
        'breakdown': {
            'By purpose of visit': [
                (p, str(n)) for p, n in sorted(per_purpose.items(), key=lambda kv: -kv[1])
            ],
            'By exit record': [
                ('Signed out themselves', str(signed_out)),
                ('Assumed at closing time', str(assumed)),
                ('Still inside', str(ongoing)),
            ],
        },
    }


def _books(start, end):
    # Real-time catalog snapshot.
    books = (Book.objects
             .select_related('shelf_level__shelf')
             .prefetch_related('inventory_records')
             .order_by('title', 'book_id'))
    rows = []
    per_genre, per_status = {}, {}
    for b in books:
        per_status[b.status] = per_status.get(b.status, 0) + 1
        genre = (b.genre or '').strip() or 'Uncategorised'
        per_genre[genre] = per_genre.get(genre, 0) + 1
        rows.append([
            b.book_id,
            _copy_id(b),
            b.title,
            _text(b.author),
            _text(b.ISBN),
            _text(b.call_number),
            _text(b.genre),
            b.material_type,
            b.publication_year or DASH,
            b.condition,
            b.status,
            _book_location(b),
            b.shelf_slot if b.shelf_slot else DASH,
        ])
    ranked_genre = sorted(per_genre.items(), key=lambda kv: (-kv[1], kv[0]))[:12]

    def _held(status):
        return per_status.get(status, 0)

    return {
        'key': 'books',
        'title': 'Books Report',
        'subtitle': 'Complete book catalog listing, one row per copy',
        'columns': ['Book ID', 'Copy ID', 'Title', 'Author', 'ISBN', 'Call Number', 'Genre',
                    'Material Type', 'Publication Year', 'Condition', 'Status',
                    'Shelf Location', 'Shelf Slot'],
        'rows': rows,
        'summary': [
            ('Total Copies', len(rows)),
            ('Available', _held('Available')),
            ('Borrowed', _held('Borrowed')),
            ('Overdue', _held('Overdue')),
            ('Being Read', _held('Being Read')),
            ('For Reshelving', _held('For Reshelving')),
            ('Missing', _held('Missing')),
            ('Lost', _held('Lost')),
            ('Genres Held', len(per_genre)),
            ('Largest Genre', ranked_genre[0][0] if ranked_genre else DASH),
            ('Deleted Records Not Shown', Book.all_objects.count() - len(rows)),
        ],
        'chart': _chart(
            ranked_genre,
            kind='bar',
            total=len(rows),
            title='Collection by genre',
            note='Twelve largest genres. Largest marked.',
            empty_note='No books catalogued yet.',
            highlight=ranked_genre[0][0] if ranked_genre else None,
            axis_note='Genre · vertical axis is number of copies held',
        ),
        'breakdown': {
            'By status': [(k, str(v)) for k, v in sorted(per_status.items(), key=lambda kv: -kv[1])],
            'By genre': [(k, str(v)) for k, v in sorted(per_genre.items(), key=lambda kv: -kv[1])],
        },
    }


def _patrons(start, end):
    # Real-time patron directory snapshot.
    patrons = (Patron.objects
               .annotate(books_out=Count('transaction',
                                         filter=Q(transaction__transaction_type='Borrow',
                                                  transaction__return_date__isnull=True)))
               .order_by('fullname', 'patron_id'))
    rows = []
    per_status, per_type = {}, {}
    for p in patrons:
        per_status[p.account_status] = per_status.get(p.account_status, 0) + 1
        per_type[p.patron_type or 'Not stated'] = per_type.get(p.patron_type or 'Not stated', 0) + 1
        rows.append([
            p.patron_id,
            _card(p),
            p.fullname,
            _text(p.patron_type),
            _text(p.email),
            _text(p.contact_number),
            _text(p.address),
            _text(p.school),
            p.account_status,
            _fmt_date(p.registration_date),
            p.registration_channel,
            p.books_out,
        ])
    ranked_type = sorted(per_type.items(), key=lambda kv: (-kv[1], kv[0]))

    def _held(status):
        return per_status.get(status, 0)

    return {
        'key': 'patrons',
        'title': 'Patrons Report',
        'subtitle': 'Registered patron directory',
        'columns': ['Patron ID', 'Card Number', 'Name', 'Patron Type', 'Email',
                    'Contact Number', 'Address', 'School', 'Account Status',
                    'Date Registered', 'Registered Through', 'Books Out'],
        'rows': rows,
        'summary': [
            ('Total Patrons', len(rows)),
            ('Active', _held('Active')),
            ('Pending', _held('Pending')),
            ('Suspended', _held('Suspended')),
            ('Inactive', _held('Inactive')),
            ('Walk-in Visitors', _held('Visitor')),
            ('Largest Group', ranked_type[0][0] if ranked_type else DASH),
            ('Deleted Records Not Shown', Patron.all_objects.count() - len(rows)),
        ],
        'chart': _chart(
            ranked_type,
            kind='bar',
            title='Membership by patron type',
            note='Who the library actually serves. Largest group marked.',
            empty_note='No patrons registered yet.',
            highlight=ranked_type[0][0] if ranked_type else None,
            axis_note='Patron type · vertical axis is number of registered patrons',
        ),
        'breakdown': {
            'By account status': [(k, str(v)) for k, v
                                  in sorted(per_status.items(), key=lambda kv: -kv[1])],
            'By patron type': [(k, str(v)) for k, v in ranked_type],
        },
    }


def _donations(start, end):
    donations = (Donation.objects
                 .select_related('book')
                 .prefetch_related('inventory_copies')
                 .filter(date_donated__range=(start, end))
                 .order_by('-date_donated', '-donation_id'))

    rows = []
    per_status, per_month, donors = {}, {}, set()
    copies_total = 0
    for d in donations:
        per_status[d.status] = per_status.get(d.status, 0) + 1
        donors.add((d.donor_name or '').strip().lower())
        copies = d.inventory_copies.count() or 1
        copies_total += copies
        if d.date_donated:
            key = (d.date_donated.year, d.date_donated.month)
            per_month[key] = per_month.get(key, 0) + copies
        rows.append([
            d.donation_id,
            _fmt_date(d.date_donated),
            _text(d.donor_name),
            d.book_id or DASH,
            _text(d.book.title if d.book else None),
            _text(d.book.author if d.book else None),
            copies,
            d.status,
        ])

    months = []
    if per_month:
        y, m = min(per_month)
        last = max(per_month)
        while (y, m) <= last:
            months.append((y, m))
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)

    def _held(status):
        return per_status.get(status, 0)

    return {
        'key': 'donations',
        'title': 'Donations Report',
        'subtitle': 'Received donations from receipt through accession',
        'columns': ['Donation ID', 'Date Donated', 'Donor', 'Book ID', 'Book Title', 'Author',
                    'Copies', 'Status'],
        'rows': rows,
        'summary': [
            ('Donation Records', len(rows)),
            ('Copies Received', copies_total),
            ('Donors', len(donors)),
            ('Received', _held('Received')),
            ('Processing', _held('Processing')),
            ('Shelved', _held('Shelved')),
            ('Months With Donations', len(per_month)),
        ],
        'chart': _chart(
            [(date(y, m, 1).strftime('%b %Y'), per_month.get((y, m), 0)) for y, m in months],
            kind='line',
            title='Donated copies received per month',
            note='A month with no donations sits on the baseline.',
            empty_note='No donations were received in this period.',
            axis_note='Month · vertical axis is number of donated copies',
        ),
        'breakdown': {
            'By status': [
                (k, str(v)) for k, v in sorted(per_status.items(), key=lambda kv: -kv[1])
            ],
        },
    }


def _stock_levels(start, end):
    """Point-in-time count of physical copies by title and condition."""
    records = (InventoryRecord.objects
               .select_related('book', 'book__shelf_level', 'book__shelf_level__shelf')
               .order_by('book__title', 'title_hint', 'inventory_id'))

    # Group copies by the title and author they belong to.
    groups = {}
    totals = {'Good': 0, 'Damaged': 0, 'Missing': 0, 'Lost': 0, 'Withdrawn': 0}
    removed = 0
    for r in records:
        if r.status == 'Removed':
            removed += 1
            continue
        key = (r.display_title, (r.book.author if r.book else '') or '')
        g = groups.setdefault(key, {
            'title': r.display_title,
            'author': (r.book.author if r.book else '') or DASH,
            'location': _book_location(r.book) if r.book else 'Not shelved',
            'catalogued': r.book is not None,
            'book_ids': set(),
            'copy_ids': set(),
            'Good': 0, 'Damaged': 0, 'Missing': 0, 'Lost': 0, 'Withdrawn': 0,
        })
        if r.book_id:
            g['book_ids'].add(r.book_id)
        g['copy_ids'].add(r.inventory_id)
        # A copy not found in a stock count is not on hand, whatever its condition.
        bucket = 'Missing' if r.status == 'Missing' else r.condition
        if bucket in g:
            g[bucket] += 1
            totals[bucket] += 1

    # Every copy is its own book record, so a book with no copy row is a gap worth naming.
    uncounted_books = Book.objects.filter(inventory_records__isnull=True).count()

    rows = []
    for g in groups.values():
        on_hand = g['Good'] + g['Damaged']
        rows.append([
            g['title'],
            g['author'],
            _id_list(g['book_ids']),
            _id_list(g['copy_ids']),
            'Yes' if g['catalogued'] else 'No',
            g['location'],
            len(g['copy_ids']),
            g['Good'], g['Damaged'], g['Missing'], g['Lost'], g['Withdrawn'], on_hand,
        ])

    return {
        'key': 'stock_levels',
        'title': 'Stock Levels Report',
        'subtitle': 'Physical copies held, by title and condition',
        'columns': ['Title', 'Author', 'Book IDs', 'Copy IDs', 'Catalogued', 'Shelf Location',
                    'Copies', 'Good', 'Damaged', 'Missing', 'Lost', 'Withdrawn', 'On Hand'],
        'rows': rows,
        'summary': [
            ('Titles Held', len(rows)),
            ('Copies Recorded', sum(totals.values())),
            ('Copies On Hand', totals['Good'] + totals['Damaged']),
            ('Good', totals['Good']),
            ('Damaged', totals['Damaged']),
            ('Missing', totals['Missing']),
            ('Lost', totals['Lost']),
            ('Withdrawn', totals['Withdrawn']),
            ('Deaccessioned', removed),
            ('Books Without A Copy Record', uncounted_books),
        ],
        'chart': _chart(
            [(k, totals[k]) for k in ('Good', 'Damaged', 'Missing', 'Lost', 'Withdrawn')],
            kind='bar',
            title='Copies by state',
            note='The state of the physical collection. Largest group marked.',
            empty_note='No copies are on record yet.',
            highlight=max(totals, key=lambda k: totals[k]) if any(totals.values()) else None,
            axis_note='Condition, or Missing after a stock count · vertical axis is number of physical copies',
        ),
        'breakdown': {
            'By condition': [(k, str(totals[k]))
                             for k in ('Good', 'Damaged', 'Missing', 'Lost', 'Withdrawn')],
        },
    }


def _stock_movement(start, end):
    """Every stock-status change in the period, with actor, reason and source."""
    movements = (StockMovement.objects
                 .select_related('inventory_record', 'inventory_record__book')
                 .filter(timestamp__date__range=(start, end))
                 .order_by('-timestamp', '-movement_id'))

    rows = []
    counts = {}
    for m in movements:
        counts[m.action] = counts.get(m.action, 0) + 1
        record = m.inventory_record
        rows.append([
            m.movement_id,
            _fmt_dt(m.timestamp),
            record.inventory_id if record else DASH,
            (record.book_id if record and record.book_id else DASH),
            _text(record.display_title if record else None),
            m.get_action_display(),
            _text(m.condition_before),
            _text(m.condition_after),
            _text(m.actor_name),
            _text(m.reason),
            _text(m.source),
        ])

    summary = [('Total Movements', len(rows))]
    for key, label in StockMovement.ACTION_CHOICES:
        if counts.get(key):
            summary.append((label, counts[key]))

    labels = dict(StockMovement.ACTION_CHOICES)
    ranked_action = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    action_series = [(labels.get(k, k), v) for k, v in ranked_action]

    return {
        'key': 'stock_movement',
        'title': 'Stock Movement Report',
        'subtitle': 'Every change to a copy stock status, with actor and reason',
        'columns': ['Movement ID', 'Date & Time', 'Copy ID', 'Book ID', 'Book Title', 'Action',
                    'Condition Before', 'Condition After', 'Actor', 'Reason', 'Source'],
        'rows': rows,
        'summary': summary,
        'chart': _chart(
            action_series,
            kind='bar',
            title='Movements by action',
            note='What is actually happening to stock. Most frequent marked.',
            empty_note='No stock movements in this period.',
            highlight=action_series[0][0] if action_series else None,
            axis_note='Action · vertical axis is number of movements',
        ),
    }


_BUILDERS = {
    'transactions': _transactions,
    'patron_logs': _patron_logs,
    'books': _books,
    'patrons': _patrons,
    'donations': _donations,
    'stock_levels': _stock_levels,
    'stock_movement': _stock_movement,
}


def _unreturned(start, end):
    """Every borrowed copy that has not come back, oldest first."""
    txns = (Transaction.objects
            .select_related('patron', 'book')
            .filter(transaction_type='Borrow', return_date__isnull=True)
            .order_by('due_date', 'transaction_date'))

    today = timezone.localdate()
    rule = BorrowingRule.current()
    rows = []
    overdue = 0
    total_days_over = 0
    accruing = Decimal('0.00')
    days_over_all = []

    for tx in txns:
        days_over = (today - tx.due_date).days if tx.due_date else 0
        days_over_all.append(days_over)
        is_over = days_over > 0
        fine_today = Decimal('0.00')
        if is_over:
            overdue += 1
            total_days_over += days_over
            # What the fine would be if it came back today.
            billable = max(days_over - (rule.grace_period_days or 0), 0)
            fine_today = Decimal(billable) * (rule.fine_per_day or Decimal('0'))
            accruing += fine_today
        rows.append([
            tx.transaction_id,
            tx.book_id or DASH,
            _text(tx.book.title if tx.book else None),
            _text(tx.book.author if tx.book else None),
            tx.patron_id or DASH,
            _card(tx.patron),
            _text(tx.patron.fullname if tx.patron else 'In-library reader'),
            _text(tx.patron.contact_number if tx.patron else None),
            _text(tx.patron.email if tx.patron else None),
            _fmt_date(tx.transaction_date),
            _fmt_date(tx.due_date),
            str(days_over) if is_over else DASH,
            f'{fine_today:.2f}',
            'Overdue' if is_over else 'On loan',
        ])

    # How overdue, not just how many.
    buckets = [
        ('Not yet due', lambda d: d <= 0),
        ('1-7 days', lambda d: 1 <= d <= 7),
        ('8-30 days', lambda d: 8 <= d <= 30),
        ('31-90 days', lambda d: 31 <= d <= 90),
        ('Over 90 days', lambda d: d > 90),
    ]
    ageing = [(label, sum(1 for d in days_over_all if test(d))) for label, test in buckets]
    worst = max((b for b in ageing if b[1]), key=lambda kv: kv[1], default=None)

    return {
        'key': 'unreturned',
        'title': 'Unreturned Books Report',
        'subtitle': 'Every borrowed copy still out, oldest due date first',
        'columns': ['Transaction ID', 'Book ID', 'Book Title', 'Author', 'Patron ID',
                    'Card Number', 'Borrowed By', 'Contact Number', 'Email', 'Date Borrowed',
                    'Due Date', 'Days Overdue', 'Fine If Returned Today (PHP)', 'Status'],
        'rows': rows,
        'summary': [
            ('Still Out', len(rows)),
            ('Overdue', overdue),
            ('On Time', len(rows) - overdue),
            ('Total Days Overdue', total_days_over),
            ('Fines If Returned Today', f'{accruing:.2f}'),
            ('Longest Overdue', f'{max(days_over_all)} days'
                                if days_over_all and max(days_over_all) > 0 else DASH),
        ],
        'chart': _chart(
            ageing,
            kind='bar',
            title='How overdue the outstanding copies are',
            note='Largest group marked. Chasing is triaged by age, not by count.',
            empty_note='Nothing is out on loan.',
            highlight=worst[0] if worst else None,
            axis_note='Days past the due date · vertical axis is number of copies',
        ),
        'breakdown': {
            'By age': [(label, str(n)) for label, n in ageing],
        },
    }


def _penalties(start, end):
    """Fines actually charged, totalled by day, week and month."""
    txns = (Transaction.objects
            .select_related('patron', 'book')
            .filter(fine_amount__gt=0)
            .filter(Q(return_date__range=(start, end))
                    | Q(return_date__isnull=True, transaction_date__range=(start, end)))
            .order_by('-return_date', '-transaction_date'))

    today = timezone.localdate()
    by_day, by_week, by_month = {}, {}, {}
    total = Decimal('0.00')
    settled = Decimal('0.00')
    accruing = Decimal('0.00')
    rows = []
    for tx in txns:
        charged_on = tx.return_date or tx.transaction_date
        amount = tx.fine_amount or Decimal('0')
        total += amount
        if tx.return_date:
            settled += amount
            reason = 'Returned late'
            days_over = (tx.return_date - tx.due_date).days if tx.due_date else 0
        else:
            accruing += amount
            reason = 'Still out, fine still growing'
            days_over = (today - tx.due_date).days if tx.due_date else 0
        if tx.book and tx.book.status == 'Lost':
            reason = 'Reported lost'
        if charged_on:
            iso_year, iso_week, _ = charged_on.isocalendar()
            by_day[charged_on] = by_day.get(charged_on, Decimal('0')) + amount
            by_week[(iso_year, iso_week)] = by_week.get((iso_year, iso_week), Decimal('0')) + amount
            by_month[(charged_on.year, charged_on.month)] = (
                by_month.get((charged_on.year, charged_on.month), Decimal('0')) + amount)
        rows.append([
            tx.transaction_id,
            _fmt_date(charged_on),
            tx.patron_id or DASH,
            _card(tx.patron),
            _text(tx.patron.fullname if tx.patron else 'In-library reader'),
            tx.book_id or DASH,
            _text(tx.book.title if tx.book else None),
            _fmt_date(tx.due_date),
            str(days_over) if days_over > 0 else DASH,
            reason,
            f'{amount:.2f}',
        ])

    def _avg(bucket):
        return (total / len(bucket)) if bucket else Decimal('0')

    return {
        'key': 'penalties',
        'title': 'Penalties Report',
        'subtitle': 'Fines charged, with daily, weekly and monthly totals',
        'columns': ['Transaction ID', 'Date Charged', 'Patron ID', 'Card Number', 'Patron',
                    'Book ID', 'Book Title', 'Due Date', 'Days Overdue', 'Reason',
                    'Amount (PHP)'],
        'rows': rows,
        'summary': [
            ('Total Charged', f'{total:.2f}'),
            ('Penalties Issued', len(rows)),
            ('On Returned Loans', f'{settled:.2f}'),
            ('Still Accruing', f'{accruing:.2f}'),
            ('Days With Fines', len(by_day)),
            ('Average Per Day With Fines', f'{_avg(by_day):.2f}'),
            ('Average Per Week With Fines', f'{_avg(by_week):.2f}'),
            ('Average Per Month With Fines', f'{_avg(by_month):.2f}'),
        ],
        # Rendered as its own table on screen; the exporters ignore it.
        'chart': _chart(
            [(_day_label(d, len(_days_in(start, end))), float(by_day.get(d, 0)))
             for d in _days_in(start, end)],
            kind='line',
            title='Fines charged per day',
            note='A day charged is the day a loan came back, or today for a loan still out. '
                 'A day with no fines sits on the baseline.',
            empty_note='No fines were charged in this period.',
            axis_note='Date · vertical axis is pesos charged',
        ),
        'breakdown': {
            'Per day': [(_fmt_date(d), f'{v:.2f}') for d, v in sorted(by_day.items(), reverse=True)],
            'Per week': [(f'{y} week {w:02d}', f'{v:.2f}')
                         for (y, w), v in sorted(by_week.items(), reverse=True)],
            'Per month': [(date(y, m, 1).strftime('%B %Y'), f'{v:.2f}')
                          for (y, m), v in sorted(by_month.items(), reverse=True)],
        },
    }


def _analytics(start, end):
    """What gets borrowed, and when the library is busiest."""
    borrows = (Transaction.objects
               .select_related('book')
               .filter(transaction_type='Borrow', transaction_date__range=(start, end)))

    per_title = {}
    for tx in borrows:
        if not tx.book:
            continue
        key = (tx.book.title, tx.book.author or DASH)
        entry = per_title.setdefault(key, {'count': 0, 'book_ids': set()})
        entry['count'] += 1
        entry['book_ids'].add(tx.book_id)
    ranked = sorted(per_title.items(), key=lambda kv: (-kv[1]['count'], kv[0][0]))

    # Busiest hours. Every visit is counted, whatever the hour.
    visits = PatronLog.objects.filter(entry_time__date__range=(start, end))

    per_hour = {}
    per_weekday = {}
    total_visits = 0
    for log in visits:
        local = timezone.localtime(log.entry_time)
        per_hour[local.hour] = per_hour.get(local.hour, 0) + 1
        per_weekday[local.weekday()] = per_weekday.get(local.weekday(), 0) + 1
        total_visits += 1

    def _hour_label(h):
        # Format the hour by hand so it works on Windows.
        if h is None:
            return DASH
        suffix = 'AM' if h < 12 else 'PM'
        hour12 = h % 12 or 12
        return f'{hour12} {suffix}'

    def _hour_span(h):
        """Renders an hour as the range it is: 9 AM - 10 AM."""
        if h is None:
            return DASH
        return f'{_hour_label(h)} – {_hour_label((h + 1) % 24)}'

    # Opening hours, plus any hour that actually had visitors, so none are hidden.
    charted_hours = sorted(set(OPEN_HOURS) | set(per_hour))
    outside_hours = sum(n for h, n in per_hour.items() if h not in OPEN_HOURS)

    # The earlier hour wins a tie.
    counted = {h: per_hour.get(h, 0) for h in charted_hours}
    peak_hour, peak_count = (max(counted.items(), key=lambda kv: (kv[1], -kv[0]))
                             if counted else (None, 0))
    # Opening hours on their own, which is what staffing is planned around.
    open_counted = {h: per_hour.get(h, 0) for h in OPEN_HOURS}
    open_peak_hour, open_peak_count = (max(open_counted.items(), key=lambda kv: (kv[1], -kv[0]))
                                       if open_counted else (None, 0))
    quiet_hour, quiet_count = (min(open_counted.items(), key=lambda kv: (kv[1], kv[0]))
                               if open_counted else (None, 0))

    WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    busy_day, busy_day_count = (max(per_weekday.items(), key=lambda kv: (kv[1], -kv[0]))
                                if per_weekday else (None, 0))

    average_per_hour = (total_visits / len(charted_hours)) if charted_hours else 0
    peak_share = (peak_count / total_visits * 100) if total_visits else 0
    peak_vs_average = (peak_count / average_per_hour) if average_per_hour else 0

    hour_chart = _chart(
        [(_hour_label(h), counted[h]) for h in charted_hours],
        kind='line',
        title='Visits by hour of day',
        note='Every hour the library opens, plus any hour that had visitors. '
             'Busiest hour marked. Counts are library entries, not borrows.',
        empty_note='No visits were logged in this period.',
        highlight=_hour_label(peak_hour) if peak_hour is not None else None,
        axis_note='Hour of day · vertical axis is number of library entries',
    )

    rows = [[rank, _id_list(entry['book_ids']), title, author, entry['count']]
            for rank, ((title, author), entry) in enumerate(ranked[:50], start=1)]

    return {
        'key': 'analytics',
        'title': 'Borrowing Analytics Report',
        'subtitle': 'Most borrowed titles, and the hours the library is busiest',
        'columns': ['Rank', 'Book IDs', 'Book Title', 'Author', 'Times Borrowed'],
        'rows': rows,
        'summary': [
            ('Borrows In Period', sum(e['count'] for e in per_title.values())),
            ('Distinct Titles', len(per_title)),
            ('Most Borrowed', ranked[0][0][0] if ranked else DASH),
            ('Busiest Hour', _hour_span(peak_hour)),
            ('Visits In Busiest Hour', peak_count),
            ('Share Of Visits In Busiest Hour', f'{peak_share:.0f}%'),
            ('Busiest Hour vs Average', f'{peak_vs_average:.1f}×' if peak_vs_average else DASH),
            ('Busiest Opening Hour', _hour_span(open_peak_hour)),
            ('Visits In Busiest Opening Hour', open_peak_count),
            ('Busiest Day', WEEKDAYS[busy_day] if busy_day is not None else DASH),
            ('Quietest Opening Hour', _hour_span(quiet_hour)),
            ('Average Visits Per Hour', f'{average_per_hour:.1f}'),
            ('Visits Outside Opening Hours', outside_hours),
            ('Total Visits', total_visits),
        ],
        # Screen only.
        'chart': dict(hour_chart, peak_label=_hour_span(peak_hour), peak_count=peak_count),
        'breakdown': {
            'Visits by hour': [
                (_hour_span(h), str(counted[h])) for h in charted_hours
            ],
            'Visits by day of week': [
                (WEEKDAYS[d], str(per_weekday[d]))
                for d in sorted(per_weekday, key=lambda d: -per_weekday[d])
            ],
        },
    }


# Registered here rather than in the literal above because these three are defined below it.
_BUILDERS['unreturned'] = _unreturned
_BUILDERS['penalties'] = _penalties
_BUILDERS['analytics'] = _analytics


def build_report(report_type, start, end):
    """Return the report data dict for the given type and date range."""
    builder = _BUILDERS.get(report_type, _transactions)
    report = builder(start, end)
    report['period_label'] = _period_label(report['key'], start, end)
    return report


# Excel rendering
def render_report_excel(report):
    """Render a report dict to a formatted .xlsx and return a BytesIO buffer."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = report['title'][:31]

    col_count = len(report['columns'])
    last_col = get_column_letter(col_count) if col_count else 'A'

    accent = PatternFill('solid', fgColor='0E7490')
    white_bold = Font(bold=True, color='FFFFFF')
    thin = Side(style='thin', color='CFD8E3')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    def _title_row(text, size=14, bold=True):
        ws.append([text])
        r = ws.max_row
        ws.merge_cells(f'A{r}:{last_col}{r}')
        cell = ws.cell(row=r, column=1)
        cell.font = Font(bold=bold, size=size)
        cell.alignment = Alignment(horizontal='center')

    _title_row(LIBRARY_NAME, size=14)
    _title_row(LIBRARY_LOCATION, size=9, bold=False)
    _title_row(report['title'], size=12)
    _title_row(f"Reporting Period: {report['period_label']}", size=9, bold=False)
    summary_str = '    |    '.join(f"{label}: {value}" for label, value in report['summary'])
    if summary_str:
        _title_row(summary_str, size=9, bold=False)
    ws.append([])  # spacer

    # Header
    ws.append(report['columns'])
    header_row = ws.max_row
    for c in range(1, col_count + 1):
        cell = ws.cell(row=header_row, column=c)
        cell.font = white_bold
        cell.fill = accent
        cell.alignment = Alignment(horizontal='left', vertical='center')
        cell.border = border

    # Data
    for row in report['rows']:
        ws.append(list(row))
        for c in range(1, col_count + 1):
            ws.cell(row=ws.max_row, column=c).border = border

    if not report['rows']:
        ws.append(['No records found for this period.'])

    # Rough auto-width based on content length
    for c in range(1, col_count + 1):
        letter = get_column_letter(c)
        max_len = len(str(report['columns'][c - 1]))
        for row in report['rows']:
            if c - 1 < len(row):
                max_len = max(max_len, len(str(row[c - 1])))
        ws.column_dimensions[letter].width = min(max(max_len + 3, 12), 50)

    # Breakdowns get their own sheet rather than being dropped.
    breakdown = report.get('breakdown') or {}
    if breakdown:
        bs = wb.create_sheet('Breakdowns')
        bs.column_dimensions['A'].width = 28
        bs.column_dimensions['B'].width = 16
        for label, pairs in breakdown.items():
            bs.append([label])
            head = bs.cell(row=bs.max_row, column=1)
            head.font = white_bold
            head.fill = accent
            bs.cell(row=bs.max_row, column=2).fill = accent
            for name, value in pairs:
                bs.append([name, value])
                for c in (1, 2):
                    bs.cell(row=bs.max_row, column=c).border = border
            if not pairs:
                bs.append(['Nothing in this period.', ''])
            bs.append([])  # spacer between tables

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


# PDF rendering

def _pdf_chart(chart, avail_width):
    """Redraw an on-screen chart as ReportLab shapes."""
    from reportlab.graphics.shapes import (
        Drawing, Line, PolyLine, Polygon, String, Circle, Rect,
    )
    from reportlab.lib import colors

    if not chart or not chart.get('has_data'):
        return None

    cw, ch = chart['width'], chart['height']
    scale = avail_width / cw
    d = Drawing(avail_width, ch * scale)

    accent = colors.HexColor('#0e7490')
    grey = colors.HexColor('#8b98a9')
    faint = colors.HexColor('#d7dee7')

    def sx(x):
        return x * scale

    def sy(y):
        return (ch - y) * scale          # flip

    # Gridlines and value axis
    for g in chart['gridlines']:
        d.add(Line(sx(chart['plot_left']), sy(g['y']),
                   sx(chart['plot_right']), sy(g['y']),
                   strokeColor=faint, strokeWidth=0.5))
        d.add(String(sx(chart['axis_label_x']), sy(g['label_y']) + 1,
                     str(g['label']), fontSize=6.5, fillColor=grey,
                     textAnchor='end'))

    if chart['kind'] == 'line':
        # Volume under the line first, so the line sits on top of its own fill.
        pts = chart['points']
        if len(pts) > 1:
            poly = [sx(pts[0]['x']), sy(chart['baseline_y'])]
            for p in pts:
                poly += [sx(p['x']), sy(p['y'])]
            poly += [sx(pts[-1]['x']), sy(chart['baseline_y'])]
            d.add(Polygon(poly, fillColor=colors.Color(14 / 255, 116 / 255, 144 / 255, 0.10),
                          strokeColor=None))
            flat = []
            for p in pts:
                flat += [sx(p['x']), sy(p['y'])]
            d.add(PolyLine(flat, strokeColor=accent, strokeWidth=1.4,
                           strokeLineJoin=1, strokeLineCap=1))
        for p in pts:
            r = 2.4 if p['is_peak'] else 1.3
            d.add(Circle(sx(p['x']), sy(p['y']), r, fillColor=accent, strokeColor=None))
            if p['is_peak']:
                d.add(Line(sx(p['x']), sy(p['y']), sx(p['x']), sy(chart['baseline_y']),
                           strokeColor=accent, strokeWidth=0.5, strokeDashArray=[2, 2]))
                d.add(String(sx(p['x']), sy(p['value_y']), str(p['value']),
                             fontSize=7, fontName='Helvetica-Bold',
                             fillColor=accent, textAnchor='middle'))
    else:
        for b in chart['bars']:
            fill = accent if b['is_peak'] else colors.Color(14 / 255, 116 / 255, 144 / 255, 0.45)
            # ReportLab rectangles are anchored bottom-left.
            d.add(Rect(sx(b['x']), sy(chart['baseline_y']),
                       sx(b['w']), b['h'] * scale,
                       fillColor=fill, strokeColor=None))
            if b['value']:
                d.add(String(sx(b['cx']), sy(b['value_y']), str(b['value']),
                             fontSize=6.5, fontName='Helvetica-Bold',
                             fillColor=accent if b['is_peak'] else grey,
                             textAnchor='middle'))

    # Category axis
    d.add(Line(sx(chart['plot_left']), sy(chart['baseline_y']),
               sx(chart['plot_right']), sy(chart['baseline_y']),
               strokeColor=grey, strokeWidth=0.6))
    for m in chart['marks']:
        if not m.get('show_label'):
            continue
        x = m.get('cx', m.get('x'))
        d.add(String(sx(x), sy(chart['tick_label_y']), str(m.get('axis_label', m['label'])),
                     fontSize=6, fillColor=accent if m['is_peak'] else grey,
                     fontName='Helvetica-Bold' if m['is_peak'] else 'Helvetica',
                     textAnchor='middle'))
    return d


def _pdf_column_widths(report, usable_width):
    """Share the page between columns by how much each actually holds."""
    columns = report['columns']
    if not columns:
        return []
    # An id column needs a fraction of what a title column needs.
    weights = []
    for index, header in enumerate(columns):
        longest = len(str(header))
        for row in report['rows'][:400]:
            if index < len(row):
                longest = max(longest, len(str(row[index])))
        weights.append(min(max(longest, 5), 34))
    total = sum(weights)
    return [usable_width * w / total for w in weights]


def render_report_pdf(report):
    """Render a report dict to a PDF and return a BytesIO buffer."""
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether
    )

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=landscape(A4),
        leftMargin=14 * mm, rightMargin=14 * mm,
        topMargin=14 * mm, bottomMargin=14 * mm,
        title=report['title'],
    )

    # Narrower type once a report carries many columns.
    col_count = len(report['columns'])
    cell_size = 8 if col_count <= 10 else 7 if col_count <= 13 else 6.5

    styles = getSampleStyleSheet()
    org_style = ParagraphStyle('Org', parent=styles['Title'], fontSize=16,
                               spaceAfter=2, alignment=TA_CENTER)
    loc_style = ParagraphStyle('Loc', parent=styles['Normal'], fontSize=9,
                               textColor=colors.HexColor('#555555'),
                               alignment=TA_CENTER, spaceAfter=10)
    title_style = ParagraphStyle('RTitle', parent=styles['Heading1'], fontSize=13,
                                 alignment=TA_CENTER, spaceAfter=2)
    sub_style = ParagraphStyle('RSub', parent=styles['Normal'], fontSize=9,
                               textColor=colors.HexColor('#666666'),
                               alignment=TA_CENTER, spaceAfter=2)
    period_style = ParagraphStyle('RPeriod', parent=styles['Normal'], fontSize=9,
                                  alignment=TA_CENTER, spaceAfter=10,
                                  textColor=colors.HexColor('#333333'))
    summary_style = ParagraphStyle('RSummary', parent=styles['Normal'], fontSize=9,
                                   alignment=TA_CENTER, spaceAfter=10)
    cell_style = ParagraphStyle('Cell', parent=styles['Normal'], fontSize=cell_size,
                                leading=cell_size + 2)
    head_style = ParagraphStyle('Head', parent=styles['Normal'], fontSize=cell_size,
                                leading=cell_size + 2, textColor=colors.white,
                                fontName='Helvetica-Bold')
    chart_title_style = ParagraphStyle('ChartTitle', parent=styles['Normal'], fontSize=9.5,
                                       alignment=TA_CENTER, spaceBefore=4, spaceAfter=1,
                                       textColor=colors.HexColor('#0e7490'))
    chart_note_style = ParagraphStyle('ChartNote', parent=styles['Normal'], fontSize=7,
                                      alignment=TA_CENTER, spaceAfter=3,
                                      textColor=colors.HexColor('#777777'))
    foot_style = ParagraphStyle('Foot', parent=styles['Normal'], fontSize=7.5,
                                textColor=colors.HexColor('#999999'),
                                alignment=TA_CENTER, spaceBefore=12)

    elements = []
    elements.append(Paragraph(LIBRARY_NAME, org_style))
    elements.append(Paragraph(LIBRARY_LOCATION, loc_style))
    elements.append(Paragraph(report['title'], title_style))
    elements.append(Paragraph(report.get('subtitle', ''), sub_style))
    elements.append(Paragraph(f"Reporting Period: {report['period_label']}", period_style))

    summary_text = '&nbsp;&nbsp;|&nbsp;&nbsp;'.join(
        f"<b>{label}:</b> {value}" for label, value in report['summary']
    )
    if summary_text:
        elements.append(Paragraph(summary_text, summary_style))

    # The chart goes above the detail rows: it is the answer, and the rows are the working.
    chart = report.get('chart')
    if chart and chart.get('has_data'):
        drawing = _pdf_chart(chart, doc.width)
        if drawing is not None:
            block = [Paragraph(f"<b>{chart['title']}</b>", chart_title_style)]
            if chart.get('note'):
                block.append(Paragraph(chart['note'], chart_note_style))
            block.append(drawing)
            if chart.get('axis_note'):
                block.append(Paragraph(chart['axis_note'], chart_note_style))
            elements.append(KeepTogether(block))
            elements.append(Spacer(1, 6 * mm))

    # Build the table with wrapped cells so long text doesn't overflow.
    header = [Paragraph(str(c), head_style) for c in report['columns']]
    data = [header]
    for row in report['rows']:
        data.append([Paragraph(str(c), cell_style) for c in row])

    if not report['rows']:
        data.append([Paragraph('No records found for this period.', cell_style)]
                    + ['' for _ in report['columns'][1:]])

    usable_width = doc.width
    col_widths = _pdf_column_widths(report, usable_width)

    table = Table(data, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0e7490')),
        ('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#cfd8e3')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f3f6fa')]),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 5),
        ('RIGHTPADDING', (0, 0), (-1, -1), 5),
    ]))
    elements.append(table)

    # Add the summary totals to the PDF.
    breakdown = report.get('breakdown') or {}
    if breakdown:
        elements.append(Spacer(1, 10 * mm))
        elements.append(Paragraph('Summary Breakdowns', sub_style))
        for label, pairs in breakdown.items():
            elements.append(Spacer(1, 3 * mm))
            rows = [[Paragraph(str(label), head_style), Paragraph('Total', head_style)]]
            for name, value in pairs:
                rows.append([Paragraph(str(name), cell_style),
                             Paragraph(str(value), cell_style)])
            if not pairs:
                rows.append([Paragraph('Nothing in this period.', cell_style), ''])
            bt = Table(rows, colWidths=[usable_width * 0.35, usable_width * 0.15],
                       repeatRows=1, hAlign='LEFT')
            bt.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0e7490')),
                ('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#cfd8e3')),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f3f6fa')]),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('TOPPADDING', (0, 0), (-1, -1), 3),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
                ('LEFTPADDING', (0, 0), (-1, -1), 5),
                ('RIGHTPADDING', (0, 0), (-1, -1), 5),
            ]))
            elements.append(bt)

    generated = timezone.localtime(timezone.now()).strftime('%B %d, %Y at %I:%M %p')
    elements.append(Paragraph(
        f"Generated on {generated} &nbsp;•&nbsp; AYLA Library Management System",
        foot_style))

    doc.build(elements)
    buf.seek(0)
    return buf
