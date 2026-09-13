"""TripUpdates estimated from vehicle positions, for feeds that publish none (vp_delays.py)."""
from __future__ import annotations

import datetime
import zoneinfo
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from freezegun import freeze_time

import ha_stub

ha_stub.install()

vp = ha_stub.load("vp_delays")
gtfs_rt_helper = ha_stub.load("gtfs_rt_helper")

TZ = zoneinfo.ZoneInfo("Europe/Prague")
UTC = datetime.timezone.utc


def at(hour, minute, second=0, day=13):
    return datetime.datetime(2026, 9, day, hour, minute, second, tzinfo=TZ)


def seconds(hour, minute):
    return hour * 3600 + minute * 60


# Four stops 0.01 degree of longitude (about 730 m) apart, three minutes apart.
TRIP = {
    "route_id": "R31",
    "direction_id": 0,
    "stops": [
        vp.StopTime("U1Z1", 1, seconds(17, 0), seconds(17, 0), 49.0, 16.00),
        vp.StopTime("U2Z1", 2, seconds(17, 3), seconds(17, 3), 49.0, 16.01),
        vp.StopTime("U3Z1", 3, seconds(17, 6), seconds(17, 6), 49.0, 16.02),
        vp.StopTime("U4Z1", 4, seconds(17, 9), seconds(17, 9), 49.0, 16.03),
    ],
}
WANTED = {"T1": [("U3Z1", None, at(17, 6))]}
LOADER = {"T1": TRIP}.get


@pytest.fixture(autouse=True)
def fresh_memory():
    vp._last_matches.clear()
    vp._started_trips.clear()
    vp._last_positions.clear()
    yield


def vehicle(lon, stop_id, when, label="3664", trip_id="T1", lat=49.0):
    return {
        "vehicle": {
            "trip": {"trip_id": trip_id, "route_id": "", "direction_id": 0},
            "vehicle": {"id": label + "1", "label": label},
            "position": {"latitude": lat, "longitude": lon, "bearing": 90, "speed": 10},
            "stop_id": stop_id,
            "timestamp": int(when.timestamp()),
        }
    }


def derive(vehicles, now, wanted=WANTED, loader=LOADER):
    return vp.derive_trip_updates(vehicles, wanted, loader, now.astimezone(UTC), TZ)


def first_update(updates):
    return updates[0]["trip_update"]["stop_time_update"][0]


def test_canonical_stop_id():
    assert vp.canonical_stop_id("U01208Z05") == "U1208Z5"
    assert vp.canonical_stop_id("U1208Z5") == "U1208Z5"
    assert vp.canonical_stop_id("100") == "100"


def test_parse_gtfs_seconds():
    assert vp.parse_gtfs_seconds("1970-01-01 04:14:00.000000") == seconds(4, 14)
    assert vp.parse_gtfs_seconds("1970-01-02 00:23:00.000000") == seconds(24, 23)
    assert vp.parse_gtfs_seconds("24:16:00") == seconds(24, 16)
    assert vp.parse_gtfs_seconds(61200) == 61200
    assert vp.parse_gtfs_seconds(None) is None


def test_has_trip_updates():
    vehicle_feed_read_as_trip_updates = [{"id": "1", "trip_update": {"trip": {"trip_id": ""}, "stop_time_update": []}}]
    assert not vp.has_trip_updates(vehicle_feed_read_as_trip_updates)
    assert not vp.has_trip_updates(None)
    assert vp.has_trip_updates([{"id": "1", "trip_update": {"trip": {"trip_id": "T1"}}}])


def test_delay_from_position_between_stops():
    # halfway between 17:00 and 17:03, a minute behind
    updates = derive([vehicle(16.005, "U2Z1", at(17, 2, 30))], at(17, 2, 30))
    stop = first_update(updates)
    assert stop["stop_id"] == "U2Z1"
    assert stop["arrival"] == {"time": int(at(17, 4).timestamp()), "delay": 60}
    trip = updates[0]["trip_update"]["trip"]
    assert trip == {"trip_id": "T1", "route_id": "R31", "direction_id": "0"}
    assert updates[0]["vehicle_position"] == {
        "label": "3664", "current_stop": "U1Z1", "at_stop": False, "started": True,
    }


def test_delay_is_whole_minutes_and_moves_the_time_by_the_same():
    # halfway between the stops the vehicle is expected at 17:01:30
    late = derive([vehicle(16.005, "U2Z1", at(17, 6, 20))], at(17, 6, 20))
    assert first_update(late)["arrival"] == {"time": int(at(17, 8).timestamp()), "delay": 300}
    slightly_late = derive([vehicle(16.005, "U2Z1", at(17, 2, 20))], at(17, 2, 20))
    assert first_update(slightly_late)["arrival"] == {"time": int(at(17, 4).timestamp()), "delay": 60}
    nearly_on_time = derive([vehicle(16.005, "U2Z1", at(17, 1, 50))], at(17, 1, 50))
    assert first_update(nearly_on_time)["arrival"] == {"time": int(at(17, 3).timestamp()), "delay": 0}


