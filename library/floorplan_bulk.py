"""Group actions for the floor plan editor: move, rotate, delete, lock, unlock and undo."""

import json
import math
from collections import Counter

from django.db import models, router, transaction
from django.db.models import Q
from django.db.models.deletion import Collector
from django.http import JsonResponse

from .audit import log_admin_action
from .auth_utils import admin_only_required
from .models import (BLEBeacon, Door, FloorPlan, Obstacle, Room, Shelf, Stairway,
                     Waypoint, WaypointConnection)

KINDS = {
    'room': Room, 'door': Door, 'shelf': Shelf, 'obstacle': Obstacle,
    'stairs': Stairway, 'beacon': BLEBeacon, 'waypoint': Waypoint,
}

# Fields a move changes, per kind.
MOVE_FIELDS = {
    'room': ['geometry', 'map_x', 'map_y'],
    'door': ['map_x', 'map_y', 'rotation'],
    'shelf': ['geometry', 'map_x', 'map_y'],
    'obstacle': ['geometry', 'map_x', 'map_y'],
    'stairs': ['geometry', 'map_x', 'map_y', 'flights'],
    'beacon': ['map_x', 'map_y'],
    'waypoint': ['map_x', 'map_y'],
}

# Fields a rotation changes, per kind.
ROTATE_FIELDS = {
    'room': ['geometry', 'map_x', 'map_y'],
    'door': ['map_x', 'map_y', 'rotation'],
    'shelf': ['geometry', 'map_x', 'map_y', 'rotation'],
    'obstacle': ['geometry', 'map_x', 'map_y'],
    'stairs': ['geometry', 'map_x', 'map_y', 'bearing', 'flights'],
    'beacon': ['map_x', 'map_y'],
    'waypoint': ['map_x', 'map_y'],
}

# Wording for the delete summary.
PLURALS = {
    'room': 'rooms', 'door': 'doors', 'shelf': 'shelves', 'shelflevel': 'shelf levels',
    'obstacle': 'furniture', 'stairway': 'stairways', 'blebeacon': 'beacons',
    'waypoint': 'waypoints', 'waypointconnection': 'waypoint links',
}
UNLINKS = {
    'book.shelf_level': 'books will become unshelved',
    'door.room_b': 'doors will lose the room on their far side',
    'waypoint.linked_shelf': 'waypoints will lose their linked shelf',
    'stockaudit.shelf': 'stock counts will lose their shelf',
}

MAX_ITEMS = 2000
MAX_SHIFT = 10000


def _fail(message, **extra):
    return JsonResponse(dict({'success': False, 'error': message}, **extra))


def _plan_id_of(kind, obj):
    if kind == 'door':
        return obj.room.floor_plan_id
    if kind == 'shelf':
        return obj.room.floor_plan_id if obj.room_id else None
    return obj.floor_plan_id


def _load_selection(plan, raw):
    """The posted [{kind, id}, ...] as model instances on this plan."""
    try:
        items = json.loads(raw or '[]')
    except (TypeError, ValueError):
        return None, 'The selection could not be read.'
    if not isinstance(items, list) or not items:
        return None, 'Select something first.'
    if len(items) > MAX_ITEMS:
        return None, 'That selection is too large to change at once.'

    wanted = {}
    for item in items:
        kind = item.get('kind') if isinstance(item, dict) else None
        if kind not in KINDS:
            return None, 'The selection could not be read.'
        try:
            wanted.setdefault(kind, set()).add(int(item.get('id')))
        except (TypeError, ValueError):
            return None, 'The selection could not be read.'

    found = {}
    for kind, pks in wanted.items():
        qs = KINDS[kind].objects.filter(pk__in=pks)
        if kind in ('door', 'shelf'):
            qs = qs.select_related('room')
        objs = list(qs)
        if len(objs) != len(pks) or any(_plan_id_of(kind, o) != plan.floor_plan_id for o in objs):
            return None, 'Part of the selection is no longer on this floor. Reload the page.'
        found[kind] = objs
    return found, None


def _label(verb, n):
    return '%s %d element%s' % (verb, n, '' if n == 1 else 's')


