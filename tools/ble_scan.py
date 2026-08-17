"""Read BLE beacons through Windows' own Bluetooth stack.

Chrome's continuous scanning (``requestLEScan``) hangs on many Windows builds,
which leaves the browser probe unable to see anything even when the radio is
perfectly healthy. The values needed to configure a beacon -- its UUID, major,
minor and calibrated power -- do not have to come from a browser, though. This
reads them straight from WinRT and prints them in the order the beacon form
asks for them.

    python tools/ble_scan.py                    list beacons for 15 seconds
    python tools/ble_scan.py --seconds 30       scan for longer
    python tools/ble_scan.py --all              include non-beacon devices
    python tools/ble_scan.py --watch            live table, refreshes as it goes
    python tools/ble_scan.py --calibrate 1a2b   stand 1 m away, get tx_power

Nothing here talks to the database. It reports what the air actually contains
so the numbers typed into Floor Plan Management are measured rather than
guessed.
"""

import argparse
import asyncio
import statistics
import sys
import time

from bleak import BleakScanner

APPLE_COMPANY_ID = 0x004C
EDDYSTONE_UUID = '0000feaa-0000-1000-8000-00805f9b34fb'

# An Eddystone UID frame calibrates at 0 m, an iBeacon at 1 m. The 41 dB gap is
# the constant Google publishes for converting between the two, so both kinds
# of beacon can be compared on one scale.
EDDYSTONE_0M_TO_1M = 41

DEFAULT_PATH_LOSS_N = 2.0


def decode_ibeacon(manufacturer_data):
    """Return iBeacon fields, or None if this is some other Apple payload."""
    payload = manufacturer_data.get(APPLE_COMPANY_ID)
    # 0x02 0x15 is the iBeacon type-and-length prefix; Apple uses the same
    # company ID for AirDrop, Handoff and everything else, so without this
    # check every nearby Mac and iPhone looks like a beacon.
    if not payload or len(payload) < 23 or payload[0] != 0x02 or payload[1] != 0x15:
        return None

    raw_uuid = payload[2:18].hex()
    return {
        'kind': 'iBeacon',
        'uuid': '%s-%s-%s-%s-%s' % (raw_uuid[0:8], raw_uuid[8:12], raw_uuid[12:16],
                                    raw_uuid[16:20], raw_uuid[20:32]),
        'major': int.from_bytes(payload[18:20], 'big'),
        'minor': int.from_bytes(payload[20:22], 'big'),
        'tx_power': int.from_bytes(payload[22:23], 'big', signed=True),
        'namespace': None,
        'instance': None,
    }


def decode_eddystone(service_data):
    """Return Eddystone-UID fields, or None for URL/TLM frames we cannot place."""
    payload = service_data.get(EDDYSTONE_UUID)
    if not payload or len(payload) < 18 or payload[0] != 0x00:
        return None

    return {
        'kind': 'Eddystone',
        'uuid': EDDYSTONE_UUID,
        'major': None,
        'minor': None,
        # Reported at 0 m; shifted so it means the same thing as an iBeacon's.
        'tx_power': int.from_bytes(payload[1:2], 'big', signed=True) - EDDYSTONE_0M_TO_1M,
        'namespace': payload[2:12].hex(),
        'instance': payload[12:18].hex(),
    }


def estimate_distance(rssi, tx_power, path_loss_n):
    """Log-distance path loss -- the same formula the patron map uses."""
    if rssi is None or tx_power is None or not path_loss_n:
        return None
    try:
        return 10 ** ((tx_power - rssi) / (10.0 * path_loss_n))
    except (OverflowError, ZeroDivisionError):
        return None


