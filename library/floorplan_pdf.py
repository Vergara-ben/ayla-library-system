"""The floor plan as a PDF: each floor drawn, then the books on each shelf."""

import re
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.graphics.shapes import Drawing, Line, Polygon, Rect, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from .reports import LIBRARY_LOCATION, LIBRARY_NAME

ACCENT = colors.HexColor('#0e7490')
GRID = colors.HexColor('#cfd8e3')
INK = colors.HexColor('#1d2733')
MUTED = colors.HexColor('#5c6675')

# (fill, outline) for each kind of shape, matching the on-screen map.
ROOM = ('#f4f6f9', '#7b8795')
SHELF = ('#dbe3ec', '#46546a')
OBSTACLE = ('#efe3d2', '#a97b41')
STAIRS = ('#f7dfe3', '#a3364a')


def _safe(text):
    """Collapse whitespace; ReportLab draws control characters as boxes."""
    return re.sub(r'\s+', ' ', str(text or '')).strip()


def _clamp(value, low, high):
    return max(low, min(high, value))


def _bounds(sheet):
    """The area the drawn shapes cover, with a margin, in canvas units."""
    xs, ys = [], []
    for group in ('rooms', 'shelves', 'obstacles', 'stairways'):
        for item in sheet[group]:
            for x, y in item['points']:
                xs.append(float(x))
                ys.append(float(y))
    if not xs:
        return 0.0, 0.0, sheet['canvas_width'], sheet['canvas_height']
    pad = 24.0
    return min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad


def _map_drawing(sheet, max_w, max_h):
    """One floor scaled to fit the page. Canvas y runs down; PDF y runs up."""
    x0, y0, x1, y1 = _bounds(sheet)
    span_w, span_h = max(x1 - x0, 1.0), max(y1 - y0, 1.0)
    scale = min(max_w / span_w, max_h / span_h)
    drawing = Drawing(span_w * scale, span_h * scale)
    drawing.hAlign = 'CENTER'

    def pt(x, y):
        return (float(x) - x0) * scale, (y1 - float(y)) * scale

    def shape(points, style, width, dash=None):
        if len(points) < 3:
            return
        flat = []
        for x, y in points:
            flat.extend(pt(x, y))
        drawing.add(Polygon(flat, fillColor=colors.HexColor(style[0]),
                            strokeColor=colors.HexColor(style[1]),
                            strokeWidth=width, strokeDashArray=dash))

    def label(text, x, y, size, color, bold=False, drop=0.0):
        text = _safe(text)
        if not text or x is None or y is None:
            return
        px, py = pt(x, y)
        drawing.add(String(px, py - size * 0.35 - drop, text,
                           fontName='Helvetica-Bold' if bold else 'Helvetica',
                           fontSize=size, fillColor=color, textAnchor='middle'))

    room_size = _clamp(15 * scale, 7, 13)
    shelf_size = _clamp(9 * scale, 4.5, 8)
    small_size = _clamp(7.5 * scale, 4, 7)

    for room in sheet['rooms']:
        shape(room['points'], ROOM, 1.2, dash=[4, 3] if room['restricted'] else None)
    for st in sheet['stairways']:
        shape(st['points'], STAIRS, 0.9)
        for (ax, ay), (bx, by) in st['treads']:
            (px, py), (qx, qy) = pt(ax, ay), pt(bx, by)
            drawing.add(Line(px, py, qx, qy, strokeColor=colors.HexColor(STAIRS[1]),
                             strokeWidth=0.5))
    for ob in sheet['obstacles']:
        shape(ob['points'], OBSTACLE, 0.8)
    for sh in sheet['shelves']:
        shape(sh['points'], SHELF, 0.9)

    # Labels last, so no shape covers them.
    for sh in sheet['shelves']:
        label(sh['name'], sh['cx'], sh['cy'], shelf_size, INK, bold=True)
    for st in sheet['stairways']:
        label(st['label'], st['label_x'], st['label_y'], small_size, colors.HexColor('#7d2436'), bold=True)
        if st['destination']:
            label('to ' + st['destination'], st['label_x'], st['label_y'], small_size,
                  colors.HexColor('#7d2436'), drop=small_size * 1.2)
    for ob in sheet['obstacles']:
        if ob['show_label']:
            label(ob['label'], ob['label_x'], ob['label_y'], small_size, colors.HexColor('#6b4f28'))
    for room in sheet['rooms']:
        label(room['name'], room['label_x'], room['label_y'], room_size,
              MUTED if room['restricted'] else colors.HexColor('#33404f'), bold=True)
        if room['restricted']:
            label('STAFF ONLY', room['label_x'], room['label_y'], small_size, MUTED,
                  bold=True, drop=room_size * 1.1)
    return drawing


