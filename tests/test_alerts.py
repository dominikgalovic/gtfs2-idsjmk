"""Alerts grouped by the line they name (gtfs_rt_helper.get_rt_alerts_by_line)."""
from __future__ import annotations

import pytest

import ha_stub

ha_stub.install()

pytest.importorskip("google.transit", reason="needs gtfs-realtime-bindings")
from google.transit import gtfs_realtime_pb2  # noqa: E402

gtfs_rt_helper = ha_stub.load("gtfs_rt_helper")

DETOUR = 4
TIMETABLE_BULLETIN = 5


def feed_entities():
    feed = gtfs_realtime_pb2.FeedMessage()

    diversion = feed.entity.add()
    diversion.id = "lockout_8"
    diversion.alert.effect = DETOUR
    diversion.alert.header_text.translation.add().text = "Výluka"
    diversion.alert.description_text.translation.add().text = "Uzavírka ulice Fryčajovy"
    for line in ("57", "75", "57"):
        diversion.alert.informed_entity.add().route_id = line

    bulletin = feed.entity.add()
    bulletin.id = "lockout_179"
    bulletin.alert.effect = TIMETABLE_BULLETIN
    bulletin.alert.header_text.translation.add().text = "Výluka"
    bulletin.alert.description_text.translation.add().text = "Změny v dopravě od 1. září 2026\nOd úterý…"
    bulletin.alert.informed_entity.add().route_id = "31"

    without_text = feed.entity.add()
    without_text.id = "empty"
    without_text.alert.effect = DETOUR
    without_text.alert.informed_entity.add().route_id = "33"

    vehicle = feed.entity.add()
    vehicle.id = "vehicle_1"
    vehicle.vehicle.trip.trip_id = "T1"

    return feed.entity


def test_disruptions_are_grouped_by_line_without_duplicates():
    alerts = gtfs_rt_helper.get_rt_alerts_by_line(feed_entities())
    assert sorted(alerts) == ["57", "75"]
    assert alerts["57"] == [{"header": "Výluka", "description": "Uzavírka ulice Fryčajovy"}]
    assert alerts["75"] == alerts["57"]


def test_general_timetable_bulletins_are_skipped():
    # they carry no validity period and stay in the feed for months
    assert "31" not in gtfs_rt_helper.get_rt_alerts_by_line(feed_entities())


def test_feeds_without_alerts():
    assert gtfs_rt_helper.get_rt_alerts_by_line(None) == {}
    assert gtfs_rt_helper.get_rt_alerts_by_line([]) == {}
    # trip updates arrive as dicts, not protobuf entities
    assert gtfs_rt_helper.get_rt_alerts_by_line([{"trip_update": {"trip": {"trip_id": "T1"}}}]) == {}
