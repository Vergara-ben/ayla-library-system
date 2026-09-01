"""Report generation for the AYLA admin panel.

Builds the manuscript-defined reports as plain data dictionaries
(consumed by both the on-screen preview and the PDF exporter) and renders
them to PDF with ReportLab (pure-Python, deploys on PythonAnywhere).

Each builder returns a dict shaped like::

    {
        'key': 'transactions',
        'title': 'Transactions Report',
        'subtitle': 'Borrowing and returning transactions',
        'period_label': 'June 01, 2026 — June 17, 2026',  # or 'As of ...'
        'columns': ['Date', 'Type', ...],
        'rows': [['2026-06-01', 'Borrow', ...], ...],
        'summary': [('Total Borrows', 12), ...],
    }
"""

import math
from datetime import date, datetime, timedelta
from decimal import Decimal
from io import BytesIO

from django.db.models import Q
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
# Stock levels are a point-in-time count; stock movement is a log over time.
# Unreturned books are a point-in-time answer to "what is out right now", not a
# question about a period -- a book borrowed last year and still missing belongs
# on the list whatever dates are chosen.
SNAPSHOT_REPORTS = {'books', 'patrons', 'stock_levels', 'unreturned'}

LIBRARY_NAME = 'Ayla Public Library'
LIBRARY_LOCATION = 'Brgy. Sala, Cabuyao, Laguna'


# ─── Date handling ────────────────────────────────────────────
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


# ─── Chart geometry ───────────────────────────────────────────
# Coordinates are worked out here, in Python, for two reasons. The template
# language cannot do arithmetic, and the same numbers have to drive two very
# different renderers: inline SVG on screen and ReportLab shapes in the PDF. One
# geometry, two drawings, so the printed chart is provably the same chart.
#
# No charting library either. The screen copy has to survive a print dialog, and
# a scripted canvas does not; a CDN chart script would also be one more thing to
# fail on a library PC with a filtered connection.

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
    """Geometry for one chart.

    `series` is [(label, value), ...] already in the order it should be drawn --
    chronological for a line, ranked or natural for bars. `highlight` names the
    label to emphasise (the peak hour, the biggest bucket); None emphasises
    nothing, which is right where no single point is the finding.

    Returns a dict the template and the PDF renderer both consume. An empty
    series still returns a well-formed chart, so callers never have to guard.
    """
    plot_w = CHART_W - CHART_PAD_L - CHART_PAD_R
    plot_h = CHART_H - CHART_PAD_T - CHART_PAD_B
    base_y = CHART_PAD_T + plot_h

    values = [v for _lbl, v in series]
    # `total` may be passed in when the series is a truncated top-N: the shares
    # then read against the whole collection rather than against the twelve bars
    # that happened to fit, which is the number the reader assumes anyway.
    charted = sum(values)
    total = charted if total is None else total
    y_max = _nice_max(max(values)) if values else 5

    # Past a dozen labels they collide. Thin them rather than shrinking the type
    # past readable; the exact figures live in the table underneath either way.
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
        # Category names are arbitrary length -- "Children's Literature" is wider
        # than its own bar at twelve across -- so the axis gets a clipped version
        # while the full name stays on the mark for the tooltip and the table.
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
    # The area under a line is closed along the baseline, so the fill reads as
    # volume rather than as a shape floating over the axis.
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
            # Offsets are computed here rather than with the template's `add`
            # filter: `add` casts both sides to int and returns an empty string
            # when either will not convert, so 14.0|add:'3.5' silently became ''
            # and dropped every axis label to y=0, stacked on each other.
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


