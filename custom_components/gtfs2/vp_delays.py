"""Estimate TripUpdates from VehiclePositions, for realtime feeds that publish no TripUpdates.

The output has the shape of convert_gtfs_realtime_to_json's entities, so
get_rt_route_trip_statuses reads it like any other TripUpdate feed.
"""
from __future__ import annotations

import datetime
import math
import re
from collections import namedtuple

from sqlalchemy.sql import text

# Positions older than this many seconds are ignored.
MAX_POSITION_AGE = 120

# A vehicle within this many metres of a stop counts as standing at it.
STOP_RADIUS = 100

# A vehicle whose reported stop is not on its trip is placed on the nearest
# stretch between two of the trip's stops, when within this many metres of it.
MAX_ROUTE_DISTANCE = 300

# A match whose schedule is further than this many seconds from the vehicle is ignored.
MAX_SCHEDULE_DEVIATION = 3600

StopTime = namedtuple("StopTime", "stop_id sequence arrival departure lat lon")

_LEADING_ZEROS = re.compile(r"(?<!\d)0+(?=\d)")


def has_trip_updates(feed_entities) -> bool:
    for entity in feed_entities or []:
        if not isinstance(entity, dict):
            return True
        trip_update = entity.get("trip_update")
        if trip_update and (trip_update.get("trip") or {}).get("trip_id"):
            return True
    return False


def canonical_stop_id(stop_id) -> str:
    # Some feeds pad the numbers in stop ids (U01208Z05 for U1208Z5).
    return _LEADING_ZEROS.sub("", str(stop_id or ""))


