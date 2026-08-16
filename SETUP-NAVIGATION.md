# Setting up indoor navigation

Everything below is configuration, not code. The routing, positioning and map
are built and tested; none of them can do anything until the library's own
floor plan, scale, waypoints and beacons exist in the database.

Work through the steps in order — each one depends on the one before it.
After every step there is a **check** that tells you it worked, so a mistake is
caught where it happened rather than three steps later.

---

## Step 0 — Prove Bluetooth can work at all (do this first, costs nothing)

This takes five minutes, needs no beacons, and can save you from buying
hardware for an approach the browser will not allow.

1. On the **laptop**, start the server and open
   `http://localhost:8000/patron/map/` in **Chrome**.
   `localhost` counts as a secure address, so Bluetooth is allowed there.
2. Sign in as a patron and press the Bluetooth button.

| What you see | What it means |
| --- | --- |
| **"BLE: Scanning…"** | The browser can do this. Carry on. |
| **"Bluetooth scanning is switched off"** | Chrome has Bluetooth but not scanning. Open `chrome://flags`, enable **Experimental Web Platform features**, restart Chrome, try again. |
| **"BLE not supported by this browser"** | This browser cannot do it at all. Use Chrome. |
| **"Needs a secure (https) address"** | You are not on `localhost`. See Step 1. |

Then repeat the same test on the **Android phone**, which needs Step 1 first.

> **iPhone and iPad cannot do this in any browser, including Chrome.** Apple
> does not expose Bluetooth to web pages, and every iOS browser is required to
> use Apple's engine. iPhone users get the map and shelf locations, but no
> live position. This is a platform limit, not a fault in the system.

---

## Step 1 — Serve the site over https

**Bluetooth only works on a secure address.** `localhost` is treated as secure;
`http://192.168.1.5:8000` is not. On a plain `http://` address the browser hides
Bluetooth completely, and the page will say *"Needs a secure (https) address"*.

This is the single most common reason a phone test fails, so do it before
blaming the beacons.

The quickest option for a demo is a tunnel, which gives you a real https address
with no certificate work:

```bash
cloudflared tunnel --url http://localhost:8000
```

It prints an `https://…trycloudflare.com` address. Open **that** on the phone.

Add the hostname it gives you to `ALLOWED_HOSTS` in your `.env`, or run with
`DEBUG=true` while testing.

**Check:** on the phone, the map loads over `https://` and the Bluetooth button
no longer says *"Needs a secure (https) address"*.

---

## Step 2 — Activate the floor plan

Right now the plan named `a` exists but is switched off, so patrons get
*"No floor plan available"* and route requests answer `No active floor plan`.
Nothing else works until this is on.

1. **Admin portal → Floor Plans**
2. Open the plan and set it **Active**.

**Check:** open the patron map. You should see the room and the shelf drawn,
with no error banner.

---

## Step 3 — Set the map scale

Beacons report distance in **metres**. The map stores everything in **canvas
units**. Without a number connecting the two, positioning refuses to run —
deliberately, because a guessed scale produces a confident and wrong dot.

1. Pick something you can measure in the real room — a shelf run, a wall.
   Measure it in **metres**.
2. On the floor plan, read how many **canvas units** the same span covers.
3. `pixels_per_meter = canvas units ÷ metres`.

   *Example:* a shelf 2.5 m long drawn 46 units wide → `46 ÷ 2.5 = 18.4`.

4. **Admin portal → Indoor Map → Set scale.**

**Check:** the patron map no longer says *"Map scale not set — ask an
administrator"* when Bluetooth is on.

---

## Step 4 — Draw the waypoint graph

This is what the route follows. Without it there is no path, however good the
position is. A waypoint is a point a person can stand; a connection is a step
they can take between two of them.

1. **Admin portal → Indoor Map**
2. Drop waypoints along the walkable aisles — every junction, every aisle end,
   and the entrance. Corners matter more than spacing.
