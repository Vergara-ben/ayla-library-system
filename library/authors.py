"""Authors as separate names: who wrote, illustrated, edited, adapted or translated a book."""

import json
import re

from .names import PARTICLES as NAME_PARTICLES

ROLES = ('Author', 'Illustrator', 'Editor', 'Adapter', 'Translator')
KINDS = ('Person', 'Organization')

UNKNOWN_WORDS = {'unknown', 'unknown author', 'author unknown', 'anonymous', 'anon', 'n/a', 'na', 'none',
                 'no author', 'not specified', 'not specified on spine'}
# A part that only says there are more names than the line gives.
OTHERS_PART = re.compile(r'^(various(\s+authors?)?|multiple authors|et\.?\s*al\.?|etc\.?|and others|others'
                         r'|contributors|educators)$', re.I)
TRAILING_OTHERS = re.compile(r',?\s+(et\.?\s*al\.?|etc\.?|and others)$', re.I)

ORG_WORDS = re.compile(
    r"\b(?:press|publishing|publishers?|publications?|books|inc\.?|corp\.?|corporation|company|co\.|ltd\.?"
    r"|department|dept\.?|association|society|council|institute|university|college|school|foundation"
    r"|bureau|office|ministry|commission|committee|agency|library|editors|editorial|staff|editorial board"
    r"|academy|fund|federation|alliance|organi[sz]ation|services|surgeons|nurses|teachers"
    r"|group|center|centre|magazine|kitchen|task force|international|united nations|philippines?"
    r"|deped|decs|nhcp|fape|diwa|grolier|macmillan|scholastic|merriam|webster|hammond|hmh|harcourt"
    r"|mcgraw|pearson|vibal|abiva|disney|britannica|encyclopa?edia|parragon|usborne|ladybird|kindersley"
    r"|time-life|reader's digest|grandreams|mouse works|vatican)(?!\w)", re.I)

PARTICLES = set(NAME_PARTICLES) | {'le', 'du', 'der', 'den', 'ter', 'st', 'st.', 'bin', 'ibn', 'al', 'el'}
SUFFIX = re.compile(r'^(jr\.?|sr\.?|ii|iii|iv)$', re.I)
# Degrees and religious orders written after a name: Ph.D., Ed.D., F.M.M., OSP.
DEGREE = re.compile(r'^(ph\.?\s?d\.?|ed\.?\s?d\.?|m\.?a\.?|m\.?s\.?|r\.?n\.?|cpa|f\.?m\.?m\.?|s\.?j\.?|o\.?s\.?b\.?'
                    r'|o\.?s\.?p\.?|o\.?p\.?|s\.?v\.?d\.?|c\.?i\.?c\.?m\.?|r\.?v\.?m\.?|o\.?f\.?m\.?|o\.?s\.?a\.?)$', re.I)
TITLE = re.compile(r'^(dr|fr|rev|prof|atty|engr|mr|mrs|ms|sis|bro|hon|sr)\.?$', re.I)
INITIAL = re.compile(r'^([A-Z]\.){1,3}$|^[A-Z]$')
# Short forms that are half of a first name: Ma. Cristina.
NAME_PREFIX = {'ma.', 'ma', 'sta.', 'sto.'}

# Role notes in brackets or before a name: "(Ill. Eric Carle)", "Edited by John Hollander".
ROLE_PHRASES = [
    ('Illustrator', r'illustrations?\s+by|illustrated\s+by|illustrator|ill(?:us)?\.|ill|pictures\s+by|drawings\s+by'),
    ('Editor', r'edited\s+by|editor|eds?\.|ed'),
    ('Adapter', r'adapted\s+by|adaptation\s+by|retold\s+by|adapter|adap\.|adap|pinagaan\s+ni'),
    ('Translator', r'translated\s+by|translator|trans\.|isinalin\s+ni'),
]
ROLE_NOTE = re.compile(r'^(?:%s)(?=[\s,.]|$)\.?\s*(?P<rest>.*)$'
                       % '|'.join('(?P<%s>%s)' % (r, p) for r, p in ROLE_PHRASES), re.I)
