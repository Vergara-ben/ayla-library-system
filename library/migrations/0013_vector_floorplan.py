"""Replace the uploaded floor-plan image with a drawable vector canvas.

Floor plans no longer carry an image. Instead each plan owns a coordinate space
(canvas_width x canvas_height) and rooms become editable polygons.

The backfill preserves every existing coordinate:
  - canvas size is derived from the furthest-placed object on the plan, so
    nothing that was already positioned falls outside the new canvas;
  - each room without geometry gets a square centred on its old map_x/map_y,
    which the Administrator can then reshape.
"""
from django.db import migrations, models


# Half-width of the square generated for a room that only had a point.
ROOM_SEED_HALF = 60.0
CANVAS_PADDING = 120.0
DEFAULT_W, DEFAULT_H = 1000.0, 800.0


def seed_canvas_and_rooms(apps, schema_editor):
    FloorPlan = apps.get_model('library', 'FloorPlan')
    Room = apps.get_model('library', 'Room')
    Shelf = apps.get_model('library', 'Shelf')
    Waypoint = apps.get_model('library', 'Waypoint')
    BLEBeacon = apps.get_model('library', 'BLEBeacon')

    for plan in FloorPlan.objects.all():
        if not plan.name or plan.name == 'Floor Plan':
            plan.name = 'Floor Plan %s' % plan.floor_plan_id

        # Widest extent of anything already placed on this plan.
        max_x = max_y = 0.0
        rooms = list(Room.objects.filter(floor_plan=plan))
        points = [(r.map_x, r.map_y) for r in rooms]
        points += [
            (s.map_x, s.map_y)
            for s in Shelf.objects.filter(room__floor_plan=plan)
        ]
        points += [
            (w.map_x, w.map_y) for w in Waypoint.objects.filter(floor_plan=plan)
        ]
        points += [
            (b.map_x, b.map_y) for b in BLEBeacon.objects.filter(floor_plan=plan)
        ]
        for x, y in points:
            if x is not None:
                max_x = max(max_x, float(x))
            if y is not None:
                max_y = max(max_y, float(y))

        plan.canvas_width = max(DEFAULT_W, max_x + CANVAS_PADDING)
        plan.canvas_height = max(DEFAULT_H, max_y + CANVAS_PADDING)
        plan.save(update_fields=['name', 'canvas_width', 'canvas_height'])

        for room in rooms:
            if room.geometry:
                continue
            cx, cy = float(room.map_x or 0), float(room.map_y or 0)
            h = ROOM_SEED_HALF
            room.geometry = [
                [cx - h, cy - h],
                [cx + h, cy - h],
                [cx + h, cy + h],
                [cx - h, cy + h],
            ]
            room.save(update_fields=['geometry'])


def drop_geometry(apps, schema_editor):
    Room = apps.get_model('library', 'Room')
    Room.objects.update(geometry=None)


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0012_backfill_patron_qr'),
    ]

    operations = [
        migrations.AddField(
            model_name='floorplan',
            name='name',
            field=models.CharField(default='Floor Plan', max_length=255),
        ),
        migrations.AddField(
            model_name='floorplan',
            name='canvas_width',
            field=models.FloatField(default=1000),
        ),
        migrations.AddField(
            model_name='floorplan',
            name='canvas_height',
            field=models.FloatField(default=800),
        ),
        migrations.AddField(
            model_name='room',
            name='geometry',
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.RunPython(seed_canvas_and_rooms, drop_geometry),
        migrations.RemoveField(
            model_name='floorplan',
            name='image_url',
        ),
    ]
