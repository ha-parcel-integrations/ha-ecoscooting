"""The parcel contract's checks, run over every normalised sample."""

from datetime import UTC, datetime

import pytest

from custom_components.ecoscooting.const import ParcelStatus
from custom_components.ecoscooting.parcels import (
    normalize_parcel,
    parse_iso,
    planned_window,
)

from .payloads import (
    active_sample,
    collected_sample,
    delivered_sample,
    failure_sample,
    pickup_sample,
    return_sample,
)

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "contents",
    "status",
    "raw_status",
    "delivered",
    "delivered_at",
    "planned_from",
    "planned_to",
    "pickup",
    "pickup_point",
    "url",
    "weight",
    "dimensions",
    "history",
    "raw",
]

SAMPLES = {
    "delivered": delivered_sample,
    "active": active_sample,
    "at_pickup_point": pickup_sample,
    "collected": collected_sample,
    "returning": return_sample,
    "problem": failure_sample,
    "bare": lambda: {"trackingNumber": "000000000000000009"},
}


@pytest.fixture(params=SAMPLES)
def parcel(request) -> dict:
    return normalize_parcel(SAMPLES[request.param]())


def test_keys_and_order(parcel):
    assert list(parcel) == CANONICAL_KEYS


def test_delivered_follows_status(parcel):
    assert parcel["delivered"] is (parcel["status"] is ParcelStatus.DELIVERED)


def test_delivered_at_only_when_delivered(parcel):
    if not parcel["delivered"]:
        assert parcel["delivered_at"] is None


def test_planned_window_is_ordered_and_anchored(parcel):
    if parcel["planned_to"] is not None:
        assert parcel["planned_from"] is not None
        assert parse_iso(parcel["planned_to"]) > parse_iso(parcel["planned_from"])


def test_planned_is_empty_when_the_parcel_is_done_or_waiting(parcel):
    if parcel["status"] in (
        ParcelStatus.DELIVERED,
        ParcelStatus.AT_PICKUP_POINT,
        ParcelStatus.RETURNING,
    ):
        assert parcel["planned_from"] is None
        assert parcel["planned_to"] is None


def test_pickup_is_a_bool_and_the_point_needs_it(parcel):
    assert isinstance(parcel["pickup"], bool)
    if not parcel["pickup"]:
        assert parcel["pickup_point"] is None


def test_a_collected_pickup_parcel_stays_pickup():
    parcel = normalize_parcel(collected_sample())
    assert parcel["status"] is ParcelStatus.DELIVERED
    assert parcel["delivered"] is True
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] == "Example Parcelshop"


def test_pickup_survives_collection_without_the_station_block():
    sample = collected_sample()
    del sample["popStationParam"]
    parcel = normalize_parcel(sample)
    assert parcel["delivered"] is True
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] is None


def test_a_home_delivery_is_not_pickup():
    parcel = normalize_parcel(delivered_sample())
    assert parcel["pickup"] is False
    assert parcel["pickup_point"] is None


def test_a_returning_parcel_is_never_delivered():
    parcel = normalize_parcel(return_sample())
    assert parcel["status"] is ParcelStatus.RETURNING
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None


def test_the_delivery_round_ends_at_the_end_of_that_local_day():
    parcel = normalize_parcel(active_sample())
    start = datetime.fromisoformat(parcel["planned_from"])
    end = datetime.fromisoformat(parcel["planned_to"])
    assert (end.hour, end.minute, end.second, end.microsecond) == (23, 59, 59, 0)
    assert end.date() == start.astimezone(end.tzinfo).date()


def test_a_moment_ends_at_the_end_of_that_day():
    start, end = planned_window("2099-04-29T13:00:00+02:00", None, zone=UTC)
    assert start == "2099-04-29T13:00:00+02:00"
    assert datetime.fromisoformat(end).timetz().replace(tzinfo=None).isoformat() == (
        "23:59:59"
    )


def test_a_date_covers_the_whole_day():
    start, end = planned_window("2099-04-29", None, zone=UTC)
    assert datetime.fromisoformat(start).isoformat() == "2099-04-29T00:00:00+00:00"
    assert datetime.fromisoformat(end).isoformat() == "2099-04-29T23:59:59+00:00"
