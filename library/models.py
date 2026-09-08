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
    # How far the plan's "up" sits from magnetic north, in degrees clockwise.
    # The compass reports bearings from north; the floor plan is drawn in
    # whatever orientation the Administrator happened to draw it. Without this
    # the two cannot be reconciled, and dead reckoning walks the marker off in
    # a consistently wrong direction -- which looks far worse than not moving
    # it at all. Measured once per plan from the Position Test page.
    north_offset_deg = models.FloatField(default=0)
    # Which storey this is. Used for ordering and for labelling the patron's
    # floor switcher; the ground floor is 1.
    floor_number = models.IntegerField(default=1)
    # Whether this floor is in service.
    #
    # This used to mean "the one floor the whole system is looking at", and
    # activating a second plan deactivated the first. That quietly broke the
    # catalogue: ActiveLocationManager treats a book as locatable only if its
    # floor plan is active, so switching to a newly drawn upper floor made
    # every book on the ground floor vanish from search.
    #
    # It now means what it says -- any number of floors may be in service at
    # once, and a book is locatable if the floor it sits on is. Which map to
    # *draw* is a separate question, answered by the floor being viewed or by
    # the floor the target book is on.
    is_active = models.BooleanField(default=True)
    renovation_notice = models.CharField(max_length=255, blank=True, null=True)
    renovation_message = models.TextField(blank=True, null=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'Floor_Plans'
        ordering = ['floor_number', 'floor_plan_id']

    @property
    def floor_label(self):
        """How this storey is named to a patron.

        Ordinals only make sense above ground: -1 came out as "-1th floor",
        because Python's -1 % 10 is 9. Below ground and at ground level get
        their own wording instead of a suffix that does not apply.
        """
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
    # Calibration. tx_power is the RSSI measured one metre from this beacon, and
    # path_loss_n the environment exponent: 2.5 by default, which suits a room
    # with shelving in it. 2.0 is free space -- the one value that is never true
    # in a library -- and 3.5 is a heavily obstructed aisle. Too low reads every
    # distance long, too high reads them short.
    #
    # Both are per-beacon because they genuinely differ per unit and per aisle;
    # null falls back to the client's defaults.
    tx_power = models.IntegerField(blank=True, null=True)
    path_loss_n = models.FloatField(blank=True, null=True)
    map_x = models.FloatField()
    map_y = models.FloatField()
    # Mounting height above the floor, in METRES -- deliberately not canvas
    # units. It is a physical measurement of the building that has nothing to do
    # with how the plan happens to be drawn, so it must survive the plan being
    # rescaled or redrawn.
    #
    # RSSI ranging yields the straight-line distance through the air, but
    # trilateration solves in the floor plane. A beacon mounted 2.4 m up reads
    # as 1.2 m away from someone standing directly beneath it, and that error
    # is largest exactly where the patron is closest to a beacon. Recording the
    # height is what lets the vertical leg be taken back out.
    #
    # Null means "not measured", and applies no correction -- the same
    # behaviour as before this field existed.
    height = models.FloatField(blank=True, null=True)
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
    # The room on the far side, when this doorway joins two of them.
    #
    # A door is one hole in one wall, and that wall usually has a room on each
    # side. Recording only one of them meant a shared doorway had to be drawn
    # twice -- two symbols on one wall, free to drift apart, with nothing
    # saying they were the same opening -- and left the system unable to
    # answer "which rooms connect to which", which is the question navigation
    # is really made of.
    #
    # Nullable because plenty of doors are not shared: a door to a corridor
    # that was never drawn as a room, or to the outside, has no far side.
    # SET_NULL rather than CASCADE, because deleting the far room should
    # demote this to an exterior door, not destroy the doorway itself.
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


# ─── 3c. OBSTACLES / FURNITURE ────────────────────────────────
class Obstacle(models.Model):
    """Anything drawn on the floor that is neither a room nor a shelf.

    Reading tables, the issue counter, structural pillars, planters, a
    partition — the things a patron has to walk around. They were missing
    entirely, which made the map read as a set of empty rooms with shelves
    floating in them, and left a patron following a route with no idea that a
    row of tables sits between them and the aisle.

    Shaped like Room deliberately: a polygon in canvas coordinates with the
    centroid kept as the label anchor, so everything that already knows how to
    draw a room can draw one of these with no new geometry code.

    `kind` exists so the map can style a pillar differently from a table
    without anyone having to name every single object.
    """

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
    # Whether patrons see it. A partition that comes down for an event can be
    # switched off without deleting the shape and redrawing it next time.
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


# ─── 3d. STAIRWAYS / LIFTS ────────────────────────────────────
class Stairway(models.Model):
    """A way between floors: a staircase, a lift, or a ramp.

    Not an obstacle with a label on it, for two reasons.

    A stair is drawn differently. On any real floor plan it is a footprint with
    tread lines across it and an arrow showing which way is up -- a plain
    rectangle says "something is here" where the convention says "these are
    steps, and they rise that way". `bearing` is what lets the map draw the
    treads perpendicular to the direction of travel instead of guessing.

    And a stair is the only object on the plan that means something on a
    *different* floor. `connects_to` is the point of this model: until now
    Waypoint had no notion of a stair, so a route stopped dead at the floor
    boundary and the patron was told the book was upstairs without being told
    how to get there. With the link recorded, the map can walk them to the foot
    of the right staircase and hand over at the landing.
    """

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

    # Degrees clockwise from canvas "up", along the direction of travel. Treads
    # are drawn across this, so a stair running north-south gets horizontal
    # steps rather than a rectangle full of guesswork.
    bearing = models.FloatField(default=0)
    direction = models.CharField(max_length=8, choices=DIRECTION_CHOICES, default='both')

    # The floor at the other end. Nullable because a plan is often drawn before
    # the floor above it exists, and a stair with nowhere to go is still worth
    # showing -- it is a real thing in the room.
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


# ─── 4. SHELVES ───────────────────────────────────────────────
class Shelf(models.Model):
    # What the thing physically is. All of these hold books, which is what
    # makes them a Shelf rather than an Obstacle: a book points at a ShelfLevel,
    # so anything a book can sit on has to be one of these. An Obstacle is
    # furniture that a route has to go around and nothing lives on.
    KIND_CHOICES = [
        ('Shelf', 'Shelf / bookcase'),
        ('Table', 'Table'),
        ('Display', 'Display stand'),
        ('Cart', 'Trolley / cart'),
        ('Ledge', 'Window ledge'),
    ]

    # Where the unit is fixed. A wall or ceiling unit occupies plan area but
    # not floor area -- you walk underneath it -- so it is drawn differently and
    # must not be treated as something a route has to go around.
    MOUNT_CHOICES = [
        ('Floor', 'Stands on the floor'),
        ('Wall', 'Fixed to the wall'),
        ('Ceiling', 'Hung from the ceiling'),
    ]

    shelf_id = models.AutoField(primary_key=True)
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, default='Shelf')
    mount = models.CharField(max_length=10, choices=MOUNT_CHOICES, default='Floor')
    # Height of the lowest shelf above the floor, in metres. Only meaningful
    # for a unit that is off the ground; null means nobody measured it.
    mount_height_m = models.FloatField(blank=True, null=True)
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
    # A traced outline, for the shelves that are not rectangles -- the ones
    # tucked into a corner at an angle, and the run that turns. Null keeps the
    # width/depth/rotation rectangle above, which is what every shelf drawn
    # before this field existed still uses, so none of them move.
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
        """The outline to draw, as [[x, y], ...] in canvas units.

        One answer for both shapes, computed once here rather than in each of
        the four maps that draw a shelf: the traced polygon when there is one,
        otherwise the corners of the rotated rectangle.
        """
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
    # Laid down by "Generate walkable route" rather than placed by hand.
    #
    # The distinction is what makes generation safe to re-run: pressing the
    # button again replaces only what the button made. Without it the second
    # press would quietly destroy every correction the Administrator had made
    # since the first -- which is the fastest way to make a helpful feature
    # one nobody dares touch.
    is_generated = models.BooleanField(default=False)

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
    # The flat top of the unit, above the highest shelf. Not a numbered level:
    # in a library this full it is a real place books end up, and calling it
    # "Level 5" would send someone looking inside the case for something
    # sitting on top of it.
    is_top = models.BooleanField(default=False)
    # The space underneath, which in a library this full is a real place books
    # are kept -- and, like the top, is not a numbered shelf inside the case.
    is_under = models.BooleanField(default=False)
    # Bays across the run, for units divided both ways. Null means the level is
    # not subdivided, which is every shelf that existed before this field.
    column_number = models.IntegerField(blank=True, null=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'Shelf_Levels'

    @property
    def label(self):
        base = ('Top' if self.is_top
                else 'Underneath' if self.is_under
                else f'Level {self.level_number}')
        if self.column_number:
            return f'{base}, Column {self.column_number}'
        return base

    @property
    def short_label(self):
        """For a QR label, where the width is measured in millimetres."""
        base = ('Top' if self.is_top
                else 'Under' if self.is_under
                else f'L{self.level_number}')
        return f'{base}C{self.column_number}' if self.column_number else base

    def __str__(self):
        return f"{self.label} - {self.category}"


# ─── 9. BOOKS ─────────────────────────────────────────────────
class Book(models.Model):

    STATUS_CHOICES = [
        ('Available', 'Available'),
        ('Borrowed', 'Borrowed'),
        ('Being Read', 'Being Read'),
        # Returned to the desk but not yet put back. The copy is in the
        # building and not with a patron, but it is not at its shelf either --
        # so sending someone to that shelf would waste their trip. Cleared by
        # staff from the reshelving queue once it is physically back.
        ('For Reshelving', 'For reshelving'),
        ('Overdue', 'Overdue'),
        ('Lost', 'Lost'),
        ('Donated', 'Donated'),
    ]

    # What kind of material this is, as distinct from what it is about.
    # `genre` already holds the subject (Fiction, Mathematics, Adventure), which
    # is a different question from whether the thing in your hand is a book, a
    # magazine or a bound journal -- and they are shelved and lent differently.
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
    # Where along the level this copy sits: 1 is the leftmost book, counting
    # from the end you reach first walking up to the shelf.
    #
    # The shelf level says which board it is on; this says where along it. A
    # patron standing in front of a full bay still has to read every spine
    # without it, which is the difference between a catalogue that says where
    # a book lives and one that actually locates it.
    #
    # Nullable, and deliberately not enforced unique: shelving drifts as books
    # are borrowed and put back, so a slot is the best record of where a book
    # was last seen rather than a guarantee of where it is now.
    shelf_slot = models.PositiveIntegerField(blank=True, null=True)
    # How worn the copy is. Three steps rather than five: with New/Fair/Poor
    # in the list, two people looking at the same book pick different words,
    # and a scale nobody applies consistently is worse than a coarse one.
    CONDITION_CHOICES = [
        ('Good', 'Good'),
        ('Worn', 'Worn'),
        ('Damaged', 'Damaged'),
    ]
    condition = models.CharField(max_length=10, choices=CONDITION_CHOICES,
                                 default='Good')

    # The call number written on the spine, e.g. "FIC A31p 1963": the class,
    # the author's Cutter mark, the first letter of the title, and the year.
    #
    # Derived rather than typed, because every part of it already exists on the
    # record -- asking a cataloguer to retype what the genre, author, title and
    # year already say is how the two drift apart. Stored rather than computed
    # on the fly because it goes on a printed label: once a spine is labelled,
    # the number must not change underneath it because somebody corrected a
    # genre. Blank means "work it out"; anything typed here is kept.
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

    # Second and third letters of the surname, as digits. A real Cutter table
    # is a printed book of them; this is a house scheme that is deterministic,
    # spreads names evenly, and can be overridden per book by anybody who wants
    # the published number instead.
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
        """'Shelf A Column 1 Level 2', or 'A C1 L2' where space is tight.

        Shelf, then across, then up -- the order somebody walks it: find the
        bay, find the bay's section, then look up to the board. The short form
        is the same three facts for a narrow column or a printed label.

        A top or an underside is named rather than numbered, because "L5" sends
        somebody to the fifth board of a case whose books are on its lid.
        """
        level = self.shelf_level
        if level is None:
            return ''
        shelf = level.shelf
        name = (getattr(shelf, 'name', '') or '').strip()
        if short and name:
            # "Shelf A" is written "A": the word is the same on every bay and
            # carries nothing once the column is only a few characters wide.
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
        # Filled here rather than at each of the four places a book can be
        # created, so an imported book and a hand-entered one are numbered the
        # same way. Never recomputed: a spine already labelled keeps its number.
        if not (self.call_number or '').strip():
            self.call_number = self.derive_call_number()
        super().save(*args, **kwargs)

    objects = models.Manager()
    active_locations = ActiveLocationManager()

    class Meta:
        db_table = 'Books'
        indexes = [
            # Catalogue and inventory pages group and filter on status; the
            # reshelving queue lives entirely on it.
            models.Index(fields=['status']),
            models.Index(fields=['title']),
        ]

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
    # Modules this account may open, as comma-separated keys from
    # library/modules.py. Applies to both roles.
    modules = models.TextField(blank=True, default='')

    class Meta:
        db_table = 'Users'

    def __str__(self):
        return self.fullname

    @property
    def initials(self):
        """One or two letters for the header's profile button.

        First and last word of the name, so "Ben Vergara" gives BV and a
        single-word name gives one letter rather than a doubled one. Computed
        here rather than in the template because Django's template language
        cannot index a split list, and the workaround for it was unreadable.
        """
        words = (self.fullname or '').split()
        if not words:
            return '?'
        if len(words) == 1:
            return words[0][:1].upper()
        return (words[0][:1] + words[-1][:1]).upper()

    @property
    def module_keys(self):
        """The operational modules this account may open.

        An Administrator is not given every module implicitly. The role exists
        to govern the system -- accounts, configuration, oversight -- and those
        views are gated by role rather than by module, so they stay available
        whatever is granted here. What this controls is the day-to-day desk
        work: circulation, logs, cataloguing, donations, patron records and
        messages.

        Granting none by default keeps an Administrator's daily surface to
        administration instead of every screen in the system at once, which is
        the whole point of having two roles. It is a grant rather than a
        removal because a library with one computer and a small team still
        needs someone able to work the desk when staffing requires it.
        """
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

    # Wrong OTP guesses allowed before the code is burnt. Matches
    # PasswordResetOTP.MAX_ATTEMPTS so both halves of the system agree.
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

    @property
    def credential_filename(self):
        """Just the file name -- the credential route serves from a fixed folder."""
        if not self.credential_document:
            return ''
        return self.credential_document.replace('\\', '/').rsplit('/', 1)[-1]

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
    # Email OTP, shared by online registration and the self-service account
    # actions (deactivate, reactivate, change password). Only one code is ever
    # outstanding, so `otp_purpose` records which action it was issued for --
    # without it a code emailed to confirm a password change would also be
    # spendable on the deactivate endpoint, since both check the same field.
    otp_code = models.CharField(max_length=6, blank=True, null=True)
    otp_purpose = models.CharField(max_length=20, blank=True, default='')
    otp_expires_at = models.DateTimeField(blank=True, null=True)
    # Wrong guesses against the current code. Without a ceiling a 6-digit code
    # is only a million cheap guesses from being walked through inside its own
    # 10-minute window, so the count is kept here and the code is burnt when it
    # runs out. Reset every time a fresh code is issued.
    otp_attempts = models.PositiveIntegerField(default=0)
    # When the last code was emailed, so a cooldown can refuse to send another
    # straight away -- otherwise "send code" is a button that mails someone
    # else's inbox as fast as it can be clicked.
    otp_last_sent_at = models.DateTimeField(blank=True, null=True)
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
        indexes = [
            # Every report filters a date range on this.
            models.Index(fields=['transaction_date']),
            # "What is currently out" -- the pending count on ten pages, the
            # unreturned report, and the borrowing-limit check on every loan.
            models.Index(fields=['transaction_type', 'return_date']),
            # One patron's history, on their account page and the desk lookup.
            models.Index(fields=['patron', '-transaction_date']),
        ]

    def __str__(self):
        return f"{self.transaction_type} - {self.book}"


class DueDateExtension(models.Model):
    """One ask to push a loan's due date out, and how it was settled.

    Covers two different business events with one record and one history,
    rather than two disconnected mechanisms: a patron asking through their
    account for staff to approve or decline, and staff changing a due date
    directly when a patron asks in person. The second case is stored as an
    already-approved, staff-initiated row — staff are their own approver at
    the desk — so both leave the same kind of trail on the loan.
    """

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
    """One patron's ask to reactivate a self-deactivated account.

    Deactivation (Figure 20) processes immediately once its own OTP is
    verified, so it needs no record of its own beyond the status change
    itself. Reactivation (Figure 19) is different: OTP verification only
    forwards the request to an Admin, who approves or rejects it, so that
    step needs somewhere to live in the meantime — this is that record,
    mirroring how DueDateExtension holds a request separately from the
    Transaction it applies to rather than as flags on it.
    """

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
        indexes = [
            # Visit reports and the peak-hour analytics scan this by date.
            models.Index(fields=['entry_time']),
            # "Who is still inside" -- the occupancy count and the stale-visit
            # sweep, both of which run on ordinary page loads.
            models.Index(fields=['exit_time']),
        ]

    def __str__(self):
        return f"{self.patron} — entry {self.entry_time}"


class StockAudit(models.Model):
    """One stock-take of one shelf.

    A library counts its shelves so it can answer "when was this section last
    checked, by whom, and what did we find" — questions a pile of adjustments
    cannot answer on its own. Each run is kept with its counts so the shelf has
    a history and the discrepancy rate can be watched over time.
    """

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
    """One patron's enquiry thread with the library — "Ask a Librarian".

    Deliberately a help desk rather than instant messaging. Ayla has one
    computer and one librarian, who cannot sit in a live chat while also
    working the desk; a patron asks a question, gets on with their day, and is
    emailed when someone answers. `status` is what makes that workable: it is
    the queue of questions still waiting for a reply, which is the only thing
    stopping an enquiry from being quietly lost.
    """

    STATUS_CHOICES = [
        ('Open', 'Waiting for a reply'),
        ('Answered', 'Answered'),
        ('Closed', 'Closed'),
    ]

    # Asked at the start so the librarian can see what a thread is about before
    # opening it, and so enquiries can be counted by kind rather than guessed at.
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
    # When the patron last had the thread open. A reply sent while they are
    # reading it does not need an email, and this is how that is known.
    patron_last_seen_at = models.DateTimeField(blank=True, null=True)
    # When they were last emailed about a reply, so a librarian typing three
    # short answers in a row does not send three emails.
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
    # Who answered, for the same reason every other action here records an
    # actor: a patron told the wrong thing should be traceable to whoever said it.
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
    """Activity log: who did what, in every portal.

    This is the manuscript's Activity Logs entity (ERD, Figure 87), which
    relates it to Patrons *and* Staff. It began as an admin-only audit trail,
    so the actor was a single FK to User; patrons live in their own table and
    were therefore invisible to it. Both actor references are now kept, with
    ``actor_role`` saying which one applies.

    Both FKs are SET_NULL and ``actor_name`` holds a snapshot of the name, so
    a deleted account leaves its history readable rather than a row of blanks
    -- an audit trail that disappears with the account it indicts is not one.
    """

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
    # Kept under its original column name so the 58 existing call sites and
    # every row already recorded stay valid; it is the actor's name whichever
    # table the actor came from.
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
            # The viewer's default is "newest first, filtered by role", and the
            # table only grows.
            models.Index(fields=['-timestamp'], name='syslog_ts_desc_idx'),
            models.Index(fields=['actor_role', '-timestamp'], name='syslog_role_ts_idx'),
        ]

    @property
    def actor_name(self):
        return self.admin_name or 'Unknown'

    def __str__(self):
        return f"{self.action} {self.entity_type} by {self.actor_name} ({self.actor_role})"

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
        # Not on the shelf where it should be, and not explained by a loan.
        # A stock-take cannot tell "gone" from "mis-shelved, on a trolley, or
        # in someone's hands two aisles away", so it says so and waits. Only a
        # deliberate write-off later turns this into a loss.
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
    # Set the first time a stock-take fails to find the copy, and cleared the
    # moment it turns up again. `audit_misses` counts consecutive stock-takes
    # that could not find it — one miss is a mislaid book, four is a loss.
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

# ─── 14. LOGIN THROTTLE ───────────────────────────────────────
class LoginAttempt(models.Model):
    """One row per identity being guessed at, holding the run of failures.

    Every sign-in door in the system -- Administrator, Library Staff, patron, and
    the desk-mode unlock -- accepted unlimited password guesses. The desk unlock
    was the worst of them: desk mode exists to be handed to a member of the
    public, and the way back out is a staff member's real account password.

    Keyed on scope + identifier rather than on a foreign key, so an address that
    belongs to no account is throttled exactly like one that does. If misses
    against unknown users were free, the throttle would itself answer the
    question of which addresses exist.
    """
    MAX_FAILURES = 5
    LOCKOUT_MINUTES = 15
    # Failures older than this are not part of the current run. Without it a
    # staff member who fumbled three times last March would start today two
    # strikes down.
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

