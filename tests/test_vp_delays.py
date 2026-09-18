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
    vp._stop_delays.clear()
    vp._stops_due.clear()
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


def test_between_stops_on_time_until_overdue_at_the_next_stop():
    # halfway between the stops, before the next one (17:03) is due: on time
    on_time = derive([vehicle(16.005, "U2Z1", at(17, 2, 30))], at(17, 2, 30))
    assert first_update(on_time)["arrival"] == {"time": int(at(17, 3).timestamp()), "delay": 0}
    # same place at 17:04:30, a minute and a half past the next stop's time
    updates = derive([vehicle(16.005, "U2Z1", at(17, 4, 30))], at(17, 4, 30))
    stop = first_update(updates)
    assert stop["stop_id"] == "U2Z1"
    assert stop["arrival"] == {"time": int(at(17, 4).timestamp()), "delay": 60}
    trip = updates[0]["trip_update"]["trip"]
    assert trip == {"trip_id": "T1", "route_id": "R31", "direction_id": "0"}
    assert updates[0]["vehicle_position"] == {
        "label": "3664", "current_stop": "U1Z1", "at_stop": False, "started": True, "age": 0,
    }


def test_delay_is_whole_minutes_rounded_down_and_moves_the_time_by_the_same():
    # the next stop was due at 17:03
    under_two = derive([vehicle(16.005, "U2Z1", at(17, 4, 59))], at(17, 4, 59))
    assert first_update(under_two)["arrival"] == {"time": int(at(17, 4).timestamp()), "delay": 60}
    under_one = derive([vehicle(16.005, "U2Z1", at(17, 3, 59))], at(17, 3, 59))
    assert first_update(under_one)["arrival"] == {"time": int(at(17, 3).timestamp()), "delay": 0}


def test_delay_from_leaving_the_last_stop_until_overdue_at_the_next():
    # stands at the first stop 40 s late, then drives on: still on time although 20 s past the next stop's time
    derive([vehicle(16.0003, "U2Z1", at(17, 0, 40))], at(17, 0, 40))
    driving = derive([vehicle(16.005, "U2Z1", at(17, 3, 20))], at(17, 3, 20))
    assert first_update(driving)["arrival"]["delay"] == 0
    # stands at the second stop 150 s late, then drives on before the third stop is due: keeps +2
    derive([vehicle(16.0101, "U3Z1", at(17, 5, 30))], at(17, 5, 30))
    carried = derive([vehicle(16.015, "U3Z1", at(17, 6))], at(17, 6))
    assert first_update(carried)["arrival"] == {"time": int(at(17, 8).timestamp()), "delay": 120}


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
        "label": "3664", "current_stop": "U1Z1", "at_stop": True, "started": False, "age": 0,
    }


def test_vehicle_leaving_a_little_early_has_started_from_its_first_stop():
    # 500 m past the first stop, 30 s before its scheduled departure
    updates = derive([vehicle(16.0068, "U2Z1", at(16, 59, 30))], at(16, 59, 30))
    assert first_update(updates)["stop_id"] == "U2Z1"
    assert first_update(updates)["arrival"]["delay"] == 0
    assert updates[0]["vehicle_position"] == {
        "label": "3664", "current_stop": "U1Z1", "at_stop": False, "started": True, "age": 0,
    }


def test_parked_bus_that_moves_away_from_its_first_stop_has_started():
    # parked 130 m from the first stop, then 205 m from it, towards the second stop, a minute later
    parked = derive([vehicle(16.0018, "U2Z1", at(16, 58))], at(16, 58))
    assert parked[0]["vehicle_position"] == {
        "label": "3664", "current_stop": "U1Z1", "at_stop": True, "started": False, "age": 0,
    }
    leaving = derive([vehicle(16.0028, "U2Z1", at(16, 59))], at(16, 59))
    assert leaving[0]["vehicle_position"] == {
        "label": "3664", "current_stop": "U1Z1", "at_stop": False, "started": True, "age": 0,
    }
    assert first_update(leaving)["stop_id"] == "U2Z1"


def test_bus_moving_to_its_layover_spot_long_before_departure_has_not_started():
    # unloads at the first stop, then parks 130 m further on, twenty minutes before its next trip
    derive([vehicle(16.0005, "U2Z1", at(16, 40))], at(16, 40))
    parking = derive([vehicle(16.0018, "U2Z1", at(16, 41))], at(16, 41))
    assert parking[0]["vehicle_position"]["started"] is False
    assert parking[0]["vehicle_position"]["current_stop"] == "U1Z1"