ROLE_PREFIX = re.compile(r'^(edited\s+by|ed\.|illustrated\s+by|ill\.|adapted\s+by|adap\.|retold\s+by'
                         r'|translated\s+by|trans\.|written\s+by|by|inihanda\s+ni|isinulat\s+ni|akda\s+ni)\s+(.+)$', re.I)
_BRACKET = re.compile(r'\s*[(\[]([^)\]]*)[)\]]?')
_HOLE = re.compile(r'\x00(\d+)\x00')


def tidy(text):
    return ' '.join(str(text if text is not None else '').split())


def _shouting(text):
    letters = [c for c in text if c.isalpha()]
    return len(letters) > 3 and all(c.isupper() for c in letters)


def _case(text):
    from .catalogue import title_case
    text = tidy(text)
    # "ROALD DAHL" is shouting; "NHCP" is an acronym.
    return title_case(text) if _shouting(text) and any(len(w) > 4 for w in text.split()) else text


def _is_particle(word):
    word = word.lower()
    return word in PARTICLES or word.rstrip('.') in PARTICLES or (
        '-' in word and word.rsplit('-', 1)[1].rstrip('.') in PARTICLES)


def _leads_surname(word):
    """The Spanish and Filipino particles that never stand as a first name: de, dela, san, sta."""
    word = word.lower()
    return any(w.rstrip('.') in NAME_PARTICLES - {'van', 'von'} for w in (word, word.rsplit('-', 1)[-1]))


def _surname_like(chunk):
    """One surname, as a "Surname, Given" line starts: Dahl, Dela Cruz, de Lara-Topacio, Moral Jr."""
    words = chunk.split()
    if len(words) > 1 and SUFFIX.match(words[-1]):
        words = words[:-1]
    if not words or any(INITIAL.match(w) for w in words):
        return False
    return len(words) == 1 or all(_is_particle(w) for w in words[:-1])


def _odd(word):
    """A word no name has: a digit, a lower-case word that is not a particle, a cut-off name."""
    return (bool(re.search(r'\d', word)) or word.endswith('...') or not re.search(r'[^\W\d_]', word)
            or (word[:1].islower() and not _is_particle(word)))


def _given(tokens):
    """First and middle from the given names: a closing initial is the middle name."""
    tokens = list(tokens)
    if len(tokens) > 1 and INITIAL.match(tokens[-1]) and not all(INITIAL.match(t) for t in tokens):
        cut = len(tokens) - 1
        while cut > 1 and INITIAL.match(tokens[cut - 1]):
            cut -= 1
        return ' '.join(tokens[:cut]), ' '.join(tokens[cut:])
    if len(tokens) > 1 and tokens[0].lower() in NAME_PREFIX:
        return ' '.join(tokens[:2]), ' '.join(tokens[2:])
    return (tokens[0] if tokens else ''), ' '.join(tokens[1:])


def person(text):
    """A name written first-name-first: (first, middle, last, sure)."""
    tokens = tidy(text).split()
    while len(tokens) > 1 and TITLE.match(tokens[0]):
        tokens.pop(0)
    while len(tokens) > 1 and DEGREE.match(tokens[-1]):
        tokens.pop()
    sure = bool(tokens) and len(tokens) <= 5 and not any(_odd(t) for t in tokens)
    suffix = tokens.pop() if len(tokens) > 1 and SUFFIX.match(tokens[-1]) else ''
    if not tokens:
        return '', '', '', False
    sure = sure and not INITIAL.match(tokens[-1])     # "E. Bragado and A.": a cut-off name
    # "De Vega" or "San Andres" alone is a surname; "Al Spicer" and "Van Morrison" are not.
    if len(tokens) == 1 or all(_leads_surname(t) for t in tokens[:-1]):
        return '', '', ' '.join(tokens + ([suffix] if suffix else [])), sure
    cut = len(tokens) - 1
    while cut > 1 and _is_particle(tokens[cut - 1]):
        cut -= 1
    first, middle = _given(tokens[:cut])
    last = ' '.join(tokens[cut:] + ([suffix] if suffix else []))
    return first, middle, last, sure


