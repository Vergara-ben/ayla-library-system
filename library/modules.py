"""Per-account module access for Library Staff.

An Administrator picks which modules a Staff account may use when the account is
created (and can change them later from User Management). Administrators always
have every module; the Dashboard is always available to everyone, so it is not
listed here.

Each entry is (key, label, description, font-awesome icon). The keys double as
the `active=` values used by templates/library_staff/_sidebar.html.
"""

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
    # Ch.1 ¶268 gives Library Staff the stock-receiving functions only — audits,
    # deaccession, condition changes and movement history stay Administrator-only.
    ('inventory', 'Stock Receiving',
     'Receive shipments and donations into inventory', 'fa-truck-ramp-box'),
]

MODULE_KEYS = [key for key, _label, _desc, _icon in STAFF_MODULES]
MODULE_LABELS = {key: label for key, label, _desc, _icon in STAFF_MODULES}


def clean_module_keys(raw):
    """Normalise submitted module keys into an ordered, de-duplicated list.

    Accepts either a comma-separated string (how the field is stored and how the
    picker posts it) or a list. Anything unrecognised is dropped, so a tampered
    form cannot grant access to a module that does not exist.
    """
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
