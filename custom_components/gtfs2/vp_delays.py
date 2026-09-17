"""Estimate TripUpdates from VehiclePositions, for realtime feeds that publish no TripUpdates.

The output has the shape of convert_gtfs_realtime_to_json's entities, so
get_rt_route_trip_statuses reads it like any other TripUpdate feed.
"""
from __future__ import annotations

import datetime
import math
import re
import threading
from collections import namedtuple

from sqlalchemy.sql import text

# Positions older than this many seconds are ignored.
MAX_POSITION_AGE = 120

# A vehicle within this many metres of a stop counts as standing at it.
STOP_RADIUS = 100

# Before its first departure, a vehicle this close to its first stop is laying over there;
# further away it has already set off.
LAYOVER_RADIUS = 300

# A trip cannot set off more than this many seconds before its first departure; a vehicle moving or
# placed on the route earlier is still finishing its previous run or repositioning at the terminus.
EARLY_DEPARTURE = 120

# When the feed names a stop the timetable does not have (railway codes, diversions), only a
# sighting counts: a vehicle this close to one of the trip's stops is taken to be at it, and
# nothing at all is inferred about where it is between stops.
NEAR_STOP_RADIUS = 200

# A match whose schedule is further than this many seconds from the vehicle is ignored.
MAX_SCHEDULE_DEVIATION = 3600

# A trip whose vehicle drops out (old position, feed error) keeps its last estimate this many seconds.
HOLD_LAST_ESTIMATE = 300

# A trip seen underway stays started for this many seconds after it was last seen underway.
REMEMBER_STARTED = 3 * 3600

# A vehicle that moves further than this many metres, away from its first stop, has set off.
MOVED_DISTANCE = 50

# Positions older than this many seconds are not compared to tell whether a vehicle moved.
MOVEMENT_WINDOW = 900

# Shared by all sensors, which Home Assistant refreshes in executor threads.
_memory_lock = threading.Lock()
_last_matches = {}
_started_trips = {}
_last_positions = {}
_stop_delays = {}

StopTime = namedtuple("StopTime", "stop_id sequence arrival departure lat lon name", defaults=(None,))

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
           s.stop_lat, s.stop_lon, t.route_id, t.direction_id, s.stop_name
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
            StopTime(row[0], row[1], parse_gtfs_seconds(row[2]), parse_gtfs_seconds(row[3]), row[4], row[5], row[8])
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


def nearest_stop(stops, lat, lon):
    """The trip's stop the vehicle is standing at, or None when it is not at one of them."""
    best_index, best_distance = None, None
    for index, stop in enumerate(stops):
        d = _distance_to_stop(lat, lon, stop)
        if d is not None and d <= NEAR_STOP_RADIUS and (best_distance is None or d < best_distance):
            best_index, best_distance = index, d
    return best_index


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


def locate(stops, k, lat, lon, t, departed=False, left_stop=None, seen_at=None):
    """Where a vehicle heading to stop k is, and its delay at service-day second t.

    Feeds report the stop a vehicle heads to, even while it still stands at stop k-1.
    departed: the vehicle was seen moving away from its first stop, so it no longer waits there.
    left_stop: (stop index, delay) from when the vehicle was last seen standing at a stop.
    seen_at: the vehicle was sighted at this stop, which settles where it is.

    Between stops the delay is how late the vehicle left its last stop, growing only once it is
    overdue at the next one: vehicles dwell, pull out and wait at lights, so assuming a steady
    pace between stops makes them look late.
    """
    heading = stops[k]
    previous = stops[k - 1] if k > 0 else None
    d_heading = _distance_to_stop(lat, lon, heading)
    d_previous = _distance_to_stop(lat, lon, previous) if previous else None

    first_departure = departure_of(stops[0])
    too_early = first_departure is not None and t < first_departure - EARLY_DEPARTURE
    departed = departed and not too_early

    at_index = None
    if seen_at is not None:
        at_index = seen_at
    elif d_previous is not None and d_previous <= STOP_RADIUS and not (departed and k == 1):
        at_index = k - 1
    elif d_heading is not None and d_heading <= STOP_RADIUS and not (departed and k == 0):
        at_index = k

    # Before its first departure a vehicle lays over near the terminus, often further than
    # STOP_RADIUS from the stop; with no earlier position to show it moving, distance decides.
    d_first = d_previous if k == 1 else d_heading if k == 0 else None
    if seen_at is None and not departed and first_departure is not None and t < first_departure and (
        too_early or (at_index is None and d_first is not None and d_first <= LAYOVER_RADIUS)
    ):
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
        start = departure_of(previous) if previous is not None else None
        if start is None:
            start = arrival
        delay = t - arrival
        if left_stop is not None and left_stop[0] == k - 1:
            delay = max(delay, left_stop[1])
        score = start - t if t < start else max(0, t - arrival)

    return {"at_index": at_index, "delay": delay, "score": score, "departed": departed}


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


