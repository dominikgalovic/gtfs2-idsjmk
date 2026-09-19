"""Keeping the imported timetable paired with the realtime feed (rt_sync.py)."""
from __future__ import annotations

import pytest

import ha_stub

ha_stub.install()

vp = ha_stub.load("vp_delays")
rt_sync = ha_stub.load("rt_sync")


def stop(stop_id, seq, lat, lon):
    return vp.StopTime(stop_id, seq, seq * 60, seq * 60, lat, lon)


# Two trips whose stops lie 10 km apart, standing in for a trip id that means something else in
# another export of the same source.
HERE = {"route_id": "R31", "direction_id": 0,
        "stops": [stop("U1Z1", 1, 49.00, 16.00), stop("U2Z1", 2, 49.00, 16.01)]}
ELSEWHERE = {"route_id": "R99", "direction_id": 0,
             "stops": [stop("U9Z1", 1, 49.10, 16.20), stop("U9Z2", 2, 49.10, 16.21)]}


def vehicle(trip_id, lat=49.0, lon=16.0):
    return {"vehicle": {"trip": {"trip_id": trip_id},
                        "vehicle": {"label": "3664"},
                        "position": {"latitude": lat, "longitude": lon}}}


@pytest.fixture(autouse=True)
def fresh_memory():
    rt_sync.forget()
    yield


def test_a_paired_feed_places_its_vehicles_at_their_stops():
    feed = [vehicle(str(i)) for i in range(30)]
    rate, sampled = rt_sync.match_rate(feed, lambda trip_id: HERE)
    assert (rate, sampled) == (1.0, 30)


def test_a_renumbered_timetable_puts_them_nowhere_near():
    feed = [vehicle(str(i)) for i in range(30)]
    rate, sampled = rt_sync.match_rate(feed, lambda trip_id: ELSEWHERE)
    assert (rate, sampled) == (0.0, 30)


def test_too_few_placeable_vehicles_is_not_an_answer():
    feed = [vehicle(str(i)) for i in range(5)]
    assert rt_sync.match_rate(feed, lambda trip_id: HERE) == (None, 5)
    assert rt_sync.match_rate([], lambda trip_id: HERE) == (None, 0)
    # trips the timetable does not have at all say nothing about the pairing either
    assert rt_sync.match_rate([vehicle(str(i)) for i in range(30)], lambda trip_id: None) == (None, 0)


def test_vehicles_without_a_position_are_skipped():
    feed = [vehicle(str(i)) for i in range(25)] + [vehicle("x", lat=0, lon=0)]
    rate, sampled = rt_sync.match_rate(feed, lambda trip_id: HERE)
    assert sampled == 25 and rate == 1.0


def test_one_bad_measurement_does_not_re_extract():
    assert rt_sync.should_resync("idsjmk", 0.03, 1000) is False
    assert rt_sync.should_resync("idsjmk", 0.03, 1060) is False


def test_a_datasource_that_stays_unpaired_is_re_extracted_once():
    for _ in range(rt_sync.UNPAIRED_REFRESHES - 1):
        assert rt_sync.should_resync("idsjmk", 0.03, 1000) is False
    assert rt_sync.should_resync("idsjmk", 0.03, 1000) is True
    # and not again for hours, however bad it looks meanwhile
    for _ in range(10):
        assert rt_sync.should_resync("idsjmk", 0.0, 2000) is False
    # once those hours have passed and it is still unpaired, it is worth another try
    assert rt_sync.should_resync("idsjmk", 0.0, 1000 + rt_sync.MIN_RESYNC_INTERVAL) is True


def test_a_run_of_bad_measurements_is_forgotten_once_it_pairs_again():
    assert rt_sync.should_resync("idsjmk", 0.03, 1000) is False
    assert rt_sync.should_resync("idsjmk", 0.72, 1060) is False
    for _ in range(rt_sync.UNPAIRED_REFRESHES - 1):
        assert rt_sync.should_resync("idsjmk", 0.03, 1120) is False
    assert rt_sync.should_resync("idsjmk", 0.03, 1120) is True


def test_datasources_are_judged_apart():
    for _ in range(rt_sync.UNPAIRED_REFRESHES - 1):
        rt_sync.should_resync("idsjmk", 0.03, 1000)
    assert rt_sync.should_resync("other", 0.03, 1000) is False


def test_the_rates_seen_on_the_live_feed_read_as_expected():
    # measured against the IDS JMK feed: 72% of vehicles placeable with the export it speaks, 3.7%
    # with the one published a week earlier
    assert rt_sync.should_resync("idsjmk", 0.718, 1000) is False
    for _ in range(rt_sync.UNPAIRED_REFRESHES - 1):
        rt_sync.should_resync("idsjmk", 0.037, 1000)
    assert rt_sync.should_resync("idsjmk", 0.037, 1000) is True