def person_reversed(surname, given):
    """A name written "Surname, Given": (first, middle, last, sure)."""
    tokens = [t for t in tidy(given).split() if not DEGREE.match(t)]
    words = tidy(surname).split()
    if len(tokens) > 1 and SUFFIX.match(tokens[-1]):
        words.append(tokens.pop())              # "Wheelock, Arthur K. Jr."
    first, middle = _given(tokens)
    sure = bool(first) and len(tokens) <= 4 and not any(_odd(t) for t in tokens + words)
    # "Basingen, Maria Q. Acosta": a name after the middle initial is a second surname.
    sure = sure and not any(INITIAL.match(a) and not INITIAL.match(b) and i > 0
                            for i, (a, b) in enumerate(zip(tokens, tokens[1:])))
    return first, middle, ' '.join(words), sure


def credit(kind='Person', role='Author', first='', middle='', last='', name=''):
    return {'kind': kind, 'role': role, 'first': _case(first), 'middle': _case(middle),
            'last': _case(last), 'name': _case(name)}


def _role_of(phrase):
    for role, pattern in ROLE_PHRASES:
        if re.fullmatch(r'(?:%s)\.?' % pattern, phrase.strip(), re.I):
            return role
    return 'Author'


def _role_note(note):
    """[(role, names or '')] when a bracket only says who did what, else None."""
    out = []
    for piece in [p.strip() for p in re.split(r'\s*[,;]\s*(?=[A-Za-z])', note) if p.strip()]:
        m = ROLE_NOTE.match(piece)
        if m:
            role = next(r for r, _ in ROLE_PHRASES if m.group(r))
            out.append([role, m.group('rest').strip(' .')])
        elif out and out[-1][1]:
            out[-1][1] += ', ' + piece          # "(Ill. Ann Lee, Bo Kim)": one more illustrator
        else:
            return None
    return out or None


def split(raw, known=None):
    """The best reading of an author line, and whether it can be trusted without a look.

    Returns {'credits': [...], 'unknown': bool, 'others': bool, 'sure': bool}."""
    text = tidy(raw)
    result = {'credits': [], 'unknown': False, 'others': False, 'sure': True}
    if not text or text.lower().strip(' .()[]') in UNKNOWN_WORDS:
        result['unknown'] = True
        result['sure'] = bool(text)
        return result

    notes = []

    def hole(m):
        notes.append(m.group(1).strip())
        return ' \x00%d\x00' % (len(notes) - 1)
    text = tidy(_BRACKET.sub(hole, text))
    if TRAILING_OTHERS.search(_HOLE.sub('', text).strip()):
        result['others'] = True
        text = TRAILING_OTHERS.sub('', text.strip())

    credits = result['credits']
    plain = _HOLE.sub('', text)
    if ORG_WORDS.search(plain) and not re.search(r'[;/]', plain):
        parts = [text]                          # "Department of Education, Culture and Sports"
    else:
        parts = [p for p in re.split(r'\s*;\s*|\s+&\s+|\s+and\s+|\s*/\s*', text) if p.strip()]

    chunked = []
    for part in parts:
        role = 'Author'
        bare = _HOLE.sub('', part).strip(' ,')
        if OTHERS_PART.match(bare):
            result['others'] = True
            continue
        if TRAILING_OTHERS.search(bare):
            result['others'] = True
            part = TRAILING_OTHERS.sub('', part.strip())
        m = ROLE_PREFIX.match(part.strip())
        if m:
            role, part = _role_of(m.group(1)), m.group(2)
        if ORG_WORDS.search(_HOLE.sub('', part)):
            chunked.append((role, 'org', [part]))
            continue
        chunks = []
        for c in [c.strip() for c in part.split(',') if c.strip()]:
            core = _HOLE.sub('', c).strip()
            if DEGREE.match(core) or (core and OTHERS_PART.match(core)):
                result['others'] = result['others'] or bool(OTHERS_PART.match(core))
                if chunks and _HOLE.search(c):
                    chunks[-1] += c[len(core):]
                continue
            if SUFFIX.match(core) and chunks:
                chunks[-1] = chunks[-1] + ' ' + c
                continue
            if core or _HOLE.search(c):
                chunks.append(c)
        chunked.append((role, 'people', chunks))

    # "Koontz, Harold & Weihrich, Heinz" says every name in the line is written surname first.
    pairs_written = len(chunked) > 1 and any(
        kind == 'people' and len(ch) == 2 and _surname_like(_HOLE.sub('', ch[0]).strip())
        for _role, kind, ch in chunked)
    # "Mary and Conrad Buff": two people sharing one surname, which the line does not say.
    lone = [kind == 'people' and len(ch) == 1 and len(_HOLE.sub('', ch[0]).split()) == 1
            for _role, kind, ch in chunked]
    if any(lone) and not all(lone):
        result['sure'] = False

    for role, kind, chunks in chunked:
        if kind == 'org':
            name = _HOLE.sub('', chunks[0]).strip(' ,')
            _add(credits, credit('Organization', role, name=name), chunks[0], notes, result)
            continue
        cores = [_HOLE.sub('', c).strip() for c in chunks]
        names, sure = _read_people(cores, pairs_written, known)
        result['sure'] = result['sure'] and sure
        # A bracket after a chunk belongs to the name in that chunk.
        if len(names) == len(chunks):
            for c, n in zip(chunks, names):
                _add(credits, credit('Person', role, *n), c, notes, result)
        else:
            for c, n in zip(chunks[1::2], names):
                _add(credits, credit('Person', role, *n), c, notes, result)
            for c in chunks[0::2]:
                _attach_notes(c, notes, credits, result, None)

    if '/' in plain and any(c['kind'] == 'Person' for c in credits):
        result['sure'] = False                  # "Collier / P.F. Collier": who is who is never certain
    seen = set()
    unique = []
    for c in credits:
        key = (c['role'], name_key(c))
        if key in seen or not (c['last'] or c['name']):
            result['sure'] = False
            continue
        seen.add(key)
        unique.append(c)
    unique.sort(key=lambda c: 0 if c['role'] == 'Author' else 1)
    result['credits'] = unique
    if not unique:
        result['unknown'] = True
    return result