def test_vehicle_still_on_its_previous_run_has_not_started():
    # already assigned this trip, but 1.8 km down the route fifteen minutes early
    updates = derive([vehicle(16.025, "U4Z1", at(16, 45))], at(16, 45))
    assert updates[0]["vehicle_position"]["started"] is False
    assert first_update(updates)["stop_id"] == "U1Z1"
    assert first_update(updates)["departure"] == {"time": int(at(17, 0).timestamp()), "delay": 0}


def test_unknown_stop_id_away_from_every_stop_reports_nothing():
    # the timetable has no such stop and the vehicle is at none of the trip's stops:
    # nothing is known about where it is, so nothing is invented
    assert derive([vehicle(16.025, "U99Z1", at(16, 45))], at(16, 45)) == []


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


def test_unknown_stop_id_counts_as_a_sighting_at_a_stop():
    # a railway code the timetable lacks, but the vehicle is 7 m from the second stop (due 17:03)
    now = at(17, 5)
    updates = derive([vehicle(16.0101, "U99Z1", now)], now)
    stop = first_update(updates)
    assert stop["stop_id"] == "U2Z1"
    assert stop["departure"] == {"time": int(now.timestamp()), "delay": 120}
    assert updates[0]["vehicle_position"] == {
        "label": "3664", "current_stop": "U2Z1", "at_stop": True, "started": True, "age": 0,
    }


def test_age_counts_from_the_last_position_in_the_feed_not_the_last_sighting():
    # sighted at the second stop at 17:05, then only positions we cannot place
    derive([vehicle(16.0101, "U99Z1", at(17, 5))], at(17, 5))
    # the newest position is 17:06:30, too old to use but newer than that sighting
    updates = derive([vehicle(16.015, "U99Z1", at(17, 6, 30))], at(17, 9))
    assert updates[0]["vehicle_position"]["age"] == 150


def test_after_a_sighting_the_delay_is_carried_not_grown():
    # seen at the second stop two minutes late, then out of reach of every stop
    derive([vehicle(16.0101, "U99Z1", at(17, 5))], at(17, 5))
    later = derive([vehicle(16.015, "U99Z1", at(17, 7))], at(17, 7))
    stop = first_update(later)
    assert stop["stop_id"] == "U3Z1"          # on its way to the third stop
    assert stop["departure"]["delay"] == 120  # still the delay measured at the second
    assert later[0]["vehicle_position"] == {
        "label": "3664", "current_stop": "U2Z1", "at_stop": False, "started": True, "age": 0,
    }


def test_stale_position_is_ignored():
    assert derive([vehicle(16.005, "U2Z1", at(16, 57))], at(17, 2, 30)) == []


def test_vehicle_reported_twice_keeps_best_schedule_fit():
    right = vehicle(16.005, "U2Z1", at(17, 2, 30), label="2020")
    wrong = vehicle(16.028, "U4Z1", at(17, 2, 30), label="21111")
    updates = derive([wrong, right], at(17, 2, 30))
    assert len(updates) == 1
    assert first_update(updates)["stop_id"] == "U2Z1"


def test_waiting_at_a_stop_past_its_time_counts_up():
    # still standing at the first stop (due 17:00) at 17:05
    updates = derive([vehicle(16.0003, "U2Z1", at(17, 5))], at(17, 5))
    stop = first_update(updates)
    assert stop["stop_id"] == "U1Z1"
    assert stop["departure"] == {"time": int(at(17, 5).timestamp()), "delay": 300}
    assert updates[0]["vehicle_position"] == {
        "label": "3664", "current_stop": "U1Z1", "at_stop": True, "started": False, "age": 0,
    }


def test_a_stop_just_past_its_time_is_held_to_the_end_of_the_minute():
    # standing at the first stop (due 17:00) half a minute later: both delays round down to zero,
    # which would put the departure in the past and drop the row from the sensor until the next
    # minute made it +1. Nothing has reported the vehicle past the stop, so it is departing now.
    now = at(17, 0, 30)
    stop = first_update(derive([vehicle(16.0003, "U2Z1", now)], now))
    assert stop["stop_id"] == "U1Z1"
    assert stop["departure"] == {"time": int(at(17, 0, 59).timestamp()), "delay": 0}


