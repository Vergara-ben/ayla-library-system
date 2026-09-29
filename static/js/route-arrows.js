/* The way to a shelf, drawn the same on the patron map and at the desk:
   a faint trail, and chevrons evenly spaced along it on screen. */
(function () {
    'use strict';

    var GAP_PX = 30;          // chevrons this far apart on screen, at any zoom
    var SHELF_W = 46, SHELF_D = 14;

    function chevron(bearing, big, color) {
        var size = big ? 26 : 18;
        return L.divIcon({
            className: 'route-arrow',
            html: '<svg width="' + size + '" height="' + size + '" viewBox="0 0 14 14" '
                + 'style="display:block;transform:rotate(' + bearing + 'deg)">'
                + '<path d="M3 2 L10 7 L3 12" fill="none" stroke="' + color + '" '
                + 'stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
            iconSize: [size, size], iconAnchor: [size / 2, size / 2],
        });
    }

    /* A shelf's outline in canvas units: its drawn footprint, or its rectangle. */
    function shelfOutline(sh) {
        if (sh.footprint && sh.footprint.length >= 3) return sh.footprint;
        if (sh.x == null || sh.y == null) return null;
        var rad = (sh.rotation || 0) * Math.PI / 180, cos = Math.cos(rad), sin = Math.sin(rad);
        var hw = (sh.width || SHELF_W) / 2, hd = (sh.depth || SHELF_D) / 2;
        return [[-hw, -hd], [hw, -hd], [hw, hd], [-hw, hd]].map(function (p) {
            return [sh.x + p[0] * cos - p[1] * sin, sh.y + p[0] * sin + p[1] * cos];
        });
    }

    function nearestOnOutline(p, poly) {
        var best = null, bestD = Infinity;
        for (var i = 0; i < poly.length; i++) {
            var a = poly[i], b = poly[(i + 1) % poly.length];
            var dx = b[0] - a[0], dy = b[1] - a[1];
            var len2 = dx * dx + dy * dy;
            var t = len2 ? Math.max(0, Math.min(1, ((p.x - a[0]) * dx + (p.y - a[1]) * dy) / len2)) : 0;
            var q = { x: a[0] + dx * t, y: a[1] + dy * t };
            var d = Math.hypot(q.x - p.x, q.y - p.y);
            if (d < bestD) { bestD = d; best = q; }
        }
        return best;
    }

    /* The walk ends at the aisle waypoint; finish it at the face of the shelf. */
    function intoShelf(points, shelf) {
        if (!points || !points.length || !shelf) return points;
        var last = points[points.length - 1];
        var poly = shelfOutline(shelf);
        var end;
        if (poly) {
            end = nearestOnOutline(last, poly);
        } else if (shelf.x != null) {
            end = { x: shelf.x, y: shelf.y };
        }
        if (!end) return points;
        var dx = end.x - last.x, dy = end.y - last.y;
        var dist = Math.hypot(dx, dy);
        if (!poly) {
            // Stop at the front of the shelf, not in its middle.
            var back = Math.min((shelf.depth || SHELF_D) / 2, dist);
            end = { x: end.x - dx / (dist || 1) * back, y: end.y - dy / (dist || 1) * back };
            dist -= back;
        }
        if (dist < 1) return points;
        return points.concat([{ x: end.x, y: end.y }]);
    }

    /* Draw onto a layer; returns what was drawn, so the caller can take it off again. */
    function draw(opts) {
        var map = opts.map, layer = opts.layer, pts = opts.points || [], ll = opts.toLatLng;
        var color = opts.color || '#1d4ed8';
        var drawn = [];
        if (pts.length < 2) return drawn;

        drawn.push(L.polyline(pts.map(function (p) { return ll(p.x, p.y); }), {
            color: color, weight: 8, opacity: 0.18, lineCap: 'round', lineJoin: 'round', interactive: false,
        }).addTo(layer));

        // Canvas units per screen pixel at this zoom.
        var gap = (opts.gapPx || GAP_PX) / Math.pow(2, map.getZoom() || 0);
        var segs = [], total = 0;
        for (var i = 0; i < pts.length - 1; i++) {
            var a = pts[i], b = pts[i + 1];
            var len = Math.hypot(b.x - a.x, b.y - a.y);
            if (len < 1e-6) continue;
            segs.push({ a: a, b: b, len: len, from: total,
                        bearing: Math.atan2(b.y - a.y, b.x - a.x) * 180 / Math.PI });
            total += len;
        }
        if (!segs.length) return drawn;

        // Evenly spaced, clear of both ends.
        var count = Math.max(1, Math.floor(total / gap));
        var step = total / (count + 1);
        var s = 0;
        for (var k = 1; k <= count; k++) {
            var at = step * k;
            while (s < segs.length - 1 && at > segs[s].from + segs[s].len) s++;
            var seg = segs[s], t = (at - seg.from) / seg.len;
            drawn.push(L.marker(ll(seg.a.x + (seg.b.x - seg.a.x) * t, seg.a.y + (seg.b.y - seg.a.y) * t), {
                icon: chevron(seg.bearing, false, color), interactive: false, keyboard: false,
            }).addTo(layer));
        }
        // A bigger one arriving at the end.
        var lastSeg = segs[segs.length - 1];
        var backT = Math.min(gap * 0.5, lastSeg.len * 0.5) / lastSeg.len;
        drawn.push(L.marker(ll(lastSeg.b.x - (lastSeg.b.x - lastSeg.a.x) * backT,
                               lastSeg.b.y - (lastSeg.b.y - lastSeg.a.y) * backT), {
            icon: chevron(lastSeg.bearing, true, color), interactive: false, keyboard: false,
        }).addTo(layer));
        return drawn;
    }

    window.AylaRoute = { draw: draw, intoShelf: intoShelf, shelfOutline: shelfOutline };
})();