def _add(credits, entry, chunk, notes, result):
    credits.append(entry)
    _attach_notes(chunk, notes, credits, result, entry)


def _attach_notes(chunk, notes, credits, result, owner):
    for index in _HOLE.findall(chunk):
        note = notes[int(index)]
        roles = _role_note(note)
        if roles is None:
            # "(FAPE)" after an organization is its short name; "(Florante at Laura)" is a note.
            if not (owner and owner['kind'] == 'Organization' and re.fullmatch(r'[A-Z]{2,8}', note)):
                result['sure'] = False
            continue
        for role, names in roles:
            if not names:
                if owner is not None:
                    owner['role'] = role        # "Greg Hildebrandt (Ill.)"
                else:
                    result['sure'] = False
                continue
            for name in [n for n in re.split(r'\s*(?:,|&|\band\b)\s*', names) if n.strip()]:
                first, middle, last, sure = person(name)
                result['sure'] = result['sure'] and sure
                credits.append(credit('Person', role, first, middle, last))


def _read_people(cores, pairs_written, known):
    """The names in one comma list, and whether the reading is certain."""
    n = len(cores)
    if n == 0:
        return [], True
    if n == 1:
        f, m, l, sure = person(cores[0])
        return [(f, m, l)], sure
    surnames = [_surname_like(c) for c in cores]
    full = [len(c.split()) > 1 and not s for c, s in zip(cores, surnames)]
    if n == 2 and surnames[0]:
        if len(cores[1].split()) == 1 and not INITIAL.match(cores[1]) and not pairs_written:
            # "Bondi, Victor" is one person; "Capco, Yang" is two surnames.
            kind = _word_kind(cores[1], known)
            if kind == 'surname':
                out = [person(c) for c in cores]
                return [o[:3] for o in out], all(o[3] for o in out)
            f, m, l, sure = person_reversed(cores[0], cores[1])
            return [(f, m, l)], sure and kind == 'given' and not SUFFIX.match(cores[0].split()[-1])
        f, m, l, sure = person_reversed(cores[0], cores[1])
        return [(f, m, l)], sure
    if all(full):
        out = [person(c) for c in cores]
        return [o[:3] for o in out], all(o[3] for o in out)
    if n % 2 == 0 and all(surnames[0::2]) and all(_given_like(c) for c in cores[1::2]):
        has_initials = any(INITIAL.match(w) for c in cores[1::2] for w in c.split())
        kinds = [_word_kind(c.split()[0], known) for c in cores[1::2]]
        if pairs_written or has_initials or all(k == 'given' for k in kinds):
            out = [person_reversed(s, g) for s, g in zip(cores[0::2], cores[1::2])]
            return [o[:3] for o in out], all(o[3] for o in out)
        if all(surnames):
            # "Arrogante, Caberio, Belleza, Ramirez" unless the odd ones are first names.
            out = [person(c) for c in cores]
            return [o[:3] for o in out], all(k in ('surname', 'neither') for k in kinds) and all(o[3] for o in out)
    if all(surnames):
        out = [person(c) for c in cores]
        return [o[:3] for o in out], all(o[3] for o in out)
    out = [person(c) for c in cores]
    return [o[:3] for o in out], False


