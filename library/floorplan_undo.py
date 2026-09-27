"""Undo for the floor plan editor: every change saves the plan as it was, and undo puts it back."""

import json
from functools import wraps

from django.core import serializers
from django.db import transaction
from django.http import JsonResponse

from .audit import log_admin_action
from .auth_utils import admin_only_required
from .models import (BLEBeacon, Book, Door, FloorPlan, FloorPlanUndo, Obstacle, Room, Shelf,
                     ShelfLevel, Stairway, StockAudit, Waypoint, WaypointConnection)

# How many steps back each plan keeps.
HISTORY = 10

# Everything drawn on a plan, parents first.
SCOPE = [
    ('room', lambda p: Room.objects.filter(floor_plan=p)),
    ('shelf', lambda p: Shelf.objects.filter(room__floor_plan=p)),
    ('shelflevel', lambda p: ShelfLevel.objects.filter(shelf__room__floor_plan=p)),
    ('door', lambda p: Door.objects.filter(room__floor_plan=p)),
    ('obstacle', lambda p: Obstacle.objects.filter(floor_plan=p)),
    ('stairway', lambda p: Stairway.objects.filter(floor_plan=p)),
    ('beacon', lambda p: BLEBeacon.objects.filter(floor_plan=p)),
    ('waypoint', lambda p: Waypoint.objects.filter(floor_plan=p)),
    ('connection', lambda p: WaypointConnection.objects.filter(waypoint_from__floor_plan=p)),
]

# The plan's own settings the editor changes. Its name and live status are not the editor's.
PLAN_FIELDS = ['canvas_width', 'canvas_height', 'pixels_per_meter', 'north_offset_deg',
               'desk_x', 'desk_y']

# What each editor action is called in the Undo button.
LABELS = {
    'set_floorplan_canvas': 'Changed the canvas size',
    'set_floorplan_desk': 'Moved the front desk',
    'add_room': 'Added a room', 'edit_room': 'Edited a room', 'delete_room': 'Deleted a room',
    'add_obstacle': 'Added furniture', 'edit_obstacle': 'Edited furniture',
    'delete_obstacle': 'Deleted furniture',
    'add_stairway': 'Added a stairway', 'edit_stairway': 'Edited a stairway',
    'delete_stairway': 'Deleted a stairway',
    'add_door': 'Added a door', 'move_door': 'Moved a door', 'edit_door': 'Edited a door',
    'flip_door': 'Flipped a door', 'delete_door': 'Deleted a door',
    'generate_waypoints': 'Generated the route', 'clear_waypoints': 'Cleared the waypoints',
    'add_shelf': 'Added a shelf', 'place_shelf': 'Placed a shelf', 'move_shelf': 'Moved a shelf',
    'rotate_shelf': 'Rotated a shelf', 'resize_shelf': 'Resized a shelf',
    'set_shelf_grid': "Changed a shelf's columns", 'edit_shelf': 'Edited a shelf',
    'unplace_shelf': 'Took a shelf off the plan', 'delete_shelf': 'Deleted a shelf',
    'delete_shelf_level': 'Deleted a shelf level',
    'add_beacon': 'Added a beacon', 'update_beacon': 'Edited a beacon',
    'move_beacon': 'Moved a beacon', 'delete_beacon': 'Deleted a beacon',
    'add_waypoint': 'Added a waypoint', 'move_waypoint': 'Moved a waypoint',
    'edit_waypoint': 'Edited a waypoint', 'delete_waypoint': 'Deleted a waypoint',
    'add_waypoint_connection': 'Connected two waypoints',
    'delete_waypoint_connection': 'Removed a connection',
    'floorplan_bulk': 'Changed the selection',
}


def snapshot(plan):
    """The plan as it stands, as JSON text."""
    rows = {key: serializers.serialize('json', query(plan)) for key, query in SCOPE}
    levels = ShelfLevel.objects.filter(shelf__room__floor_plan=plan)
    shelves = Shelf.objects.filter(room__floor_plan=plan)
    return json.dumps({
        'plan': {f: getattr(plan, f) for f in PLAN_FIELDS},
        'rows': rows,
        # Links into the plan from outside it, which a delete would clear.
        'books': list(Book.objects.filter(shelf_level__in=levels)
                      .values_list('book_id', 'shelf_level_id', 'shelf_slot')),
        'audits': list(StockAudit.objects.filter(shelf__in=shelves)
                       .values_list('audit_id', 'shelf_id')),
        'other_stairs': list(Stairway.objects.exclude(floor_plan=plan)
                             .values_list('stairway_id', 'partner_id', 'connects_to_id')),
    }, sort_keys=True, default=str)


def restore(plan, text):
    """Put the plan back as it was in `text`."""
    data = json.loads(text)
    with transaction.atomic():
        FloorPlan.objects.filter(pk=plan.pk).update(**data['plan'])
        kept = {}
        planned_stairs = {row['pk'] for row in json.loads(data['rows']['stairway'])}
        for key, _query in SCOPE:
            kept[key] = set()
            for item in serializers.deserialize('json', data['rows'][key]):
                obj = item.object
                if key == 'stairway':
                    # A link to something since deleted on another floor is dropped, not restored.
                    if (obj.partner_id and obj.partner_id not in planned_stairs
                            and not Stairway.objects.filter(pk=obj.partner_id).exists()):
                        obj.partner_id = None
                    if obj.connects_to_id and not FloorPlan.objects.filter(pk=obj.connects_to_id).exists():
                        obj.connects_to_id = None
                item.save()
                kept[key].add(item.object.pk)
        # Whatever the undone step added goes, children first.
        for key, query in reversed(SCOPE):
            query(plan).exclude(pk__in=kept[key]).delete()

        _put_back(Book, 'book_id', ('shelf_level_id', 'shelf_slot'), data['books'])
        _put_back(StockAudit, 'audit_id', ('shelf_id',), data['audits'])
        _put_back(Stairway, 'stairway_id', ('partner_id', 'connects_to_id'), data['other_stairs'])


