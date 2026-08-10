from django.db import models
from django.utils import timezone
from datetime import time

from .modules import MODULE_KEYS, MODULE_LABELS, clean_module_keys
from .names import compose_name, name_matches, parse_name


class ActiveLocationManager(models.Manager):
    """Manager that filters books to only include those in active locations"""
    def get_queryset(self):
        return super().get_queryset().filter(
            shelf_level__shelf__room__floor_plan__is_active=True,
            shelf_level__shelf__room__is_active=True,
            shelf_level__shelf__is_active=True,
            shelf_level__is_active=True
        )


# ─── 1. FLOOR PLANS ───────────────────────────────────────────
class FloorPlan(models.Model):
    """A floor plan is a blank vector canvas the Administrator draws on.

    There is no floor-plan image: rooms are drawn as editable polygons
    (see Room.geometry), and shelves, beacons and waypoints are placed on the
    same canvas. `canvas_width`/`canvas_height` define the coordinate space that
    every map_x/map_y in the navigation tables is expressed in.
    """
    floor_plan_id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=255, default='Floor Plan')
    canvas_width = models.FloatField(default=1000)
    canvas_height = models.FloatField(default=800)
    # Canvas units per real-world metre. Everything on this plan (rooms, shelves,
    # beacons, waypoints) is stored in canvas units, while BLE path-loss returns
    # metres — without this scale the two cannot be mixed, so trilateration
    # refuses to run until an Administrator measures and sets it.
    pixels_per_meter = models.FloatField(blank=True, null=True)
    is_active = models.BooleanField(default=True)
    renovation_notice = models.CharField(max_length=255, blank=True, null=True)
    renovation_message = models.TextField(blank=True, null=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'Floor_Plans'

    def __str__(self):
        return self.name or f"Floor Plan {self.floor_plan_id}"


# ─── 2. BLE BEACONS ───────────────────────────────────────────
class BLEBeacon(models.Model):
    beacon_id = models.AutoField(primary_key=True)
    floor_plan = models.ForeignKey(
        FloorPlan,
        on_delete=models.CASCADE,
        db_column='floor_plan_id'
    )
    # How this beacon identifies itself over the air. Classic iBeacons put their
    # proximity UUID in manufacturer data (company 0x004C) and advertise no
    # service UUID at all; Eddystone advertises service 0xFEAA on every unit, so
    # the per-beacon ID is the namespace+instance inside its service data.
    # Matching on the wrong one yields zero RSSI samples while the config looks
    # perfectly correct, so the type is explicit rather than guessed.
    ADVERTISEMENT_TYPE_CHOICES = [
        ('iBeacon', 'iBeacon (Apple, manufacturer data)'),
        ('Eddystone', 'Eddystone-UID (service 0xFEAA)'),
        ('ServiceUUID', 'Advertises its own service UUID'),
        ('DeviceName', 'Match on device name'),
    ]

    beacon_uuid = models.CharField(max_length=255)
    advertisement_type = models.CharField(
        max_length=20, choices=ADVERTISEMENT_TYPE_CHOICES, default='iBeacon')
    # iBeacon: several beacons usually share one proximity UUID and differ only
    # by major/minor, so both are needed to tell them apart.
    major = models.IntegerField(blank=True, null=True)
    minor = models.IntegerField(blank=True, null=True)
    # Eddystone-UID: 10-byte namespace + 6-byte instance, stored as hex.
    namespace_id = models.CharField(max_length=32, blank=True, null=True)
    instance_id = models.CharField(max_length=16, blank=True, null=True)
    # Calibration. tx_power is the RSSI measured one metre from this beacon and
    # path_loss_n the environment exponent (2.0 free space; 2.5-3.5 indoors with
    # metal shelving). Both are per-beacon because they differ per unit and per
    # aisle; null falls back to the conservative defaults in the client.
    tx_power = models.IntegerField(blank=True, null=True)
    path_loss_n = models.FloatField(blank=True, null=True)
    map_x = models.FloatField()
    map_y = models.FloatField()
    label = models.CharField(max_length=255, blank=True, null=True)

    class Meta:
        db_table = 'BLE_Beacons'

    def __str__(self):
        return f"Beacon {self.beacon_uuid}"


# ─── 3. ROOMS ─────────────────────────────────────────────────
class Room(models.Model):
    room_id = models.AutoField(primary_key=True)
    floor_plan = models.ForeignKey(
        FloorPlan,
        on_delete=models.CASCADE,
        db_column='floor_plan_id'
    )
    name = models.CharField(max_length=255)
    # Polygon drawn by the Administrator: a list of [x, y] vertices in canvas
    # coordinates. map_x/map_y remain the label anchor (polygon centroid).
    geometry = models.JSONField(blank=True, null=True)
    map_x = models.FloatField()
    map_y = models.FloatField()
    description = models.TextField(blank=True, null=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'Rooms'

    def __str__(self):
        return self.name


# ─── 3b. DOORS ────────────────────────────────────────────────
class Door(models.Model):
    """An opening on a room's wall.

    Placed by snapping to the nearest edge of the room polygon, so `rotation`
    records the bearing of that wall and the door can be drawn as a proper
    floor-plan symbol (a gap in the wall plus a swing arc) rather than a pin.
    """
    door_id = models.AutoField(primary_key=True)
    room = models.ForeignKey(
        Room,
        on_delete=models.CASCADE,
        db_column='room_id'
    )
    map_x = models.FloatField()
    map_y = models.FloatField()
    width = models.FloatField(default=28)       # opening size, canvas units
    rotation = models.FloatField(default=0)     # bearing of the wall, degrees
    swing = models.SmallIntegerField(default=1) # 1 = arc inward, -1 = outward
    label = models.CharField(max_length=255, blank=True, null=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'Doors'

    def __str__(self):
        return self.label or f"Door {self.door_id}"


# ─── 4. SHELVES ───────────────────────────────────────────────
class Shelf(models.Model):
    shelf_id = models.AutoField(primary_key=True)
    room = models.ForeignKey(
        Room,
        on_delete=models.CASCADE,
        db_column='room_id',
        null=True,
        blank=True
    )
    name = models.CharField(max_length=255)
    # Null coordinates mean the shelf exists in the hierarchy but has not been
    # placed on the floor plan yet (or was unplaced by the Administrator).
    map_x = models.FloatField(blank=True, null=True)
    map_y = models.FloatField(blank=True, null=True)
    rotation = models.FloatField(default=0)     # degrees clockwise
    # Footprint in canvas units. Shelves are not all the same size, so each
    # carries its own; rotation is applied about the centre of this rectangle.
    width = models.FloatField(default=46)       # along the shelf run
    depth = models.FloatField(default=14)       # front to back
    description = models.TextField(blank=True, null=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'Shelves'

    def __str__(self):
        return self.name


# ─── 5. WAYPOINTS ─────────────────────────────────────────────
class Waypoint(models.Model):
    waypoint_id = models.AutoField(primary_key=True)
    floor_plan = models.ForeignKey(
        FloorPlan,
        on_delete=models.CASCADE,
        db_column='floor_plan_id'
    )
    map_x = models.FloatField()
    map_y = models.FloatField()
    label = models.CharField(max_length=255, blank=True, null=True)
    linked_shelf = models.ForeignKey(
        Shelf,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        db_column='linked_shelf'
    )

    class Meta:
        db_table = 'Waypoints'

    def __str__(self):
        return f"Waypoint {self.waypoint_id} ({self.label})"


# ─── 6. WAYPOINT CONNECTIONS ──────────────────────────────────
class WaypointConnection(models.Model):
    connection_id = models.AutoField(primary_key=True)
    waypoint_from = models.ForeignKey(
        Waypoint,
        on_delete=models.CASCADE,
        related_name='connections_from',
        db_column='waypoint_from_id'
    )
    waypoint_to = models.ForeignKey(
        Waypoint,
        on_delete=models.CASCADE,
        related_name='connections_to',
        db_column='waypoint_to_id'
    )
    distance = models.FloatField()

    class Meta:
        db_table = 'Waypoint_Connections'

    def __str__(self):
        return f"Connection {self.waypoint_from_id} → {self.waypoint_to_id}"


# ─── 7. SHELF LEVELS ──────────────────────────────────────────
class ShelfLevel(models.Model):
    shelf_level_id = models.AutoField(primary_key=True)
    shelf = models.ForeignKey(
        Shelf,
        on_delete=models.CASCADE,
        db_column='shelf_id'
    )
    level_number = models.IntegerField()
    category = models.CharField(max_length=255, blank=True, null=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'Shelf_Levels'

    def __str__(self):
        return f"Level {self.level_number} - {self.category}"


# ─── 9. BOOKS ─────────────────────────────────────────────────
class Book(models.Model):

    STATUS_CHOICES = [
        ('Available', 'Available'),
        ('Borrowed', 'Borrowed'),
        ('Being Read', 'Being Read'),
        ('Overdue', 'Overdue'),
        ('Lost', 'Lost'),
        ('Donated', 'Donated'),
    ]

    book_id = models.AutoField(primary_key=True)
    shelf_level = models.ForeignKey(
        ShelfLevel,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        db_column='shelf_level_id'
    )
    title = models.CharField(max_length=255)
    author = models.CharField(max_length=255)
    publication_year = models.IntegerField(blank=True, null=True)
    ISBN = models.CharField(max_length=255, blank=True, null=True)
    genre = models.CharField(max_length=255, blank=True, null=True)
    status = models.CharField(
        max_length=50,
        choices=STATUS_CHOICES,
        default='Available'
    )
    cover_img_url = models.CharField(max_length=255, blank=True, null=True)
    qr_code = models.CharField(max_length=255, blank=True, null=True)

    objects = models.Manager()
    active_locations = ActiveLocationManager()

    class Meta:
        db_table = 'Books'

    def __str__(self):
        return self.title


# ─── 10. DONATIONS ─────────────────────────────────────────────
class Donation(models.Model):

    STATUS_CHOICES = [
        ('Received', 'Received'),
        ('Processing', 'Processing'),
        ('Shelved', 'Shelved'),
    ]

    donation_id = models.AutoField(primary_key=True)
    book = models.ForeignKey(
        Book,
        on_delete=models.CASCADE,
        db_column='book_id'
    )
    donor_name = models.CharField(max_length=255)
    date_donated = models.DateField()
    status = models.CharField(
        max_length=50,
        choices=STATUS_CHOICES,
        default='Received'
    )

    class Meta:
        db_table = 'Donations'

    def __str__(self):
        return f"Donation from {self.donor_name}"


# ─── 11. USERS (ADMIN ACCOUNTS) ───────────────────────────────
class User(models.Model):

    STATUS_CHOICES = [
        ('Active', 'Active'),
        ('Inactive', 'Inactive'),
        ('Suspended', 'Suspended'),
    ]

    ROLE_CHOICES = [
        ('Admin', 'Admin'),
        ('Staff', 'Library Staff'),
    ]

    admin_id = models.AutoField(primary_key=True)
    fullname = models.CharField(max_length=255)
    email = models.EmailField(max_length=255, unique=True)
    password_hash = models.CharField(max_length=255)
    role = models.CharField(
        max_length=20,
        choices=ROLE_CHOICES,
        default='Admin'
    )
    account_status = models.CharField(
        max_length=50,
        choices=STATUS_CHOICES,
        default='Active'
    )
    # Modules this Staff account may open, as comma-separated keys from
    # library/modules.py. Ignored for Admins, who always have every module.
    modules = models.TextField(blank=True, default='')

    class Meta:
        db_table = 'Users'

    def __str__(self):
        return self.fullname

    @property
    def module_keys(self):
        """The modules this account may open (every module for an Admin)."""
        if self.role != 'Staff':
            return list(MODULE_KEYS)
        return clean_module_keys(self.modules)

    @property
    def module_labels(self):
        return [MODULE_LABELS[key] for key in self.module_keys]

    def has_module(self, key):
        return key in self.module_keys


# ─── 12. ANNOUNCEMENTS ────────────────────────────────────────
class Announcement(models.Model):
    announcement_id = models.AutoField(primary_key=True)
    posted_by = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        db_column='posted_by'
    )
    title = models.CharField(max_length=255)
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'Announcements'

    def __str__(self):
        return self.title


# ─── 13. PATRON ───────────────────────────────────────────────
class Patron(models.Model):

    PATRON_TYPE_CHOICES = [
        ('Student', 'Student'),
        ('Teacher', 'Teacher'),
        ('Parent', 'Parent'),
        ('General Visitor', 'General Visitor'),
    ]

    STATUS_CHOICES = [
        ('Pending', 'Pending'),
        ('Active', 'Active'),
        ('Suspended', 'Suspended'),
        ('Inactive', 'Inactive'),
        # Walked in and used the library without joining it. Has no password,
        # no QR and no borrowing rights — the row exists so that a repeat
        # visitor is recognised, and so that their visit history follows them
        # if they later register rather than being stranded as loose names.
        ('Visitor', 'Visitor'),
    ]

    REGISTRATION_CHANNEL_CHOICES = [
        ('Online', 'Online'),
        ('On-site', 'On-site'),
    ]

    patron_id = models.AutoField(primary_key=True)
    # The name is kept in parts because that is what makes it readable back.
    # "Juan Perez Dela Cruz" in one box gives no way to know whether "Dela"
    # belongs to the middle name or the surname; last_name = "Dela Cruz" says
    # so outright, which is what lets the front desk recognise someone who
    # types "Cruz, Juan" or "juan p. delacruz".
    first_name = models.CharField(max_length=100, blank=True, default='')
    middle_name = models.CharField(max_length=100, blank=True, default='')
    last_name = models.CharField(max_length=100, blank=True, default='')
    # Derived from the three above on save: "Juan P. Dela Cruz". Kept as a
    # stored column because every table, card, receipt and export in the system
    # already reads it, and none of them should have to learn about the parts.
    fullname = models.CharField(max_length=255)
    patron_type = models.CharField(
        max_length=50,
        choices=PATRON_TYPE_CHOICES,
        default='Student'
    )
    email = models.EmailField(max_length=255, unique=True, blank=True, null=True)
    contact_number = models.CharField(max_length=255, blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    account_status = models.CharField(
        max_length=50,
        choices=STATUS_CHOICES,
        default='Active'
    )
    password_hash = models.CharField(max_length=255)
    registration_date = models.DateField(auto_now_add=True)
    # Identity QR — generated on approval (online) or on the spot (on-site).
    qr_code = models.CharField(max_length=255, unique=True, blank=True, null=True)
    registration_channel = models.CharField(
        max_length=20,
        choices=REGISTRATION_CHANNEL_CHOICES,
        default='On-site'
    )
    # Uploaded ID / proof of residency (media-relative path). Only online
    # sign-ups carry one: nobody sees the applicant, so the upload is the only
    # identity evidence there is and a reviewer must open it before approving.
    # On-site registrations have none by design - see below.
    credential_document = models.CharField(max_length=255, blank=True, null=True)
    # Who validated this patron's identity, and when. On-site that is the desk
    # staff who looked at the physical ID; online it is whoever reviewed the
    # uploaded one before approving. Deliberately the *only* thing kept about
    # the check: the library records that an ID was verified, never the ID
    # itself, so a data breach cannot leak anyone's identity documents.
    identity_verified_by = models.ForeignKey(
        'User',
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        db_column='identity_verified_by',
        related_name='verified_patrons',
    )
    identity_verified_at = models.DateTimeField(blank=True, null=True)
    # Email OTP for the online registration flow.
    otp_code = models.CharField(max_length=6, blank=True, null=True)
    otp_expires_at = models.DateTimeField(blank=True, null=True)
    otp_verified = models.BooleanField(default=False)

    def save(self, *args, **kwargs):
        composed = compose_name(self.first_name, self.middle_name, self.last_name)
        if composed:
            self.fullname = composed
        elif self.fullname and not (self.first_name or self.last_name):
            # A name that arrived as one string — a walk-in typed at the desk,
            # or a row imported from a spreadsheet — is split so it can be
            # matched later. A librarian can correct the guess on the record.
            self.first_name, self.middle_name, self.last_name = parse_name(self.fullname)
        super().save(*args, **kwargs)

    class Meta:
        db_table = 'Patron'

    def __str__(self):
        return self.fullname


# ─── 14. TRANSACTIONS ─────────────────────────────────────────
class Transaction(models.Model):

    TRANSACTION_TYPE_CHOICES = [
        ('Borrow', 'Borrow'),
        ('Return', 'Return'),
        ('In-Library Reading', 'In-Library Reading'),
    ]

    transaction_id = models.AutoField(primary_key=True)
    patron = models.ForeignKey(
        Patron,
        on_delete=models.CASCADE,
        blank=True,
        null=True,         # nullable for In-Library Reading
        db_column='patron_id'
    )
    book = models.ForeignKey(
        Book,
        on_delete=models.CASCADE,
        db_column='book_id'
    )
    processed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        db_column='processed_by'
    )
    transaction_type = models.CharField(
        max_length=50,
        choices=TRANSACTION_TYPE_CHOICES
    )
    transaction_date = models.DateField(auto_now_add=True)
    due_date = models.DateField(blank=True, null=True)  # nullable for In-Library Reading
    return_date = models.DateField(blank=True, null=True)
    overdue_flag = models.BooleanField(default=False)
    fine_amount = models.DecimalField(max_digits=8, decimal_places=2, default=0)

    class Meta:
        db_table = 'Transactions'

    def __str__(self):
        return f"{self.transaction_type} - {self.book}"


# ─── 15. PATRON LOGS ──────────────────────────────────────────
class PatronLog(models.Model):
    """One visit session: entry_time is set on entry, exit_time on exit."""

    log_id = models.AutoField(primary_key=True)
    patron = models.ForeignKey(
        Patron,
        on_delete=models.CASCADE,
        db_column='patron_id'
    )
    school = models.CharField(max_length=255, blank=True, null=True)
    purpose_of_visit = models.CharField(max_length=255, blank=True, null=True)
    entry_time = models.DateTimeField(default=timezone.now)
    exit_time = models.DateTimeField(blank=True, null=True)
    # People leave without logging out. The nightly sweep stamps those visits
    # with the library's closing time, and flags them here so a guessed exit is
    # never mistaken for a real one — visit durations built on assumed exits
    # have to be readable as assumptions.
    auto_closed = models.BooleanField(default=False)

    class Meta:
        db_table = 'Patron_Logs'

    def __str__(self):
        return f"{self.patron} — entry {self.entry_time}"


# ─── 17. BORROWING RULES (ADMIN-CONFIGURABLE) ─────────────────
class BorrowingRule(models.Model):
    """Library-wide borrowing policy. A single active row is used; edit it
    through the admin Borrowing Rules page. Feeds due-date calculation,
    the borrowing limit, and overdue penalty computation."""

    rule_id = models.AutoField(primary_key=True)
    loan_period_days = models.IntegerField(default=14)
    max_books_per_patron = models.IntegerField(default=3)
    fine_per_day = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    grace_period_days = models.IntegerField(default=0)
    lost_book_fee = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'Borrowing_Rules'

    def __str__(self):
        return f"Borrowing Rule (loan {self.loan_period_days}d, fine {self.fine_per_day}/day)"

    @classmethod
    def current(cls):
        """Return the active rule, creating defaults on first use."""
        rule = cls.objects.first()
        if rule is None:
            rule = cls.objects.create()
        return rule

    def compute_fine(self, due_date, return_date):
        """Penalty for returning on return_date against due_date (grace applied)."""
        if not due_date or not return_date or return_date <= due_date:
            return 0
        days_late = (return_date - due_date).days - self.grace_period_days
        if days_late <= 0:
            return 0
        return days_late * self.fine_per_day


# ─── 16. SYSTEM LOGS (ADMIN AUDIT TRAIL) ──────────────────────
class SystemLog(models.Model):
    """Audit trail of administrative actions performed in the system.

    Powers the manuscript's System Log Report. ``admin`` is kept with
    SET_NULL and ``admin_name`` stores a snapshot so the trail survives
    even if the admin account is later deleted.
    """

    log_id = models.AutoField(primary_key=True)
    admin = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        db_column='admin_id'
    )
    admin_name = models.CharField(max_length=255, blank=True, null=True)
    action = models.CharField(max_length=50)          # Create, Update, Delete, Login, Process
    entity_type = models.CharField(max_length=100)    # Book, Patron, Transaction, Donation, ...
    entity_id = models.CharField(max_length=100, blank=True, null=True)
    detail = models.CharField(max_length=500, blank=True, null=True)
    timestamp = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'System_Logs'
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.action} {self.entity_type} by {self.admin_name}"

# ─── 16. PASSWORD RESET OTP ───────────────────────────────────
class PasswordResetOTP(models.Model):
    """A one-time code emailed for a forgot-password reset.

    Covers all three portals: patrons resolve against Patron, Library Staff and
    Administrators against User (kept apart by `account_type` plus the role the
    portal view looks up), so a code issued at the staff login cannot be spent
    at the admin login.
    """

    ACCOUNT_TYPE_CHOICES = [
        ('Patron', 'Patron'),
        ('Staff', 'Library Staff'),
        ('Admin', 'Administrator'),
    ]

    MAX_ATTEMPTS = 5

    reset_id = models.AutoField(primary_key=True)
    account_type = models.CharField(max_length=20, choices=ACCOUNT_TYPE_CHOICES)
    email = models.EmailField(max_length=255)
    code = models.CharField(max_length=6)
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    attempts = models.PositiveIntegerField(default=0)
    used_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        db_table = 'Password_Reset_OTP'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.account_type} reset for {self.email}"

    @property
    def is_expired(self):
        return timezone.now() > self.expires_at

    @property
    def is_usable(self):
        return (self.used_at is None
                and not self.is_expired
                and self.attempts < self.MAX_ATTEMPTS)


# ─── 17. INVENTORY RECORDS ────────────────────────────────────
class InventoryRecord(models.Model):
    """One physical copy on the shelves (Ch.1 ¶268).

    Deliberately separate from Book, which is the *catalogue* record: this
    module does not catalogue titles and does not assign shelf locations, so a
    received copy may exist here before it is catalogued or shelved. That is why
    `book` is nullable.

    Its `qr_label` is the copy-level QR, distinct from the catalogue QR on Book.
    """

    SOURCE_CHOICES = [
        ('Purchase', 'Shipment'),
        ('Donation', 'Donation'),
    ]

    CONDITION_CHOICES = [
        ('Good', 'Good'),
        ('Damaged', 'Damaged'),
        ('Lost', 'Lost'),
        ('Withdrawn', 'Withdrawn'),
    ]

    # Donated copies keep the accessioning lifecycle from Ch.1 ¶258; a shipment
    # has no equivalent stage, so this stays null for purchases.
    STAGE_CHOICES = [
        ('Received', 'Received'),
        ('Processing', 'Processing'),
        ('Shelved', 'Shelved'),
    ]

    STATUS_CHOICES = [
        ('In Stock', 'In Stock'),
        ('Removed', 'Removed'),        # audit adjustment or deaccession
    ]

    inventory_id = models.AutoField(primary_key=True)
    book = models.ForeignKey(
        'Book',
        on_delete=models.SET_NULL,
        blank=True,
        null=True,                     # received before catalogued
        db_column='book_id',
        related_name='inventory_records',
    )
    # Free-text stand-in used only while the copy has no catalogue record yet.
    title_hint = models.CharField(max_length=255, blank=True, null=True)
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default='Purchase')
    # Shipment intake fields — meaningless on a donation, so both stay null there.
    supplier = models.CharField(max_length=255, blank=True, null=True)
    po_number = models.CharField(max_length=100, blank=True, null=True)
    # Donation intake fields — the mirror image, null on a shipment.
    donor_name = models.CharField(max_length=255, blank=True, null=True)
    donated_date = models.DateField(blank=True, null=True)
    processing_stage = models.CharField(
        max_length=20, choices=STAGE_CHOICES, blank=True, null=True)
    # The accessioning row this copy is tracked by on the Donations page.
    # Null while the donated copy is still uncatalogued, since Donation requires
    # a Book; it is created the moment the copy is linked to a catalogue record.
    donation = models.ForeignKey(
        'Donation',
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        db_column='donation_id',
        related_name='inventory_copies',
    )
    condition = models.CharField(max_length=20, choices=CONDITION_CHOICES, default='Good')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='In Stock')
    qr_label = models.CharField(max_length=255, unique=True, blank=True, null=True)
    received_date = models.DateField(default=timezone.localdate)
    received_by = models.ForeignKey(
        'User',
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        db_column='received_by',
        related_name='received_inventory',
    )
    notes = models.TextField(blank=True, null=True)

    class Meta:
        db_table = 'Inventory_Records'
        ordering = ['-received_date', '-inventory_id']

    def __str__(self):
        return f"{self.display_title} ({self.condition})"

    @property
    def display_title(self):
        if self.book:
            return self.book.title
        return self.title_hint or f'Uncatalogued copy #{self.inventory_id}'

    @property
    def shelf_location(self):
        """Where the catalogue says this copy lives, or None if unshelved."""
        level = self.book.shelf_level if self.book else None
        if level is None:
            return None
        shelf = level.shelf
        return f"{shelf.name} · Level {level.level_number}" if shelf else f"Level {level.level_number}"

    @property
    def counts_as_held(self):
        """Whether this copy should be found on the shelves during an audit."""
        return self.status == 'In Stock' and self.condition in ('Good', 'Damaged')

    @property
    def source_detail(self):
        """The intake reference for this copy, whichever source it came from."""
        if self.source == 'Donation':
            parts = [self.donor_name] if self.donor_name else []
            if self.donated_date:
                parts.append(self.donated_date.strftime('%b %d, %Y'))
            return ' · '.join(parts)
        parts = [self.supplier] if self.supplier else []
        if self.po_number:
            parts.append('PO ' + self.po_number)
        return ' · '.join(parts)


