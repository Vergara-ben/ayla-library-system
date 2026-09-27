# Fills in the stairway room and partner columns added in 0069.

import math

from django.db import migrations


def _inside(x, y, poly):
    hit = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i][0], poly[i][1]
        xj, yj = poly[j][0], poly[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
            hit = not hit
        j = i
    return hit


def _area(poly):
    return abs(sum(poly[i][0] * poly[i - 1][1] - poly[i - 1][0] * poly[i][1]
                   for i in range(len(poly)))) / 2.0


def _long_side_bearing(poly):
    best, bearing = -1.0, 0.0
    for i in range(len(poly)):
        dx = poly[i][0] - poly[i - 1][0]
        dy = poly[i][1] - poly[i - 1][1]
        length = math.hypot(dx, dy)
        if length > best:
            best, bearing = length, math.degrees(math.atan2(dx, -dy)) % 180
    return round(bearing, 2)


def fill_in(apps, schema_editor):
    """Give existing stairs their room, their partner, and a single straight run."""
    Stairway = apps.get_model('library', 'Stairway')
    Room = apps.get_model('library', 'Room')
    stairs = list(Stairway.objects.all())
    for st in stairs:
        fields = []
        if st.flights:
            st.flights = None
            fields.append('flights')
        if st.geometry and len(st.geometry) >= 3:
            st.bearing = _long_side_bearing(st.geometry)
            fields.append('bearing')
        # The smallest room its centre stands in.
        rooms = [r for r in Room.objects.filter(floor_plan_id=st.floor_plan_id)
                 if r.geometry and len(r.geometry) >= 3 and _inside(st.map_x, st.map_y, r.geometry)]
        if rooms and st.room_id is None:
            st.room_id = min(rooms, key=lambda r: _area(r.geometry)).room_id
            fields.append('room')
        if fields:
            st.save(update_fields=fields)

    # Stairs already linked to each other's floor become explicit pairs, nearest first.
    taken = set()
    for st in stairs:
        if st.stairway_id in taken or not st.connects_to_id:
            continue
        options = [o for o in stairs
                   if o.floor_plan_id == st.connects_to_id and o.connects_to_id == st.floor_plan_id
                   and o.stairway_id not in taken]
        if not options:
            continue
        other = min(options, key=lambda o: math.hypot(o.map_x - st.map_x, o.map_y - st.map_y))
        Stairway.objects.filter(stairway_id=st.stairway_id).update(partner_id=other.stairway_id)
        Stairway.objects.filter(stairway_id=other.stairway_id).update(partner_id=st.stairway_id)
        taken.update({st.stairway_id, other.stairway_id})


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0069_stairway_room_partner'),
    ]

    operations = [
        migrations.RunPython(fill_in, migrations.RunPython.noop),
    ]