3. Connect neighbouring waypoints that a person can actually walk between.
   **Do not connect through a shelf**: the route will happily walk through it.
4. Put a waypoint near each shelf, so a shelf has something to route *to*.

Rules of thumb: one waypoint every 2–3 metres along an aisle, one at every
junction, and never a connection that crosses a wall or a shelf.

**Check:** Admin → Indoor Map shows waypoints joined by lines, and no line
crosses a shelf or a wall.

---

## Step 5 — Place and configure the beacons

You need **at least three beacons within range** of anywhere you want a
position, and they must **not sit in a straight line** — three collinear
beacons make the maths degenerate, and the system correctly refuses to guess
rather than showing a wrong dot. Spread them around the edges of the room.

For each beacon:

1. **Find out how it identifies itself.** Use a scanner app (nRF Connect,
   BLE Scanner) standing next to it, and note:
   - **iBeacon** → proximity UUID, **major**, **minor**
   - **Eddystone-UID** → namespace ID and instance ID
   - Neither → its service UUID or device name
2. **Admin portal → Indoor Map → add a beacon**, and enter the type and those
   identifiers exactly. A single wrong digit means the beacon is never matched,
   and it looks exactly like broken hardware.
3. **Place it on the map** where it physically is.
4. **Calibrate it:**
   - **`tx_power`** — stand **exactly one metre** away, read the RSSI in the
     scanner app, enter that number (a negative value, usually −59 to −65).
     Do this per beacon; they differ even within one pack.
   - **`path_loss_n`** — how fast the signal fades. Start at **2.5** for a room
     with metal shelving; 2.0 is open space, 3.5 is cluttered. Adjust after
     testing.

**Check:** on the patron map the status line counts up —
*"BLE: 2 of 3 beacons seen — need 3 to fix a position"*. If it stays at 0 with
beacons switched on, the identifiers in Step 5.2 are wrong.

---

## Step 6 — Walk it

1. Stand at a known spot. The dot should land within a few metres.
2. Walk to another known spot and watch it follow.
3. Search for a book and check the drawn route goes down the aisles rather
   than through a shelf.

**Expect a few metres of error, and expect the dot to wander while you stand
still.** That is how RSSI positioning behaves — it is not a defect, and it is
worth saying so plainly in the defense rather than being caught out by it.

If it is consistently too far or too near, adjust `path_loss_n`: **raise** it if
the dot reads further away than you are, **lower** it if it reads nearer.

---

## When something does not work

| Symptom | Cause |
| --- | --- |
| "Needs a secure (https) address" | Page opened over `http://`. Step 1. |
| "BLE not supported by this browser" | Not Chrome, or it is an iPhone/iPad. |
| "Bluetooth scanning is switched off" | Enable the Chrome experimental flag. Step 0. |
| "Map scale not set" | Step 3. |
| "0 of N beacons seen" | Beacon identifiers are wrong. Step 5.2. |
| Position jumps around wildly | Fewer than 3 beacons in range, or they are in a line. Step 5. |
| Position is steadily too far / too near | `tx_power` or `path_loss_n` needs adjusting. Step 5.4. |
| "No active floor plan" | Step 2. |
| Route walks through a shelf | A waypoint connection crosses it. Step 4.3. |
| No route at all, but the dot works | No waypoints near that shelf. Step 4.4. |

---

## What the panel will ask

- **"Why not iPhone?"** — Apple does not expose Bluetooth to web pages in any
  iOS browser. Any web-based indoor navigation has this constraint; Android
  scope is common in published indoor-positioning work for the same reason.
- **"How accurate is it?"** — a few metres, and it drifts. Enough to find the
  right aisle, not the right book on the shelf. Say the number you measured in
  Step 6 rather than a number from a paper.
- **"What if a beacon dies?"** — with three, positioning stops and the status
  line says how many are seen. The map and shelf locations keep working.
