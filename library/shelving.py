"""Which books still need a shelf or a label, where a book should go, and what a shelf's records get wrong.
One answer for every page that counts them."""

import re
from collections import Counter, defaultdict

from django.db.models import F

from .models import Book, ShelfLevel

# Here, and only waiting to be put on a board. Loans and repairs come back to these first.
WAITING_STATUSES = ('Available', 'Donated', 'For Reshelving')
# Off the shelves for good; a label means nothing on them.
GONE_STATUSES = ('Lost', 'Beyond Repair')


def need_shelf():
    """Books here that have no board yet."""
    return Book.objects.filter(shelf_level__isnull=True, status__in=WAITING_STATUSES)


def need_label():
    """Books on a board with no label printed or confirmed on them."""
    return (Book.objects.filter(shelf_level__isnull=False, label_printed_at__isnull=True)
            .exclude(status__in=GONE_STATUSES))


def board_name(level):
    return ('%s · %s' % (level.shelf.name, level.label)) if level.shelf_id else level.label


class BoardSuggester:
    """Where a book belongs: with its other copies, else where most of its genre stands."""

    def __init__(self):
        self.levels = {lv.shelf_level_id: lv for lv in ShelfLevel.objects.select_related('shelf')}
        by_title, by_genre = defaultdict(Counter), defaultdict(Counter)
        for title, author, genre, level_id in (Book.objects.filter(shelf_level__isnull=False)
                                               .values_list('title', 'author', 'genre', 'shelf_level')):
            by_title[((title or '').strip().lower(), (author or '').strip().lower())][level_id] += 1
            if genre:
                by_genre[genre.strip().lower()][level_id] += 1
        self.by_title = {k: c.most_common(1)[0][0] for k, c in by_title.items()}
        self.by_genre = {k: c.most_common(1)[0][0] for k, c in by_genre.items()}
        self.by_category = {}
        for level in self.levels.values():
            if level.category:
                self.by_category.setdefault(level.category.strip().lower(), level.shelf_level_id)

    def suggest(self, title, author, genre):
        """(ShelfLevel, why) or (None, '')."""
        key = ((title or '').strip().lower(), (author or '').strip().lower())
        genre_key = (genre or '').strip().lower()
        if key in self.by_title:
            return self.levels[self.by_title[key]], 'where its other copies are'
        if genre_key in self.by_genre:
            return self.levels[self.by_genre[genre_key]], 'where most of its genre, %s, stands' % genre
        if genre_key in self.by_category:
            return self.levels[self.by_category[genre_key]], 'the board labelled %s' % genre
        return None, ''


COPY_IN_TITLE = re.compile(r'\(\s*copy\s*\d+\s*\)\s*$', re.I)

# What a shelf's records can get wrong, in the order the audit lists them.
PROBLEM_KINDS = [
    ('gone', 'Recorded as gone, but still on this shelf'),
    ('call_number', 'No usable call number'),
    ('slot', 'Two books share one position'),
    ('rules', 'Breaks the cataloguing rules'),
    ('genre', 'Genre unlike the rest of its board'),
    ('qr', 'No QR code yet'),
]


def shelf_problems(shelf):
    """Every problem in the records of the books on one shelf: [{book, board, kind, text}]."""
    from .catalogue import clean_author, clean_isbn, clean_title, clean_year
    from .views import call_number_error
    books = list(Book.objects.filter(shelf_level__shelf=shelf).select_related('shelf_level', 'shelf_level__shelf')
                 .order_by('shelf_level__level_number', 'shelf_level__column_number',
                           F('shelf_slot').asc(nulls_last=True), 'title'))
    problems = []

    def add(book, kind, text):
        problems.append({'book': book, 'board': board_name(book.shelf_level), 'kind': kind, 'text': text})

    by_board = defaultdict(list)
    for book in books:
        by_board[book.shelf_level_id].append(book)

    for board_books in by_board.values():
        slots = defaultdict(list)
        for book in board_books:
            if book.shelf_slot:
                slots[book.shelf_slot].append(book)
        genres = Counter(b.genre for b in board_books if b.genre)
        main, main_count = genres.most_common(1)[0] if genres else ('', 0)
        # A board that is mostly one genre makes a stray genre worth a look.
        mostly = len(board_books) >= 5 and main_count >= 0.7 * len(board_books)

        for book in board_books:
            if book.status in GONE_STATUSES:
                add(book, 'gone', 'Recorded as %s. Take it off the shelf.' % book.status)
            call = (book.call_number or '').strip()
            if not call:
                add(book, 'call_number', 'No call number on the record.')
            elif call_number_error(call):
                add(book, 'call_number', '"%s" is a sticker number, not a call number.' % call)
            if book.shelf_slot and len(slots[book.shelf_slot]) > 1:
                others = [b.title for b in slots[book.shelf_slot] if b.pk != book.pk]
                add(book, 'slot', 'Position %d is also given to "%s".' % (book.shelf_slot, others[0]))
            isbn_problem = clean_isbn(book.ISBN, book.title, exclude_ids=[book.pk])[1]
            if isbn_problem and COPY_IN_TITLE.search(book.title or ''):
                isbn_problem = ('The copy number is part of the title. Copies share one title; each already '
                                'has its own QR code.')
            rule_problems = [p for p in (clean_title(book.title)[1], clean_author(book.author)[1],
                                         isbn_problem, clean_year(book.publication_year)[1]) if p]
            if rule_problems:
                add(book, 'rules', ' '.join(rule_problems))
            if mostly and book.genre and book.genre != main and genres[book.genre] <= 2:
                add(book, 'genre', '%s, on a board that is mostly %s.' % (book.genre, main))
            if not book.qr_code:
                add(book, 'qr', 'It gets one when its label is printed.')

    order = {kind: i for i, (kind, _title) in enumerate(PROBLEM_KINDS)}
    problems.sort(key=lambda p: order[p['kind']])
    return books, problems
