"""The cataloguing rules every way of adding or changing a book follows: Add Book, Edit Book,
receiving a new title, and the spreadsheet imports. Each check returns (value, problem)."""

import re
from collections import Counter
from decimal import Decimal, InvalidOperation

from django.db.models import Value
from django.db.models.functions import Replace, Upper
from django.utils import timezone

from .models import CONDITION_NOTES, Book

# Words a sheet or a hurried typist uses when there is nothing real to write.
PLACEHOLDER = re.compile(
    r'^(n/?a|na|none|nil|null|unknown title|untitled|no title|not available|blank( spine)?|tba|tbd'
    r'|-+|\?+|\.+|x+|[\W_]+)$', re.I)
UNREADABLE = re.compile(r'not visible|blank spine|unreadable|illegible', re.I)
AUTHOR_UNKNOWN = {'unknown', 'unknown author', 'anonymous', 'anon'}
AUTHOR_VARIOUS = {'various', 'various authors', 'various author', 'et al'}
EARLIEST_YEAR = 1450
MAX_PRICE = Decimal('100000')
MAX_COPIES = 50

SMALL_WORDS = {'a', 'an', 'the', 'and', 'or', 'nor', 'but', 'of', 'in', 'on', 'at', 'to', 'for', 'by',
               'with', 'from', 'as', 'into', 'vs', 'sa', 'ng', 'mga', 'ang', 'na', 'para'}
ACRONYMS = {'ESP', 'NTE', 'MAPEH', 'PE', 'USA', 'UK', 'US', 'TESOL', 'ESL', 'EFL', 'ELT', 'DECS', 'DEPED',
            'II', 'III', 'IV', 'VI', 'VII', 'VIII', 'IX', 'XI', 'XII', 'CHED', 'TV', 'HIV', 'AIDS', 'SPED',
            'ICT', 'IQ', 'ASEAN', 'UNESCO', 'CAT', 'ROTC', 'NSTP', 'K'}


def tidy(text):
    """One space between words, none at the ends."""
    return ' '.join(str(text if text is not None else '').split())


def title_case(text):
    """'BECOMING A MASTER STUDENT' -> 'Becoming a Master Student', keeping acronyms and numerals."""
    words = tidy(text).split(' ')
    out = []
    for i, w in enumerate(words):
        core = re.sub(r'[^A-Za-z]', '', w)
        if core.upper() in ACRONYMS and core.isupper():
            out.append(w)
            continue
        low = w.lower()
        if 0 < i < len(words) - 1 and re.sub(r'[^a-z]', '', low) in SMALL_WORDS and not out[-1].endswith(':'):
            out.append(low)
            continue
        out.append(re.sub(r"(^|[-/(\"'.])([a-z])", lambda m: m.group(1) + m.group(2).upper(), low))
    return re.sub(r"'S\b", "'s", ' '.join(out))


def _shouting(text):
    letters = [c for c in text if c.isalpha()]
    return len(letters) > 3 and all(c.isupper() for c in letters)


def clean_title(raw):
    title = tidy(raw)
    if not title:
        return None, 'Type the title.'
    if PLACEHOLDER.match(title) or UNREADABLE.search(title):
        return None, ('"%s" is not a title. If the spine cannot be read, take the title from the '
                      'title page.' % title[:40])
    return (title_case(title) if _shouting(title) else title), None


def clean_author(raw):
    author = tidy(raw)
    if not author:
        return None, 'Type the author, or "Unknown" if the book names none.'
    key = author.lower().strip('.').strip()
    if key in AUTHOR_UNKNOWN:
        return 'Unknown', None
    if key in AUTHOR_VARIOUS:
        return 'Various Authors', None
    if PLACEHOLDER.match(author) or UNREADABLE.search(author):
        return None, '"%s" is not an author. Type "Unknown" if the book names none.' % author[:40]
    return (title_case(author) if _shouting(author) else author), None


def _isbn_digits(text):
    return re.sub(r'[\s-]', '', str(text or '')).upper()


def _isbn_checks_out(digits):
    if len(digits) == 10 and re.fullmatch(r'\d{9}[\dX]', digits):
        return sum((10 - i) * (10 if c == 'X' else int(c)) for i, c in enumerate(digits)) % 11 == 0
    if len(digits) == 13 and digits.isdigit():
        return sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(digits)) % 10 == 0
    return False


def _other_form(digits):
    """The same book's ISBN in the other length, so 10 and 13 digits are matched."""
    if len(digits) == 10:
        core = '978' + digits[:9]
        check = (10 - sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(core)) % 10) % 10
        return core + str(check)
    if len(digits) == 13 and digits.startswith('978'):
        core = digits[3:12]
        check = (11 - sum((10 - i) * int(c) for i, c in enumerate(core)) % 11) % 11
        return core + ('X' if check == 10 else str(check))
    return None


def books_with_isbn(digits):
    """Live books whose ISBN is these digits, written any way, in either length."""
    forms = [digits] + ([_other_form(digits)] if _other_form(digits) else [])
    return (Book.objects.annotate(
        isbn_digits=Upper(Replace(Replace('ISBN', Value('-'), Value('')), Value(' '), Value(''))))
        .filter(isbn_digits__in=forms))


# A library's own number when a book has no ISBN: 4521, ACC-0042, 2023-0451, 111-1.
ACCESSION = re.compile(r'^[A-Za-z]{0,4}[- ]?\d{1,8}(?:[-/ ]\d{1,4})?[A-Za-z]?$')