def test_padded_stop_id_in_feed():
    updates = derive([vehicle(16.005, "U02Z01", at(17, 2, 30))], at(17, 2, 30))
    assert first_update(updates)["stop_id"] == "U2Z1"


def test_waiting_at_terminus_before_departure_is_on_time():
    # parked 130 m from the first stop, feed already names the second
    updates = derive([vehicle(16.0018, "U2Z1", at(16, 50))], at(16, 50))
    stop = first_update(updates)
    assert stop["stop_id"] == "U1Z1"
    assert stop["departure"] == {"time": int(at(17, 0).timestamp()), "delay": 0}
    assert updates[0]["vehicle_position"] == {
        "label": "3664", "current_stop": "U1Z1", "at_stop": True, "started": False,
    }


def test_vehicle_leaving_a_little_early_has_started_from_its_first_stop():
    # 500 m past the first stop, 30 s before its scheduled departure
    updates = derive([vehicle(16.0068, "U2Z1", at(16, 59, 30))], at(16, 59, 30))
    assert first_update(updates)["stop_id"] == "U2Z1"
    assert first_update(updates)["arrival"]["delay"] == 0
    assert updates[0]["vehicle_position"] == {
        "label": "3664", "current_stop": "U1Z1", "at_stop": False, "started": True,
    }


def test_parked_bus_that_moves_away_from_its_first_stop_has_started():
    # parked 130 m from the first stop, then 205 m from it, towards the second stop, a minute later
    parked = derive([vehicle(16.0018, "U2Z1", at(16, 58))], at(16, 58))
    assert parked[0]["vehicle_position"] == {
        "label": "3664", "current_stop": "U1Z1", "at_stop": True, "started": False,
    }
    leaving = derive([vehicle(16.0028, "U2Z1", at(16, 59))], at(16, 59))
    assert leaving[0]["vehicle_position"] == {
        "label": "3664", "current_stop": "U1Z1", "at_stop": False, "started": True,
    }
    assert first_update(leaving)["stop_id"] == "U2Z1"


def test_bus_moving_to_its_layover_spot_long_before_departure_has_not_started():
    # unloads at the first stop, then parks 130 m further on, twenty minutes before its next trip
    derive([vehicle(16.0005, "U2Z1", at(16, 40))], at(16, 40))
    parking = derive([vehicle(16.0018, "U2Z1", at(16, 41))], at(16, 41))
    assert parking[0]["vehicle_position"]["started"] is False
    assert parking[0]["vehicle_position"]["current_stop"] == "U1Z1"


def test_vehicle_still_on_its_previous_run_has_not_started():
    # already assigned this trip, but driving the route's streets 1.8 km away, fifteen minutes early
    updates = derive([vehicle(16.025, "U99Z1", at(16, 45))], at(16, 45))
    assert updates[0]["vehicle_position"]["started"] is False
    assert first_update(updates)["stop_id"] == "U1Z1"
    assert first_update(updates)["departure"] == {"time": int(at(17, 0).timestamp()), "delay": 0}


def test_bus_pulling_up_to_its_first_stop_has_not_started():
    derive([vehicle(16.0040, "U2Z1", at(16, 55))], at(16, 55))
    at_the_stop = derive([vehicle(16.0010, "U2Z1", at(16, 56))], at(16, 56))
    assert at_the_stop[0]["vehicle_position"]["started"] is False


def test_gps_jitter_while_parked_is_not_a_departure():
    derive([vehicle(16.0018, "U2Z1", at(16, 55))], at(16, 55))
    jitter = derive([vehicle(16.0022, "U2Z1", at(16, 56))], at(16, 56))
    assert jitter[0]["vehicle_position"]["started"] is False


def test_standing_late_at_stop_departs_now():
    now = at(17, 5)
    updates = derive([vehicle(16.0101, "U3Z1", now)], now)
    stop = first_update(updates)
    assert stop["stop_id"] == "U2Z1"
    assert stop["departure"]["delay"] == 120
    assert stop["departure"]["time"] == int(now.timestamp())


def test_reported_stop_not_on_trip_uses_position():
    updates = derive([vehicle(16.005, "U99Z1", at(17, 2, 30))], at(17, 2, 30))
    assert first_update(updates)["arrival"]["delay"] == 60


def test_stale_position_is_ignored():
    assert derive([vehicle(16.005, "U2Z1", at(16, 57))], at(17, 2, 30)) == []