def _given_like(chunk):
    return all(w[:1].isupper() for w in chunk.split())


def _word_kind(word, known):
    """'given', 'surname', 'both' or 'neither', from how often the library has seen it as each."""
    w = word.lower()
    given = known['given'][w] if known else 0
    surname = known['surname'][w] if known else 0
    if given and given >= 2 * surname:
        return 'given'
    if surname and surname >= 2 * given:
        return 'surname'
    return 'both' if given else 'neither'


_KNOWN = {'at': 0, 'names': None}


def known_names():
    """First names and surnames already on record, to tell "Bondi, Victor" from "Capco, Yang"."""
    import time
    from collections import Counter
    from .models import Author, Donor, Patron
    # Read again after a few minutes; a sheet of a hundred rows asks once.
    if _KNOWN['names'] is not None and time.monotonic() - _KNOWN['at'] < 300:
        return _KNOWN['names']
    known = {'given': Counter(), 'surname': Counter()}
    for model in (Author, Donor, Patron):
        manager = getattr(model, 'all_objects', model.objects)
        for first, last in manager.values_list('first_name', 'last_name'):
            learn(known, first, last)
    _KNOWN.update(at=time.monotonic(), names=known)
    return known


def learn(known, first, last):
    words = (first or '').split()
    if words and not INITIAL.match(words[0]):
        known['given'][words[0].lower()] += 1
    words = (last or '').split()
    if words:
        known['surname'][words[-1].lower()] += 1


# Names as they are shown and filed

def norm(text):
    return ' '.join(re.sub(r"[^\w\s'-]", ' ', (text or '').lower()).split())


def name_key(entry):
    """One record per shown name, however its boxes were filled: "p:roald dahl"."""
    return (('o:' if entry['kind'] == 'Organization' else 'p:') + norm(shown_name(entry)))[:255]


def shown_name(entry):
    """Roald Dahl; a single-name writer is just the surname; an organization is its name."""
    if entry['kind'] == 'Organization':
        return entry['name']
    return ' '.join(p for p in (entry['first'], entry['middle'], entry['last']) if p)


def sort_name(entry):
    """Dahl, Roald: surname first for the reports, without Jr. or a leading "The"."""
    if entry['kind'] == 'Organization':
        return re.sub(r'^the\s+', '', entry['name'], flags=re.I)
    words = entry['last'].split()
    if len(words) > 1 and SUFFIX.match(words[-1]):
        words = words[:-1]
    given = ' '.join(p for p in (entry['first'], entry['middle']) if p)
    return ', '.join(p for p in (' '.join(words), given) if p)


def display(credits, unknown=False, others=False):
    """The author line every list, label and search uses: Walter B. Meigs & Robert F. Meigs."""
    authors = [shown_name(c) for c in credits if c['role'] == 'Author']
    if not authors:
        if unknown or not credits:
            return 'Various Authors' if others else 'Unknown'
        role = credits[0]['role']
        names = [shown_name(c) for c in credits if c['role'] == role]
        return '%s (%s)' % (_join(names, others), role)
    return _join(authors, others)


def _join(names, others, limit=255):
    if others:
        line = ', '.join(names) + ' et al.'
    elif len(names) > 1:
        line = ', '.join(names[:-1]) + ' & ' + names[-1]
    else:
        line = names[0]
    while len(line) > limit and len(names) > 1:
        names = names[:-1]
        line = ', '.join(names) + ' et al.'
    return line[:limit]