# ─── 18. STOCK MOVEMENTS ──────────────────────────────────────
class StockMovement(models.Model):
    """Audit trail for every change to a copy's stock status (Ch.1 ¶268).

    "Every action that changes a copy's stock status — whether an addition, a
    condition change, an audit adjustment, or a correction — is logged in a
    movement history that records the actor, the reason, and the source."
    """

    ACTION_CHOICES = [
        ('Received', 'Received'),
        ('ConditionChange', 'Condition Change'),
        ('AuditAdjustment', 'Audit Adjustment'),
        ('Correction', 'Correction'),
        ('Deaccession', 'Deaccession'),
    ]

    movement_id = models.AutoField(primary_key=True)
    inventory_record = models.ForeignKey(
        InventoryRecord,
        on_delete=models.CASCADE,
        db_column='inventory_id',
        related_name='movements',
    )
    action = models.CharField(max_length=30, choices=ACTION_CHOICES)
    actor = models.ForeignKey(
        'User',
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        db_column='actor_id',
        related_name='stock_movements',
    )
    actor_name = models.CharField(max_length=255, blank=True, null=True)
    reason = models.CharField(max_length=500, blank=True, null=True)
    source = models.CharField(max_length=100, blank=True, null=True)
    condition_before = models.CharField(max_length=20, blank=True, null=True)
    condition_after = models.CharField(max_length=20, blank=True, null=True)
    timestamp = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'Stock_Movements'
        ordering = ['-timestamp', '-movement_id']

    def __str__(self):
        return f"{self.action} — {self.inventory_record_id}"
