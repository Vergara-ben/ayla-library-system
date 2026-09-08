"""Printable QR label sheets, laid out as a grid at true physical size.

These get cut out and stuck onto physical books, which makes this the one PDF
in AYLA where the millimetre matters. Everything is therefore positioned in mm
against the page itself rather than flowed through a document template: a
flowable that reflows is exactly what must not happen when the output is going
to be measured against a book spine.

The grid packs as many labels onto a sheet as the chosen QR size allows, since
a class set of two hundred books is a lot of pages otherwise.

One subtlety worth stating: the catalogue stores one row per physical copy, so
every Book already carries its own unique qr_code. "Copy 2 of 5" is therefore
counted across the whole catalogue rather than across the selection -- print
three of five copies and they must still read 2, 3 and 4 of 5, or the labels
would contradict the shelf.
"""

import re
from io import BytesIO

import qrcode
from django.db.models import Q
from reportlab.lib.pagesizes import A4, legal, letter
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas as pdfcanvas

PAGE_SIZES = {
    'A4': ('A4', A4),
    'Letter': ('Short bond / Letter', letter),
    'Legal': ('Long bond / Legal', legal),
}
DEFAULT_PAGE = 'A4'

# The printed QR square, edge to edge, in millimetres. 25 mm scans reliably off
# a phone at arm's length and still fits a paperback spine; below about 15 mm
# the modules get smaller than most office printers resolve cleanly.
DEFAULT_QR_MM = 25.0
MIN_QR_MM = 12.0
MAX_QR_MM = 60.0

PAGE_MARGIN_MM = 10.0
CELL_PAD_MM = 2.0            # breathing room around each label, for the scissors
LINE_LEADING = 1.18          # multiple of font size

# Text sizes step down with the label so a 15 mm sticker does not carry 7pt
# type it has no room for.
def _font_size(qr_mm):
    return max(3.6, min(7.0, qr_mm * 0.235))


def _safe(text):
    """Collapse whitespace; ReportLab draws control characters as boxes."""
    return re.sub(r'\s+', ' ', str(text or '')).strip()


def _fit_width(text, font, size, max_w):
    """_fit, measured without a canvas so it can run before one is opened."""
    text = _safe(text)
    if not text or stringWidth(text, font, size) <= max_w:
        return text
    ell = '…'
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if stringWidth(text[:mid] + ell, font, size) <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return (text[:lo].rstrip() + ell) if lo else ''


def _fit(canvas, text, font, size, max_w):
    """Truncate to fit max_w, with a real ellipsis rather than a hard cut."""
    text = _safe(text)
    if not text:
        return ''
    if canvas.stringWidth(text, font, size) <= max_w:
        return text
    ell = '…'
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if canvas.stringWidth(text[:mid] + ell, font, size) <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return (text[:lo].rstrip() + ell) if lo else ''


def copy_numbers(books):
    """{book_id: (n, total)} for every book that shares a title with another.

    Counted over the whole catalogue, not the selection -- see the module note.
    Books held as a single copy are absent, so nothing prints "Copy 1 of 1".
    """
    from .models import Book

    keys = {(_safe(b.title).lower(), _safe(b.author).lower()) for b in books}
    if not keys:
        return {}

    lookup = Q()
    for title, _author in keys:
        lookup |= Q(title__iexact=title)
    siblings = Book.objects.filter(lookup).order_by('book_id').values_list(
        'book_id', 'title', 'author')

    grouped = {}
    for book_id, title, author in siblings:
        grouped.setdefault((_safe(title).lower(), _safe(author).lower()), []).append(book_id)

    numbers = {}
    for key, ids in grouped.items():
        if len(ids) < 2:
            continue
        for index, book_id in enumerate(ids, start=1):
            numbers[book_id] = (index, len(ids))
    return numbers


def sort_for_printing(books):
    """Shelf, then level, then title -- so the cut pile is in walking order.

    Nothing is grouped onto its own page or its own row: the sheet stays packed
    edge to edge, because a 20-shelf job that started each group on a fresh row
    would throw away most of two pages. What keeps the pile sortable is that
    each label carries its own location, not that the paper was divided up.

    Books with no shelf assigned sort last rather than first -- they are the
    ones still to be placed, and they belong at the end of the walk.
    """
    def key(b):
        level = getattr(b, 'shelf_level', None)
        shelf = getattr(level, 'shelf', None) if level else None
        # Bottom to top within a unit -- underneath first, the top last -- and
        # left to right across its bays, which is the order someone actually
        # works a case when they are putting labels on.
        return (
            0 if shelf is not None else 1,
            _safe(getattr(shelf, 'name', '')).lower(),
            0 if getattr(level, 'is_under', False) else (
                2 if getattr(level, 'is_top', False) else 1),
            getattr(level, 'level_number', 0) or 0,
            getattr(level, 'column_number', 0) or 0,
            _safe(b.title).lower(),
            b.book_id,
        )
    return sorted(books, key=key)


