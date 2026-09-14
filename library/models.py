import math

from django.db import models
from django.utils import timezone
from datetime import time

from .modules import MODULE_LABELS, clean_module_keys
from .names import compose_name, parse_name


class ActiveLocationManager(models.Manager):
    """Manager that filters books to only include those in active locations"""
    def get_queryset(self):
        return super().get_queryset().filter(
            shelf_level__shelf__room__floor_plan__is_active=True,
            shelf_level__shelf__room__is_active=True,
            shelf_level__shelf__is_active=True,
            shelf_level__is_active=True
        )


# Floor plans
class FloorPlan(models.Model):
    """A floor plan is a blank vector canvas the Administrator draws on."""
    floor_plan_id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=255, default='Floor Plan')
    canvas_width = models.FloatField(default=1000)
    canvas_height = models.FloatField(default=800)
    # Canvas units per real-world metre.
    pixels_per_meter = models.FloatField(blank=True, null=True)
    # How far the plan's "up" sits from magnetic north, in degrees clockwise.
    north_offset_deg = models.FloatField(default=0)
    # Which storey this is.
    floor_number = models.IntegerField(default=1)
    # Whether this floor is in service.
    is_active = models.BooleanField(default=True)
    renovation_notice = models.CharField(max_length=255, blank=True, null=True)
    renovation_message = models.TextField(blank=True, null=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'Floor_Plans'
        ordering = ['floor_number', 'floor_plan_id']

    @property
    def floor_label(self):
        """How this storey is named to a patron."""
        n = self.floor_number
        if n is None:
            n = 1
        if n < 0:
            return f"Basement {abs(n)}"
        if n == 0:
            return "Ground floor"
        suffix = 'th' if 11 <= (n % 100) <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
        return f"{n}{suffix} floor"

    def __str__(self):
        return self.name or f"Floor Plan {self.floor_plan_id}"


# BLE beacons
class BLEBeacon(models.Model):
    beacon_id = models.AutoField(primary_key=True)
    floor_plan = models.ForeignKey(
        FloorPlan,
        on_delete=models.CASCADE,
        db_column='floor_plan_id'
    )
    # How this beacon identifies itself over the air.
    ADVERTISEMENT_TYPE_CHOICES = [
        ('iBeacon', 'iBeacon (Apple, manufacturer data)'),
        ('Eddystone', 'Eddystone-UID (service 0xFEAA)'),
        ('ServiceUUID', 'Advertises its own service UUID'),
        ('DeviceName', 'Match on device name'),
    ]

    beacon_uuid = models.CharField(max_length=255)
    advertisement_type = models.CharField(
        max_length=20, choices=ADVERTISEMENT_TYPE_CHOICES, default='iBeacon')
    # iBeacons are identified by UUID, major and minor.
    major = models.IntegerField(blank=True, null=True)
    minor = models.IntegerField(blank=True, null=True)
    # Eddystone-UID: 10-byte namespace + 6-byte instance, stored as hex.
    namespace_id = models.CharField(max_length=32, blank=True, null=True)
    instance_id = models.CharField(max_length=16, blank=True, null=True)
    # Calibration.
    tx_power = models.IntegerField(blank=True, null=True)
    path_loss_n = models.FloatField(blank=True, null=True)
    map_x = models.FloatField()
    map_y = models.FloatField()
    # Mounting height above the floor, in METRES, deliberately not canvas units.
    height = models.FloatField(blank=True, null=True)
    label = models.CharField(max_length=255, blank=True, null=True)

    class Meta:
        db_table = 'BLE_Beacons'

    def __str__(self):
        return f"Beacon {self.beacon_uuid}"


# Rooms
class Room(models.Model):
    room_id = models.AutoField(primary_key=True)
    floor_plan = models.ForeignKey(
        FloorPlan,
        on_delete=models.CASCADE,
        db_column='floor_plan_id'
    )
    name = models.CharField(max_length=255)
    # Polygon drawn by the Administrator: a list of [x, y] vertices in canvas coordinates.
    geometry = models.JSONField(blank=True, null=True)
    map_x = models.FloatField()
    map_y = models.FloatField()
    description = models.TextField(blank=True, null=True)
    is_active = models.BooleanField(default=True)

    # Whether a patron may walk in here.
    patron_access = models.BooleanField(default=True)

    class Meta:
        db_table = 'Rooms'

    def __str__(self):
        return self.name


# Doors
class Door(models.Model):
    """An opening on a room's wall."""
    door_id = models.AutoField(primary_key=True)
    room = models.ForeignKey(
        Room,
        on_delete=models.CASCADE,
        db_column='room_id'
    )
    # The room on the far side, when this doorway joins two of them.
    room_b = models.ForeignKey(
        Room,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='doors_far_side',
        db_column='room_b_id'
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


# Obstacles / furniture
class Obstacle(models.Model):
    """Anything drawn on the floor that is neither a room nor a shelf."""

    KIND_CHOICES = [
        ('Table', 'Table'),
        ('Counter', 'Counter / desk'),
        ('Seating', 'Seating area'),
        ('Pillar', 'Pillar / column'),
        ('Partition', 'Partition / divider'),
        ('Equipment', 'Equipment'),
        ('Other', 'Other obstacle'),
    ]

    obstacle_id = models.AutoField(primary_key=True)
    floor_plan = models.ForeignKey(
        FloorPlan,
        on_delete=models.CASCADE,
        db_column='floor_plan_id'
    )
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, default='Table')
    # Optional: "Study table 3" is worth labelling, a pillar is not.
    name = models.CharField(max_length=255, blank=True, null=True)
    geometry = models.JSONField(blank=True, null=True)
    map_x = models.FloatField(default=0)
    map_y = models.FloatField(default=0)
    # Whether patrons see it.
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'Obstacles'
        ordering = ['kind', 'obstacle_id']
        indexes = [models.Index(fields=['floor_plan', 'is_active'])]

    @property
    def label(self):
        """What to write on the map: the given name, else the kind."""
        return (self.name or '').strip() or self.get_kind_display()

    def __str__(self):
        return f'{self.get_kind_display()} #{self.obstacle_id}'


# Stairways / lifts
class Stairway(models.Model):
    """A way between floors: a staircase, a lift, or a ramp."""

    KIND_CHOICES = [
        ('Stairs', 'Staircase'),
        ('Elevator', 'Lift / elevator'),
        ('Ramp', 'Ramp'),
    ]
    DIRECTION_CHOICES = [
        ('up', 'Goes up'),
        ('down', 'Goes down'),
        ('both', 'Up and down'),
    ]

    stairway_id = models.AutoField(primary_key=True)
    floor_plan = models.ForeignKey(
        FloorPlan, on_delete=models.CASCADE, db_column='floor_plan_id',
        related_name='stairways',
    )
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, default='Stairs')
    name = models.CharField(max_length=255, blank=True, null=True)

    # Footprint, same shape as a room or an obstacle.
    geometry = models.JSONField(blank=True, null=True)
    map_x = models.FloatField(default=0)
    map_y = models.FloatField(default=0)

    # Flights and landings of a turning staircase.
    flights = models.JSONField(blank=True, null=True)

    # Degrees clockwise from canvas "up", along the direction of travel.
    bearing = models.FloatField(default=0)
    direction = models.CharField(max_length=8, choices=DIRECTION_CHOICES, default='both')

    # The floor at the other end.
    connects_to = models.ForeignKey(
        FloorPlan, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='stairways_arriving', db_column='connects_to_id',
    )

    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'Stairways'
        ordering = ['kind', 'stairway_id']
        indexes = [models.Index(fields=['floor_plan', 'is_active'])]

    @property
    def label(self):
        return (self.name or '').strip() or self.get_kind_display()

    @property
    def destination_label(self):
        """"2nd floor", or empty when this stair is not linked to anything yet."""
        return self.connects_to.floor_label if self.connects_to_id else ''

    def __str__(self):
        return f'{self.get_kind_display()} #{self.stairway_id}'