def _put_back(model, pk_name, fields, rows):
    """Reset those fields on the rows that still exist, touching only the ones that changed."""
    wanted = {row[0]: tuple(row[1:]) for row in rows}
    if not wanted:
        return
    for row in model.objects.filter(pk__in=wanted).values_list(pk_name, *fields):
        if tuple(row[1:]) != wanted[row[0]]:
            model.objects.filter(pk=row[0]).update(**dict(zip(fields, wanted[row[0]])))


def _same(a, b):
    """Two snapshots hold the same plan; 100 and 100.0 are the same corner."""
    if a == b:
        return True
    a, b = json.loads(a), json.loads(b)
    for side in (a, b):
        side['rows'] = {k: json.loads(v) for k, v in side['rows'].items()}
    return a == b


def history_state(plan):
    """(label of the step Undo would take back, how many steps there are)."""
    steps = FloorPlanUndo.objects.filter(floor_plan=plan)
    latest = steps.order_by('-undo_id').values_list('label', flat=True).first()
    return latest, steps.count()


# Where to find the plan when only the element's id was posted.
OWNERS = [
    ('room_id', Room, 'floor_plan_id'),
    ('shelf_id', Shelf, 'room__floor_plan_id'),
    ('shelf_level_id', ShelfLevel, 'shelf__room__floor_plan_id'),
    ('door_id', Door, 'room__floor_plan_id'),
    ('obstacle_id', Obstacle, 'floor_plan_id'),
    ('stairway_id', Stairway, 'floor_plan_id'),
    ('beacon_id', BLEBeacon, 'floor_plan_id'),
    ('waypoint_id', Waypoint, 'floor_plan_id'),
]


def _plan_of(request):
    for key in ('undo_plan', 'floor_plan_id', 'floorplan_id'):
        raw = (request.POST.get(key) or '').strip()
        if raw.isdigit():
            return FloorPlan.objects.filter(pk=int(raw)).first()
    for key, model, path in OWNERS:
        raw = (request.POST.get(key) or '').strip()
        if raw.isdigit():
            plan_id = model.objects.filter(pk=int(raw)).values_list(path, flat=True).first()
            if plan_id:
                return FloorPlan.objects.filter(pk=plan_id).first()
    return None


def _json_of(response):
    if 'json' not in (response.get('Content-Type') or ''):
        return None
    try:
        return json.loads(response.content)
    except (TypeError, ValueError):
        return None


def undoable(name):
    """Save the plan before an editor action, so the action can be undone."""
    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if request.method != 'POST' or 'admin_id' not in request.session:
                return view(request, *args, **kwargs)
            if name == 'floorplan_bulk' and request.POST.get('action') in ('undo', 'preview_delete'):
                return view(request, *args, **kwargs)
            plan = _plan_of(request)
            if plan is None:
                return view(request, *args, **kwargs)

            before = snapshot(plan)
            response = view(request, *args, **kwargs)
            data = _json_of(response)
            # A plain form answers with a redirect; the comparison below tells if it changed anything.
            if data is None and not (200 <= response.status_code < 400):
                return response
            if data is not None and not data.get('success'):
                return response
            plan.refresh_from_db()
            # Nothing changed, so there is nothing to take back.
            if not _same(snapshot(plan), before):
                label = (data or {}).get('undo')
                label = label if isinstance(label, str) else LABELS.get(name)
                FloorPlanUndo.objects.create(
                    floor_plan=plan, admin_id=request.session.get('admin_id'),
                    label=(label or 'Changed the plan')[:255], snapshot=before)
                old = (FloorPlanUndo.objects.filter(floor_plan=plan)
                       .order_by('-undo_id').values_list('undo_id', flat=True)[HISTORY:])
                FloorPlanUndo.objects.filter(undo_id__in=list(old)).delete()
            if data is not None:
                data['undo_label'], data['undo_count'] = history_state(plan)
                response.content = json.dumps(data, default=str)
            return response
        return wrapped
    return decorator


def wrap_editor_views(urlpatterns):
    """Make every floor plan editor endpoint undoable."""
    for pattern in urlpatterns:
        if getattr(pattern, 'name', None) in LABELS:
            pattern.callback = undoable(pattern.name)(pattern.callback)


def undo_latest(request, plan):
    step = FloorPlanUndo.objects.filter(floor_plan=plan).order_by('-undo_id').first()
    if step is None:
        return JsonResponse({'success': False, 'error': 'Nothing to undo.'})
    restore(plan, step.snapshot)
    label = step.label
    step.delete()
    log_admin_action(request, 'Update', 'FloorPlan', plan.floor_plan_id,
                     f'Undid "{label}" on {plan.name}')
    next_label, count = history_state(plan)
    return JsonResponse({'success': True, 'message': 'Undone: %s.' % label.lower(),
                         'undo_label': next_label, 'undo_count': count})


@admin_only_required
def floorplan_undo(request):
    """Take back the latest change to a floor plan."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Only POST method allowed'})
    plan = _plan_of(request)
    if plan is None:
        return JsonResponse({'success': False, 'error': 'Floor plan not found'})
    return undo_latest(request, plan)
