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

from datetime import datetime, timedelta
from io import BytesIO

from django.utils import timezone

from .models import Transaction, Patron, PatronLog, Book, Donation


REPORT_TYPES = [
    ('transactions', 'Transactions Report'),
    ('patron_logs', 'Patron Logs Report'),
    ('books', 'Books Report'),
    ('patrons', 'Patrons Report'),
    ('donations', 'Donations Report'),
]

# Reports that represent a real-time snapshot rather than a date range.
SNAPSHOT_REPORTS = {'books', 'patrons'}

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
        ],
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
        ],
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
        ],
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
        ],
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
        ],
    }


_BUILDERS = {
    'transactions': _transactions,
    'patron_logs': _patron_logs,
    'books': _books,
    'patrons': _patrons,
    'donations': _donations,
}


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

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


# ─── PDF rendering ────────────────────────────────────────────
def render_report_pdf(report):
    """Render a report dict to a PDF and return a BytesIO buffer."""
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
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

    generated = timezone.localtime(timezone.now()).strftime('%B %d, %Y at %I:%M %p')
    elements.append(Paragraph(
        f"Generated on {generated} &nbsp;•&nbsp; AYLA Library Management System",
        foot_style))

    doc.build(elements)
    buf.seek(0)
    return buf