class Sighting:
    """Every advertisement seen from one device, so RSSI can be averaged.

    A single reading is close to meaningless -- indoor RSSI swings 10 dB or more
    between packets from a beacon that has not moved. Judging anything from one
    number is how a working setup gets mistaken for a broken one.
    """

    def __init__(self, address, name, beacon):
        self.address = address
        self.name = name
        self.beacon = beacon
        self.readings = []
        self.first_seen = time.monotonic()
        self.last_seen = self.first_seen

    def record(self, rssi, beacon):
        if beacon:
            self.beacon = beacon
        if rssi is not None:
            self.readings.append(rssi)
        self.last_seen = time.monotonic()

    @property
    def mean_rssi(self):
        return statistics.fmean(self.readings) if self.readings else None

    @property
    def spread(self):
        if len(self.readings) < 2:
            return 0
        return max(self.readings) - min(self.readings)


def format_table(sightings, path_loss_n, show_all):
    lines = []
    beacons = [s for s in sightings.values() if s.beacon]
    others = [s for s in sightings.values() if not s.beacon]
    beacons.sort(key=lambda s: s.mean_rssi if s.mean_rssi is not None else -999,
                 reverse=True)

    if not beacons:
        lines.append('  No beacons recognised yet.')
    for index, s in enumerate(beacons, 1):
        b = s.beacon
        distance = estimate_distance(s.mean_rssi, b['tx_power'], path_loss_n)
        lines.append('')
        lines.append('  [%d] %s   %s' % (index, b['kind'], s.name or s.address))
        lines.append('      Beacon UUID   %s' % b['uuid'])
        if b['major'] is not None:
            lines.append('      Major         %d' % b['major'])
            lines.append('      Minor         %d' % b['minor'])
        if b['namespace']:
            lines.append('      Namespace     %s' % b['namespace'])
            lines.append('      Instance      %s' % b['instance'])
        lines.append('      Tx power      %d dBm  (advertised, at 1 m)' % b['tx_power'])
        lines.append('      Signal        %.1f dBm  (%d packets, spread %d dB)'
                     % (s.mean_rssi, len(s.readings), s.spread))
        if distance is not None:
            lines.append('      Distance      ~%.2f m  at n=%.1f' % (distance, path_loss_n))

    if show_all and others:
        lines.append('')
        lines.append('  Other devices (not beacons):')
        for s in sorted(others, key=lambda s: s.mean_rssi or -999, reverse=True)[:20]:
            lines.append('      %-18s %-24s %s dBm'
                         % (s.address, (s.name or '')[:24],
                            '%.0f' % s.mean_rssi if s.mean_rssi is not None else '?'))
    return '\n'.join(lines)


async def scan(seconds, path_loss_n, show_all, watch):
    sightings = {}

    def on_detection(device, advertisement_data):
        beacon = (decode_ibeacon(advertisement_data.manufacturer_data)
                  or decode_eddystone(advertisement_data.service_data))
        key = device.address
        if key not in sightings:
            sightings[key] = Sighting(device.address,
                                      advertisement_data.local_name or device.name,
                                      beacon)
        sightings[key].record(advertisement_data.rssi, beacon)

    print('Scanning for %d seconds. Ctrl+C to stop early.\n' % seconds)
    scanner = BleakScanner(detection_callback=on_detection, scanning_mode='active')

    await scanner.start()
    try:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            await asyncio.sleep(1.0 if watch else 0.5)
            if watch:
                # Redrawing beats scrollback when the point is to walk around
                # and watch one number move.
                print('\033[2J\033[H', end='')
                remaining = int(deadline - time.monotonic())
                print('Live scan -- %ds left, %d device(s) seen\n'
                      % (remaining, len(sightings)))
                print(format_table(sightings, path_loss_n, show_all))
    except KeyboardInterrupt:
        pass
    finally:
        await scanner.stop()

    return sightings