def test_a_held_stop_keeps_the_delay_it_was_measured_with():
    # two and a half minutes late at the second stop (due 17:03): the delay stays as measured,
    # rounded down, while the time moves to the next minute so the row survives
    now = at(17, 5, 30)
    stop = first_update(derive([vehicle(16.0101, "U3Z1", now)], now))
    assert stop["stop_id"] == "U2Z1"
    assert stop["departure"] == {"time": int(at(17, 5, 59).timestamp()), "delay": 120}


def test_a_stop_that_went_due_stays_due_when_the_delay_grows():
    # due now at the first stop (17:00) and still standing there
    now = at(17, 0, 30)
    assert first_update(derive([vehicle(16.0003, "U2Z1", now)], now))["departure"]["time"]         == int(at(17, 0, 59).timestamp())

    # three minutes on, still not reported past the stop: the delay it has run up says how late it
    # already is, not that it leaves later, so it is still departing now rather than in three minutes
    later = at(17, 3, 20)
    stop = first_update(derive([vehicle(16.0003, "U2Z1", later)], later))
    assert stop["departure"] == {"time": int(at(17, 3, 59).timestamp()), "delay": 180}


def test_being_overdue_at_one_stop_makes_the_later_ones_at_least_as_late():
    # standing at the second stop (due 17:03) at 17:07, so four minutes late there
    derive([vehicle(16.0101, "U3Z1", at(17, 7))], at(17, 7))

    # a minute on, still reporting and still not at the third stop (due 17:06), so it is a minute
    # late there - and the fourth stop (due 17:09) cannot be reached any earlier than that, so its
    # delay grows with it instead of staying at what was last measured
    moving = derive([vehicle(16.0101, "U3Z1", at(17, 7, 30))], at(17, 7, 40))
    delays = {s["stop_id"]: s["departure"]["delay"] for s in moving[0]["trip_update"]["stop_time_update"]}
    assert delays["U3Z1"] == 240 and delays["U4Z1"] == 240


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
    derive([vehicle(16.005, "U2Z1", at(17, 4, 30))], at(17, 4, 30))

    # the position went stale but the feed still carries the trip: the row stays, still departing
    # now, and keeps the delay it was last measured with - silence says nothing about how late it
    # has become - while its age shows the data is old
    stale = derive([vehicle(16.005, "U2Z1", at(17, 4, 30))], at(17, 8))
    assert first_update(stale)["departure"] == {"time": int(at(17, 8, 59).timestamp()), "delay": 60}
    assert stale[0]["vehicle_position"]["current_stop"] == "U1Z1"
    assert stale[0]["vehicle_position"]["age"] == 210

    # the trip is gone from the feed too: the last estimate is kept briefly, then dropped
    feed_failed = derive(None, at(17, 9))
    assert first_update(feed_failed)["departure"] == {"time": int(at(17, 9, 59).timestamp()), "delay": 60}
    assert feed_failed[0]["vehicle_position"]["age"] == 270

    # five minutes after the feed last carried the trip, the estimate is forgotten
    assert derive(None, at(17, 13, 1)) == []


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
    now = at(17, 4, 30)
    with freeze_time(now.astimezone(UTC).replace(tzinfo=None), tz_offset=0):
        updates = derive([vehicle(16.005, "U2Z1", now)], now)
        result = gtfs_rt_helper.get_rt_route_trip_statuses(_route_sensor(), updates)
    at_stop = result["R31"]["0"]["U3Z1"]
    assert at_stop["departures"][0].timestamp() == at(17, 7).timestamp()
    assert at_stop["delays"] == [60]
    assert at_stop["vehicles"] == [
        {"label": "3664", "current_stop": "U1Z1", "at_stop": False, "started": True, "age": 0}
    ]


def test_route_sensor_falls_back_to_vehicle_positions():
    now = at(17, 4, 30)
    feeds = {
        "trip_data": [{"id": "1", "trip_update": {"trip": {"trip_id": ""}, "stop_time_update": []}}],
        "vehicle_positions": [vehicle(16.005, "U02Z01", now)],
    }
    with freeze_time(now.astimezone(UTC).replace(tzinfo=None), tz_offset=0), \
         patch.object(gtfs_rt_helper, "get_gtfs_feed_entities", side_effect=lambda url, headers, label: feeds[label]), \
         patch.object(gtfs_rt_helper, "trip_loader_for", return_value=LOADER):
        result = gtfs_rt_helper.get_rt_route_trip_statuses(_route_sensor())
    assert result["R31"]["0"]["U3Z1"]["delays"] == [60]
