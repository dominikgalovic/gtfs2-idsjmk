"""Alerts grouped by the line they name (gtfs_rt_helper.get_rt_alerts_by_line).

The entities are built here rather than with gtfs-realtime-bindings: the suite runs without
that package, and ha_stub then answers `gtfs_realtime_pb2.FeedMessage` with a stub that
raises. Only the handful of fields the function reads are modelled.
"""
from __future__ import annotations

import ha_stub

ha_stub.install()

gtfs_rt_helper = ha_stub.load("gtfs_rt_helper")

DETOUR = 4
TIMETABLE_BULLETIN = 5


class Translated:
    """A TranslatedString: header_text and description_text carry translations."""

    def __init__(self, text=""):
        self.translation = [Translation(text)] if text else []


class Translation:
    def __init__(self, text):
        self.text = text


class Informed:
    def __init__(self, route_id=""):
        self.route_id = route_id


class Alert:
    def __init__(self, effect, header="", description="", lines=()):
        self.effect = effect
        self.header_text = Translated(header)
        self.description_text = Translated(description)
        self.informed_entity = [Informed(line) for line in lines]


class Entity:
    def __init__(self, alert=None):
        self.alert = alert

    def HasField(self, field):
        return field == "alert" and self.alert is not None


def feed_entities():
    return [
        Entity(Alert(DETOUR, "Výluka", "Uzavírka ulice Fryčajovy", ("57", "75", "57"))),
        Entity(Alert(TIMETABLE_BULLETIN, "Výluka", "Změny v dopravě od 1. září 2026\nOd úterý…", ("31",))),
        Entity(Alert(DETOUR, lines=("33",))),  # no texts at all
        Entity(),  # a vehicle position, not an alert
    ]


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