def test_vehicle_reported_twice_keeps_best_schedule_fit():
    right = vehicle(16.005, "U2Z1", at(17, 2, 30), label="2020")
    wrong = vehicle(16.028, "U4Z1", at(17, 2, 30), label="21111")
    updates = derive([wrong, right], at(17, 2, 30))
    assert len(updates) == 1
    assert first_update(updates)["stop_id"] == "U2Z1"


def test_passed_stops_are_not_reported():
    updates = derive([vehicle(16.025, "U4Z1", at(17, 7))], at(17, 7))
    assert [s["stop_id"] for s in updates[0]["trip_update"]["stop_time_update"]] == ["U4Z1"]


def test_trip_past_midnight_uses_previous_service_day():
    night = {
        "route_id": "N96",
        "direction_id": 1,
        "stops": [
            vp.StopTime("U1Z1", 1, seconds(24, 10), seconds(24, 10), 49.0, 16.00),
            vp.StopTime("U2Z1", 2, seconds(24, 16), seconds(24, 16), 49.0, 16.01),
        ],
    }
    wanted = {"N1": [("U2Z1", None, at(0, 16, day=14))]}
    now = at(0, 13, day=14)
    updates = derive([vehicle(16.005, "U2Z1", now, trip_id="N1")], now, wanted, {"N1": night}.get)
    assert first_update(updates)["arrival"] == {"time": int(at(0, 16, day=14).timestamp()), "delay": 0}


def test_vehicle_dropping_out_keeps_its_last_estimate_for_a_while():
    derive([vehicle(16.005, "U2Z1", at(17, 2, 30))], at(17, 2, 30))

    stale = derive([vehicle(16.005, "U2Z1", at(17, 2, 30))], at(17, 6))
    assert first_update(stale)["arrival"]["delay"] == 60
    assert stale[0]["vehicle_position"]["current_stop"] == "U1Z1"

    feed_failed = derive(None, at(17, 7))
    assert first_update(feed_failed)["arrival"]["delay"] == 60

    assert derive(None, at(17, 7, 31)) == []


def test_started_trip_does_not_go_back_to_not_started():
    underway = derive([vehicle(16.005, "U2Z1", at(17, 1))], at(17, 1))
    assert underway[0]["vehicle_position"]["started"] is True

    # GPS puts it back within reach of the first stop
    jitter = derive([vehicle(16.0005, "U2Z1", at(17, 1, 20))], at(17, 1, 20))
    assert jitter[0]["vehicle_position"]["started"] is True


def _route_sensor(stop_id="U3Z1"):
    return SimpleNamespace(
        _vehicle_position_url=None,
        _trip_update_url="https://example.invalid/feed",
        _headers=None,
        _rt_group="route",
        _route_id="R31",
        _route_delimiter=None,
        _trip_id="T1",
        _trip_short_name=None,
        _trip_list=["T1"],
        _direction="0",
        _stop_id=stop_id,
        _stop_sequence=3,
        hass=SimpleNamespace(config=SimpleNamespace(time_zone="Europe/Prague")),
        _data={
            "schedule": "FAKE_SCHEDULE",
            "next_departure": {
                "next_departures_trip_id": ["T1"],
                "next_departures": [at(17, 6).astimezone(UTC).isoformat()],
            },
        },
    )


def test_route_sensor_reads_the_estimate():
    now = at(17, 2, 30)
    with freeze_time(now.astimezone(UTC).replace(tzinfo=None), tz_offset=0):
        updates = derive([vehicle(16.005, "U2Z1", now)], now)
        result = gtfs_rt_helper.get_rt_route_trip_statuses(_route_sensor(), updates)
    at_stop = result["R31"]["0"]["U3Z1"]
    assert at_stop["departures"][0].timestamp() == at(17, 7).timestamp()
    assert at_stop["delays"] == [60]
    assert at_stop["vehicles"] == [{"label": "3664", "current_stop": "U1Z1", "at_stop": False, "started": True}]


def test_route_sensor_falls_back_to_vehicle_positions():
    now = at(17, 2, 30)
    feeds = {
        "trip_data": [{"id": "1", "trip_update": {"trip": {"trip_id": ""}, "stop_time_update": []}}],
        "vehicle_positions": [vehicle(16.005, "U02Z01", now)],
    }
    with freeze_time(now.astimezone(UTC).replace(tzinfo=None), tz_offset=0), \
         patch.object(gtfs_rt_helper, "get_gtfs_feed_entities", side_effect=lambda url, headers, label: feeds[label]), \
         patch.object(gtfs_rt_helper, "trip_loader_for", return_value=LOADER):
        result = gtfs_rt_helper.get_rt_route_trip_statuses(_route_sensor())
    assert result["R31"]["0"]["U3Z1"]["delays"] == [60]
