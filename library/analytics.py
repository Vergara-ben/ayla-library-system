"""Charts for the Analytics page."""

from collections import Counter
from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone

from .models import Book, Patron, PatronLog, ShelfLevel, Shelf, Transaction
from .reports import OPEN_HOURS, _chart, _days_in, _day_label


WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday',
            'Saturday', 'Sunday']


def _hour_label(hour):
    """'9 AM'."""
    suffix = 'AM' if hour < 12 else 'PM'
    return '%d %s' % (hour % 12 or 12, suffix)


# Visits

def visits_by_hour(start, end):
    """Arrivals per hour of day."""
    logs = PatronLog.objects.filter(entry_time__date__range=(start, end))
    per_hour = Counter(h for h in (timezone.localtime(l.entry_time).hour for l in logs)
                       if h in OPEN_HOURS)
    # The earlier hour wins a tie.
    peak = max(sorted(per_hour), key=lambda h: per_hour[h]) if per_hour else None

    series = [(_hour_label(h), per_hour.get(h, 0)) for h in OPEN_HOURS]
    return {
        'chart': _chart(
            series, kind='line', title='Arrivals by hour',
            note='When people come in, 8 AM to 5 PM. The busiest hour is marked.',
            empty_note='No visits were logged in this period.',
            highlight=_hour_label(peak) if peak is not None else None,
            axis_note='Hour of day · vertical axis is people arriving'),
        'rows': [(label, str(value)) for label, value in series],
        'peak': _hour_label(peak) if peak is not None else '—',
        'peak_count': per_hour.get(peak, 0) if peak is not None else 0,
        'total': sum(per_hour.values()),
    }


def occupancy_by_hour(start, end):
    """How many people were inside during each hour, averaged over the days."""
    logs = list(PatronLog.objects
                .filter(entry_time__date__range=(start, end))
                .exclude(exit_time__isnull=True)
                .exclude(auto_closed=True))

    per_hour = Counter()
    days = set()
    for log in logs:
        entry = timezone.localtime(log.entry_time)
        exit_at = timezone.localtime(log.exit_time)
        if exit_at < entry:
            continue
        days.add(entry.date())
        for hour in range(entry.hour, min(exit_at.hour, 23) + 1):
            if hour in OPEN_HOURS:
                per_hour[hour] += 1

    day_count = len(days) or 1
    series = [(_hour_label(h), round(per_hour.get(h, 0) / day_count, 1)) for h in OPEN_HOURS]
    busiest = max(per_hour, key=lambda h: per_hour[h]) if per_hour else None

    excluded = (PatronLog.objects
                .filter(entry_time__date__range=(start, end))
                .filter(Q(exit_time__isnull=True) | Q(auto_closed=True))
                .count())

    return {
        'chart': _chart(
            series, kind='line', title='People inside, by hour',
            note='Average number in the room, not arrivals, 8 AM to 5 PM. '
                 'The fullest hour is marked.',
            empty_note='No completed visits to measure in this period.',
            highlight=_hour_label(busiest) if busiest is not None else None,
            axis_note='Hour of day · vertical axis is average people inside'),
        'rows': [(label, str(value)) for label, value in series],
        'busiest': _hour_label(busiest) if busiest is not None else '—',
        'measured_visits': len(logs),
        'excluded_visits': excluded,
        'days_measured': len(days),
    }


def visits_by_weekday(start, end):
    """Which days of the week are busy. Answers when to roster people."""
    logs = PatronLog.objects.filter(entry_time__date__range=(start, end))
    per_day = Counter(timezone.localtime(l.entry_time).weekday() for l in logs)
    series = [(WEEKDAYS[d][:3], per_day.get(d, 0)) for d in range(7)]
    busiest = max(per_day, key=lambda d: per_day[d]) if per_day else None
    return {
        'chart': _chart(
            series, kind='bar', title='Visits by day of the week',
            note='Totals across the whole period, not an average.',
            empty_note='No visits were logged in this period.',
            highlight=WEEKDAYS[busiest][:3] if busiest is not None else None,
            axis_note='Day of week · vertical axis is number of visits'),
        'rows': [(WEEKDAYS[d], str(per_day.get(d, 0))) for d in range(7)],
        'busiest': WEEKDAYS[busiest] if busiest is not None else '—',
    }


def visits_by_purpose(start, end):
    """Why people come."""
    logs = PatronLog.objects.filter(entry_time__date__range=(start, end))
    per_purpose = Counter((l.purpose_of_visit or 'Not recorded') for l in logs)
    ranked = per_purpose.most_common()
    return {
        'chart': _chart(
            [(p, n) for p, n in ranked], kind='bar',
            title='Why people visit',
            note='Recorded at the desk from a fixed list.',
            empty_note='No visits were logged in this period.',
            highlight=ranked[0][0] if ranked else None,
            axis_note='Purpose · vertical axis is number of visits'),
        'rows': [(p, str(n)) for p, n in ranked],
        'top': ranked[0][0] if ranked else '—',
    }


