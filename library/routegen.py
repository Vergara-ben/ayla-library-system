"""Laying a walkable waypoint network over a floor plan.

Built to match how the network is drawn by hand: lanes about half a metre clear of
shelves, furniture and walls, short steps of a metre or two, a mesh that rings each
table, one waypoint tied to each shelf, and nothing ever drawn through a solid.
"""

import heapq
import math

# Distances in metres, turned into canvas units with the plan's scale.
LANE_M = 0.45            # a lane's distance from a shelf, table or wall
RING_SPACING_M = 1.1     # between waypoints along a lane
POINT_CLEAR_M = 0.35     # a waypoint's least distance from anything solid
KEY_CLEAR_M = 0.25       # the same, for doorway, stair and shelf waypoints
WALL_CLEAR_M = 0.30      # a lane waypoint's least distance from a wall
DOOR_WALL_CLEAR_M = 0.12
EDGE_CLEAR_M = 0.18      # a connection's least distance from anything solid
MERGE_M = 0.50           # lane waypoints closer than this become one
FILL_SPACING_M = 1.5     # open floor with no lane nearby
FILL_CLEAR_M = 0.60
MAX_EDGE_M = 2.5         # longest ordinary connection
DETOUR = 1.3             # a connection is skipped if the mesh already gets there within this factor
DOOR_STANDOFF_M = (0.45, 0.6, 0.3)
DOOR_APERTURE_SLACK = 6.0
DEFAULT_PPM = 100.0
EPS = 1e-9

KIND_ORDER = {'kept': 0, 'door': 1, 'stairs': 2, 'shelf': 3, 'aisle': 4}


