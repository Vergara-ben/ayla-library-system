# AYLA — Patron Mapping & Navigation Plan
*Written against `CAPSTONE-MANUSCRIPT.docx`, diffed against the current codebase.*

## Decisions locked with the researchers

1. **Build the vector canvas.** Chapter 1 §Scope ¶369 specifies a Leaflet-Geoman vector
   workspace with drawable room shapes and *"completely eliminating the need for an
   uploaded floor-plan image."* Chapter 4 (Fig. 115) contradicts this and describes image
   upload, which is what the code does today. We build to Chapter 1 and amend Chapter 4,
   because Figures 46 (Drawing Room), 47 (Edit Room), 50 (Rotate Shelf), 51/57 (Unplace
   Shelf) and 54 (Draw Room) are formal activity diagrams already printed in the
   manuscript — removing them means renumbering every figure that follows.
2. **Move BLE positioning to the Django backend.** Chapter 2 states twice (¶463, ¶498)
   that RSSI is transmitted to the backend where Moving Average, Position Threshold and
   trilateration are performed. Today all three run in browser JavaScript.
3. **"Section labels" layer = ShelfLevel category text** (`Fiction`, `Economics`). The
   `Section` table was deliberately removed in an earlier migration, and Chapter 1 ¶364
   defines the hierarchy as Room → Shelf → Shelf Level. No new model.

## What already conforms — do not touch

| Manuscript requirement | Implementation |
|---|---|
| A* over the admin waypoint graph, executed on the Django backend (Ch.2 ¶454) | `_astar` — `views.py:3265` |
| Leaflet.js CRS.Simple rendering (Ch.1 ¶369) | `patronmap.html`, `indoormap.html` |
| Waypoint graph hidden from the patron map (Ch.1 ¶372) | Explicitly excluded, with a comment |
| Manual tap fallback for limited-BLE devices (Ch.1 ¶372) | `setPatronPosition(..., 'manual')` |
| Continuously updating route line (Ch.1 ¶372) | `computeRoute()` on every position update |
| Beacons pinned with UUID + label (Ch.1 ¶369) | `BLEBeacon` model + admin placement |
| Room → Shelf → Shelf Level hierarchy (Ch.1 ¶364) | `models.py:53-163` |

The routing core is faithful. Every gap below is in floor-plan representation, map layers,
or where the BLE math executes.

---

## Phase 1 — Vector floor plan
*Closes: Ch.1 ¶369; Figures 46, 47, 48, 49, 50, 51, 54, 57.*

> **Status: DONE.** Image upload removed (`FloorPlan.image_url` dropped); floor
> plans are named blank canvases; rooms are drawn and reshaped with
> Leaflet-Geoman and have a right-click Rename/Delete menu; shelves carry a
> `rotation` and nullable coordinates, with Place/Move/Rotate/Unplace endpoints
> (Figures 48–51) and a right-click menu. Any plan can be edited via a picker,
> so a layout can be finished before it goes live.
>
> Per-shelf `width`/`depth` **were** added after all (migration `0015`): the
> library's shelves are not a uniform size, so each carries its own footprint,
> editable from the shelf's right-click menu. Rotation is applied about the
> centre of that rectangle.
>
> All room and shelf editing lives in the **right-click context menu** on the
> polygon itself — Reshape / Rename / Delete for rooms, Resize / Rotate / Move /
> Rename / Unplace for shelves — with the actions stacked vertically. The
> earlier global "Edit Rooms" toolbar toggle is gone; reshaping is now per-room,
> so dragging one room's corners cannot disturb its neighbours.

### 1.1 Model changes (`library/models.py`, migration `0013_vector_floorplan`)

**`FloorPlan`**
- `canvas_width` — `FloatField(default=1000)`
- `canvas_height` — `FloatField(default=1000)`
- `pixels_per_meter` — `FloatField(null=True, blank=True)` (consumed in Phase 4)
- `image_url` — **keep the column**, now optional. Chapter 1 eliminates the *need* for an
  image; retaining it as an optional tracing backdrop costs nothing and keeps existing
  rows renderable while rooms are being drawn.

**`Room`** — currently a single point, which cannot satisfy "draw each room as an
independent, editable vector shape."
- `geometry` — `JSONField(null=True, blank=True)`, a list of `[x, y]` vertices.
- Keep `map_x` / `map_y` as the **label anchor** (polygon centroid), so the section-label
  layer has a stable place to draw and existing queries keep working.

