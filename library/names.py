"""Reading a name someone typed, in whatever order they typed it."""

import re

# Surname particles are part of the surname, not a middle name.
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
    """Best-effort split of a free-typed name into (first, middle, last)."""
    raw = ' '.join((raw or '').split())
    if not raw:
        return '', '', ''

    # "Dela Cruz, Juan Perez": the comma marks where the surname ends.
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

    # Include surname particles like Dela and delos.
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
    """Remove the surname from the typed name."""
    if not last_tokens:
        return typed_tokens, True

    acceptable = set(last_tokens) | {''.join(last_tokens)}
    count = len(typed_tokens)
    # Match the longest surname first.
    for size in range(min(count, 4), 0, -1):
        for start in range(count - size + 1):
            run = typed_tokens[start:start + size]
            if ''.join(run) in acceptable:
                return typed_tokens[:start] + typed_tokens[start + size:], True
    return typed_tokens, False


def name_matches(typed, first, middle, last):
    """Is what they typed a way of writing this person's name?"""
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