# Shelves
class Shelf(models.Model):
    # What the thing physically is.
    KIND_CHOICES = [
        ('Shelf', 'Shelf / bookcase'),
        ('Table', 'Table'),
        ('Display', 'Display stand'),
        ('Cart', 'Trolley / cart'),
        ('Ledge', 'Window ledge'),
    ]

    # Where the unit is fixed.
    MOUNT_CHOICES = [
        ('Floor', 'Stands on the floor'),
        ('Wall', 'Fixed to the wall'),
        ('Ceiling', 'Hung from the ceiling'),
    ]

    shelf_id = models.AutoField(primary_key=True)
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, default='Shelf')
    mount = models.CharField(max_length=10, choices=MOUNT_CHOICES, default='Floor')
    # Height of the lowest shelf above the floor, in metres.
    mount_height_m = models.FloatField(blank=True, null=True)
    room = models.ForeignKey(
        Room,
        on_delete=models.CASCADE,
        db_column='room_id',
        null=True,
        blank=True
    )
    name = models.CharField(max_length=255)
    # Null until the shelf is placed on the floor plan.
    map_x = models.FloatField(blank=True, null=True)
    map_y = models.FloatField(blank=True, null=True)
    rotation = models.FloatField(default=0)     # degrees clockwise
    # Footprint in canvas units.
    width = models.FloatField(default=46)       # along the shelf run
    depth = models.FloatField(default=14)       # front to back
    # Traced outline for non-rectangular shelves.
    geometry = models.JSONField(blank=True, null=True)
    description = models.TextField(blank=True, null=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'Shelves'

    @property
    def label(self):
        """'Shelf A', or 'Table 3 (table)' when the kind is worth saying."""
        bits = []
        if self.kind and self.kind != 'Shelf':
            bits.append(self.get_kind_display().lower())
        if self.mount and self.mount != 'Floor':
            bits.append('%s-mounted' % self.mount.lower())
        return f'{self.name} ({", ".join(bits)})' if bits else self.name

    @property
    def is_elevated(self):
        """Off the floor, so a route passes under it rather than around it."""
        return self.mount in ('Wall', 'Ceiling')

    def footprint(self):
        """The outline to draw, as [[x, y], ...] in canvas units."""
        if self.geometry and len(self.geometry) >= 3:
            return [[float(p[0]), float(p[1])] for p in self.geometry]
        if self.map_x is None or self.map_y is None:
            return []
        rad = math.radians(self.rotation or 0)
        cos, sin = math.cos(rad), math.sin(rad)
        hw, hd = (self.width or 46) / 2.0, (self.depth or 14) / 2.0
        return [
            [round(self.map_x + dx * cos - dy * sin, 2),
             round(self.map_y + dx * sin + dy * cos, 2)]
            for dx, dy in ((-hw, -hd), (hw, -hd), (hw, hd), (-hw, hd))
        ]

    def __str__(self):
        return self.name


# Waypoints
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
    # Laid down by "Generate walkable route" rather than placed by hand.
    is_generated = models.BooleanField(default=False)

    class Meta:
        db_table = 'Waypoints'

    def __str__(self):
        return f"Waypoint {self.waypoint_id} ({self.label})"


# Waypoint connections
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


# Shelf levels
class ShelfLevel(models.Model):
    shelf_level_id = models.AutoField(primary_key=True)
    shelf = models.ForeignKey(
        Shelf,
        on_delete=models.CASCADE,
        db_column='shelf_id'
    )
    level_number = models.IntegerField()
    category = models.CharField(max_length=255, blank=True, null=True)
    # The flat top of the unit, above the highest shelf.
    is_top = models.BooleanField(default=False)
    # The space under the shelf.
    is_under = models.BooleanField(default=False)
    # Bays across the run, for units divided both ways.
    column_number = models.IntegerField(blank=True, null=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'Shelf_Levels'

    # What the surface of a non-shelf unit is called.
    SURFACE_NAMES = {'Table': 'Table top', 'Counter': 'Counter top'}
    SHORT_SURFACE_NAMES = {'Table': 'Table', 'Counter': 'Counter'}

    @property
    def _kind(self):
        """The kind of unit this board belongs to, without risking a query."""
        shelf = self.__dict__.get('_state') and self._state.fields_cache.get('shelf')
        return getattr(shelf, 'kind', 'Shelf') or 'Shelf'

    @property
    def board_label(self):
        """The board on its own, with no column named."""
        kind = self._kind
        if kind != 'Shelf' and not self.is_under:
            return self.SURFACE_NAMES.get(kind, 'Surface')
        if self.is_top:
            return 'Top'
        if self.is_under:
            return 'Underneath'
        return f'Level {self.level_number}'

    @property
    def label(self):
        """The board named in full, for anywhere it stands alone."""
        base = self.board_label
        if self.column_number:
            return f'{base}, Column {self.column_number}'
        return base

    @property
    def short_label(self):
        """For a QR label, where the width is measured in millimetres."""
        kind = self._kind
        if kind != 'Shelf' and not self.is_under:
            base = self.SHORT_SURFACE_NAMES.get(kind, 'Surface')
        elif self.is_top:
            base = 'Top'
        elif self.is_under:
            base = 'Under'
        else:
            base = f'L{self.level_number}'
        return f'{base}C{self.column_number}' if self.column_number else base

    def __str__(self):
        return f"{self.label} - {self.category}"


# Books
class Book(models.Model):

    STATUS_CHOICES = [
        ('Available', 'Available'),
        ('Borrowed', 'Borrowed'),
        ('Being Read', 'Being Read'),
        # Returned to the desk but not yet put back.
        ('For Reshelving', 'For reshelving'),
        ('Overdue', 'Overdue'),
        # Not on its shelf at a stock-take, and no loan explains it.
        ('Missing', 'Missing'),
        ('Lost', 'Lost'),
        ('Donated', 'Donated'),
    ]

    # What kind of material this is, as distinct from what it is about.
    MATERIAL_TYPE_CHOICES = [
        ('Book', 'Book'),
        ('Magazine', 'Magazine'),
        ('Journal', 'Journal'),
        ('Comic', 'Comic / Graphic novel'),
        ('Newspaper', 'Newspaper'),
        ('Reference', 'Reference'),
        ('Thesis', 'Thesis / Research paper'),
    ]

    book_id = models.AutoField(primary_key=True)
    shelf_level = models.ForeignKey(
        ShelfLevel,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        db_column='shelf_level_id'
    )
    # Position along the level, counted from the left.
    shelf_slot = models.PositiveIntegerField(blank=True, null=True)
    # How worn the copy is.
    CONDITION_CHOICES = [
        ('Good', 'Good'),
        ('Worn', 'Worn'),
        ('Damaged', 'Damaged'),
    ]
    condition = models.CharField(max_length=10, choices=CONDITION_CHOICES,
                                 default='Good')

    # The call number written on the spine, e.g.
    call_number = models.CharField(max_length=64, blank=True, null=True)

    title = models.CharField(max_length=255)
    author = models.CharField(max_length=255)
    publication_year = models.IntegerField(blank=True, null=True)
    ISBN = models.CharField(max_length=255, blank=True, null=True)
    genre = models.CharField(max_length=255, blank=True, null=True)
    material_type = models.CharField(
        max_length=20, choices=MATERIAL_TYPE_CHOICES, default='Book')
    status = models.CharField(
        max_length=50,
        choices=STATUS_CHOICES,
        default='Available'
    )
    cover_img_url = models.CharField(max_length=255, blank=True, null=True)
    qr_code = models.CharField(max_length=255, blank=True, null=True)

    # Stock count results for this copy.
    audit_misses = models.IntegerField(default=0)
    # Date of the first missed count.
    missing_since = models.DateField(blank=True, null=True)
    # When this copy was last confirmed on its shelf.
    last_seen = models.DateTimeField(blank=True, null=True)

    # Second and third letters of the surname, as digits.
    @staticmethod
    def _cutter_digits(surname):
        digits = ''
        for ch in surname[1:]:
            if ch.isalpha():
                digits += str(((ord(ch.lower()) - ord('a')) % 9) + 1)
            if len(digits) == 2:
                break
        return digits or '1'

    @staticmethod
    def _surname_of(author):
        """Austen from "Austen, Jane" or from "Jane Austen"."""
        author = (author or '').strip()
        if not author:
            return ''
        if ',' in author:
            return author.split(',', 1)[0].strip()
        return author.split()[-1]

    @staticmethod
    def _title_letter(title):
        """First letter that carries meaning: "The Quiet Sea" files under q."""
        words = [w for w in (title or '').split() if w]
        if words and words[0].lower() in ('a', 'an', 'the'):
            words = words[1:]
        for word in words:
            for ch in word:
                if ch.isalpha():
                    return ch.lower()
        return ''

    def location_label(self, short=False):
        """'Shelf A Column 1 Level 2', or 'A C1 L2' where space is tight."""
        level = self.shelf_level
        if level is None:
            return ''
        shelf = level.shelf
        name = (getattr(shelf, 'name', '') or '').strip()
        if short and name:
            # Shorten 'Shelf A' to 'A'.
            words = name.split()
            if len(words) > 1 and words[0].lower() in ('shelf', 'bay', 'case', 'rack'):
                name = ' '.join(words[1:])

        parts = [name] if name else []
        if level.column_number:
            parts.append(('C%d' % level.column_number) if short
                         else ('Column %d' % level.column_number))
        if level.is_top:
            parts.append('Top')
        elif level.is_under:
            parts.append('Under' if short else 'Underneath')
        elif level.level_number:
            parts.append(('L%d' % level.level_number) if short
                         else ('Level %d' % level.level_number))
        return ' '.join(parts)

    @property
    def location(self):
        """The long form, for a template that cannot pass arguments."""
        return self.location_label()

    @property
    def location_short(self):
        return self.location_label(short=True)

    def derive_call_number(self):
        """The spine number this copy would be given."""
        klass = ''.join(ch for ch in (self.genre or '') if ch.isalpha())[:3].upper()
        surname = self._surname_of(self.author)
        mark = ''
        if surname:
            mark = surname[0].upper() + self._cutter_digits(surname) + self._title_letter(self.title)
        year = str(self.publication_year) if self.publication_year else ''
        return ' '.join(p for p in (klass or 'GEN', mark, year) if p)

    def save(self, *args, **kwargs):
        # Generate the call number once, on first save.
        if not (self.call_number or '').strip():
            self.call_number = self.derive_call_number()
        super().save(*args, **kwargs)

    objects = models.Manager()
    active_locations = ActiveLocationManager()

    class Meta:
        db_table = 'Books'
        indexes = [
            # Indexed for status filters.
            models.Index(fields=['status']),
            models.Index(fields=['title']),
        ]

    def __str__(self):
        return self.title


# Donations
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


# Users (admin accounts)
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
    # Modules this account may open, as comma-separated keys from library/modules.py.
    modules = models.TextField(blank=True, default='')

    class Meta:
        db_table = 'Users'

    def __str__(self):
        return self.fullname

    @property
    def initials(self):
        """One or two letters for the header's profile button."""
        words = (self.fullname or '').split()
        if not words:
            return '?'
        if len(words) == 1:
            return words[0][:1].upper()
        return (words[0][:1] + words[-1][:1]).upper()

    @property
    def module_keys(self):
        """The operational modules this account may open."""
        return clean_module_keys(self.modules)

    @property
    def module_labels(self):
        return [MODULE_LABELS[key] for key in self.module_keys]

    def has_module(self, key):
        return key in self.module_keys


# Announcements
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


# Patron
class Patron(models.Model):

    # Wrong OTP guesses allowed before the code is burnt.
    MAX_OTP_ATTEMPTS = 5

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
        # Walked in and used the library without joining it.
        ('Visitor', 'Visitor'),
    ]

    REGISTRATION_CHANNEL_CHOICES = [
        ('Online', 'Online'),
        ('On-site', 'On-site'),
    ]

    patron_id = models.AutoField(primary_key=True)
    # The number printed on the card.
    card_number = models.CharField(max_length=7, unique=True, blank=True, null=True)
    # The name is kept in parts because that is what makes it readable back.
    first_name = models.CharField(max_length=100, blank=True, default='')
    middle_name = models.CharField(max_length=100, blank=True, default='')
    last_name = models.CharField(max_length=100, blank=True, default='')
    # Derived from the three above on save: "Juan P.
    fullname = models.CharField(max_length=255)
    patron_type = models.CharField(
        max_length=50,
        choices=PATRON_TYPE_CHOICES,
        default='Student'
    )
    email = models.EmailField(max_length=255, unique=True, blank=True, null=True)
    contact_number = models.CharField(max_length=255, blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    # Where they study, for the patrons who study anywhere.
    school = models.CharField(max_length=255, blank=True, null=True)
    account_status = models.CharField(
        max_length=50,
        choices=STATUS_CHOICES,
        default='Active'
    )
    password_hash = models.CharField(max_length=255)
    registration_date = models.DateField(auto_now_add=True)
    # Identity QR, made on approval (online) or at the desk (on-site).
    qr_code = models.CharField(max_length=255, unique=True, blank=True, null=True)
    registration_channel = models.CharField(
        max_length=20,
        choices=REGISTRATION_CHANNEL_CHOICES,
        default='On-site'
    )
    # Uploaded ID / proof of residency (media-relative path).
    credential_document = models.CharField(max_length=255, blank=True, null=True)

    @property
    def credential_filename(self):
        """Just the file name -- the credential route serves from a fixed folder."""
        if not self.credential_document:
            return ''
        return self.credential_document.replace('\\', '/').rsplit('/', 1)[-1]

    # Who validated this patron's identity, and when.
    identity_verified_by = models.ForeignKey(
        'User',
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        db_column='identity_verified_by',
        related_name='verified_patrons',
    )
    identity_verified_at = models.DateTimeField(blank=True, null=True)
    # Email OTP and what it was issued for.
    otp_code = models.CharField(max_length=6, blank=True, null=True)
    otp_purpose = models.CharField(max_length=20, blank=True, default='')
    otp_expires_at = models.DateTimeField(blank=True, null=True)
    # Wrong guesses against the current code.
    otp_attempts = models.PositiveIntegerField(default=0)
    # When the last code was sent, for the cooldown.
    otp_last_sent_at = models.DateTimeField(blank=True, null=True)
    otp_verified = models.BooleanField(default=False)

    def save(self, *args, **kwargs):
        composed = compose_name(self.first_name, self.middle_name, self.last_name)
        if composed:
            self.fullname = composed
        elif self.fullname and not (self.first_name or self.last_name):
            # Split a single full name into parts.
            self.first_name, self.middle_name, self.last_name = parse_name(self.fullname)

        # Assign a card number to new members.
        if not self.card_number and self.account_status != 'Visitor':
            from .cardnumbers import generate
            self.card_number = generate()
            if 'update_fields' in kwargs and kwargs['update_fields'] is not None:
                kwargs['update_fields'] = list(kwargs['update_fields']) + ['card_number']

        super().save(*args, **kwargs)

    @property
    def card_display(self):
        """7482051, as it is printed on the card and read back at the desk."""
        from .cardnumbers import format_card
        return format_card(self.card_number) if self.card_number else ''

    class Meta:
        db_table = 'Patron'

    def __str__(self):
        return self.fullname


# Transactions
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
        indexes = [
            # Every report filters a date range on this.
            models.Index(fields=['transaction_date']),
            # Index for open loans.
            models.Index(fields=['transaction_type', 'return_date']),
            # One patron's history, on their account page and the desk lookup.
            models.Index(fields=['patron', '-transaction_date']),
        ]

    def __str__(self):
        return f"{self.transaction_type} - {self.book}"


class DueDateExtension(models.Model):
    """A request to extend a loan's due date."""

    STATUS_CHOICES = [
        ('Pending', 'Pending'),
        ('Approved', 'Approved'),
        ('Declined', 'Declined'),
    ]

    extension_id = models.AutoField(primary_key=True)
    transaction = models.ForeignKey(
        Transaction,
        on_delete=models.CASCADE,
        related_name='extension_requests',
    )
    requested_by_patron = models.BooleanField(default=True)
    previous_due_date = models.DateField()
    requested_due_date = models.DateField()
    reason = models.CharField(max_length=255, blank=True, null=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='Pending')
    requested_at = models.DateTimeField(default=timezone.now)
    resolved_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        db_column='resolved_by',
    )
    resolved_at = models.DateTimeField(blank=True, null=True)
    staff_note = models.CharField(max_length=255, blank=True, null=True)

    class Meta:
        db_table = 'Due_Date_Extensions'
        ordering = ['-requested_at']

    def __str__(self):
        return f'Extension for transaction #{self.transaction_id} ({self.status})'


class ReactivationRequest(models.Model):
    """One patron's ask to reactivate a self-deactivated account."""

    STATUS_CHOICES = [
        ('Pending', 'Pending'),
        ('Approved', 'Approved'),
        ('Rejected', 'Rejected'),
    ]

    request_id = models.AutoField(primary_key=True)
    patron = models.ForeignKey(
        Patron,
        on_delete=models.CASCADE,
        related_name='reactivation_requests',
    )
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='Pending')
    requested_at = models.DateTimeField(default=timezone.now)
    resolved_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        db_column='resolved_by',
    )
    resolved_at = models.DateTimeField(blank=True, null=True)
    staff_note = models.CharField(max_length=255, blank=True, null=True)

    class Meta:
        db_table = 'Reactivation_Requests'
        ordering = ['-requested_at']

    def __str__(self):
        return f'Reactivation request for {self.patron} ({self.status})'


