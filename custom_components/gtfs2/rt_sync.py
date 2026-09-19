"""Keep the imported timetable paired with the realtime feed.

Some providers number trips per export, so a rebuild renumbers every trip: the feed's trip ids then
resolve to other lines in the imported timetable and every realtime field quietly empties. Nothing
in either file says which export it came from, so the pairing is measured instead - a vehicle on the
trip it claims stands near one of that trip's stops, and one on a renumbered trip does not, usually
by kilometres. When the measurement collapses the datasource is re-extracted, which is the only
thing that can restore it.
"""
from __future__ import annotations

import logging
import threading

from .vp_delays import distance_m

_LOGGER = logging.getLogger(__name__)

# A vehicle this close to a stop of the trip it reports counts as being on that trip.
MATCH_RADIUS = 200

# Vehicles measured per refresh, and the fewest worth judging a feed on.
MATCH_SAMPLE = 60
MIN_SAMPLE = 20

# Below this share the timetable and the feed are not the same export; above it they are. Paired
# feeds measure around 70%, mismatched ones a few percent, so anything between is left alone.
UNPAIRED_RATE = 0.2

# Consecutive measurements below that share before re-extracting: a single quiet or odd refresh
# (a depot full of parked vehicles, a feed hiccup) is not evidence.
UNPAIRED_REFRESHES = 3

# The datasource is re-extracted at most this often, whatever the measurements say.
MIN_RESYNC_INTERVAL = 6 * 3600

_lock = threading.Lock()
_unpaired = {}
_last_resync = {}


def match_rate(vehicle_entities, trip_loader):
    """Share of sampled vehicles standing near a stop of the trip they report, and how many were used.

    Returns (rate, sampled). A rate of None means the feed carried too few placeable vehicles.
    """
    vehicles = []
    for entity in vehicle_entities or []:
        vehicle = entity.get("vehicle") or {}
        trip_id = str((vehicle.get("trip") or {}).get("trip_id") or "")
        position = vehicle.get("position") or {}
        lat, lon = position.get("latitude"), position.get("longitude")
        if trip_id and lat is not None and lon is not None and not (lat == 0 and lon == 0):
            vehicles.append((trip_id, lat, lon))
    if not vehicles:
        return None, 0
    step = max(1, len(vehicles) // MATCH_SAMPLE)
    matched = sampled = 0
    for trip_id, lat, lon in vehicles[::step][:MATCH_SAMPLE]:
        trip = trip_loader(trip_id)
        if not trip or not trip["stops"]:
            continue
        distances = [
            distance_m(lat, lon, stop.lat, stop.lon)
            for stop in trip["stops"]
            if stop.lat is not None and stop.lon is not None
        ]
        if not distances:
            continue
        sampled += 1
        matched += min(distances) <= MATCH_RADIUS
    if sampled < MIN_SAMPLE:
        return None, sampled
    return matched / sampled, sampled


def should_resync(name, rate, now):
    """Whether the datasource has looked unpaired for long enough to be worth re-extracting."""
    if rate is None:
        return False
    with _lock:
        if rate > UNPAIRED_RATE:
            _unpaired.pop(name, None)
            return False
        _unpaired[name] = _unpaired.get(name, 0) + 1
        if _unpaired[name] < UNPAIRED_REFRESHES:
            return False
        last = _last_resync.get(name)
        if last is not None and now - last < MIN_RESYNC_INTERVAL:
            return False
        _last_resync[name] = now
        _unpaired.pop(name, None)
    return True


def forget(name=None):
    """Drop what is remembered about a datasource, or about all of them."""
    with _lock:
        if name is None:
            _unpaired.clear()
            _last_resync.clear()
        else:
            _unpaired.pop(name, None)
            _last_resync.pop(name, None)