def filing_surname(text):
    """The name a call number files under, from an author line in either order."""
    text = _BRACKET.sub(' ', text or '')
    text = TRAILING_OTHERS.sub('', tidy(text))
    first = re.split(r'\s+(?:and|&)\s+|;|/', text)[0].strip(' ,')
    if not first:
        return ''
    if ORG_WORDS.search(first):
        words = [w for w in re.findall(r"[^\W\d_][\w'-]*", first) if w.lower() != 'the']
        return words[0] if words else ''
    if ',' in first:
        # "Austen, Jane" files under its first part; "Harold Koontz, Cyril O'Donnell" under Koontz.
        head = first.split(',', 1)[0].strip()
        return _last_of(head if _surname_like(head) else person(head)[2])
    return _last_of(person(first)[2])


def _last_of(last):
    words = last.split()
    if len(words) > 1 and SUFFIX.match(words[-1]):
        words = words[:-1]
    return ' '.join(words)


# Reading what a form or a sheet sent

def make_entry(credits, unknown=False, others=False, sure=True, original=''):
    authors = [c for c in credits if c['role'] == 'Author']
    unknown = bool(unknown or not credits) and not authors
    return {'credits': credits, 'unknown': unknown, 'others': bool(others), 'sure': bool(sure),
            'display': display(credits, unknown, others), 'original': tidy(original)[:255]}


def from_text(text, known=None):
    """One typed author line, split as best we can: (entry, problem)."""
    from .catalogue import clean_author
    line, problem = clean_author(text)
    if problem:
        return None, problem
    reading = split(line, known)
    return make_entry(reading['credits'], reading['unknown'], reading['others'], reading['sure'],
                      original=tidy(text)), None


def from_boxes(data):
    """The name boxes: each person's first, middle and surname, or an organization's name."""
    from .catalogue import PLACEHOLDER, UNREADABLE
    data = data if isinstance(data, dict) else {}
    credits = []
    for row in (data.get('credits') or [])[:30]:
        if not isinstance(row, dict):
            continue
        kind = row.get('kind') if row.get('kind') in KINDS else 'Person'
        role = row.get('role') if row.get('role') in ROLES else 'Author'
        if kind == 'Organization':
            entry = credit(kind, role, name=row.get('name'))
        else:
            entry = credit(kind, role, row.get('first'), row.get('middle'), row.get('last'))
        parts = [p for p in (entry['first'], entry['middle'], entry['last'], entry['name']) if p]
        if not parts:
            continue                            # an empty row is left out
        for part in parts:
            if PLACEHOLDER.match(part) or UNREADABLE.search(part):
                return None, '"%s" is not a name. Tick "Author unknown" if the book names none.' % part[:40]
        if kind == 'Person' and not entry['last']:
            return None, ('Type the surname of %s. A writer known by one name goes in Surname.'
                          % ' '.join(parts))
        if len(entry['first']) > 100 or len(entry['middle']) > 100 or len(entry['last']) > 150 \
                or len(shown_name(entry)) > 255:
            return None, 'One of the names is too long. Type it as printed on the book.'
        if any(c['role'] == role and name_key(c) == name_key(entry) for c in credits):
            continue
        credits.append(entry)
    unknown, others = bool(data.get('unknown')), bool(data.get('others'))
    if unknown and any(c['role'] == 'Author' for c in credits):
        return None, 'Untick "Author unknown", or clear the author names.'
    if not credits and not unknown:
        return None, 'Type the author, or tick "Author unknown" if the book names none.'
    credits.sort(key=lambda c: 0 if c['role'] == 'Author' else 1)
    return make_entry(credits, unknown, others), None


def from_post(post, known=None):
    """The author a form sent: its name boxes, or a typed line from an older page."""
    raw = post.get('authors_json')
    if raw:
        try:
            return from_boxes(json.loads(raw))
        except ValueError:
            pass
    return from_text(post.get('author'), known)


def from_item(item, known=None):
    """The author of one received line or sheet row."""
    if isinstance(item.get('authors'), dict):
        return from_boxes(item['authors'])
    return from_text(item.get('author'), known)


# Saving them