# Patron logs
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
    # People leave without logging out.
    auto_closed = models.BooleanField(default=False)

    class Meta:
        db_table = 'Patron_Logs'
        indexes = [
            # Visit reports and the peak-hour analytics scan this by date.
            models.Index(fields=['entry_time']),
            # Index for open visits.
            models.Index(fields=['exit_time']),
        ]

    def __str__(self):
        return f"{self.patron} — entry {self.entry_time}"


class StockAudit(models.Model):
    """One stock-take of one shelf."""

    audit_id = models.AutoField(primary_key=True)
    shelf = models.ForeignKey(
        'Shelf',
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        db_column='shelf_id',
        related_name='stock_audits',
    )
    shelf_name = models.CharField(max_length=255, blank=True, default='')
    audited_by = models.ForeignKey(
        'User',
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        db_column='audited_by',
        related_name='stock_audits',
    )
    audited_at = models.DateTimeField(default=timezone.now)
    expected_count = models.IntegerField(default=0)
    scanned_count = models.IntegerField(default=0)
    found_count = models.IntegerField(default=0)
    on_loan_count = models.IntegerField(default=0)
    missing_count = models.IntegerField(default=0)
    recovered_count = models.IntegerField(default=0)
    unexpected_count = models.IntegerField(default=0)
    notes = models.TextField(blank=True, null=True)

    class Meta:
        db_table = 'Stock_Audits'
        ordering = ['-audited_at']

    def __str__(self):
        return f"Audit of {self.shelf_name or 'shelf'} on {self.audited_at:%Y-%m-%d}"

    @property
    def accounted_for(self):
        """Copies the shelf could explain: on it, or out on loan."""
        return self.found_count + self.on_loan_count

    @property
    def discrepancy_rate(self):
        if not self.expected_count:
            return 0
        return round(self.missing_count * 100.0 / self.expected_count, 1)