**`Shelf`** — rotation is meaningless for a point, so a shelf needs an extent.
- `rotation` — `FloatField(default=0)`, degrees clockwise.
- `width` — `FloatField(default=40)`, `depth` — `FloatField(default=12)` (canvas units).
- `map_x` / `map_y` → `null=True, blank=True`. This is what makes **Unplace Shelf**
  (Fig. 51) expressible: an unplaced shelf still exists in the hierarchy but has no
  position on the canvas.

**`ShelfLevel`** — unchanged. `category` becomes the section-label layer source.

### 1.2 Data migration (same migration file, `RunPython`)

Existing rooms carry only a point, so the vector map would render empty. Synthesize a
default square polygon of 120×120 canvas units centred on each room's existing
`map_x`/`map_y`; the Administrator then reshapes it. Existing shelves get
`rotation=0` and the default extent. Reversible by dropping `geometry`.

### 1.3 Endpoints (`library/urls.py`, `library/views.py`)

Named to trace 1:1 to the manuscript figures — worth the small redundancy, because a
panelist can then follow a figure straight to a URL and a view.

| Figure | Route | Notes |
|---|---|---|
| 46, 54 Draw Room | extend `add_room` | accept `geometry` JSON |
| 47 Edit Room | extend `edit_room` | accept `geometry` JSON; reshape without touching neighbours |
| 48 Place Shelf | `admin-portal/place-shelf/` | sets `map_x`, `map_y` |
| 49 Move Shelf | `admin-portal/move-shelf/` | same fields, distinct action for the diagram |
| 50 Rotate Shelf | `admin-portal/rotate-shelf/` | sets `rotation` |
| 51, 57 Unplace Shelf | `admin-portal/unplace-shelf/` | nulls `map_x`/`map_y` |

Extend `get_map_data` and `get_patron_map_data` to return `room.geometry`, shelf
`rotation`/`width`/`depth`, and each shelf's level categories.

**Every one of these activity diagrams has an explicit confirm/cancel branch.** The admin
UI must show a confirmation step before persisting, and cancel must leave the object
untouched. This is stated in the Ch.4 text for Figures 42–51 and is trivial to demo — do
not skip it.

### 1.4 Admin editor (`templates/admin/floorplanadmin.html`)

- Add `leaflet-geoman-free` (CSS + JS) after the existing Leaflet CDN tags.
- Enable Geoman controls limited to polygon draw / edit / drag / remove.
- Rooms render as `L.polygon` with Geoman editing enabled. `pm:create` → prompt for name
  → confirm → POST. `pm:edit` → confirm → POST updated vertices.
