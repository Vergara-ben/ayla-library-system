"""Group actions for the floor plan editor: move, delete, lock, unlock and undo."""

import json
import math
from collections import Counter

from django.apps import apps
from django.core import serializers
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
UNDO_KEY = 'floorplan_undo'
# Very large deletes are not kept for undo, to keep the session small.
MAX_UNDO_BYTES = 4 * 1024 * 1024


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


def _snapshot(objs, fields):
    """Rows as they are now, so undo can put them back. fields=None means re-insert."""
    return {'objects': serializers.serialize('json', list(objs)), 'fields': fields}


def _remember(request, plan, action, label, restores, nulls=()):
    record = {'plan': plan.floor_plan_id, 'action': action, 'label': label,
              'restores': restores, 'nulls': list(nulls)}
    if len(json.dumps(record)) > MAX_UNDO_BYTES:
        request.session.pop(UNDO_KEY, None)
        return False
    request.session[UNDO_KEY] = record
    return True


def _move(found, dx, dy):
    """Shift every selected element. Returns undo snapshots taken before the change."""
    from .views import _snap_to_polygon_edge, _stair_flights, _stair_shape_of, _translate_geometry

    restores = [_snapshot(objs, MOVE_FIELDS[kind]) for kind, objs in found.items()]

    room_ids = {r.room_id for r in found.get('room', [])}
    selected_doors = {d.door_id for d in found.get('door', [])}
    # Doors sit on a wall, so they ride with their room.
    carried = list(Door.objects.filter(room_id__in=room_ids).exclude(door_id__in=selected_doors))
    if carried:
        restores.append(_snapshot(carried, ['map_x', 'map_y']))
    waypoint_ids = [w.waypoint_id for w in found.get('waypoint', [])]
    links = list(WaypointConnection.objects.filter(
        Q(waypoint_from_id__in=waypoint_ids) | Q(waypoint_to_id__in=waypoint_ids)))
    if links:
        restores.append(_snapshot(links, ['distance']))

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
    return restores


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


def _undo(request, plan):
    record = request.session.get(UNDO_KEY)
    if not record or record.get('plan') != plan.floor_plan_id:
        return _fail('Nothing to undo.')
    with transaction.atomic():
        for part in record['restores']:
            for item in serializers.deserialize('json', part['objects']):
                if part['fields'] is None:
                    item.save()
                elif type(item.object).objects.filter(pk=item.object.pk).exists():
                    item.object.save(update_fields=part['fields'])
        for label, pk, attname, value, _name in record.get('nulls', []):
            apps.get_model(label).objects.filter(pk=pk, **{attname: None}).update(**{attname: value})
    request.session.pop(UNDO_KEY, None)
    log_admin_action(request, 'Update', 'FloorPlan', plan.floor_plan_id,
                     f'Undid "{record["label"]}" on {plan.name}')
    return JsonResponse({'success': True, 'message': 'Undone: %s.' % record['label'].lower()})


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
        return _undo(request, plan)
    if action not in ('move', 'preview_delete', 'delete', 'lock', 'unlock'):
        return _fail('Unknown action')

    found, error = _load_selection(plan, request.POST.get('items'))
    if error:
        return _fail(error)
    count = sum(len(objs) for objs in found.values())

    if action in ('lock', 'unlock'):
        want = action == 'lock'
        with transaction.atomic():
            restores = [_snapshot(objs, ['locked']) for objs in found.values()]
            for objs in found.values():
                for obj in objs:
                    if obj.locked != want:
                        obj.locked = want
                        obj.save(update_fields=['locked'])
            label = _label('Locked' if want else 'Unlocked', count)
            kept = _remember(request, plan, action, label, restores)
        log_admin_action(request, 'Update', 'FloorPlan', plan.floor_plan_id, f'{label} on {plan.name}')
        return JsonResponse({'success': True, 'message': label + '.', 'undo': label if kept else None})

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
            restores = _move(found, dx, dy)
            label = _label('Moved', count)
            kept = _remember(request, plan, 'move', label, restores)
        log_admin_action(request, 'Update', 'FloorPlan', plan.floor_plan_id,
                         f'{label} by ({dx:.0f}, {dy:.0f}) on {plan.name}')
        return JsonResponse({'success': True, 'message': label + '.', 'undo': label if kept else None})

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

        # Parents first when putting rows back.
        restores = ([_snapshot(rows, None) for _, rows in reversed(data_groups)]
                    + [_snapshot(rows, None) for _, rows in fast_groups])
        collector.delete()
        label = _label('Deleted', count)
        kept = _remember(request, plan, 'delete', label, restores, unlinks)
    log_admin_action(request, 'Delete', 'FloorPlan', plan.floor_plan_id, f'{label} from {plan.name}')
    return JsonResponse({'success': True, 'message': label + '.', 'undo': label if kept else None,
                         'summary': summary})