def parse_gtfs_seconds(value) -> int | None:
    """Seconds after the service day's start, from pygtfs' '1970-01-02 00:23:00.000000', 'HH:MM:SS' or a number."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, datetime.timedelta):
        return int(value.total_seconds())
    if isinstance(value, datetime.datetime):
        return int((value - datetime.datetime(1970, 1, 1)).total_seconds())
    value = str(value)
    days = 0
    if len(value) >= 19 and value[4] == "-":
        days = (datetime.date.fromisoformat(value[:10]) - datetime.date(1970, 1, 1)).days
        value = value[11:19]
    hours, minutes, seconds = (int(part) for part in value.split(":")[:3])
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def trip_loader_for(schedule):
    cache = {}

    def load(trip_id):
        if trip_id not in cache:
            cache[trip_id] = load_trip(schedule, trip_id)
        return cache[trip_id]

    return load


def load_trip(schedule, trip_id):
    sql = """
    SELECT st.stop_id, st.stop_sequence, st.arrival_time, st.departure_time,
           s.stop_lat, s.stop_lon, t.route_id, t.direction_id
    FROM stop_times st
    JOIN trips t ON t.trip_id = st.trip_id
    LEFT JOIN stops s ON s.stop_id = st.stop_id
    WHERE st.trip_id = :trip_id
    ORDER BY st.stop_sequence
    """
    with schedule.engine.connect() as conn:
        rows = conn.execute(text(sql), {"trip_id": trip_id}).fetchall()
    if not rows:
        return None
    return {
        "route_id": rows[0][6],
        "direction_id": rows[0][7],
        "stops": [
            StopTime(row[0], row[1], parse_gtfs_seconds(row[2]), parse_gtfs_seconds(row[3]), row[4], row[5])
            for row in rows
        ],
    }


def arrival_of(stop):
    return stop.arrival if stop.arrival is not None else stop.departure


def departure_of(stop):
    return stop.departure if stop.departure is not None else stop.arrival


def distance_m(lat1, lon1, lat2, lon2):
    x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    y = math.radians(lat2 - lat1)
    return 6371000 * math.hypot(x, y)


def _distance_to_stop(lat, lon, stop):
    if stop.lat is None or stop.lon is None:
        return None
    return distance_m(lat, lon, stop.lat, stop.lon)


def distance_to_segment(lat, lon, a, b):
    kx = 6371000 * math.cos(math.radians(lat)) * math.pi / 180
    ky = 6371000 * math.pi / 180
    ax, ay = (a.lon - lon) * kx, (a.lat - lat) * ky
    bx, by = (b.lon - lon) * kx, (b.lat - lat) * ky
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = 0 if length2 == 0 else max(0, min(1, -(ax * dx + ay * dy) / length2))
    return math.hypot(ax + t * dx, ay + t * dy)


def segment_from_position(stops, lat, lon):
    best_k, best_distance = None, None
    for k in range(1, len(stops)):
        a, b = stops[k - 1], stops[k]
        if None in (a.lat, a.lon, b.lat, b.lon):
            continue
        d = distance_to_segment(lat, lon, a, b)
        if d <= MAX_ROUTE_DISTANCE and (best_distance is None or d < best_distance):
            best_k, best_distance = k, d
    return best_k


def stop_index(stops, stop_id, stop_sequence=None):
    if stop_sequence is not None:
        for index, stop in enumerate(stops):
            if stop.sequence == stop_sequence and stop.stop_id == stop_id:
                return index
    for index, stop in enumerate(stops):
        if stop.stop_id == stop_id:
            return index
    wanted = canonical_stop_id(stop_id)
    for index, stop in enumerate(stops):
        if canonical_stop_id(stop.stop_id) == wanted:
            return index
    return None


def locate(stops, k, lat, lon, t):
    """Where a vehicle heading to stop k is, and its delay at service-day second t.

    Feeds report the stop a vehicle heads to, even while it still stands at stop k-1.
    """
    heading = stops[k]
    previous = stops[k - 1] if k > 0 else None
    d_heading = _distance_to_stop(lat, lon, heading)
    d_previous = _distance_to_stop(lat, lon, previous) if previous else None

    at_index = None
    if d_previous is not None and d_previous <= STOP_RADIUS:
        at_index = k - 1
    elif d_heading is not None and d_heading <= STOP_RADIUS:
        at_index = k

    # Before its first departure a vehicle lays over near the terminus,
    # often further than STOP_RADIUS from the stop.
    first_departure = departure_of(stops[0])
    if at_index is None and k <= 1 and first_departure is not None and t < first_departure:
        at_index = 0

    if at_index is not None:
        stop = stops[at_index]
        arrival, departure = arrival_of(stop), departure_of(stop)
        if arrival is None:
            return None
        # Standing early means waiting for departure, so no negative delay.
        if t < arrival:
            score, delay = arrival - t, 0
        elif t <= departure:
            score, delay = 0, 0
        else:
            score = delay = t - departure
    else:
        arrival = arrival_of(heading)
        if arrival is None:
            return None
        expected = arrival
        if previous is not None and d_previous is not None and d_heading is not None:
            start = departure_of(previous)
            if start is not None:
                fraction = d_previous / (d_previous + d_heading)
                expected = start + fraction * (arrival - start)
        delay = round(t - expected)
        score = abs(delay)

    return {"at_index": at_index, "delay": delay, "score": score}


def _service_day_starts(stops, references, time_zone):
    """The start ("noon minus 12 h") of each service day the references point at."""
    starts = {}
    for stop_id, stop_sequence, departure in references:
        if departure is None:
            continue
        index = stop_index(stops, stop_id, stop_sequence)
        if index is None or departure_of(stops[index]) is None:
            continue
        local = departure.astimezone(time_zone) - datetime.timedelta(seconds=departure_of(stops[index]))
        service_date = local.date()
        noon = datetime.datetime.combine(service_date, datetime.time(12), tzinfo=time_zone)
        starts[service_date] = noon - datetime.timedelta(hours=12)
    return list(starts.values())


def _match_vehicle(vehicle, stops, day_starts, now_utc):
    timestamp = vehicle.get("timestamp") or now_utc.timestamp()
    if now_utc.timestamp() - timestamp > MAX_POSITION_AGE:
        return None

    position = vehicle.get("position") or {}
    lat, lon = position.get("latitude"), position.get("longitude")
    if lat is None or lon is None or (lat == 0 and lon == 0):
        return None

    k = stop_index(stops, vehicle.get("stop_id")) if vehicle.get("stop_id") else None
    # The reported stop is sometimes not on the trip even though the vehicle is (e.g. a diversion).
    if k is None:
        k = segment_from_position(stops, lat, lon)
    if k is None:
        return None

    best = None
    for day_start in day_starts:
        located = locate(stops, k, lat, lon, timestamp - day_start.timestamp())
        if located is None or located["score"] > MAX_SCHEDULE_DEVIATION:
            continue
        if best is None or located["score"] < best["score"]:
            best = {**located, "k": k, "day_start": day_start}
    return best


def _trip_update(trip_id, trip, match, now_utc):
    stops = trip["stops"]
    at_index = match["at_index"]
    first = at_index if at_index is not None else match["k"]
    delay = max(0, int(match["delay"]))
    day_start = match["day_start"].timestamp()
    now = int(now_utc.timestamp())

    stop_time_updates = []
    for index in range(first, len(stops)):
        stop = stops[index]
        if arrival_of(stop) is None:
            continue
        arrival = int(day_start + arrival_of(stop) + delay)
        departure = int(day_start + departure_of(stop) + delay)
        if index == at_index:
            # still standing there, so it has not left yet
            departure = max(departure, now)
            arrival = min(arrival, departure)
        stop_time_updates.append({
            "stop_id": stop.stop_id,
            "stop_sequence": stop.sequence,
            "arrival": {"time": arrival, "delay": delay},
            "departure": {"time": departure, "delay": delay},
        })

    trip_descriptor = {"trip_id": trip_id, "route_id": trip["route_id"]}
    if trip["direction_id"] not in (None, ""):
        trip_descriptor["direction_id"] = str(trip["direction_id"])
    return {
        "id": trip_id,
        "trip_update": {"trip": trip_descriptor, "stop_time_update": stop_time_updates},
    }


def derive_trip_updates(vehicle_entities, wanted, trip_loader, now_utc, time_zone):
    """TripUpdate entities for the wanted trips that have a vehicle in the feed.

    vehicle_entities: convert_gtfs_realtime_positions_to_json's entities.
    wanted: {trip_id: [(stop_id, stop_sequence or None, scheduled departure as aware datetime), ...]},
            the departures a sensor shows, used to tell which service day a trip runs on.
    trip_loader: trip_id -> {"route_id", "direction_id", "stops": [StopTime, ...]} or None.
    """
    vehicles_by_trip = {}
    for entity in vehicle_entities or []:
        vehicle = entity.get("vehicle") or {}
        trip_id = str((vehicle.get("trip") or {}).get("trip_id") or "")
        if trip_id in wanted:
            vehicles_by_trip.setdefault(trip_id, []).append(vehicle)

    updates = []
    for trip_id, vehicles in vehicles_by_trip.items():
        trip = trip_loader(trip_id)
        if not trip or not trip["stops"]:
            continue
        day_starts = _service_day_starts(trip["stops"], wanted[trip_id], time_zone)
        # A vehicle can be reported twice under different labels; keep the one fitting the schedule best.
        matches = [m for m in (_match_vehicle(v, trip["stops"], day_starts, now_utc) for v in vehicles) if m]
        if matches:
            updates.append(_trip_update(trip_id, trip, min(matches, key=lambda m: m["score"]), now_utc))
    return updates
