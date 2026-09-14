"""Per-account module access for Library Staff."""

STAFF_MODULES = [
    ('transactions', 'Transaction Module',
     'Process borrowing, returning, and in-library reading', 'fa-file-invoice'),
    ('logs', 'Log Management Module',
     'Record patron entry and exit at the front desk', 'fa-clipboard-list'),
    ('books', 'Book Management Module',
     'Add, edit, and catalogue book records', 'fa-book'),
    ('donations', 'Donation Module',
     'Accession donated books into the catalog', 'fa-hand-holding-heart'),
    ('shelf', 'Shelf Manager Module',
     'Maintain rooms, shelves, and shelf levels', 'fa-folder-tree'),
    ('indoor_map', 'Floor Plan Module',
     'Edit the floor plan, waypoints, and beacons', 'fa-map'),
    # Staff get stock receiving only.
    ('inventory', 'Stock Receiving',
     'Receive shipments and donations into inventory', 'fa-truck-ramp-box'),
    # Patron desk tasks for staff.
    ('patrons', 'Patron Registration & Review',
     'Register walk-ins and review online sign-ups', 'fa-user-check'),
    # Answering patron enquiries.
    ('chat', 'Messages',
     'Answer questions patrons send to the library', 'fa-comments'),
]

MODULE_KEYS = [key for key, _label, _desc, _icon in STAFF_MODULES]
MODULE_LABELS = {key: label for key, label, _desc, _icon in STAFF_MODULES}


def clean_module_keys(raw):
    """Normalise submitted module keys into an ordered, de-duplicated list."""
    if isinstance(raw, str):
        raw = raw.split(',')
    seen = set()
    cleaned = []
    for key in raw or []:
        key = (key or '').strip()
        if key in MODULE_KEYS and key not in seen:
            seen.add(key)
            cleaned.append(key)
    return cleaned