def location_of(book):
    """'Shelf A · L2', 'Shelf A · Top', or '' when it has not been placed.

    "Top" rather than a number because that is where the book physically is --
    on the flat top of the case, not on a shelf inside it. Someone handed a
    label reading L5 would open the fifth shelf and not find it.
    """
    level = getattr(book, 'shelf_level', None)
    if level is None:
        return ''
    shelf = getattr(level, 'shelf', None)
    parts = [_safe(getattr(shelf, 'name', ''))]
    where = getattr(level, 'short_label', None)
    if where:
        parts.append(where)
    elif level.level_number:
        parts.append('L%d' % level.level_number)
    return ' · '.join(p for p in parts if p)


def _qr_image(payload):
    """A QR bitmap at a resolution that survives being scaled down to 15 mm."""
    qr = qrcode.QRCode(
        version=None,
        # M tolerates roughly 15% damage. These end up stuck on books that get
        # handled, scuffed and shelved; the extra modules are cheap insurance.
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=1,          # 1 module: the cell padding supplies the quiet zone
    )
    qr.add_data(payload)
    qr.make(fit=True)
    buf = BytesIO()
    qr.make_image(fill_color='black', back_color='white').save(buf, format='PNG')
    buf.seek(0)
    return ImageReader(buf)


# How many lines a title may take before it is cut. Three is enough for the
# long ones this catalogue actually holds -- "A Manual for Writers of Term
# Papers, Theses, and Dissertations (Fourth Edition)" -- without one runaway
# title making every cell on the sheet that tall.
MAX_TITLE_LINES = 3


def _wrap(text, font, size, max_w, max_lines):
    """Break text across lines at spaces, ellipsising only if it still overruns.

    Truncating a title is how "Volume 1" and "Volume 2" become the same label.
    The distinguishing part of a title is almost always at the end -- the
    volume, the edition, the book number -- which is exactly what a single
    ellipsised line throws away.
    """
    text = _safe(text)
    if not text:
        return ['']
    words = text.split(' ')
    lines, current = [], ''
    for word in words:
        trial = (current + ' ' + word).strip()
        if current and stringWidth(trial, font, size) > max_w:
            lines.append(current)
            current = word
            if len(lines) == max_lines:
                break
        else:
            current = trial
    if len(lines) < max_lines:
        lines.append(current)
        return [l for l in lines if l]

    # Out of lines with words still to place. Cutting here would throw away the
    # end of the title -- and the end is where "Volume 2", "Book 3" and
    # "(Fourth Edition)" live, which is the whole reason two labels need to be
    # told apart. So the middle goes instead and the ending survives.
    remaining = text[len(' '.join(lines)):].strip()
    if remaining:
        ell = '… '
        tail_words = remaining.split(' ')
        tail = ''
        for i in range(len(tail_words) - 1, -1, -1):
            candidate = ' '.join(tail_words[i:])
            if stringWidth(ell + candidate, font, size) <= max_w:
                tail = candidate
            else:
                break
        if not tail:
            # Not even one word of the ending fits; keep as much of it as does.
            lines[-1] = _fit_width(ell + tail_words[-1], font, size, max_w)
        else:
            lines[-1] = ell + tail
    return [l for l in lines if l]


def _label_lines(book, numbers, show):
    """The text under one QR, in priority order, already filtered by `show`."""
    lines = []
    if show.get('title'):
        lines.append(('title', _safe(book.title) or 'Untitled'))
    if show.get('author') and _safe(book.author):
        lines.append(('author', _safe(book.author)))
    # The spine number, above the copy count: it is the thing somebody reads
    # off a label to reshelve the book, and a database id never was.
    if show.get('call_number') and _safe(getattr(book, 'call_number', '')):
        lines.append(('call_number', _safe(book.call_number)))
    if show.get('copy') and book.book_id in numbers:
        n, total = numbers[book.book_id]
        lines.append(('copy', 'Copy %d of %d' % (n, total)))
    if show.get('location'):
        where = location_of(book)
        if where:
            lines.append(('location', where))
    if show.get('book_id'):
        lines.append(('book_id', '#%d' % book.book_id))
    return lines