class Conversation(models.Model):
    """A patron's Ask a Librarian thread."""

    STATUS_CHOICES = [
        ('Open', 'Waiting for a reply'),
        ('Answered', 'Answered'),
        ('Closed', 'Closed'),
    ]

    # Enquiry topic.
    TOPIC_CHOICES = [
        ('Book enquiry', 'Looking for a book'),
        ('Borrowing', 'Borrowing, returning or fines'),
        ('Account', 'My account or library card'),
        ('Facilities', 'Opening hours or facilities'),
        ('Other', 'Something else'),
    ]

    conversation_id = models.AutoField(primary_key=True)
    patron = models.ForeignKey(
        'Patron',
        on_delete=models.CASCADE,
        db_column='patron_id',
        related_name='conversations',
    )
    topic = models.CharField(max_length=40, choices=TOPIC_CHOICES, default='Other')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Open')
    created_at = models.DateTimeField(default=timezone.now)
    last_message_at = models.DateTimeField(default=timezone.now)
    closed_at = models.DateTimeField(blank=True, null=True)
    closed_by = models.ForeignKey(
        'User',
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        db_column='closed_by',
        related_name='closed_conversations',
    )
    # When the patron last had the thread open.
    patron_last_seen_at = models.DateTimeField(blank=True, null=True)
    # Last reply email time.
    last_notified_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        db_table = 'Conversations'
        ordering = ['-last_message_at']

    def __str__(self):
        return f"{self.patron.fullname} — {self.topic} ({self.status})"

    @property
    def is_awaiting_reply(self):
        return self.status == 'Open'