def _position(vehicle, now_utc):
    """(lat, lon, timestamp) of a vehicle, or None when missing or older than MAX_POSITION_AGE."""
    timestamp = vehicle.get("timestamp") or now_utc.timestamp()
    if now_utc.timestamp() - timestamp > MAX_POSITION_AGE:
        return None
    position = vehicle.get("position") or {}
    lat, lon = position.get("latitude"), position.get("longitude")
    if lat is None or lon is None or (lat == 0 and lon == 0):
        return None
    return lat, lon, timestamp


def _label(vehicle):
    return str((vehicle.get("vehicle") or {}).get("label") or "")


def _moved_away(previous, current, first_stop, second_stop):
    """Whether a vehicle set off between two positions: further from its first stop and closer to its second."""
    if previous is None or first_stop.lat is None or first_stop.lon is None:
        return False
    (prev_lat, prev_lon, prev_time), (lat, lon, time_now) = previous, current
    if time_now <= prev_time or distance_m(prev_lat, prev_lon, lat, lon) <= MOVED_DISTANCE:
        return False
    if distance_m(lat, lon, first_stop.lat, first_stop.lon) <= distance_m(prev_lat, prev_lon, first_stop.lat, first_stop.lon):
        return False
    if second_stop is None or second_stop.lat is None or second_stop.lon is None:
        return True
    return distance_m(lat, lon, second_stop.lat, second_stop.lon) < distance_m(prev_lat, prev_lon, second_stop.lat, second_stop.lon)


def _match_vehicle(vehicle, position, stops, day_starts, departed, left_stop):
    lat, lon, timestamp = position

    k = stop_index(stops, vehicle.get("stop_id")) if vehicle.get("stop_id") else None
    seen_at = None
    if k is None:
        # The feed named a stop the timetable does not have. Only a sighting at one of the trip's
        # own stops settles anything; between stops nothing is known and nothing is invented.
        seen_at = nearest_stop(stops, lat, lon)
        if seen_at is None:
            return None
        k = min(seen_at + 1, len(stops) - 1)

    best = None
    for day_start in day_starts:
        located = locate(stops, k, lat, lon, timestamp - day_start.timestamp(), departed, left_stop, seen_at)
        if located is None or located["score"] > MAX_SCHEDULE_DEVIATION:
            continue
        if best is None or located["score"] < best["score"]:
            best = {
                **located,
                "k": k,
                "day_start": day_start,
                "label": _label(vehicle),
                "timestamp": timestamp,
            }
    return best


def _trip_update(trip_id, trip, match, now_utc, started, last_seen=None):
    stops = trip["stops"]
    at_index = match["at_index"]
    first = at_index if at_index is not None else match["k"]
    # Whole minutes rounded down, like timetables where any second of a stop's minute is on time,
    # so a time shown as HH:MM and the delay always agree.
    delay = max(0, math.floor(match["delay"] / 60) * 60)
    day_start = match["day_start"].timestamp()
    now = int(now_utc.timestamp())
    # How long since the feed last carried a position for this vehicle, which is not the same as
    # the last position we could place: a train between stations reports without being placeable.
    age = max(0, now - int(last_seen or match["timestamp"]))

    stop_time_updates = []
    for index in range(first, len(stops)):
        stop = stops[index]
        if arrival_of(stop) is None:
            continue
        # Nothing has reported the vehicle past this stop, so it cannot have called there in the
        # past: once the stop's time has passed, it is late by at least that much.
        overdue = math.floor((now - (day_start + departure_of(stop))) / 60) * 60
        stop_delay = max(delay, overdue)
        arrival = int(day_start + arrival_of(stop) + stop_delay)
        departure = int(day_start + departure_of(stop) + stop_delay)
        stop_time_updates.append({
            "stop_id": stop.stop_id,
            "stop_sequence": stop.sequence,
            "arrival": {"time": arrival, "delay": stop_delay},
            "departure": {"time": departure, "delay": stop_delay},
        })

    trip_descriptor = {"trip_id": trip_id, "route_id": trip["route_id"]}
    if trip["direction_id"] not in (None, ""):
        trip_descriptor["direction_id"] = str(trip["direction_id"])
    current = stops[at_index] if at_index is not None else stops[max(match["k"] - 1, 0)]
    return {
        "id": trip_id,
        "trip_update": {"trip": trip_descriptor, "stop_time_update": stop_time_updates},
        # Not part of a TripUpdate: where the vehicle is, for sensors that show it.
        "vehicle_position": {
            "label": match["label"],
            "current_stop": current.name or current.stop_id,
            "at_stop": at_index is not None,
            "started": started,
            "age": age,
        },
    }