# ─── Individual report builders ───────────────────────────────
def _transactions(start, end):
    txns = (Transaction.objects
            .select_related('patron', 'book')
            .filter(transaction_date__range=(start, end))
            .order_by('-transaction_date', '-transaction_id'))

    rows = []
    borrows = returns = in_library = overdue = 0
    for tx in txns:
        if tx.transaction_type == 'Borrow':
            borrows += 1
        elif tx.transaction_type == 'Return':
            returns += 1
        elif tx.transaction_type == 'In-Library Reading':
            in_library += 1
        if tx.overdue_flag:
            overdue += 1
        rows.append([
            _fmt_date(tx.transaction_date),
            tx.transaction_type,
            tx.book.title if tx.book else '—',
            tx.patron.fullname if tx.patron else 'In-Library User',
            _fmt_date(tx.due_date),
            _fmt_date(tx.return_date),
            'Yes' if tx.overdue_flag else 'No',
            f'{tx.fine_amount:.2f}' if tx.fine_amount else '—',
        ])

    # Activity per day. A transactions report that is only a list answers "what
    # happened"; the line answers "is it rising or falling", which is the
    # question a librarian actually brings to it.
    per_day = {}
    for tx in txns:
        if tx.transaction_date:
            per_day[tx.transaction_date] = per_day.get(tx.transaction_date, 0) + 1
    days = _days_in(start, end)
    busiest_day = max(per_day.items(), key=lambda kv: (kv[1], kv[0]))[0] if per_day else None

    return {
        'key': 'transactions',
        'title': 'Transactions Report',
        'subtitle': 'Borrowing, returning, and in-library reading transactions',
        'columns': ['Date', 'Type', 'Book', 'Patron', 'Due Date', 'Returned', 'Overdue', 'Fine'],
        'rows': rows,
        'summary': [
            ('Total Transactions', len(rows)),
            ('Borrows', borrows),
            ('Returns', returns),
            ('In-Library Reading', in_library),
            ('Overdue', overdue),
            ('Busiest Day', _fmt_date(busiest_day)),
        ],
        'chart': _chart(
            [(_day_label(d, len(days)), per_day.get(d, 0)) for d in days],
            kind='line',
            title='Transactions per day',
            note='Every transaction type counted together. Busiest day marked.',
            empty_note='No transactions in this period.',
            highlight=_day_label(busiest_day, len(days)) if busiest_day else None,
            axis_note='Date \u00b7 vertical axis is number of transactions',
        ),
        'breakdown': {
            'By type': [
                ('Borrow', str(borrows)),
                ('Return', str(returns)),
                ('In-Library Reading', str(in_library)),
            ],
        },
    }


def _patron_logs(start, end):
    logs = (PatronLog.objects
            .select_related('patron')
            .filter(entry_time__date__range=(start, end))
            .order_by('-entry_time'))

    rows = []
    completed = ongoing = 0
    for lg in logs:
        if lg.exit_time:
            completed += 1
        else:
            ongoing += 1
        rows.append([
            lg.patron.fullname if lg.patron else '—',
            lg.school or '—',
            lg.purpose_of_visit or '—',
            _fmt_dt(lg.entry_time),
            _fmt_dt(lg.exit_time),
        ])

    # Visits per day, which is the shape of demand: term time against holidays,
    # and which weekdays carry the load.
    per_day, per_purpose = {}, {}
    for lg in logs:
        d = timezone.localtime(lg.entry_time).date()
        per_day[d] = per_day.get(d, 0) + 1
        purpose = lg.purpose_of_visit or 'Not stated'
        per_purpose[purpose] = per_purpose.get(purpose, 0) + 1
    days = _days_in(start, end)
    busiest_day = max(per_day.items(), key=lambda kv: (kv[1], kv[0]))[0] if per_day else None

    return {
        'key': 'patron_logs',
        'title': 'Patron Logs Report',
        'subtitle': 'Library visit entry and exit records',
        'columns': ['Patron', 'School', 'Purpose', 'Entry Time', 'Exit Time'],
        'rows': rows,
        'summary': [
            ('Total Visits', len(rows)),
            ('Completed', completed),
            ('Still Inside', ongoing),
            ('Busiest Day', _fmt_date(busiest_day)),
            ('Average Per Day', f'{len(rows) / len(days):.1f}' if days else '0'),
        ],
        'chart': _chart(
            [(_day_label(d, len(days)), per_day.get(d, 0)) for d in days],
            kind='line',
            title='Visits per day',
            note='Busiest day marked. A day with no entries sits on the baseline.',
            empty_note='No visits were logged in this period.',
            highlight=_day_label(busiest_day, len(days)) if busiest_day else None,
            axis_note='Date \u00b7 vertical axis is number of library entries',
        ),
        'breakdown': {
            'By purpose of visit': [
                (p, str(n)) for p, n in sorted(per_purpose.items(), key=lambda kv: -kv[1])
            ],
        },
    }


