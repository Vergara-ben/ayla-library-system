"""Reading a name someone typed, in whatever order they typed it.

The library asks for a first name, an optional middle name and a surname when
someone registers. At the front desk it asks for none of that — a patron types
their name however they think of it and the system has to recognise them:

    Juan A. Delacruz        Delacruz, Juan      juan dela cruz
    Juan Perez Dela Cruz    Dela Cruz, Juan P.  JUAN  DELACRUZ

Storing the parts separately is what makes this possible. "Juan Perez Dela
Cruz" in a single box gives no way to tell whether `Dela` belongs to the middle
name or to the surname; `last_name = "Dela Cruz"` simply says so.

Matching is deliberately strict: tokens must match in full, or as an initial.
Nothing here guesses at typos or nicknames. At a supervised desk a name that
fails to match costs five seconds of the librarian's time, while a name that
matches the wrong person files a visit under a stranger and nobody ever finds
out.
"""

import re

# Surname particles are part of the surname, not a middle name. Without this,
# "Juan Dela Cruz" is filed under the surname "Cruz" with "Dela" as a middle
# name, and half of Cabuyao is misfiled.
PARTICLES = {
    'de', 'dela', 'del', 'delos', 'delas', 'della', 'di', 'da', 'das', 'dos',
    'la', 'las', 'los', 'san', 'santa', 'santo', 'sta', 'sto', 'van', 'von',
    'y', 'ng',
}

# Not part of anyone's name for matching purposes.
SUFFIXES = {'jr', 'jr.', 'sr', 'sr.', 'ii', 'iii', 'iv', 'v', 'vi'}


def tokenise(value):
    """Lower-case, letters-only words, in the order they were written."""
    if not value:
        return []
    cleaned = re.sub(r"[^A-Za-z\s]", ' ', str(value))
    return [t for t in cleaned.lower().split() if t and t not in SUFFIXES]


def parse_name(raw):
    """Best-effort split of a free-typed name into (first, middle, last).

    Used when a walk-in is logged as a visitor from the one name box, and when
    an existing single-string name is migrated. It is a guess, and a librarian
    can correct it later — the structured fields are what the system trusts
    from then on.
    """
    raw = ' '.join((raw or '').split())
    if not raw:
        return '', '', ''

    # "Dela Cruz, Juan Perez" — the comma already told us where the surname ends.
    if ',' in raw:
        surname, _, given = raw.partition(',')
        given_parts = given.split()
        return (given_parts[0] if given_parts else '',
                ' '.join(given_parts[1:]),
                surname.strip())

    parts = [p for p in raw.split() if p.lower().strip('.') not in SUFFIXES]
    if len(parts) == 1:
        return parts[0], '', ''
    if len(parts) == 2:
        return parts[0], '', parts[1]

    # Walk back from the end while the word before is a particle, so "Dela Cruz"
    # and "delos Reyes" stay whole.
    cut = len(parts) - 1
    while cut > 1 and parts[cut - 1].lower().strip('.') in PARTICLES:
        cut -= 1
    return parts[0], ' '.join(parts[1:cut]), ' '.join(parts[cut:])


def compose_name(first, middle, last):
    """The formal display form: Juan A. Dela Cruz."""
    pieces = []
    first = (first or '').strip()
    middle = (middle or '').strip()
    last = (last or '').strip()
    if first:
        pieces.append(first)
    if middle:
        # An initial, which is how the name appears on every form here.
        pieces.append(middle[0].upper() + '.')
    if last:
        pieces.append(last)
    return ' '.join(pieces)


def _matches_token(typed, stored):
    """A full word, or a single letter standing in for one."""
    return typed == stored or (len(typed) == 1 and stored.startswith(typed))


def _take_surname(typed_tokens, last_tokens):
    """Remove the surname from what was typed, however it was spelled.

    A surname written as two words on one visit and joined on the next is the
    same surname: "Dela Cruz" and "Delacruz" both have to land on the same
    person, so runs of adjacent words are joined and compared as well.
    """
    if not last_tokens:
        return typed_tokens, True

    acceptable = set(last_tokens) | {''.join(last_tokens)}
    count = len(typed_tokens)
    # Longest run first, so "dela cruz" is taken as one surname rather than
    # "dela" alone leaving "cruz" stranded.
    for size in range(min(count, 4), 0, -1):
        for start in range(count - size + 1):
            run = typed_tokens[start:start + size]
            if ''.join(run) in acceptable:
                return typed_tokens[:start] + typed_tokens[start + size:], True
    return typed_tokens, False


def name_matches(typed, first, middle, last):
    """Is what they typed a way of writing this person's name?

    Every word typed has to be accounted for, and both ends of the name have to
    appear: "Juan" alone or "Cruz" alone is not enough to sign anybody in.
    """
    typed_tokens = tokenise(typed)
    if not typed_tokens:
        return False

    first_tokens = tokenise(first)
    middle_tokens = tokenise(middle)
    last_tokens = tokenise(last)
    if not (first_tokens or last_tokens):
        return False

    remaining, surname_seen = _take_surname(typed_tokens, last_tokens)
    if last_tokens and not surname_seen:
        return False

    given = first_tokens + middle_tokens
    given_joined = ''.join(first_tokens)
    first_seen = False
    for token in remaining:
        if token == given_joined and first_tokens:
            first_seen = True
            continue
        if any(_matches_token(token, g) for g in first_tokens):
            first_seen = True
            continue
        if any(_matches_token(token, g) for g in middle_tokens):
            continue
        if not given:
            return False
        return False        # a word that is not part of this person's name

    return first_seen or not first_tokens