def _legend(width):
    """Swatches naming what each colour on the map is."""
    drawing = Drawing(width, 14)
    x = 0
    for name, style in (('Room', ROOM), ('Shelf', SHELF),
                        ('Table / obstacle', OBSTACLE), ('Stairs / lift', STAIRS)):
        drawing.add(Rect(x, 2, 14, 9, fillColor=colors.HexColor(style[0]),
                         strokeColor=colors.HexColor(style[1]), strokeWidth=0.8))
        drawing.add(String(x + 19, 3.5, name, fontName='Helvetica', fontSize=8, fillColor=MUTED))
        x += 19 + len(name) * 4.6 + 22
    return drawing


def render_floorplan_pdf(sheets, printed_on):
    """Render the floors to a PDF and return a BytesIO buffer."""
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=landscape(A4),
        leftMargin=14 * mm, rightMargin=14 * mm,
        topMargin=12 * mm, bottomMargin=14 * mm,
        title='Floor plan', author=LIBRARY_NAME,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('FpTitle', parent=styles['Heading1'], fontSize=15,
                                 leading=18, spaceAfter=1, textColor=INK)
    sub_style = ParagraphStyle('FpSub', parent=styles['Normal'], fontSize=9,
                               textColor=MUTED, spaceAfter=8)
    shelf_style = ParagraphStyle('FpShelf', parent=styles['Normal'], fontSize=10.5,
                                 leading=13, textColor=ACCENT, spaceBefore=10, spaceAfter=4)
    note_style = ParagraphStyle('FpNote', parent=styles['Normal'], fontSize=8.5,
                                textColor=MUTED, spaceAfter=4)
    cell_style = ParagraphStyle('FpCell', parent=styles['Normal'], fontSize=8, leading=10)
    head_style = ParagraphStyle('FpHead', parent=cell_style, textColor=colors.white,
                                fontName='Helvetica-Bold')

    printed = '%d %s' % (printed_on.day, printed_on.strftime('%B %Y'))

    def footer(canvas, _doc):
        canvas.saveState()
        canvas.setFont('Helvetica', 7.5)
        canvas.setFillColor(colors.HexColor('#999999'))
        canvas.drawString(doc.leftMargin, 8 * mm, '%s · %s' % (LIBRARY_NAME, LIBRARY_LOCATION))
        canvas.drawRightString(doc.leftMargin + doc.width, 8 * mm,
                               'Printed %s · Page %d' % (printed, canvas.getPageNumber()))
        canvas.restoreState()

    headers = ['Level', 'Position', 'Title', 'Author', 'Call number', 'Status']
    shares = [0.15, 0.07, 0.33, 0.20, 0.14, 0.11]
    col_widths = [(doc.width - 12) * s for s in shares]

    def cell(text):
        return Paragraph(escape(_safe(text)) or '&mdash;', cell_style)

    elements = []
    for index, sheet in enumerate(sheets):
        if index:
            elements.append(PageBreak())
        plan = sheet['plan']
        where = _safe(plan.floor_label)
        if plan.name and _safe(plan.name) != where:
            where += ' · ' + _safe(plan.name)

        elements.append(Paragraph('Floor plan: %s' % escape(where), title_style))
        elements.append(Paragraph(escape(LIBRARY_NAME), sub_style))
        # The frame keeps 6pt of padding on every side; the title and legend need the rest.
        elements.append(_map_drawing(sheet, doc.width - 12, doc.height - 12 - 80))
        elements.append(Spacer(1, 6))
        elements.append(_legend(doc.width - 12))

        elements.append(PageBreak())
        elements.append(Paragraph('Books on each shelf: %s' % escape(where), title_style))
        elements.append(Paragraph(
            'In shelf order: level by level, left to right along each.', sub_style))
        if not sheet['shelves']:
            elements.append(Paragraph('No shelves are placed on this floor.', note_style))
        for sh in sheet['shelves']:
            count = len(sh['books'])
            heading = Paragraph('<b>%s</b> &nbsp;<font color="#5c6675" size="8.5">%s · %d %s</font>' % (
                escape(_safe(sh['name'])), escape(_safe(sh['kind'])),
                count, 'book' if count == 1 else 'books'), shelf_style)
            if not count:
                elements.append(KeepTogether([
                    heading, Paragraph('No books are filed on this shelf.', note_style)]))
                continue
            rows = [[Paragraph(h, head_style) for h in headers]]
            for b in sh['books']:
                rows.append([cell(b['level']), cell(b['position']), cell(b['title']),
                             cell(b['author']), cell(b['call_number']), cell(b['status'])])
            table = Table(rows, colWidths=col_widths, repeatRows=1)
            table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), ACCENT),
                ('GRID', (0, 0), (-1, -1), 0.4, GRID),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f5f8fb')]),
                ('TOPPADDING', (0, 0), (-1, -1), 3),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ]))
            # Keep a shelf's name with the start of its list.
            elements.append(KeepTogether([heading, table]) if count <= 12 else heading)
            if count > 12:
                elements.append(table)

    doc.build(elements, onFirstPage=footer, onLaterPages=footer)
    buf.seek(0)
    return buf
