"""What became of each book a stock count recorded: the trail from the count to the book as it is now."""

from django.utils import timezone

from .models import StockAuditLine, StockMovement, SystemLog, Transaction

# What a count's finding asks of the library, and how to tell it has been done.
FOLLOW_UP = ('Missing', 'Damaged', 'Here on loan', 'Stray')


def state(book):
    """Status, condition and shelf of a book right now; None once it is off the records."""
    if book is None:
        return None
    return {
        'status': 'Withdrawn' if book.is_archived else book.status,
        'condition': book.condition or '',
        'location': book.location_short or 'Not shelved',
    }


def _gone(book):
    """How a book left the collection after the count, or None while it is still the library's."""
    if book.is_archived:
        return 'Removed from the records'
    if book.status == 'Lost' or book.condition == 'Lost':
        return 'Reported lost'
    if book.condition == 'Withdrawn':
        return 'Withdrawn'
    return None


def _who(name):
    return ' by %s' % name if name else ''


def events_since(line):
    """Everything that happened to the book after the count, oldest first, as (date, text)."""
    book = line.book
    if book is None:
        return [(None, 'Removed from the records')]
    t0 = line.audit.audited_at
    day0 = timezone.localtime(t0).date()
    out = []

    for m in (StockMovement.objects.filter(inventory_record__book=book, timestamp__gt=t0)
              .order_by('timestamp')):
        if m.action == 'ConditionChange':
            text = 'Condition %s to %s' % (m.condition_before or '?', m.condition_after or '?')
            if m.reason:
                text += ': ' + m.reason
        elif m.action == 'Deaccession':
            text = 'Removed from the records' + (': ' + m.reason if m.reason else '')
        else:
            text = m.reason or m.get_action_display()
        out.append((timezone.localtime(m.timestamp).date(), text + _who(m.actor_name)))

    was_out = line.status_then in ('Borrowed', 'Overdue')
    for tx in (Transaction.objects.filter(book=book, transaction_type='Borrow')
               .select_related('patron').order_by('transaction_id')):
        who = tx.patron.fullname if tx.patron else 'a guest'
        if tx.transaction_date > day0 or (tx.transaction_date == day0 and not was_out):
            out.append((tx.transaction_date, 'Borrowed by %s' % who))
        if tx.return_date and (tx.return_date > day0 or (tx.return_date == day0 and was_out)):
            out.append((tx.return_date, 'Returned by %s' % who))
        if tx.marked_lost:
            # The loan keeps no date for it; the log of the desk's action does.
            logged = (SystemLog.objects.filter(entity_type='Transaction', entity_id=str(tx.transaction_id),
                                               timestamp__gt=t0).order_by('timestamp').first())
            if logged is not None or tx.transaction_date > day0:
                when = timezone.localtime(logged.timestamp).date() if logged else tx.transaction_date
                out.append((when, 'Reported lost by %s' % who
                            + (' (₱%s charged)' % f'{tx.fine_amount:,.2f}' if tx.fine_amount else '')))

    for later in (StockAuditLine.objects.filter(book=book, audit__audited_at__gt=t0)
                  .select_related('audit').order_by('audit__audited_at')):
        out.append((timezone.localtime(later.audit.audited_at).date(),
                    'Counted again on %s: %s' % (later.audit.shelf_name.strip(),
                                                 later.get_result_display().lower())))

    out.sort(key=lambda e: e[0])
    # Moved by shelving, which keeps no per-book history: the move is still said, so 'now' is explained.
    now = state(book)
    if line.location_then and now and now['location'] != line.location_then:
        out.append((None, 'Now %s (moved since the count)' % (
            'on ' + now['location'] if now['location'] != 'Not shelved' else 'off its shelf')))
    return out


def outcome(line):
    """(words, still open?) for a finding that needed following up; ('', False) otherwise."""
    book = line.book
    if line.result not in FOLLOW_UP:
        return '', False
    if book is None:
        return 'Removed from the records', False
    gone = _gone(book)
    if line.result == 'Missing':
        if book.status == 'Missing' and not book.is_archived:
            return 'Still missing', True
        if gone:
            return 'Written off', False
        return 'Found again', False
    # Whatever the finding, a book no longer in the collection settles it.
    if gone:
        return gone, False
    if line.result == 'Here on loan':
        if book.status in ('Borrowed', 'Overdue'):
            return 'Still recorded on loan: check it in', True
        return 'Checked in', False
    if line.result == 'Damaged':
        if book.condition == 'Damaged':
            return 'Still under repair', True
        if book.condition == 'Beyond Repair':
            return 'Beyond repair', False
        return 'Repaired', False
    # A stray goes back to its own shelf.
    if book.status == 'For Reshelving':
        return 'Waiting to be reshelved', True
    return 'Back in place', False


def audit_rows(audit):
    """One row per book in a count: what it found, the book then and now, the trail, the outcome."""
    rows = []
    for line in audit.lines.select_related('book', 'book__shelf_level', 'book__shelf_level__shelf'):
        then = {'status': line.status_then, 'condition': line.condition_then,
                'location': line.location_then}
        now = state(line.book)
        words, still_open = outcome(line)
        rows.append({
            'line': line,
            'title': line.title,
            'copy_number': line.book.copy_number if line.book else None,
            'result': line.get_result_display(),
            'kind': line.result,
            'detail': line.detail,
            'then': then,
            'now': now,
            'changed': [k for k in ('status', 'condition', 'location')
                        if now is None or (then[k] and now[k] != then[k])],
            'trail': events_since(line),
            'outcome': words,
            'open': still_open,
        })
    return rows


def summary(rows):
    """What a count turned up, and how much of it has since been settled."""
    def n(kind, words=None):
        return sum(1 for r in rows if r['kind'] == kind and (words is None or r['outcome'] == words))
    return {
        'checked': len(rows),
        'missing': n('Missing'),
        'found_again': n('Missing', 'Found again'),
        'written_off': n('Missing', 'Written off'),
        'still_missing': n('Missing', 'Still missing'),
        'damaged': n('Damaged'),
        'repaired': n('Damaged', 'Repaired'),
        'loan_mismatch': n('Here on loan'),
        'checked_in': n('Here on loan', 'Checked in'),
        'strays': n('Stray'),
        'open': sum(1 for r in rows if r['open']),
    }


def summary_words(s):
    """The summary as one line, naming only what happened."""
    parts = []
    if s['missing']:
        parts.append('%d not found: %d found again, %d written off, %d still missing'
                     % (s['missing'], s['found_again'], s['written_off'], s['still_missing']))
    if s['damaged']:
        parts.append('%d found damaged, %d repaired' % (s['damaged'], s['repaired']))
    if s['loan_mismatch']:
        parts.append('%d on the shelf while on loan, %d checked in' % (s['loan_mismatch'], s['checked_in']))
    if s['strays']:
        parts.append('%d found on the wrong shelf' % s['strays'])
    if not parts:
        return 'Nothing needed following up.'
    return '; '.join(parts) + ('. %d still open.' % s['open'] if s['open'] else '. All settled.')