def _move(found, dx, dy):
    """Shift every selected element."""
    from .views import _snap_to_polygon_edge, _stair_flights, _stair_shape_of, _translate_geometry

    room_ids = {r.room_id for r in found.get('room', [])}
    selected_doors = {d.door_id for d in found.get('door', [])}
    # Doors sit on a wall, so they ride with their room.
    carried = list(Door.objects.filter(room_id__in=room_ids).exclude(door_id__in=selected_doors))
    waypoint_ids = [w.waypoint_id for w in found.get('waypoint', [])]
    links = list(WaypointConnection.objects.filter(
        Q(waypoint_from_id__in=waypoint_ids) | Q(waypoint_to_id__in=waypoint_ids)))

    def shift(obj, geometry=True):
        if geometry and obj.geometry and len(obj.geometry) >= 3:
            obj.geometry = _translate_geometry(obj.geometry, dx, dy)
        obj.map_x = round(obj.map_x + dx, 2)
        obj.map_y = round(obj.map_y + dy, 2)

    for room in found.get('room', []):
        shift(room)
        room.save(update_fields=MOVE_FIELDS['room'])
    for door in carried:
        shift(door, geometry=False)
        door.save(update_fields=['map_x', 'map_y'])
    for door in found.get('door', []):
        outline = door.room.geometry or []
        if door.room_id in room_ids or len(outline) < 3:
            shift(door, geometry=False)
        else:
            # A door moved on its own slides back onto its wall.
            door.map_x, door.map_y, door.rotation = _snap_to_polygon_edge(
                outline, door.map_x + dx, door.map_y + dy)
        door.save(update_fields=MOVE_FIELDS['door'])
    for shelf in found.get('shelf', []):
        if shelf.map_x is None or shelf.map_y is None:
            continue
        shift(shelf)
        shelf.save(update_fields=MOVE_FIELDS['shelf'])
    for obstacle in found.get('obstacle', []):
        shift(obstacle)
        obstacle.save(update_fields=MOVE_FIELDS['obstacle'])
    for stair in found.get('stairs', []):
        shape = _stair_shape_of(stair)
        shift(stair)
        stair.flights = _stair_flights(stair.geometry, stair.bearing, shape) or None
        stair.save(update_fields=MOVE_FIELDS['stairs'])
    for thing in found.get('beacon', []) + found.get('waypoint', []):
        shift(thing, geometry=False)
        thing.save(update_fields=['map_x', 'map_y'])

    for link in WaypointConnection.objects.filter(
            pk__in=[l.pk for l in links]).select_related('waypoint_from', 'waypoint_to'):
        a, b = link.waypoint_from, link.waypoint_to
        link.distance = math.hypot(a.map_x - b.map_x, a.map_y - b.map_y)
        link.save(update_fields=['distance'])


def _points_of(kind, obj):
    if getattr(obj, 'geometry', None) and len(obj.geometry) >= 3:
        return [(float(p[0]), float(p[1])) for p in obj.geometry]
    if obj.map_x is None or obj.map_y is None:
        return []
    return [(obj.map_x, obj.map_y)]