async def calibrate(match, seconds):
    """Average RSSI from one beacon, to fill in tx_power honestly.

    tx_power is not a spec sheet number. It is what this radio reads from that
    beacon at one metre, in this room, and it is the single value that decides
    whether every distance the map computes is right or uniformly wrong.
    """
    readings = []
    found = {'name': None, 'advertised': None}
    needle = match.lower()

    def on_detection(device, advertisement_data):
        beacon = (decode_ibeacon(advertisement_data.manufacturer_data)
                  or decode_eddystone(advertisement_data.service_data))
        if not beacon:
            return
        haystack = ' '.join(filter(None, [
            device.address, advertisement_data.local_name or device.name or '',
            beacon['uuid'], str(beacon['major']), str(beacon['minor']),
            beacon['namespace'] or '', beacon['instance'] or '',
        ])).lower()
        if needle not in haystack:
            return
        found['name'] = advertisement_data.local_name or device.name or device.address
        found['advertised'] = beacon['tx_power']
        if advertisement_data.rssi is not None:
            readings.append(advertisement_data.rssi)

    print('Hold the scanning laptop exactly 1 metre from the beacon,')
    print('with nothing between them, and keep still for %d seconds.\n' % seconds)
    for count in (3, 2, 1):
        print('  starting in %d...' % count)
        await asyncio.sleep(1)
    print()

    scanner = BleakScanner(detection_callback=on_detection, scanning_mode='active')
    await scanner.start()
    try:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            print('\r  %d packets, %ds left   ' % (len(readings),
                                                   int(deadline - time.monotonic())),
                  end='', flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        await scanner.stop()
    print('\n')

    if not readings:
        print('No matching beacon heard. Check the text you searched for --')
        print('run a plain scan first and copy part of the UUID or name.')
        return 1

    mean = statistics.fmean(readings)
    median = statistics.median(readings)
    print('  %s' % found['name'])
    print('  packets      %d' % len(readings))
    print('  mean RSSI    %.1f dBm' % mean)
    print('  median RSSI  %.1f dBm' % median)
    print('  spread       %d dB' % (max(readings) - min(readings)))
    if found['advertised'] is not None:
        print('  advertised   %d dBm' % found['advertised'])
    print()
    # The median resists the odd dropped packet reading 20 dB low, which a mean
    # does not, and those outliers are common indoors.
    print('  ==> Enter %d as Tx power for this beacon.' % round(median))
    if max(readings) - min(readings) > 15:
        print('      Spread is wide -- something is moving or reflecting.')
        print('      Re-run holding still, away from metal shelving.')
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--seconds', type=int, default=15,
                        help='how long to scan (default 15)')
    parser.add_argument('--all', action='store_true',
                        help='also list devices that are not beacons')
    parser.add_argument('--watch', action='store_true',
                        help='refresh the table while scanning')
    parser.add_argument('--path-loss', type=float, default=DEFAULT_PATH_LOSS_N,
                        help='n for the distance estimate (default %.1f)' % DEFAULT_PATH_LOSS_N)
    parser.add_argument('--calibrate', metavar='TEXT',
                        help='measure tx_power for the beacon matching TEXT')
    args = parser.parse_args()

    try:
        if args.calibrate:
            return asyncio.run(calibrate(args.calibrate, args.seconds))

        sightings = asyncio.run(scan(args.seconds, args.path_loss, args.all, args.watch))
        beacons = [s for s in sightings.values() if s.beacon]
        print('\n' + '=' * 62)
        print('  %d device(s) heard, %d recognised beacon(s)'
              % (len(sightings), len(beacons)))
        print('=' * 62)
        print(format_table(sightings, args.path_loss, args.all))
        if beacons:
            print('\n  Paste these into Floor Plan Management -> beacon -> ')
            print('  "Edit identity & calibration". Then measure real tx_power with:')
            print('      python tools/ble_scan.py --calibrate <part of the UUID>')
        else:
            print('\n  Nothing recognised. If the beacons are powered on, they may be')
            print('  advertising a format this does not decode -- run with --all to')
            print('  see every device, and check the vendor app for the frame type.')
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - surfaced to the user, not swallowed
        print('\nScan failed: %s' % exc, file=sys.stderr)
        print('Check that Bluetooth is switched on in Windows Settings.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