def build_label_sheet(books, page=DEFAULT_PAGE, qr_mm=DEFAULT_QR_MM,
                      show=None, cut_guides=True, skip=0):
    """Render the grid. Returns PDF bytes.

    `skip` leaves that many cells blank at the start, so a sheet that was half
    used last time can be fed back through the printer instead of thrown away.
    """
    show = show or {'title': True, 'call_number': True, 'copy': True}
    qr_mm = max(MIN_QR_MM, min(MAX_QR_MM, float(qr_mm or DEFAULT_QR_MM)))
    _label, size = PAGE_SIZES.get(page, PAGE_SIZES[DEFAULT_PAGE])
    page_w, page_h = size

    numbers = copy_numbers(books) if show.get('copy') else {}

    font, bold = 'Helvetica', 'Helvetica-Bold'
    fs = _font_size(qr_mm)
    line_h = fs * LINE_LEADING

    # The cell's width comes from the QR alone, so the text can be measured
    # against it before deciding how tall a cell has to be.
    cell_w = qr_mm * mm + CELL_PAD_MM * mm
    inner_w = cell_w - CELL_PAD_MM * mm

    # Wrapped once, up front: the tallest label decides the cell height, and
    # that cannot be known until every title has been broken into its lines.
    rendered = {}
    for book in books:
        out = []
        for kind, value in _label_lines(book, numbers, show):
            if kind == 'title':
                for line in _wrap(value, bold, fs, inner_w, MAX_TITLE_LINES):
                    out.append((kind, line))
            else:
                out.append((kind, value))
        rendered[book.book_id] = out

    max_lines = max((len(v) for v in rendered.values()), default=0)
    text_h = max_lines * line_h
    cell_h = qr_mm * mm + text_h + CELL_PAD_MM * mm

    usable_w = page_w - 2 * PAGE_MARGIN_MM * mm
    usable_h = page_h - 2 * PAGE_MARGIN_MM * mm
    cols = max(1, int(usable_w // cell_w))
    rows = max(1, int(usable_h // cell_h))
    per_page = cols * rows

    # Centre the block so the leftover margin is shared, which also means a
    # sheet cut by hand has an even border rather than all the slack on one side.
    origin_x = (page_w - cols * cell_w) / 2
    origin_y = page_h - (page_h - rows * cell_h) / 2

    buf = BytesIO()
    c = pdfcanvas.Canvas(buf, pagesize=size)
    c.setTitle('AYLA book QR labels')

    slots = [None] * max(0, int(skip or 0)) + list(books)
    total_pages = max(1, -(-len(slots) // per_page))

    for index, book in enumerate(slots):
        page_index, cell_index = divmod(index, per_page)
        if cell_index == 0:
            if index:
                c.showPage()
            _page_footer(c, page_w, page_index + 1, total_pages, qr_mm)

        row, col = divmod(cell_index, cols)
        x = origin_x + col * cell_w
        y = origin_y - (row + 1) * cell_h

        if cut_guides:
            c.setStrokeColorRGB(0.85, 0.85, 0.85)
            c.setLineWidth(0.25)
            c.rect(x, y, cell_w, cell_h)

        if book is None:
            continue          # a skipped cell on a part-used sheet

        qr_x = x + (cell_w - qr_mm * mm) / 2
        qr_y = y + cell_h - CELL_PAD_MM * mm / 2 - qr_mm * mm
        c.drawImage(_qr_image(book.qr_code), qr_x, qr_y,
                    width=qr_mm * mm, height=qr_mm * mm)

        c.setFillColorRGB(0, 0, 0)
        text_y = qr_y - line_h + (line_h - fs) / 2
        for kind, value in rendered.get(book.book_id, []):
            face = bold if kind in ('title', 'book_id') else font
            c.setFont(face, fs)
            # Title lines are already broken to width; everything else is one
            # line and is still trimmed rather than allowed to run over.
            text = value if kind == 'title' else _fit(c, value, face, fs, inner_w)
            c.drawCentredString(x + cell_w / 2, text_y, text)
            text_y -= line_h

    c.showPage()
    c.save()
    return buf.getvalue(), {'per_page': per_page, 'columns': cols, 'rows': rows,
                            'pages': total_pages, 'qr_mm': qr_mm}


def _page_footer(c, page_w, page_no, total, qr_mm):
    """A ruler statement, so a mis-scaled print is obvious before it is used.

    "Printed at 100%" is the whole point of the feature: if the printer has
    helpfully shrunk the page to fit, these labels are the wrong size and the
    only way to notice is to measure one.
    """
    c.setFont('Helvetica', 6.5)
    c.setFillColorRGB(0.45, 0.45, 0.45)
    c.drawString(PAGE_MARGIN_MM * mm, PAGE_MARGIN_MM * mm / 2,
                 'AYLA book labels · QR %.0f mm — print at 100%% scale '
                 '(no "fit to page"), then measure one square to confirm.'
                 % qr_mm)
    c.drawRightString(page_w - PAGE_MARGIN_MM * mm, PAGE_MARGIN_MM * mm / 2,
                      'Page %d of %d' % (page_no, total))