def _rotate(found, degrees):
    """Turn the selection about its centre. A room takes its doors and contents with it."""
    from .routegen import point_in_polygon
    from .views import _infer_stair_bearing, _snap_to_polygon_edge

    everything = [(k, o) for k, objs in found.items() for o in objs]
    pts = [p for k, o in everything for p in _points_of(k, o)]
    if not pts:
        return []
    cx = (min(p[0] for p in pts) + max(p[0] for p in pts)) / 2.0
    cy = (min(p[1] for p in pts) + max(p[1] for p in pts)) / 2.0
    rad = math.radians(degrees)
    cos, sin = math.cos(rad), math.sin(rad)

    def turn(x, y):
        dx, dy = x - cx, y - cy
        return round(cx + dx * cos - dy * sin, 2), round(cy + dx * sin + dy * cos, 2)

    # What stands in a turned room turns with it.
    rooms = found.get('room', [])
    chosen = {k: {o.pk for o in objs} for k, objs in found.items()}
    carried = {}
    if rooms:
        room_ids = [r.room_id for r in rooms]
        polys = [r.geometry for r in rooms if r.geometry and len(r.geometry) >= 3]
        plan_id = rooms[0].floor_plan_id

        def inside(o):
            return (o.map_x is not None and o.map_y is not None
                    and any(point_in_polygon(o.map_x, o.map_y, poly) for poly in polys))

        carried['door'] = list(Door.objects.filter(room_id__in=room_ids))
        carried['shelf'] = list(Shelf.objects.filter(room_id__in=room_ids, map_x__isnull=False))
        for kind in ('obstacle', 'stairs', 'beacon', 'waypoint'):
            carried[kind] = [o for o in KINDS[kind].objects.filter(floor_plan_id=plan_id) if inside(o)]
        for kind in list(carried):
            carried[kind] = [o for o in carried[kind] if o.pk not in chosen.get(kind, set())]

    groups = {}
    for kind, objs in list(found.items()) + list(carried.items()):
        groups.setdefault(kind, []).extend(objs)

    waypoint_ids = [w.waypoint_id for w in groups.get('waypoint', [])]
    links = list(WaypointConnection.objects.filter(
        Q(waypoint_from_id__in=waypoint_ids) | Q(waypoint_to_id__in=waypoint_ids)))

    turned_rooms = {r.room_id for r in rooms}
    for kind, objs in groups.items():
        for obj in objs:
            if getattr(obj, 'geometry', None) and len(obj.geometry) >= 3:
                obj.geometry = [list(turn(p[0], p[1])) for p in obj.geometry]
            if obj.map_x is not None and obj.map_y is not None:
                obj.map_x, obj.map_y = turn(obj.map_x, obj.map_y)
            if kind in ('shelf', 'door'):
                obj.rotation = round(((obj.rotation or 0) + degrees) % 360, 2)
            if kind == 'door' and obj.room_id not in turned_rooms:
                # A door turned on its own stays on its wall.
                outline = obj.room.geometry or []
                if len(outline) >= 3:
                    obj.map_x, obj.map_y, obj.rotation = _snap_to_polygon_edge(
                        outline, obj.map_x, obj.map_y)
            if kind == 'stairs':
                obj.bearing = _infer_stair_bearing(obj.geometry)
                obj.flights = None
            obj.save(update_fields=ROTATE_FIELDS[kind])

    for link in WaypointConnection.objects.filter(
            pk__in=[l.pk for l in links]).select_related('waypoint_from', 'waypoint_to'):
        a, b = link.waypoint_from, link.waypoint_to
        link.distance = math.hypot(a.map_x - b.map_x, a.map_y - b.map_y)
        link.save(update_fields=['distance'])


def _full_rows(model, rows):
    """Reload rows completely; the delete collector may have fetched only some fields."""
    return list(model.objects.filter(pk__in=[r.pk for r in rows]))


def _plan_delete(found):
    """What deleting the selection removes and unlinks, without deleting anything."""
    collector = Collector(using=router.db_for_write(Room))
    for objs in found.values():
        collector.collect(objs)
    collector.sort()

    # Deletion order is children first.
    data_groups = [(model, _full_rows(model, rows)) for model, rows in collector.data.items() if rows]
    fast_groups = []
    for qs in collector.fast_deletes:
        rows = list(qs.all()) if hasattr(qs, 'all') else list(qs)
        if rows:
            fast_groups.append((type(rows[0]), _full_rows(type(rows[0]), rows)))

    unlinks = []
    for (field, value), instances_list in collector.field_updates.items():
        for instances in instances_list:
            rows = instances.all() if isinstance(instances, models.QuerySet) else instances
            for row in rows:
                unlinks.append([row._meta.label, row.pk, field.attname,
                                getattr(row, field.attname), field.name])
    return collector, data_groups, fast_groups, unlinks


def _summary(groups, unlinks):
    removed = Counter()
    for model, rows in groups:
        removed[model._meta.model_name] += len(rows)
    nulled = Counter('%s.%s' % (u[0].split('.')[-1].lower(), u[4]) for u in unlinks)
    return {
        'removes': [{'what': PLURALS.get(k, k), 'count': c} for k, c in removed.items() if c],
        'unlinks': [{'what': UNLINKS[k], 'count': c} for k, c in nulled.items() if k in UNLINKS],
    }


