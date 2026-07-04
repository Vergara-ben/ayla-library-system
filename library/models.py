from django.db import models
from django.utils import timezone


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
    floor_plan_id = models.AutoField(primary_key=True)
    image_url = models.CharField(max_length=255, blank=True, null=True)
    is_active = models.BooleanField(default=True)
    renovation_notice = models.CharField(max_length=255, blank=True, null=True)
    renovation_message = models.TextField(blank=True, null=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'Floor_Plans'

    def __str__(self):
        return f"Floor Plan {self.floor_plan_id}"


# ─── 2. BLE BEACONS ───────────────────────────────────────────
class BLEBeacon(models.Model):
    beacon_id = models.AutoField(primary_key=True)
    floor_plan = models.ForeignKey(
        FloorPlan,
        on_delete=models.CASCADE,
        db_column='floor_plan_id'
    )
    beacon_uuid = models.CharField(max_length=255)
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
    map_x = models.FloatField()
    map_y = models.FloatField()
    description = models.TextField(blank=True, null=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'Rooms'

    def __str__(self):
        return self.name


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
    map_x = models.FloatField()
    map_y = models.FloatField()
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

    class Meta:
        db_table = 'Users'

    def __str__(self):
        return self.fullname


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
    ]

    REGISTRATION_CHANNEL_CHOICES = [
        ('Online', 'Online'),
        ('On-site', 'On-site'),
    ]

    patron_id = models.AutoField(primary_key=True)
    fullname = models.CharField(max_length=255)
    patron_type = models.CharField(
        max_length=50,
        choices=PATRON_TYPE_CHOICES,
        default='Student'
    )
    email = models.EmailField(max_length=255, unique=True)
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
    # Transaction PIN — second factor scanned alongside the QR at the desk.
    pin_hash = models.CharField(max_length=255, blank=True, null=True)
    registration_channel = models.CharField(
        max_length=20,
        choices=REGISTRATION_CHANNEL_CHOICES,
        default='On-site'
    )
    # Uploaded ID / proof of residency (media-relative path, online channel).
    credential_document = models.CharField(max_length=255, blank=True, null=True)
    # Email OTP for the online registration flow.
    otp_code = models.CharField(max_length=6, blank=True, null=True)
    otp_expires_at = models.DateTimeField(blank=True, null=True)
    otp_verified = models.BooleanField(default=False)

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