def find_or_create(entry):
    from .models import Author
    author, _ = Author.objects.get_or_create(name_key=name_key(entry), defaults={
        'kind': entry['kind'], 'first_name': entry['first'][:100], 'middle_name': entry['middle'][:100],
        'last_name': entry['last'][:150], 'name': shown_name(entry)[:255], 'sort_name': sort_name(entry)[:255]})
    return author


def attach(book, entry):
    """Give a saved book these names, and the author line built from them."""
    from .models import Book, BookAuthor
    BookAuthor.objects.filter(book=book).delete()
    for position, c in enumerate(entry['credits']):
        BookAuthor.objects.get_or_create(book=book, author=find_or_create(c), role=c['role'],
                                         defaults={'position': position})
    fields = {'author': entry['display'], 'author_unknown': entry['unknown'],
              'authors_and_others': entry['others'], 'authors_unsure': not entry['sure']}
    if entry['original'] and entry['original'] != entry['display']:
        fields['author_original'] = entry['original']
    changed = [f for f, v in fields.items() if getattr(book, f) != v]
    clash = None
    if 'author' in changed and book.copy_number:
        # Now under the same title and author as another copy with its number: the unlabelled one moves on.
        clash = (Book.all_objects.filter(title__iexact=(book.title or '').strip(), author__iexact=entry['display'],
                                         copy_number=book.copy_number).exclude(pk=book.pk).first())
        if clash is not None and not (book.label_printed_at and not clash.label_printed_at):
            book.copy_number, clash = None, None
            changed.append('copy_number')
    for f in changed:
        if f != 'copy_number':
            setattr(book, f, fields[f])
    if changed:
        book.save(update_fields=changed)
    if clash is not None:
        clash.copy_number = None
        clash.save(update_fields=['copy_number'])
    return book


def copy_credits(source, book):
    """A new copy of a title carries the same names as the copy it was made from."""
    from .models import BookAuthor
    if not source.author_unknown and not source.credits.exists():
        backfill([source])
        source.refresh_from_db()
    for c in source.credits.all():
        BookAuthor.objects.get_or_create(book=book, author_id=c.author_id, role=c.role,
                                         defaults={'position': c.position})
    type(book).all_objects.filter(pk=book.pk).update(
        author_unknown=source.author_unknown, authors_and_others=source.authors_and_others,
        authors_unsure=source.authors_unsure, author_original=source.author_original)


def entry_of(book):
    """A book's names as the boxes show them, split from its author line if never saved as names."""
    credits = [{'kind': c.author.kind, 'role': c.role, 'first': c.author.first_name,
                'middle': c.author.middle_name, 'last': c.author.last_name,
                'name': c.author.name if c.author.kind == 'Organization' else ''}
               for c in book.credits.select_related('author')]
    if credits or book.author_unknown:
        return make_entry(credits, book.author_unknown, book.authors_and_others, not book.authors_unsure,
                          book.author_original)
    reading = split(book.author)
    return make_entry(reading['credits'], reading['unknown'], reading['others'], reading['sure'], book.author)


def boxes_json(book=None):
    """What the name boxes start with: a book's names, or one empty person."""
    if book is None:
        return json.dumps({'credits': [], 'unknown': False, 'others': False})
    entry = entry_of(book)
    return json.dumps({'credits': entry['credits'], 'unknown': entry['unknown'], 'others': entry['others']})


def unsplit():
    """Books whose author line has never been made into names."""
    from .models import Book
    return Book.all_objects.filter(credits__isnull=True, author_unknown=False)


def backfill(books=None, known=None):
    """Split the author lines of books that have none, copying from another copy where one has names."""
    from .models import Book
    books = list(unsplit() if books is None else books)
    if not books:
        return 0
    known = known or known_names()
    for book in sorted(books, key=lambda b: (b.label_printed_at is None, b.pk)):
        sibling = (Book.all_objects.filter(title__iexact=(book.title or '').strip(),
                                           author__iexact=(book.author or '').strip(), credits__isnull=False)
                   .exclude(pk=book.pk).first())
        if sibling is not None:
            copy_credits(sibling, book)
            continue
        entry, problem = from_text(book.author, known)
        if problem:
            entry = make_entry([], True, sure=False, original=book.author)
        attach(book, entry)
    return len(books)