def visitors_by_type(start, end):
    """Students, teachers, or people off the street."""
    logs = PatronLog.objects.select_related('patron').filter(
        entry_time__date__range=(start, end))
    per_type = Counter(
        (l.patron.patron_type if l.patron and l.patron.patron_type else 'Not recorded')
        for l in logs)
    ranked = per_type.most_common()
    return {
        'chart': _chart(
            [(t, n) for t, n in ranked], kind='bar',
            title='Who visits',
            note='Counted per visit, so a regular counts each time they come.',
            empty_note='No visits were logged in this period.',
            highlight=ranked[0][0] if ranked else None,
            axis_note='Type of visitor · vertical axis is number of visits'),
        'rows': [(t, str(n)) for t, n in ranked],
    }


def visitors_by_school(start, end, limit=10):
    """Where they come from. Useful to a school library deciding who it serves."""
    logs = PatronLog.objects.filter(entry_time__date__range=(start, end))
    per_school = Counter((l.school or 'Not recorded').strip() or 'Not recorded'
                         for l in logs)
    ranked = per_school.most_common(limit)
    return {
        'chart': _chart(
            [(s[:18], n) for s, n in ranked], kind='bar',
            title='Visitors by school',
            note='Top %d. Recorded as typed at the desk.' % limit,
            empty_note='No visits were logged in this period.',
            highlight=ranked[0][0][:18] if ranked else None,
            axis_note='School · vertical axis is number of visits',
            total=sum(per_school.values())),
        'rows': [(s, str(n)) for s, n in ranked],
        'distinct': len(per_school),
    }


# Collection.

def books_by_genre(limit=12):
    ranked = (Book.objects.exclude(genre__isnull=True).exclude(genre='')
              .values('genre').annotate(n=Count('book_id')).order_by('-n'))
    top = list(ranked[:limit])
    total = Book.objects.count()
    return {
        'chart': _chart(
            [(r['genre'][:18], r['n']) for r in top], kind='bar',
            title='Collection by genre',
            note='Top %d of %d genres. Shares are of the whole collection.'
                 % (len(top), ranked.count()),
            empty_note='No genres recorded yet.',
            highlight=top[0]['genre'][:18] if top else None,
            axis_note='Genre · vertical axis is number of copies',
            total=total),
        'rows': [(r['genre'], str(r['n'])) for r in top],
        'distinct': ranked.count(),
    }


def books_by_condition():
    """Good, Worn, Damaged. This is the repair and replacement budget."""
    counts = Counter(Book.objects.values_list('condition', flat=True))
    order = [c for c, _ in Book.CONDITION_CHOICES]
    series = [(c, counts.get(c, 0)) for c in order]
    total = sum(counts.values())
    damaged = counts.get('Damaged', 0)
    return {
        'chart': _chart(
            series, kind='bar', title='Condition of the collection',
            note='Recorded when a copy is catalogued or checked in.',
            empty_note='No copies catalogued yet.',
            highlight='Damaged' if damaged else None,
            axis_note='Condition · vertical axis is number of copies',
            total=total),
        'rows': [(c, str(n)) for c, n in series],
        'damaged': damaged,
        'damaged_share': round(damaged / total * 100, 1) if total else 0,
    }


def shelf_occupancy(limit=15):
    """How loaded each shelf is, fullest first."""
    shelves = (Shelf.objects.annotate(n=Count('shelflevel__book'))
               .order_by('-n', 'name'))
    top = list(shelves[:limit])
    shelved = Book.objects.filter(shelf_level__isnull=False).count()
    return {
        'chart': _chart(
            [(s.name[:16], s.n) for s in top], kind='bar',
            title='Books per shelf',
            note='Fullest first. Shares are of every shelved copy.',
            empty_note='Nothing is shelved yet.',
            highlight=top[0].name[:16] if top else None,
            axis_note='Shelf · vertical axis is number of copies',
            total=shelved),
        'rows': [(s.name, str(s.n)) for s in top],
        'empty_shelves': sum(1 for s in shelves if not s.n),
    }


def unshelved_summary():
    """Books in the catalogue that are on no shelf at all."""
    unshelved = Book.objects.filter(shelf_level__isnull=True).count()
    total = Book.objects.count()
    return {
        'count': unshelved,
        'total': total,
        'share': round(unshelved / total * 100, 1) if total else 0,
        'shelved': total - unshelved,
    }


# Borrowing

def most_borrowed_books(start, end, limit=12):
    """Which titles actually move. What to buy more of."""
    borrows = (Transaction.objects.select_related('book')
               .filter(transaction_type='Borrow',
                       transaction_date__range=(start, end)))
    per_title = Counter()
    for tx in borrows:
        if tx.book:
            per_title[(tx.book.title, tx.book.author or '—')] += 1
    ranked = per_title.most_common(limit)
    return {
        'chart': _chart(
            [(title[:22], n) for (title, _author), n in ranked], kind='bar',
            title='Most borrowed titles',
            note='Top %d over the period.' % limit,
            empty_note='No books were borrowed in this period.',
            highlight=ranked[0][0][0][:22] if ranked else None,
            axis_note='Title · vertical axis is times borrowed',
            total=sum(per_title.values())),
        'rows': [(title, author, str(n)) for (title, author), n in ranked],
        'distinct': len(per_title),
        'total': sum(per_title.values()),
    }