def _is_started(match):
    return not (match["at_index"] == 0 or (match["at_index"] is None and match["k"] == 0))


def _forget_old(now):
    with _memory_lock:
        for trip_id in [t for t, (_, seen) in _last_matches.items() if now - seen > HOLD_LAST_ESTIMATE]:
            del _last_matches[trip_id]
        for trip_id in [t for t, seen in _started_trips.items() if now - seen > REMEMBER_STARTED]:
            del _started_trips[trip_id]
        for key in [key for key, (_, _, seen) in _last_positions.items() if now - seen > MOVEMENT_WINDOW]:
            del _last_positions[key]
        for trip_id in [t for t, (_, _, seen) in _stop_delays.items() if now - seen > MOVEMENT_WINDOW]:
            del _stop_delays[trip_id]


def derive_trip_updates(vehicle_entities, wanted, trip_loader, now_utc, time_zone):
    """TripUpdate entities for the wanted trips that have a vehicle in the feed, or had one moments ago.

    vehicle_entities: convert_gtfs_realtime_positions_to_json's entities (None when the feed failed).
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

    now = now_utc.timestamp()
    _forget_old(now)

    updates = []
    for trip_id in wanted:
        vehicles = vehicles_by_trip.get(trip_id)
        with _memory_lock:
            remembered = _last_matches.get(trip_id)
        if not vehicles and not remembered:
            continue
        trip = trip_loader(trip_id)
        if not trip or not trip["stops"]:
            continue

        match = None
        still_reporting = None
        if vehicles:
            day_starts = _service_day_starts(trip["stops"], wanted[trip_id], time_zone)
            matches = []
            for vehicle in vehicles:
                position = _position(vehicle, now_utc)
                if position is None:
                    continue
                still_reporting = max(still_reporting or 0, position[2])
                # Per label: two units in one vehicle report positions metres apart.
                key = (trip_id, _label(vehicle))
                with _memory_lock:
                    departed = trip_id in _started_trips or _moved_away(
                        _last_positions.get(key),
                        position,
                        trip["stops"][0],
                        trip["stops"][1] if len(trip["stops"]) > 1 else None,
                    )
                    _last_positions[key] = position
                    left_stop = _stop_delays.get(trip_id)
                found = _match_vehicle(
                    vehicle, position, trip["stops"], day_starts, departed, left_stop[:2] if left_stop else None
                )
                if found:
                    matches.append(found)
            # A vehicle can be reported twice under different labels; keep the one fitting the schedule best.
            if matches:
                match = min(matches, key=lambda m: m["score"])

        with _memory_lock:
            if match:
                _last_matches[trip_id] = (match, now)
                if match["at_index"] is not None:
                    _stop_delays[trip_id] = (match["at_index"], match["delay"], now)
            elif remembered:
                match = dict(remembered[0])
                if vehicles:
                    # The feed still carries this trip, so nothing has reported the vehicle past
                    # its stops: keep the row alive, with the delay last measured.
                    if still_reporting:
                        match = {**match, "timestamp": still_reporting}
                    if match["at_index"] is not None:
                        # It was last seen standing at a stop and is not there now, so it is on
                        # its way to the next one, carrying the delay it had when it left.
                        match = {**match, "k": min(match["at_index"] + 1, len(trip["stops"]) - 1), "at_index": None}
                    _last_matches[trip_id] = (match, now)
            else:
                continue
            # Back near the first stop after being underway is GPS noise, not a new start.
            started = _is_started(match) or match.get("departed", False) or trip_id in _started_trips
            if started:
                _started_trips[trip_id] = now

        last_seen = max((vehicle.get("timestamp") or 0) for vehicle in vehicles) if vehicles else None
        updates.append(_trip_update(trip_id, trip, match, now_utc, started, last_seen))
    return updates