@admin_only_required
def floorplan_bulk(request):
    """One endpoint for every group action in the floor plan editor."""
    if request.method != 'POST':
        return _fail('Only POST method allowed')
    raw_plan = (request.POST.get('floor_plan_id') or '').strip()
    plan = FloorPlan.objects.filter(floor_plan_id=int(raw_plan)).first() if raw_plan.isdigit() else None
    if plan is None:
        return _fail('Floor plan not found')

    action = (request.POST.get('action') or '').strip()
    if action == 'undo':
        # The editor's history covers group actions too.
        from .floorplan_undo import undo_latest
        return undo_latest(request, plan)
    if action not in ('move', 'rotate', 'preview_delete', 'delete', 'lock', 'unlock'):
        return _fail('Unknown action')

    found, error = _load_selection(plan, request.POST.get('items'))
    if error:
        return _fail(error)
    count = sum(len(objs) for objs in found.values())

    if action in ('lock', 'unlock'):
        want = action == 'lock'
        with transaction.atomic():
            for objs in found.values():
                for obj in objs:
                    if obj.locked != want:
                        obj.locked = want
                        obj.save(update_fields=['locked'])
            label = _label('Locked' if want else 'Unlocked', count)
        log_admin_action(request, 'Update', 'FloorPlan', plan.floor_plan_id, f'{label} on {plan.name}')
        return JsonResponse({'success': True, 'message': label + '.', 'undo': label})

    locked = sum(1 for objs in found.values() for obj in objs if obj.locked)
    if locked:
        return _fail('%d of the selected elements %s locked. Unlock %s first, or leave %s out.'
                     % (locked, 'is' if locked == 1 else 'are',
                        'it' if locked == 1 else 'them', 'it' if locked == 1 else 'them'),
                     locked=True)

    if action == 'move':
        try:
            dx = float(request.POST.get('dx'))
            dy = float(request.POST.get('dy'))
        except (TypeError, ValueError):
            return _fail('dx and dy must be numbers')
        if not (math.isfinite(dx) and math.isfinite(dy)) or abs(dx) > MAX_SHIFT or abs(dy) > MAX_SHIFT:
            return _fail('That move is too far.')
        if abs(dx) < 1e-9 and abs(dy) < 1e-9:
            return JsonResponse({'success': True, 'message': 'Nothing moved.', 'undo': None})
        with transaction.atomic():
            _move(found, dx, dy)
            label = _label('Moved', count)
        log_admin_action(request, 'Update', 'FloorPlan', plan.floor_plan_id,
                         f'{label} by ({dx:.0f}, {dy:.0f}) on {plan.name}')
        return JsonResponse({'success': True, 'message': label + '.', 'undo': label})

    if action == 'rotate':
        try:
            degrees = float(request.POST.get('degrees'))
        except (TypeError, ValueError):
            return _fail('degrees must be a number')
        if not math.isfinite(degrees) or abs(degrees) > 360:
            return _fail('That turn is out of range.')
        if abs(degrees % 360) < 1e-9:
            return JsonResponse({'success': True, 'message': 'Nothing turned.', 'undo': None})
        with transaction.atomic():
            _rotate(found, degrees)
            label = _label('Rotated', count)
        log_admin_action(request, 'Update', 'FloorPlan', plan.floor_plan_id,
                         f'{label} by {degrees:.0f}° on {plan.name}')
        return JsonResponse({'success': True, 'message': label + '.', 'undo': label})

    # preview_delete and delete
    with transaction.atomic():
        collector, data_groups, fast_groups, unlinks = _plan_delete(found)
        everything = data_groups + fast_groups
        summary = _summary(everything, unlinks)
        caught = sum(1 for _, rows in everything for row in rows if getattr(row, 'locked', False))
        if caught:
            return _fail('%d locked element%s would be deleted along with the selection. '
                         'Unlock %s first.' % (caught, '' if caught == 1 else 's',
                                               'it' if caught == 1 else 'them'),
                         locked=True, summary=summary)
        if action == 'preview_delete':
            return JsonResponse({'success': True, 'count': count, 'summary': summary})

        collector.delete()
        label = _label('Deleted', count)
    log_admin_action(request, 'Delete', 'FloorPlan', plan.floor_plan_id, f'{label} from {plan.name}')
    return JsonResponse({'success': True, 'message': label + '.', 'undo': label,
                         'summary': summary})