def most_borrowed_genres(start, end, limit=12):
    """Borrowing by subject."""
    borrows = (Transaction.objects.select_related('book')
               .filter(transaction_type='Borrow',
                       transaction_date__range=(start, end)))
    per_genre = Counter()
    for tx in borrows:
        if tx.book:
            per_genre[(tx.book.genre or 'Not recorded')] += 1
    ranked = per_genre.most_common(limit)
    return {
        'chart': _chart(
            [(g[:18], n) for g, n in ranked], kind='bar',
            title='Most borrowed genres',
            note='What subjects people take home.',
            empty_note='No books were borrowed in this period.',
            highlight=ranked[0][0][:18] if ranked else None,
            axis_note='Genre · vertical axis is times borrowed',
            total=sum(per_genre.values())),
        'rows': [(g, str(n)) for g, n in ranked],
    }


def never_borrowed(limit=10):
    """Stock that has never left the building."""
    idle = (Book.objects.annotate(loans=Count('transaction'))
            .filter(loans=0).select_related('shelf_level', 'shelf_level__shelf'))
    total = Book.objects.count()
    count = idle.count()
    return {
        'count': count,
        'total': total,
        'share': round(count / total * 100, 1) if total else 0,
        'rows': [(b.title, b.genre or '—',
                  b.location_label(short=True) if b.shelf_level else 'Not shelved')
                 for b in idle.order_by('title')[:limit]],
    }


# Penalties

def penalties_over_time(start, end, period='day'):
    """Fines charged, bucketed by day, week or month."""
    txns = (Transaction.objects
            .filter(fine_amount__gt=0)
            .filter(Q(return_date__range=(start, end))
                    | Q(return_date__isnull=True, transaction_date__range=(start, end))))

    buckets = {}
    for tx in txns:
        charged_on = tx.return_date or tx.transaction_date
        if not charged_on:
            continue
        if period == 'month':
            key = charged_on.replace(day=1)
        elif period == 'week':
            key = charged_on - timedelta(days=charged_on.weekday())
        else:
            key = charged_on
        buckets[key] = buckets.get(key, 0) + float(tx.fine_amount or 0)

    if period == 'month':
        labels = lambda k: k.strftime('%b %Y')
        keys = sorted(buckets)
    elif period == 'week':
        labels = lambda k: 'w/c %s' % k.strftime('%d %b')
        keys = sorted(buckets)
    else:
        keys = _days_in(start, end)
        labels = lambda k: _day_label(k, len(keys))

    series = [(labels(k), round(buckets.get(k, 0), 2)) for k in keys]
    total = round(sum(buckets.values()), 2)
    biggest = max(buckets, key=lambda k: buckets[k]) if buckets else None

    return {
        'chart': _chart(
            series, kind='line' if period == 'day' else 'bar',
            title='Fines charged per %s' % period,
            note='Charged when a copy is returned late, or when it is marked lost.',
            empty_note='No fines were charged in this period.',
            highlight=labels(biggest) if biggest is not None else None,
            axis_note='%s · vertical axis is pesos charged' % period.title()),
        'rows': [(label, '%.2f' % value) for label, value in series if value],
        'total': total,
        'periods_with_fines': len(buckets),
        'average': round(total / len(buckets), 2) if buckets else 0,
    }


# Headline figures

def headline(arrivals, occupancy, unshelved, conditions,
             borrowed_books, penalties):
    """The half-dozen figures that answer the page before anybody scrolls."""
    def tile(label, value, caption, scope, tone='neutral'):
        return {'label': label, 'value': value, 'caption': caption,
                'scope': scope, 'tone': tone}

    damaged = conditions.get('damaged') or 0
    missing_shelf = unshelved.get('count') or 0

    return [
        tile('Visits', arrivals.get('total') or 0,
             'Busiest at %s' % (arrivals.get('peak') or 'no arrivals yet'),
             'period'),
        tile('Fullest hour', occupancy.get('busiest') or '—',
             'Averaged over %d day%s' % (occupancy.get('days_measured') or 0,
                                         '' if occupancy.get('days_measured') == 1 else 's'),
             'period'),
        tile('Loans', borrowed_books.get('total') or 0,
             '%d different title%s' % (borrowed_books.get('distinct') or 0,
                                       '' if borrowed_books.get('distinct') == 1 else 's'),
             'period'),
        tile('Fines charged', '₱%s' % ('{:,.2f}'.format(penalties.get('total') or 0)),
             'Late returns and lost copies', 'period',
             tone='warn' if (penalties.get('total') or 0) else 'neutral'),
        tile('Copies held', unshelved.get('total') or 0,
             '%d on a shelf' % (unshelved.get('shelved') or 0), 'now'),
        tile('Needs attention', missing_shelf + damaged,
             '%d unshelved, %d damaged' % (missing_shelf, damaged), 'now',
             tone='bad' if (missing_shelf + damaged) else 'good'),
    ]