def _books(start, end):
    # Real-time catalog snapshot.
    books = Book.objects.select_related('shelf_level__shelf').order_by('title')
    rows = []
    available = borrowed = 0
    for b in books:
        if b.status == 'Available':
            available += 1
        elif b.status == 'Borrowed':
            borrowed += 1
        if b.shelf_level:
            location = b.shelf_level.category or f'Level {b.shelf_level.level_number}'
        else:
            location = '—'
        rows.append([
            b.title,
            b.author,
            b.ISBN or '—',
            b.genre or '—',
            b.status,
            location,
        ])

    # What the collection is made of. A catalogue listing cannot be read for
    # shape; the distribution is what a collection-development decision needs.
    per_genre, per_status = {}, {}
    for b in books:
        g = (b.genre or 'Uncategorised').strip() or 'Uncategorised'
        per_genre[g] = per_genre.get(g, 0) + 1
        per_status[b.status] = per_status.get(b.status, 0) + 1
    ranked_genre = sorted(per_genre.items(), key=lambda kv: (-kv[1], kv[0]))[:12]

    return {
        'key': 'books',
        'title': 'Books Report',
        'subtitle': 'Complete book catalog listing',
        'columns': ['Title', 'Author', 'ISBN', 'Genre', 'Status', 'Location'],
        'rows': rows,
        'summary': [
            ('Total Books', len(rows)),
            ('Available', available),
            ('Borrowed', borrowed),
            ('Genres Held', len(per_genre)),
            ('Largest Genre', ranked_genre[0][0][:24] if ranked_genre else '\u2014'),
        ],
        'chart': _chart(
            ranked_genre,
            kind='bar',
            total=len(rows),
            title='Collection by genre',
            note='Twelve largest genres. Largest marked.',
            empty_note='No books catalogued yet.',
            highlight=ranked_genre[0][0] if ranked_genre else None,
            axis_note='Genre \u00b7 vertical axis is number of titles held',
        ),
        'breakdown': {
            'By status': [(k, str(v)) for k, v in sorted(per_status.items(), key=lambda kv: -kv[1])],
            'By genre': [(k, str(v)) for k, v in sorted(per_genre.items(), key=lambda kv: -kv[1])],
        },
    }


def _patrons(start, end):
    # Real-time patron directory snapshot.
    patrons = Patron.objects.order_by('fullname')
    rows = []
    active = suspended = inactive = 0
    for p in patrons:
        if p.account_status == 'Active':
            active += 1
        elif p.account_status == 'Suspended':
            suspended += 1
        else:
            inactive += 1
        rows.append([
            p.fullname,
            p.patron_type,
            p.email,
            p.contact_number or '—',
            p.account_status,
            _fmt_date(p.registration_date),
        ])

    per_type = {}
    for p in patrons:
        t = p.patron_type or 'Not stated'
        per_type[t] = per_type.get(t, 0) + 1
    ranked_type = sorted(per_type.items(), key=lambda kv: (-kv[1], kv[0]))

    return {
        'key': 'patrons',
        'title': 'Patrons Report',
        'subtitle': 'Registered patron directory',
        'columns': ['Name', 'Type', 'Email', 'Contact', 'Status', 'Registered'],
        'rows': rows,
        'summary': [
            ('Total Patrons', len(rows)),
            ('Active', active),
            ('Suspended', suspended),
            ('Inactive', inactive),
            ('Largest Group', ranked_type[0][0] if ranked_type else '\u2014'),
        ],
        'chart': _chart(
            ranked_type,
            kind='bar',
            title='Membership by patron type',
            note='Who the library actually serves. Largest group marked.',
            empty_note='No patrons registered yet.',
            highlight=ranked_type[0][0] if ranked_type else None,
            axis_note='Patron type \u00b7 vertical axis is number of registered patrons',
        ),
    }