class ChatMessage(models.Model):
    """A single message in an enquiry thread."""

    SENDER_CHOICES = [
        ('Patron', 'Patron'),
        ('Staff', 'Library staff'),
    ]

    message_id = models.AutoField(primary_key=True)
    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        db_column='conversation_id',
        related_name='messages',
    )
    sender_type = models.CharField(max_length=10, choices=SENDER_CHOICES)
    # Staff member who replied.
    staff = models.ForeignKey(
        'User',
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        db_column='staff_id',
        related_name='chat_replies',
    )
    body = models.TextField()
    sent_at = models.DateTimeField(default=timezone.now)
    read_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        db_table = 'Chat_Messages'
        ordering = ['sent_at', 'message_id']

    def __str__(self):
        return f"{self.sender_type}: {self.body[:40]}"


# Borrowing rules (admin-configurable)
class BorrowingRule(models.Model):
    """Library-wide borrowing policy."""

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


# System logs (admin audit trail)
class SystemLog(models.Model):
    """Activity log: who did what, in every portal."""

    ACTOR_ROLE_CHOICES = [
        ('Admin', 'Administrator'),
        ('Staff', 'Library Staff'),
        ('Patron', 'Patron'),
        ('System', 'System'),          # scheduled jobs: overdue sweeps, auto-closed visits
    ]

    log_id = models.AutoField(primary_key=True)
    admin = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        db_column='admin_id'
    )
    patron = models.ForeignKey(
        'Patron',
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        db_column='patron_id',
        related_name='activity_logs'
    )
    actor_role = models.CharField(max_length=20, choices=ACTOR_ROLE_CHOICES, default='Admin')
    # Actor name, kept under the original column name.
    admin_name = models.CharField(max_length=255, blank=True, null=True)
    action = models.CharField(max_length=50)          # Create, Update, Delete, Login, Search, ...
    entity_type = models.CharField(max_length=100)    # Book, Patron, Transaction, Donation, ...
    entity_id = models.CharField(max_length=100, blank=True, null=True)
    detail = models.CharField(max_length=500, blank=True, null=True)
    timestamp = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'System_Logs'
        ordering = ['-timestamp']
        indexes = [
            # The viewer's default is "newest first, filtered by role", and the table only grows.
            models.Index(fields=['-timestamp'], name='syslog_ts_desc_idx'),
            models.Index(fields=['actor_role', '-timestamp'], name='syslog_role_ts_idx'),
        ]

    @property
    def actor_name(self):
        return self.admin_name or 'Unknown'

    def __str__(self):
        return f"{self.action} {self.entity_type} by {self.actor_name} ({self.actor_role})"