def clean_isbn(raw, title, exclude_ids=()):
    """An ISBN that checks out and is not another title's, an accession number, or blank."""
    isbn = tidy(raw)
    if not isbn:
        return '', None
    # "ISBN 978-..." is written as an ISBN, so it must be a whole one.
    named = re.match(r'^isbn(?:-?1[03])?[:\s-]*', isbn, re.I)
    if named:
        isbn = isbn[named.end():]
        if len(_isbn_digits(isbn)) not in (10, 13):
            return None, 'ISBN "%s" is not complete: an ISBN has 10 or 13 digits.' % tidy(raw)[:30]
    digits = _isbn_digits(isbn)
    if re.fullmatch(r'[0-9Xx\- ]+', isbn) and len(digits) in (10, 13):
        if not _isbn_checks_out(digits):
            return None, 'ISBN "%s" has a wrong check digit. Check it for a typo.' % isbn
        other = (books_with_isbn(digits).exclude(book_id__in=list(exclude_ids))
                 .exclude(title__iexact=tidy(title)).first())
        if other is not None:
            return None, 'ISBN %s already belongs to "%s". Check the number, or the title.' % (isbn, other.title)
        return isbn, None
    if ACCESSION.match(isbn):
        return isbn, None
    return None, ('"%s" is not an ISBN or an accession number. Leave it blank if the book has '
                  'neither.' % isbn[:30])


def clean_year(raw):
    text = tidy(raw)
    if not text:
        return None, None
    try:
        year = int(float(text))
    except (TypeError, ValueError):
        return None, 'The year "%s" is not a year.' % text[:12]
    latest = timezone.localdate().year + 1
    if not EARLIEST_YEAR <= year <= latest:
        return None, 'The year %d is outside %d to %d.' % (year, EARLIEST_YEAR, latest)
    return year, None


def clean_price(raw):
    text = tidy(raw).replace(',', '')
    for mark in ('₱', 'PHP', 'Php', 'php', 'P'):
        if text.startswith(mark):
            text = text[len(mark):].strip()
    if not text:
        return None, None
    try:
        price = Decimal(text)
    except (InvalidOperation, ValueError):
        return None, 'The price "%s" is not a number.' % tidy(raw)[:20]
    if not price.is_finite() or price < 0 or price > MAX_PRICE:
        return None, 'The price must be between 0 and %s.' % f'{MAX_PRICE:,}'
    return price.quantize(Decimal('0.01')), None


def clean_condition(raw, intake=False):
    """A new book arrives Good or with Minor Damage; a copy already here can also become Damaged."""
    from .models import InventoryRecord
    value = tidy(raw)
    if not value:
        return 'Good', None
    allowed = InventoryRecord.ARRIVAL_CONDITIONS if intake else tuple(CONDITION_NOTES)
    for c in allowed:
        if c.lower() == value.lower():
            return c, None
    if intake:
        return None, 'The library takes in books in Good condition or with Minor Damage only.'
    return None, 'Condition must be Good, Minor Damage or Damaged.'


def clean_copies(raw):
    text = tidy(raw)
    if not text:
        return 1, None
    try:
        n = int(float(text))
    except (TypeError, ValueError):
        return None, 'Number of copies must be a whole number.'
    if not 1 <= n <= MAX_COPIES:
        return None, 'Number of copies must be between 1 and %d.' % MAX_COPIES
    return n, None


def known_genres():
    """The library's genres, lower-case -> the spelling most of its books use."""
    spellings = Counter(tidy(g) for g in Book.objects.exclude(genre__isnull=True).exclude(genre='')
                        .values_list('genre', flat=True))
    out = {}
    for spelling, n in spellings.most_common():
        out.setdefault(spelling.lower(), spelling)
    return out


def clean_genre(raw, allow_new=False, known=None):
    genre = tidy(raw)
    if not genre:
        return None, 'Choose a genre.'
    known = known_genres() if known is None else known
    if genre.lower() in known:
        return known[genre.lower()], None
    if allow_new:
        return (title_case(genre) if _shouting(genre) else genre), None
    return None, ('"%s" is not one of the library\'s genres. Pick one from the list, or add it as a '
                  'new genre on purpose.' % genre[:40])


def matched_genre(raw, known=None):
    """The library genre a sheet's own wording belongs to ("Legal Thriller" -> Thriller & Suspense), or None."""
    from .genres import standard_genre
    target = standard_genre(raw)
    known = known_genres() if known is None else known
    return known.get(target.lower()) if target else None


# What a sheet writes when a book has no ISBN, or does not say its year.
NO_ISBN_WORDS = {'none', 'no isbn', 'no-isbn', 'without isbn', 'n/a', 'na', '-', '--'}
UNKNOWN_YEAR_WORDS = {'unknown', 'year unknown', 'unknown year', 'n.d.', 'nd', 'no date', 'n/a', 'na', '-', '--'}


def missing_details(isbn, no_isbn, year, year_unknown, price, sheet=False):
    """What a book still lacks: [(field, problem)]. A blank ISBN or year is fine only when said on purpose."""
    out = []
    if not isbn and not no_isbn:
        out.append(('ISBN', 'no ISBN: write it, or "none" if the book has none' if sheet else
                    'Type or scan the ISBN, or tick "No ISBN" if the book has none.'))
    if year is None and not year_unknown:
        out.append(('publication_year', 'no publication year: write it, or "unknown"' if sheet else
                    'Type the publication year, or tick "Year unknown".'))
    if price is None:
        out.append(('price', 'no price, which is what a patron pays if the book is lost' if sheet else
                    'Type the price. It is what a patron pays if the book is lost.'))
    return out


def same_title(title, author):
    """Live books already catalogued under this title and author."""
    return Book.objects.filter(title__iexact=tidy(title), author__iexact=tidy(author))