- Shelves render as **rotated rectangles**: `L.polygon` with four corners computed from
  centre + `width`/`depth` + `rotation` (Leaflet's `L.rectangle` cannot rotate).
- Rotation control: select a shelf → angle input/slider → confirm → POST.
- Right-click a shelf → Unplace → confirm.

> **Conflict to watch:** the page already binds its own map-click handlers for the
> beacon/waypoint placement modes (`floorplanadmin.html:927+`). Geoman also captures map
> clicks while drawing. Placement modes must be disabled whenever a Geoman draw/edit mode
> is active, or clicks will double-fire.

### 1.5 Renderers

- `templates/admin/indoormap.html` — draw room polygons and rotated shelf rectangles.
- `templates/patron/patronmap.html` — same geometry (layer wiring lands in Phase 2).

### 1.6 Knock-on risk: nullable shelf coordinates

Making `map_x`/`map_y` nullable touches routing. `get_patron_map_data` must exclude
unplaced shelves, and `get_navigation_route` must return a clear error when the target
book sits on an unplaced shelf ("this book's shelf has not been placed on the map yet")
rather than crashing on `None`. Audit every `Shelf.map_x` read before merging.

---

## Phase 2 — Six independently toggleable layers
*Closes: Ch.1 ¶369, Ch.2 ¶473, Sprint 5 (Table 1).*

> **Status: DONE.** `L.control.layers` now carries all six overlays; BLE beacons
> are off the patron map (admin-only, like the waypoint graph). Section labels
> render from `ShelfLevel.category` and level indicators from the level count.

The manuscript names the six layers in three separate places:
**floor plan base · shelf markers · section labels · shelf level indicators · A\* route ·
patron position marker.**

Today the patron map has shelf markers, the route, the position marker — plus **BLE
beacons**, which are *not* one of the six. Beacons are infrastructure, exactly like the
waypoint graph the map correctly hides; they move to admin-only. The ad-hoc
"🗂️ Layers" button is replaced by a real `L.control.layers` overlay control, which is
literally "independently toggleable" and is a standard, defensible Leaflet feature.

| Layer | Source |
|---|---|
| Floor plan base | `Room.geometry` polygons (+ optional image backdrop) |
| Shelf markers | rotated shelf rectangles |
| Section labels | `ShelfLevel.category` text at the shelf anchor |
| Shelf level indicators | `ShelfLevel.level_number` badges |
| A* route | existing `routeLine` polyline |
| Patron position | existing `patronMarker` |

---

## Phase 3 — Server-side positioning
*Closes: Ch.2 ¶463 and ¶498.*

> **Status: NOT STARTED. Contains the single highest defense risk in the project
> — read the unit bug below before demoing BLE to anyone.**

### 3.0 Three defects found in the existing BLE path

**(a) Unit mismatch — this will visibly misplace the patron.**
`rssiToDistance()` returns **metres** (log-distance path-loss model), but beacon
`map_x`/`map_y` are **canvas units**. `trilaterate()` mixes both in one equation:

```js
bb = p.x*p.x - ref.x*ref.x + p.y*p.y - ref.y*ref.y + ref.d*ref.d - p.d*p.d
//   └────────── canvas units² ──────────┘   └───── metres² ─────┘
```

There is **no scale field in the model** — `pixels_per_meter` was planned here and
never added. On a 1000×800 canvas representing a ~20 m room, one unit is ~2 cm, so
RSSI distances are under-scaled ~50×: every circle collapses and the marker pins
near the beacon centroid regardless of where the patron stands.

This is invisible today because nothing can reach the BLE path (see 3.1), and both
the manual tap and the demo simulation bypass `trilaterate()` entirely. It will
surface the moment BLE runs — i.e. during the defense demo. **Fix first.**

**(b) Beacon identification may not match real hardware.**
`onAdvertisement()` matches only on advertised **service UUIDs** (`e.uuids`) or
`device.name` vs the beacon's label. Classic **iBeacons advertise neither** — their
proximity UUID lives in manufacturer data (company `0x004C`) — so `e.uuids` is empty
and nothing matches, yielding zero RSSI samples while the admin config looks correct.
**Eddystone** advertises service UUID `feaa`, identical on every Eddystone beacon, so
it cannot distinguish one from another (the per-beacon ID is in `serviceData`).

Run `tools/beacon-probe.html` beside the beacons to see what they actually broadcast
before writing the matching code.

**(c) No calibration.** `txPower` is hardcoded to −59 dBm and the path-loss exponent
`n` to 2.0. Real beacons need their own measured RSSI-at-1 m, and indoors with metal
shelving `n` is typically 2.5–3.5. Needs a per-beacon `tx_power` field.

### 3.1 Reality of Web Bluetooth availability (verified July 2026)

Per the [WebBluetoothCG implementation status](https://github.com/WebBluetoothCG/web-bluetooth/blob/main/implementation-status.md):

| API | Status |
|---|---|
| `requestLEScan()` *(what the code uses)* | Flag required: `chrome://flags/#enable-experimental-web-platform-features`. Chrome OS, Android, Mac, Windows |
| `watchAdvertisements()` | Same flag, Chrome 85+ |
| `getDevices()` | Same flag, Chrome 83+ |
| Safari / iOS | *"Not supported and no plan to support it in the near future."* Third-party browsers (Bluefy, iOSWebBLE) polyfill it; Bluefy lists `watchAdvertisements` but **not** `requestLEScan` |

There is **no** flag-free combination of stable Web Bluetooth APIs that yields beacon
RSSI. This is a platform constraint, and Ch.1 ¶389 already declares it — including the
sentence that these are *"well known limitations of the BLE technology and are not
considered as the shortcomings of the system architecture."*

**Test/demo devices for this project:**
- **Laptop (Windows 11, MediaTek MT7921, BT 5.2)** — primary BLE development. `localhost`
  is a secure context, so `runserver` suffices; DevTools make the maths debuggable.
- **Groupmate's Android + flag** — defense demo and walking tests.
- **iPhone** — everything except BLE (map, rooms, shelves, layers, routing, QR fallback).

**Hardware on hand: 3 beacons** — the exact minimum. The system is then *determined*, not
over-determined, so there is no error averaging and losing one beacon (a body blocks
2.4 GHz by 10–20 dB) drops below three and freezes the marker silently. Place them as a
**wide triangle**, never near-collinear, and scope the BLE claim to that triangle's
interior. Worth adding a **proximity fallback**: with 1–2 beacons visible, show the
strongest beacon's position labelled "Approximate", instead of freezing.

New `library/positioning.py`:
- `moving_average(buffer, window)` — RSSI smoothing (Ch.1 ¶369, Ch.2 ¶463).
- `position_threshold(previous, candidate, max_jump)` — rejects implausible jumps.
- `trilaterate(readings)` — linear least-squares over ≥3 beacons (Ch.2 ¶463 requires "at
  least three beacons"; the existing JS already enforces this and the maths ports directly).

New endpoint `patron/position/` (POST): accepts `{readings: [{uuid, rssi}, …]}`, keeps the
moving-average window in the Django cache keyed by patron session, returns
`{x, y, accuracy, beacons_used}`.

The browser is reduced to **collecting advertisements and POSTing raw RSSI** — matching
"the collected RSSI data is transmitted to the Django backend." The manual tap fallback
stays client-side; Ch.1 ¶372 explicitly permits it.

> **Trade-off to accept knowingly:** this adds a network round-trip per position update
> (~1.5 s cadence). On PythonAnywhere's free tier that is acceptable at library scale, and
> it is what the architecture diagram claims. Keeping the filter state server-side is what
> makes the claim *true*, not just the trilateration call.

---

## Phase 4 — Guidance quality
*Not manuscript-mandated, but two items here are defense risks.*

1. **The admin Directions panel is hardcoded mockup text.** `indoormap.html:633-650`
   contains three fixed steps — *"Head straight from Entrance toward East Wing"*, *"Turn
   right at the Section 3 marker"*, *"Shelf A, Row 12"* — that never change whatever route
   is computed. Figure 108 (Indoor Map Interface) is a manuscript screenshot. A panelist
   who clicks a second book sees identical directions. **Replace with generated steps.**
2. **Distance is reported in pixels** — `Distance ≈ 431 px · 5 segment(s)`. With
   `FloorPlan.pixels_per_meter` (Phase 1.1) plus a calibration tool (draw a line across a
   known real-world distance), this becomes metres and an estimated walking time at
   ~1.2 m/s.
3. **Turn-by-turn generation** (`library/navigation.py`): bearing delta between
   consecutive route segments → straight / slight left / left / right; waypoint labels and
   room names supply the landmarks; the final step names the shelf **and** its level
   number and category. Returned as `steps: []` from `get_navigation_route` and rendered
   on both the patron map and the admin panel.
4. **Arrival state** — on reaching the target, show "You've arrived: <shelf> · Level N
   (<category>)".

---

## Manuscript edits required (text only, no code)

1. **Ch.4, Fig. 115** — *"Administrators can upload floor-plan images"* must be rewritten
   to describe the vector canvas, or it directly contradicts Ch.1 ¶369.
2. **Ch.1 ¶369** — delete *"or any deliberate action from the patron."* The Web Bluetooth
   API **requires** a user gesture and a permission prompt; browsers enforce this and it
   cannot be bypassed. The existing "Enable Navigation" button is the correct
   implementation — the sentence overclaims. Suggested replacement: *"…without requiring
   native application installation; the patron grants a one-time browser permission."*
3. **Six-layer list** — if "section labels" is kept verbatim, add a clarifying clause that
   section labels are rendered from shelf-level category text, since the hierarchy defined
   in ¶364 has no Section entity.
4. *(Already tracked in `PANEL_REVISION_PLAN.md`)* Ch.4 Fig. 117 lists a System Log report
   while Ch.1 lists Inventory.

---

## Suggested sequencing

| Step | Deliverable | Checkpoint |
|---|---|---|
| 1 | Migration `0013` + data backfill | `manage.py check`, existing map still renders |
| 2 | Room polygons: model → endpoint → editor → renderers | Draw and reshape a room end-to-end |
| 3 | Shelf place / move / rotate / unplace, each with confirm | Figures 48–51 demonstrable |
| 4 | Six-layer patron map, beacons demoted to admin | Toggle each layer independently |
| 5 | `positioning.py` + `patron/position/` endpoint | Position updates with JS doing no maths |
| 6 | `navigation.py` turn-by-turn + metre distances | Admin mockup panel deleted |

Steps 1–3 are the bulk of the work and unblock everything else. Step 4 is mostly
front-end. Steps 5–6 are self-contained and can be done in either order.

## Explicitly out of scope

Multi-floor navigation. `Waypoint` has no floor-transition concept, and Ch.1 ¶303 states
the library *"operates within a single-floor layout of manageable dimensions"* — the
manuscript rules it out, so no stairs/elevator modelling is needed.