# Password reset OTP
class PasswordResetOTP(models.Model):
    """A one-time code emailed for a forgot-password reset."""

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


# Inventory records
class InventoryRecord(models.Model):
    """One physical copy on the shelves."""

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

    # Accessioning stage for donations only.
    STAGE_CHOICES = [
        ('Received', 'Received'),
        ('Processing', 'Processing'),
        ('Shelved', 'Shelved'),
    ]

    STATUS_CHOICES = [
        ('In Stock', 'In Stock'),
        # Not on the shelf where it should be, and not explained by a loan.
        ('Missing', 'Missing'),
        ('Removed', 'Removed'),        # write-off or deaccession
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
    # Shipment fields, empty on a donation.
    supplier = models.CharField(max_length=255, blank=True, null=True)
    po_number = models.CharField(max_length=100, blank=True, null=True)
    # Donation fields, empty on a shipment.
    donor_name = models.CharField(max_length=255, blank=True, null=True)
    donated_date = models.DateField(blank=True, null=True)
    processing_stage = models.CharField(
        max_length=20, choices=STAGE_CHOICES, blank=True, null=True)
    # The accessioning row this copy is tracked by on the Donations page.
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
    # First date the copy went missing in a stock count.
    missing_since = models.DateField(blank=True, null=True)
    audit_misses = models.IntegerField(default=0)
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


# Stock movements
class StockMovement(models.Model):
    """Audit trail for every change to a copy's stock status."""

    ACTION_CHOICES = [
        ('Received', 'Received'),
        ('ConditionChange', 'Condition Change'),
        ('AuditAdjustment', 'Audit Adjustment'),
        ('Found', 'Found During Audit'),
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

# Login throttle
class LoginAttempt(models.Model):
    """One row per identity being guessed at, holding the run of failures."""
    MAX_FAILURES = 5
    LOCKOUT_MINUTES = 15
    # Failures older than this are not part of the current run.
    WINDOW_MINUTES = 30

    attempt_id = models.AutoField(primary_key=True)
    scope = models.CharField(max_length=20)          # admin | staff | patron | desk
    identifier = models.CharField(max_length=255)    # email, or account id for desk
    failures = models.PositiveIntegerField(default=0)
    first_failure_at = models.DateTimeField(null=True, blank=True)
    last_failure_at = models.DateTimeField(null=True, blank=True)
    locked_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'Login_Attempts'
        unique_together = ('scope', 'identifier')
        indexes = [models.Index(fields=['scope', 'identifier'])]

    def __str__(self):
        return f'{self.scope}:{self.identifier} ({self.failures})'