# Geometry
def point_in_polygon(x, y, poly):
    inside = False
    for i in range(len(poly)):
        xi, yi = poly[i][0], poly[i][1]
        xj, yj = poly[i - 1][0], poly[i - 1][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
    return inside


def point_segment_distance(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    t = 0.0 if length_sq == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _orient(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def proper_crossing(p1, p2, p3, p4):
    """Where segments p1p2 and p3p4 cross through each other, or None."""
    d1, d2 = _orient(p3, p4, p1), _orient(p3, p4, p2)
    d3, d4 = _orient(p1, p2, p3), _orient(p1, p2, p4)
    if not (((d1 > EPS and d2 < -EPS) or (d1 < -EPS and d2 > EPS))
            and ((d3 > EPS and d4 < -EPS) or (d3 < -EPS and d4 > EPS))):
        return None
    t = d1 / (d1 - d2)
    return (p1[0] + (p2[0] - p1[0]) * t, p1[1] + (p2[1] - p1[1]) * t)


def segment_distance(a, b, c, d):
    if proper_crossing(a, b, c, d) is not None:
        return 0.0
    return min(point_segment_distance(a[0], a[1], c[0], c[1], d[0], d[1]),
               point_segment_distance(b[0], b[1], c[0], c[1], d[0], d[1]),
               point_segment_distance(c[0], c[1], a[0], a[1], b[0], b[1]),
               point_segment_distance(d[0], d[1], a[0], a[1], b[0], b[1]))


def edges_of(poly):
    return [(poly[i], poly[(i + 1) % len(poly)]) for i in range(len(poly))]


def bounds(poly):
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return min(xs), min(ys), max(xs), max(ys)


def signed_area(poly):
    return sum(poly[i - 1][0] * poly[i][1] - poly[i][0] * poly[i - 1][1]
               for i in range(len(poly))) / 2.0


class Solid:
    """Something a route goes around: a shelf, a piece of furniture, stairs, a closed room."""

    def __init__(self, poly, kind, ref=None):
        self.poly = [(float(p[0]), float(p[1])) for p in poly]
        self.kind = kind
        self.ref = ref
        self.box = bounds(self.poly)
        self.edges = edges_of(self.poly)

    def distance_to_point(self, x, y):
        if point_in_polygon(x, y, self.poly):
            return 0.0
        return min(point_segment_distance(x, y, a[0], a[1], b[0], b[1]) for a, b in self.edges)

    def distance_to_segment(self, a, b, limit):
        """Distance from a segment, or `limit` once it is known to be at least that far."""
        x0, y0, x1, y1 = self.box
        if (max(a[0], b[0]) < x0 - limit or min(a[0], b[0]) > x1 + limit
                or max(a[1], b[1]) < y0 - limit or min(a[1], b[1]) > y1 + limit):
            return limit
        if point_in_polygon(a[0], a[1], self.poly) or point_in_polygon(b[0], b[1], self.poly):
            return 0.0
        return min(segment_distance(a, b, c, d) for c, d in self.edges)


# The plan, loaded once
class PlanShape:
    def __init__(self, plan, rooms, doors, shelves, obstacles, stairways):
        self.ppm = float(plan.pixels_per_meter or DEFAULT_PPM)
        m = self.ppm
        self.lane = LANE_M * m
        self.rooms = [r for r in rooms if r.geometry and len(r.geometry) >= 3]
        self.open_rooms = [r for r in self.rooms if r.patron_access]
        self.room_poly = {r.room_id: [(float(p[0]), float(p[1])) for p in r.geometry] for r in self.rooms}
        self.room_area = {rid: abs(signed_area(p)) for rid, p in self.room_poly.items()}
        self.doors = [(float(d.map_x), float(d.map_y), (d.width or 28) / 2.0,
                       {d.room_id, d.room_b_id} if d.room_b_id else None) for d in doors]

        self.solids = []
        for o in obstacles:
            if o.geometry and len(o.geometry) >= 3:
                self.solids.append(Solid(o.geometry, 'furniture', o))
        for s in shelves:
            if not s.is_elevated and len(s.footprint()) >= 3:
                self.solids.append(Solid(s.footprint(), 'shelf', s))
        for st in stairways:
            if st.geometry and len(st.geometry) >= 3:
                self.solids.append(Solid(st.geometry, 'stairs', st))
        for r in self.rooms:
            if not r.patron_access:
                self.solids.append(Solid(r.geometry, 'closed room', r))

    def room_at(self, x, y):
        """The smallest open room around a point, so a room drawn inside another wins."""
        best = None
        for r in self.open_rooms:
            if point_in_polygon(x, y, self.room_poly[r.room_id]):
                if best is None or self.room_area[r.room_id] < self.room_area[best.room_id]:
                    best = r
        return best

    def wall_distance(self, x, y, room):
        return min(point_segment_distance(x, y, a[0], a[1], b[0], b[1])
                   for a, b in edges_of(self.room_poly[room.room_id]))

    def solid_distance(self, x, y, ignore=None):
        return min((s.distance_to_point(x, y) for s in self.solids if s.ref is not ignore),
                   default=float('inf'))

    def walls_crossed(self, a, b):
        """True when a straight step goes through a wall anywhere but a doorway."""
        for r in self.rooms:
            for c, d in edges_of(self.room_poly[r.room_id]):
                hit = proper_crossing(a, b, c, d)
                if hit is None:
                    continue
                through_door = any(
                    math.hypot(hit[0] - dx, hit[1] - dy) <= half + DOOR_APERTURE_SLACK
                    and (joins is None or r.room_id in joins)
                    for dx, dy, half, joins in self.doors)
                if not through_door:
                    return True
        return False

    def step_is_clear(self, a, b):
        limit = EDGE_CLEAR_M * self.ppm
        if any(s.distance_to_segment(a, b, limit) < limit for s in self.solids):
            return False
        return not self.walls_crossed(a, b)


class Node:
    def __init__(self, x, y, room, kind, label, shelf=None, waypoint=None):
        self.x, self.y = x, y
        self.room = room
        self.kind = kind
        self.label = label
        self.shelf = shelf
        self.waypoint = waypoint     # an existing waypoint that is kept
        self.index = None

    @property
    def xy(self):
        return (self.x, self.y)


# Placing waypoints
def _outward_normal(a, b, poly_area_sign):
    """Unit normal of an edge pointing out of its polygon."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy) or 1.0
    # For a counter-clockwise outline the outside is to the right of each edge.
    nx, ny = dy / length, -dx / length
    return (nx, ny) if poly_area_sign > 0 else (-nx, -ny)


def _valid_point(shape, x, y, clear, wall_clear, ignore=None):
    room = shape.room_at(x, y)
    if room is None:
        return None
    if shape.solid_distance(x, y, ignore) < clear:
        return None
    if shape.wall_distance(x, y, room) < wall_clear:
        return None
    return room


def door_nodes(shape, doors):
    m = shape.ppm
    nodes, pairs = [], []
    for door in doors:
        rad = math.radians(door.rotation or 0)
        nx, ny = -math.sin(rad), math.cos(rad)
        sides = []
        for sign in (1, -1):
            for standoff in DOOR_STANDOFF_M:
                x = door.map_x + nx * standoff * m * sign
                y = door.map_y + ny * standoff * m * sign
                room = _valid_point(shape, x, y, KEY_CLEAR_M * m, DOOR_WALL_CLEAR_M * m)
                if room is not None:
                    sides.append(Node(x, y, room, 'door', 'Doorway'))
                    break
        nodes.extend(sides)
        if len(sides) == 2 and sides[0].room.room_id != sides[1].room.room_id:
            pairs.append((sides[0], sides[1]))
    return nodes, pairs


def _face_points(shape, poly, preferred_normal=None, per_face=False):
    """Candidate standing spots in front of each face, best face first.

    With per_face, each spot comes as (face number, x, y).
    """
    sign = signed_area(poly)
    faces = []
    for a, b in edges_of(poly):
        length = math.hypot(b[0] - a[0], b[1] - a[1])
        if length < EPS:
            continue
        n = _outward_normal(a, b, sign)
        facing = (n[0] * preferred_normal[0] + n[1] * preferred_normal[1]) if preferred_normal else 0
        faces.append((-(facing > 0.7), -length, a, b, n))
    faces.sort(key=lambda f: (f[0], f[1]))
    for number, (_front, _length, a, b, n) in enumerate(faces):
        for t in (0.5, 0.35, 0.65, 0.2, 0.8):
            fx = a[0] + (b[0] - a[0]) * t
            fy = a[1] + (b[1] - a[1]) * t
            spot = (fx + n[0] * shape.lane, fy + n[1] * shape.lane)
            yield (number,) + spot if per_face else spot


def shelf_nodes(shape, shelves):
    m = shape.ppm
    nodes, missed = [], []
    for shelf in shelves:
        poly = shelf.footprint()
        if len(poly) < 3:
            continue
        # A drawn outline says nothing about which side is the front; a placed rectangle does.
        front = None
        if not (shelf.geometry and len(shelf.geometry) >= 3):
            rad = math.radians(shelf.rotation or 0)
            front = (-math.sin(rad), math.cos(rad))
        cx = sum(p[0] for p in poly) / len(poly)
        cy = sum(p[1] for p in poly) / len(poly)
        home = shape.room_at(cx, cy)
        placed = None
        for x, y in _face_points(shape, poly, front):
            room = _valid_point(shape, x, y, KEY_CLEAR_M * m, DOOR_WALL_CLEAR_M * m, ignore=shelf)
            if room is None or shape.solid_distance(x, y) < KEY_CLEAR_M * m * 0.9:
                continue
            # Never the far side of the wall a shelf stands against.
            if (home is not None and room is not home) or shape.walls_crossed((cx, cy), (x, y)):
                continue
            placed = Node(x, y, room, 'shelf', shelf.name or 'Shelf', shelf=shelf)
            break
        if placed is None:
            missed.append(shelf.name or 'Shelf')
        else:
            nodes.append(placed)
    return nodes, missed


def stair_nodes(shape, stairways):
    m = shape.ppm
    nodes = []
    for st in stairways:
        poly = st.geometry or []
        if len(poly) < 3:
            continue
        # One way on from each side that has room to stand, since the entrance is not recorded.
        done = set()
        for face, x, y in _face_points(shape, [(float(p[0]), float(p[1])) for p in poly],
                                       per_face=True):
            if face in done:
                continue
            room = _valid_point(shape, x, y, KEY_CLEAR_M * m * 0.9, DOOR_WALL_CLEAR_M * m, ignore=st)
            if room is not None and all(math.hypot(x - n.x, y - n.y) > MERGE_M * m for n in nodes):
                nodes.append(Node(x, y, room, 'stairs', st.label))
                done.add(face)
    return nodes


def _lane_along(shape, poly, outward):
    """Points on a lane that follows an outline, outside it (a solid) or inside it (a room)."""
    m = shape.ppm
    lane = shape.lane
    sign = signed_area(poly) * (1 if outward else -1)
    pts = []
    count = len(poly)
    for i in range(count):
        a, b = poly[i], poly[(i + 1) % count]
        length = math.hypot(b[0] - a[0], b[1] - a[1])
        if length < EPS:
            continue
        n = _outward_normal(a, b, sign)
        steps = max(1, int(round(length / (RING_SPACING_M * m))))
        for k in range(steps + 1):
            t = k / steps
            pts.append((a[0] + (b[0] - a[0]) * t + n[0] * lane,
                        a[1] + (b[1] - a[1]) * t + n[1] * lane))
        # Round the corner with one more point on the bisector.
        c = poly[(i + 2) % count]
        n2 = _outward_normal(b, c, sign)
        bx, by = n[0] + n2[0], n[1] + n2[1]
        blen = math.hypot(bx, by)
        if blen > 0.3:
            pts.append((b[0] + bx / blen * lane * 1.3, b[1] + by / blen * lane * 1.3))
    return pts


def _inner_corners(shape, poly):
    """A point just inside each corner that juts into a room, so routes can turn round it."""
    sign = signed_area(poly)
    pts = []
    count = len(poly)
    for i in range(count):
        a, b, c = poly[i - 1], poly[i], poly[(i + 1) % count]
        turn = _orient(a, b, c)
        # A reflex corner turns against the outline's winding.
        if abs(turn) < EPS or (turn > 0) == (sign > 0):
            continue
        n1 = _outward_normal(a, b, sign)
        n2 = _outward_normal(b, c, sign)
        bx, by = -(n1[0] + n2[0]), -(n1[1] + n2[1])
        blen = math.hypot(bx, by)
        if blen > 0.3:
            pts.append((b[0] + bx / blen * shape.lane * 1.3, b[1] + by / blen * shape.lane * 1.3))
    return pts


def lane_nodes(shape):
    m = shape.ppm
    raw = []   # (x, y, the room the lane belongs to, or None)
    for solid in shape.solids:
        if solid.kind != 'closed room':
            cx = sum(p[0] for p in solid.poly) / len(solid.poly)
            cy = sum(p[1] for p in solid.poly) / len(solid.poly)
            home = shape.room_at(cx, cy)
            raw.extend((x, y, home) for x, y in _lane_along(shape, solid.poly, outward=True))
    for room in shape.open_rooms:
        raw.extend((x, y, room) for x, y in _inner_corners(shape, shape.room_poly[room.room_id]))
    nodes = []
    for x, y, home in raw:
        room = _valid_point(shape, x, y, POINT_CLEAR_M * m, WALL_CLEAR_M * m)
        # A lane round a shelf stays on the shelf's side of the wall.
        if room is not None and (home is None or room is home):
            nodes.append(Node(x, y, room, 'aisle', 'Aisle'))
    return nodes


def fill_nodes(shape, existing):
    """Open floor: a regular grid, then any narrow part the grid missed."""
    m = shape.ppm
    placed = list(existing)
    nodes = []

    def place(x, y, room, spacing):
        if all(math.hypot(x - n.x, y - n.y) >= spacing for n in placed):
            node = Node(x, y, room, 'aisle', 'Aisle')
            nodes.append(node)
            placed.append(node)

    def clearance(x, y, room):
        if shape.room_at(x, y) is not room:
            return -1.0
        return min(shape.solid_distance(x, y), shape.wall_distance(x, y, room))

    step = FILL_SPACING_M * m
    fine = 0.3 * m
    for room in shape.open_rooms:
        x0, y0, x1, y1 = bounds(shape.room_poly[room.room_id])
        # Centre the grid in the room rather than hanging it off one corner.
        ox = x0 + ((x1 - x0) % step) / 2 + step / 2
        oy = y0 + ((y1 - y0) % step) / 2 + step / 2
        y = oy
        while y < y1:
            x = ox
            while x < x1:
                if clearance(x, y, room) >= FILL_CLEAR_M * m:
                    place(x, y, room, 1.0 * m)
                x += step
            y += step

        spots = []
        y = y0 + fine / 2
        while y < y1:
            x = x0 + fine / 2
            while x < x1:
                c = clearance(x, y, room)
                if c >= FILL_CLEAR_M * m:
                    spots.append((c, x, y))
                x += fine
            y += fine
        for _c, x, y in sorted(spots, key=lambda sp: -sp[0]):
            place(x, y, room, step * 0.85)
    return nodes


def merge_nodes(shape, nodes):
    """Keep every key waypoint; fold lane waypoints that crowd another into it."""
    m = shape.ppm
    ordered = sorted(nodes, key=lambda n: KIND_ORDER[n.kind])
    kept = []
    cell = MERGE_M * m
    grid = {}
    for n in ordered:
        gx, gy = int(n.x // cell), int(n.y // cell)
        if n.kind == 'aisle':
            crowded = any(
                math.hypot(n.x - o.x, n.y - o.y) < cell
                for i in (-1, 0, 1) for j in (-1, 0, 1)
                for o in grid.get((gx + i, gy + j), ()))
            if crowded:
                continue
        kept.append(n)
        grid.setdefault((gx, gy), []).append(n)
    return kept


# Joining them
class _EdgeIndex:
    """Edges bucketed by area, for the no-crossing test."""

    def __init__(self, cell):
        self.cell = cell
        self.buckets = {}

    def _cells(self, a, b):
        x0, x1 = sorted((a[0], b[0]))
        y0, y1 = sorted((a[1], b[1]))
        for i in range(int(x0 // self.cell), int(x1 // self.cell) + 1):
            for j in range(int(y0 // self.cell), int(y1 // self.cell) + 1):
                yield (i, j)

    def add(self, i, j, a, b):
        for c in self._cells(a, b):
            self.buckets.setdefault(c, []).append((i, j, a, b))

    def crosses(self, i, j, a, b):
        for c in self._cells(a, b):
            for p, q, c1, c2 in self.buckets.get(c, ()):
                if len({i, j, p, q}) == 4 and proper_crossing(a, b, c1, c2) is not None:
                    return True
        return False


def _graph_distance(adj, start, goal, limit):
    best = {start: 0.0}
    heap = [(0.0, start)]
    while heap:
        d, u = heapq.heappop(heap)
        if u == goal:
            return d
        if d > best.get(u, float('inf')) or d > limit:
            continue
        for v, w in adj[u]:
            nd = d + w
            if nd < best.get(v, float('inf')) and nd <= limit:
                best[v] = nd
                heapq.heappush(heap, (nd, v))
    return float('inf')


def build_network(shape, doors, shelves, stairways, kept_waypoints, kept_links):
    """Return (nodes, edges, report); edges are index pairs into nodes."""
    m = shape.ppm

    kept = []
    for w in kept_waypoints:
        room = shape.room_at(w.map_x, w.map_y)
        kept.append(Node(w.map_x, w.map_y, room, 'kept', w.label or '', waypoint=w))

    doors_made, door_pairs = door_nodes(shape, doors)
    shelves_made, missed_shelves = shelf_nodes(shape, shelves)
    stairs_made = stair_nodes(shape, stairways)
    key = kept + doors_made + stairs_made + shelves_made
    lanes = lane_nodes(shape)
    nodes = merge_nodes(shape, key + lanes)
    nodes = merge_nodes(shape, nodes + fill_nodes(shape, nodes))
    for i, n in enumerate(nodes):
        n.index = i

    adj = {i: [] for i in range(len(nodes))}
    edges = set()
    index = _EdgeIndex(MAX_EDGE_M * m)

    def length(a, b):
        return math.hypot(a.x - b.x, a.y - b.y)

    def add(a, b):
        key_ = (min(a.index, b.index), max(a.index, b.index))
        if key_ in edges:
            return
        edges.add(key_)
        w = length(a, b)
        adj[a.index].append((b.index, w))
        adj[b.index].append((a.index, w))
        index.add(a.index, b.index, a.xy, b.xy)

    # Links between kept waypoints stay as they were.
    by_waypoint = {n.waypoint.waypoint_id: n for n in nodes if n.waypoint is not None}
    existing = set()
    for a_id, b_id in kept_links:
        if a_id in by_waypoint and b_id in by_waypoint:
            add(by_waypoint[a_id], by_waypoint[b_id])
            existing.add((min(by_waypoint[a_id].index, by_waypoint[b_id].index),
                          max(by_waypoint[a_id].index, by_waypoint[b_id].index)))

    # Through each doorway.
    for a, b in door_pairs:
        if a.index is not None and b.index is not None:
            add(a, b)

    # Short, clear steps within a room, shortest first, skipping any the mesh already covers.
    cell = MAX_EDGE_M * m
    grid = {}
    for n in nodes:
        grid.setdefault((int(n.x // cell), int(n.y // cell)), []).append(n)
    candidates = []
    for a in nodes:
        gx, gy = int(a.x // cell), int(a.y // cell)
        for i in (-1, 0, 1):
            for j in (-1, 0, 1):
                for b in grid.get((gx + i, gy + j), ()):
                    if b.index <= a.index or a.room is None or b.room is not a.room:
                        continue
                    d = length(a, b)
                    if d <= MAX_EDGE_M * m:
                        candidates.append((d, a, b))
    candidates.sort(key=lambda c: c[0])
    for d, a, b in candidates:
        if _graph_distance(adj, a.index, b.index, d * DETOUR) <= d * DETOUR:
            continue
        if index.crosses(a.index, b.index, a.xy, b.xy):
            continue
        if not shape.step_is_clear(a.xy, b.xy):
            continue
        add(a, b)

    # A room left in pieces is joined at its closest clear gap.
    _join_pieces(shape, nodes, adj, add, index)

    # Lane waypoints left unjoined are dropped.
    removed = _drop_dead_ends(nodes, adj)
    keep = [n for n in nodes if n.index not in removed]
    remap = {n.index: k for k, n in enumerate(keep)}
    final_edges = sorted({(min(remap[a], remap[b]), max(remap[a], remap[b]))
                          for a, b in edges if a in remap and b in remap
                          and (a, b) not in existing})
    for k, n in enumerate(keep):
        n.index = k

    piece_of = _pieces(len(keep), final_edges + [
        (remap[a], remap[b]) for a, b in existing if a in remap and b in remap])
    # The rooms in each separate part of the network, largest part first.
    parts = {}
    for n in keep:
        if n.room is not None:
            parts.setdefault(piece_of[n.index], set()).add(n.room.name)
    parts = sorted((sorted(names) for names in parts.values()), key=len, reverse=True)
    report = {
        'missed_shelves': missed_shelves,
        'rooms_without': [r.name for r in shape.open_rooms
                          if not any(n.room is r for n in keep)],
        'pieces': len(set(piece_of)),
        'parts': parts,
    }
    return keep, final_edges, report


def _find(parent, i):
    while parent[i] != i:
        parent[i] = parent[parent[i]]
        i = parent[i]
    return i


def _join_pieces(shape, nodes, adj, add, index):
    m = shape.ppm
    for room in shape.open_rooms:
        members = [n for n in nodes if n.room is room]
        if len(members) < 2:
            continue
        while True:
            parent = {n.index: n.index for n in nodes}
            for u in adj:
                for v, _w in adj[u]:
                    ru, rv = _find(parent, u), _find(parent, v)
                    if ru != rv:
                        parent[ru] = rv
            pieces = {}
            for n in members:
                pieces.setdefault(_find(parent, n.index), []).append(n)
            if len(pieces) < 2:
                break
            groups = sorted(pieces.values(), key=len, reverse=True)
            main = groups[0]
            best = None
            for other in groups[1:]:
                for a in other:
                    for b in main:
                        d = math.hypot(a.x - b.x, a.y - b.y)
                        if best is not None and d >= best[0]:
                            continue
                        if d > 8 * m or index.crosses(a.index, b.index, a.xy, b.xy):
                            continue
                        if shape.step_is_clear(a.xy, b.xy):
                            best = (d, a, b)
            if best is None:
                break
            add(best[1], best[2])


def _drop_dead_ends(nodes, adj):
    """Lane waypoints nothing could be joined to."""
    return {n.index for n in nodes if n.kind == 'aisle' and not adj[n.index]}


def _pieces(count, edges):
    """For each waypoint, a label shared by every waypoint it can reach."""
    parent = list(range(count))
    for a, b in edges:
        ra, rb = _find(parent, a), _find(parent, b)
        if ra != rb:
            parent[ra] = rb
    return [_find(parent, i) for i in range(count)]