def _donations(start, end):
    donations = (Donation.objects
                 .select_related('book')
                 .filter(date_donated__range=(start, end))
                 .order_by('-date_donated', '-donation_id'))

    rows = []
    received = processing = shelved = 0
    for d in donations:
        if d.status == 'Received':
            received += 1
        elif d.status == 'Processing':
            processing += 1
        elif d.status == 'Shelved':
            shelved += 1
        rows.append([
            _fmt_date(d.date_donated),
            d.donor_name,
            d.book.title if d.book else '—',
            d.book.author if d.book else '—',
            d.status,
        ])

    # Donations arrive in bursts -- a school clear-out, an estate -- so the
    # month-by-month line says more about supply than any single total.
    per_month, per_status = {}, {}
    for d in donations:
        if d.date_donated:
            key = (d.date_donated.year, d.date_donated.month)
            per_month[key] = per_month.get(key, 0) + 1
        per_status[d.status] = per_status.get(d.status, 0) + 1

    months = []
    if per_month:
        y, m = min(per_month)
        last = max(per_month)
        while (y, m) <= last:
            months.append((y, m))
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)

    return {
        'key': 'donations',
        'title': 'Donations Report',
        'subtitle': 'Received donations from receipt through accession',
        'columns': ['Date Donated', 'Donor', 'Book Title', 'Author', 'Status'],
        'rows': rows,
        'summary': [
            ('Total Donations', len(rows)),
            ('Received', received),
            ('Processing', processing),
            ('Shelved', shelved),
            ('Months With Donations', len(per_month)),
        ],
        'chart': _chart(
            [(date(y, m, 1).strftime('%b %Y'), per_month.get((y, m), 0)) for y, m in months],
            kind='line',
            title='Donations received per month',
            note='A month with no donations sits on the baseline.',
            empty_note='No donations were received in this period.',
            axis_note='Month \u00b7 vertical axis is number of donated copies',
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

    # Group copies by the title they belong to, so the report reads as stock
    # levels rather than a list of individual copies.
    groups = {}
    totals = {'Good': 0, 'Damaged': 0, 'Lost': 0, 'Withdrawn': 0}
    removed = 0
    for r in records:
        if r.status == 'Removed':
            removed += 1
            continue
        key = r.display_title
        g = groups.setdefault(key, {
            'title': key,
            'location': r.shelf_location or 'Not shelved',
            'catalogued': r.book is not None,
            'Good': 0, 'Damaged': 0, 'Lost': 0, 'Withdrawn': 0,
        })
        if r.condition in g:
            g[r.condition] += 1
            totals[r.condition] += 1

    rows = []
    for g in groups.values():
        on_hand = g['Good'] + g['Damaged']
        rows.append([
            g['title'],
            'Yes' if g['catalogued'] else 'No',
            g['location'],
            g['Good'], g['Damaged'], g['Lost'], g['Withdrawn'], on_hand,
        ])

    return {
        'key': 'stock_levels',
        'title': 'Stock Levels Report',
        'subtitle': 'Physical copies held, by title and condition',
        'columns': ['Title', 'Catalogued', 'Shelf Location',
                    'Good', 'Damaged', 'Lost', 'Withdrawn', 'On Hand'],
        'rows': rows,
        'summary': [
            ('Titles Held', len(rows)),
            ('Copies On Hand', totals['Good'] + totals['Damaged']),
            ('Good', totals['Good']),
            ('Damaged', totals['Damaged']),
            ('Lost', totals['Lost']),
            ('Withdrawn', totals['Withdrawn']),
            ('Deaccessioned', removed),
        ],
        'chart': _chart(
            [(k, totals[k]) for k in ('Good', 'Damaged', 'Lost', 'Withdrawn')],
            kind='bar',
            title='Copies by condition',
            note='The state of the physical collection. Largest group marked.',
            empty_note='No copies are on record yet.',
            highlight=max(totals, key=lambda k: totals[k]) if any(totals.values()) else None,
            axis_note='Condition · vertical axis is number of physical copies',
        ),
        'breakdown': {
            'By condition': [(k, str(totals[k]))
                             for k in ('Good', 'Damaged', 'Lost', 'Withdrawn')],
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
        if m.condition_before and m.condition_after and m.condition_before != m.condition_after:
            change = m.condition_before + ' to ' + m.condition_after
        else:
            change = m.condition_after or '—'
        rows.append([
            _fmt_date(timezone.localtime(m.timestamp).date()),
            m.inventory_record.display_title if m.inventory_record else '—',
            m.get_action_display(),
            change,
            m.actor_name or '—',
            m.reason or '—',
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
        'columns': ['Date', 'Copy', 'Action', 'Condition Change', 'Actor', 'Reason'],
        'rows': rows,
        'summary': summary,
        'chart': _chart(
            action_series,
            kind='bar',
            title='Movements by action',
            note='What is actually happening to stock. Most frequent marked.',
            empty_note='No stock movements in this period.',
            highlight=action_series[0][0] if action_series else None,
            axis_note='Action \u00b7 vertical axis is number of movements',
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
    """Every borrowed copy that has not come back, oldest first.

    A snapshot rather than a period: the point of the list is chasing what is
    still out, and the ones worth chasing hardest are the oldest, so the date
    range is deliberately ignored.
    """
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
        if is_over:
            overdue += 1
            total_days_over += days_over
            # What the fine would be if it came back today. Not charged yet --
            # the fine is only written when the return is processed -- so this is
            # an exposure figure, not money owed.
            billable = max(days_over - (rule.grace_period_days or 0), 0)
            accruing += Decimal(billable) * (rule.fine_per_day or Decimal('0'))
        rows.append([
            tx.book.title if tx.book else '—',
            tx.patron.fullname if tx.patron else 'In-Library User',
            _fmt_date(tx.transaction_date),
            _fmt_date(tx.due_date),
            str(days_over) if is_over else '—',
            'Overdue' if is_over else 'On loan',
        ])

    # How overdue, not just how many. Chasing is triaged by age -- a book three
    # days late is a reminder, one three months late is a replacement invoice --
    # and a single "Overdue: 15" count cannot tell those apart.
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
        'columns': ['Book', 'Borrowed By', 'Borrowed', 'Due', 'Days Overdue', 'Status'],
        'rows': rows,
        'summary': [
            ('Still Out', len(rows)),
            ('Overdue', overdue),
            ('On Time', len(rows) - overdue),
            ('Total Days Overdue', total_days_over),
            ('Fines If Returned Today', f'{accruing:.2f}'),
            ('Longest Overdue', f'{max(days_over_all)} days' if days_over_all and max(days_over_all) > 0 else '\u2014'),
        ],
        'chart': _chart(
            ageing,
            kind='bar',
            title='How overdue the outstanding copies are',
            note='Largest group marked. Chasing is triaged by age, not by count.',
            empty_note='Nothing is out on loan.',
            highlight=worst[0] if worst else None,
            axis_note='Days past the due date \u00b7 vertical axis is number of copies',
        ),
        'breakdown': {
            'By age': [(label, str(n)) for label, n in ageing],
        },
    }


def _penalties(start, end):
    """Fines actually charged, totalled by day, week and month.

    Only fines on transactions settled inside the range are counted: a fine is
    written when the book comes back or is written off, so the return date is
    when the library actually charged it.
    """
    txns = (Transaction.objects
            .select_related('patron', 'book')
            .filter(fine_amount__gt=0)
            .filter(Q(return_date__range=(start, end))
                    | Q(return_date__isnull=True, transaction_date__range=(start, end)))
            .order_by('-return_date', '-transaction_date'))

    by_day, by_week, by_month = {}, {}, {}
    total = Decimal('0.00')
    rows = []
    for tx in txns:
        charged_on = tx.return_date or tx.transaction_date
        amount = tx.fine_amount or Decimal('0')
        total += amount
        if charged_on:
            iso_year, iso_week, _ = charged_on.isocalendar()
            by_day[charged_on] = by_day.get(charged_on, Decimal('0')) + amount
            by_week[(iso_year, iso_week)] = by_week.get((iso_year, iso_week), Decimal('0')) + amount
            by_month[(charged_on.year, charged_on.month)] = (
                by_month.get((charged_on.year, charged_on.month), Decimal('0')) + amount)
        rows.append([
            _fmt_date(charged_on),
            tx.patron.fullname if tx.patron else 'In-Library User',
            tx.book.title if tx.book else '—',
            'Lost' if (tx.book and tx.book.status == 'Lost') else 'Overdue',
            f'{amount:.2f}',
        ])

    def _avg(bucket):
        return (total / len(bucket)) if bucket else Decimal('0')

    return {
        'key': 'penalties',
        'title': 'Penalties Report',
        'subtitle': 'Fines charged, with daily, weekly and monthly totals',
        'columns': ['Date Charged', 'Patron', 'Book', 'Reason', 'Amount'],
        'rows': rows,
        'summary': [
            ('Total Charged', f'{total:.2f}'),
            ('Penalties Issued', len(rows)),
            ('Days With Fines', len(by_day)),
            ('Average Per Day', f'{_avg(by_day):.2f}'),
            ('Average Per Week', f'{_avg(by_week):.2f}'),
            ('Average Per Month', f'{_avg(by_month):.2f}'),
        ],
        # Rendered as its own table on screen; the exporters ignore it.
        'chart': _chart(
            [(_day_label(d, len(_days_in(start, end))), float(by_day.get(d, 0)))
             for d in _days_in(start, end)],
            kind='line',
            title='Fines charged per day',
            note='A day with no fines sits on the baseline.',
            empty_note='No fines were charged in this period.',
            axis_note='Date \u00b7 vertical axis is pesos charged',
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
    """What gets borrowed, and when the library is busiest.

    Two questions the library actually plans around: which titles to buy more
    of, and which hours need someone on the desk. Both are counts over the
    chosen period rather than all time, because last year's answer does not
    tell you how to staff next week.
    """
    borrows = (Transaction.objects
               .select_related('book')
               .filter(transaction_type='Borrow', transaction_date__range=(start, end)))

    per_title = {}
    for tx in borrows:
        if not tx.book:
            continue
        key = (tx.book.title, tx.book.author or '—')
        per_title[key] = per_title.get(key, 0) + 1
    ranked = sorted(per_title.items(), key=lambda kv: (-kv[1], kv[0][0]))

    # ── When the library is busy ──────────────────────────────────────
    # Visits are the honest measure of how busy the room is: a borrow happens at
    # the desk, but most people who come in never borrow anything, and staffing
    # has to cover the room rather than the counter.
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
        # Built by hand rather than with strftime('%-I'): the dash modifier that
        # strips the leading zero is a glibc extension and raises on Windows,
        # which is what this runs on.
        if h is None:
            return '\u2014'
        suffix = 'AM' if h < 12 else 'PM'
        hour12 = h % 12 or 12
        return f'{hour12} {suffix}'

    def _hour_span(h):
        """Renders an hour as the range it is: 9 AM - 10 AM.

        Staffing is rostered in ranges, and a bare "9 AM" reads as a moment.
        """
        if h is None:
            return '\u2014'
        return f'{_hour_label(h)} \u2013 {_hour_label((h + 1) % 24)}'

    # Ties broken towards the earlier hour so the same data always names the same
    # peak; dict order would otherwise depend on which visit happened to be read
    # first.
    peak_hour, peak_count = (max(per_hour.items(), key=lambda kv: (kv[1], -kv[0]))
                             if per_hour else (None, 0))
    quiet_hour, quiet_count = (min(per_hour.items(), key=lambda kv: (kv[1], kv[0]))
                               if per_hour else (None, 0))

    WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    busy_day, busy_day_count = (max(per_weekday.items(), key=lambda kv: (kv[1], -kv[0]))
                                if per_weekday else (None, 0))

    # Averaged over the hours that actually saw someone, not over 24: a library
    # open seven hours a day would otherwise look two-thirds empty by arithmetic
    # alone, and the number would say nothing about how busy it gets.
    active_hours = len(per_hour)
    avg_per_active_hour = (total_visits / active_hours) if active_hours else 0
    peak_share = (peak_count / total_visits * 100) if total_visits else 0

    # How much busier the peak is than a typical open hour. This is the number
    # that answers "is one hour worth extra staff, or is the day flat?" -- a peak
    # 1.1x the average is noise, 3x is a queue.
    peak_vs_average = (peak_count / avg_per_active_hour) if avg_per_active_hour else 0

    # The series runs from the first hour anyone arrived to the last, rather than
    # over only the hours with visits: a quiet hour in the middle of the day is
    # part of the shape, and dropping it would close the gap on the line and hide
    # the dip entirely.
    hours = []
    if per_hour:
        for h in range(min(per_hour), max(per_hour) + 1):
            n = per_hour.get(h, 0)
            hours.append({
                'hour': h,
                'label': _hour_label(h),
                'value': n,
                'share': round((n / total_visits) * 100, 1) if total_visits else 0,
                'is_peak': h == peak_hour,
            })

    hour_chart = _chart(
        [(h['label'], h['value']) for h in hours],
        kind='line',
        title='Visits by hour of day',
        note='Busiest hour marked. Counts are library entries, not borrows.',
        empty_note='No visits were logged in this period.',
        highlight=_hour_label(peak_hour) if peak_hour is not None else None,
        axis_note='Hour of day \u00b7 vertical axis is number of library entries',
    )

    rows = [[title, author, str(n)] for (title, author), n in ranked[:50]]

    return {
        'key': 'analytics',
        'title': 'Borrowing Analytics Report',
        'subtitle': 'Most borrowed titles, and the hours the library is busiest',
        'columns': ['Book', 'Author', 'Times Borrowed'],
        'rows': rows,
        'summary': [
            ('Borrows In Period', sum(per_title.values())),
            ('Distinct Titles', len(per_title)),
            ('Most Borrowed', ranked[0][0][0][:28] if ranked else '\u2014'),
            ('Peak Hour', _hour_span(peak_hour)),
            ('Visits In Peak Hour', peak_count),
            ('Share Of Visits In Peak', f'{peak_share:.0f}%'),
            ('Peak vs Average Hour', f'{peak_vs_average:.1f}\u00d7' if peak_vs_average else '\u2014'),
            ('Busiest Day', WEEKDAYS[busy_day] if busy_day is not None else '\u2014'),
            ('Quietest Open Hour', _hour_span(quiet_hour)),
            ('Average Per Open Hour', f'{avg_per_active_hour:.1f}'),
            ('Total Visits', total_visits),
        ],
        # Screen only. The exporters walk `columns`/`rows`, so the hourly numbers
        # are repeated under `breakdown` below to keep them in the Excel and PDF.
        'chart': dict(hour_chart, peak_label=_hour_span(peak_hour), peak_count=peak_count),
        'breakdown': {
            'Visits by hour': [
                (_hour_span(h), str(per_hour.get(h, 0)))
                for h in sorted(per_hour)
            ],
            'Visits by day of week': [
                (WEEKDAYS[d], str(per_weekday[d]))
                for d in sorted(per_weekday, key=lambda d: -per_weekday[d])
            ],
        },
    }


# Registered here rather than in the literal above because these three are
# defined below it.
_BUILDERS['unreturned'] = _unreturned
_BUILDERS['penalties'] = _penalties
_BUILDERS['analytics'] = _analytics


def build_report(report_type, start, end):
    """Return the report data dict for the given type and date range."""
    builder = _BUILDERS.get(report_type, _transactions)
    report = builder(start, end)
    report['period_label'] = _period_label(report['key'], start, end)
    return report


# ─── Excel rendering ──────────────────────────────────────────
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

    # Breakdowns get their own sheet rather than being dropped. Visits by hour is
    # the substance of the peak-hour finding, and fine totals per day/week/month
    # are what the penalties report is for -- a spreadsheet that carries only the
    # detail rows makes the reader recompute what the screen already worked out.
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


# ─── PDF rendering ────────────────────────────────────────────

def _pdf_chart(chart, avail_width):
    """Redraw an on-screen chart as ReportLab shapes.

    The same coordinates the SVG uses, with two conversions: everything is
    scaled to the page width, and y is flipped, because ReportLab's origin is at
    the bottom-left while SVG's is at the top-left. Drawing from the shared
    geometry rather than re-deriving it is what keeps the printed chart honest --
    the two cannot drift apart and start disagreeing about where the peak is.
    """
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
            # Rect is anchored bottom-left, so the y is the baseline minus height
            # once flipped -- not the top edge the SVG uses.
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
    cell_style = ParagraphStyle('Cell', parent=styles['Normal'], fontSize=8,
                                leading=10)
    head_style = ParagraphStyle('Head', parent=styles['Normal'], fontSize=8,
                                leading=10, textColor=colors.white,
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

    # The chart goes above the detail rows: it is the answer, and the rows are the
    # working. Kept together with its caption so a page break cannot separate a
    # chart from the sentence explaining what its axes are.
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
    col_count = len(report['columns'])
    col_widths = [usable_width / col_count] * col_count

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

    # Same reasoning as the spreadsheet: the hourly and per-period totals are the
    # answer, not a footnote to it, so they go in the printed copy too. Side by
    # side under one heading, after the detail rows they summarise.
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
