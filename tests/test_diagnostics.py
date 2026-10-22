"""Tests for Ecoscooting diagnostics."""

from datetime import timedelta
from unittest.mock import MagicMock

from custom_components.ecoscooting.diagnostics import async_get_config_entry_diagnostics
from custom_components.ecoscooting.parcels import normalize_parcel

from .payloads import DELIVERED_CODE, delivered_sample, pickup_sample


def _entry(parcels: list[dict]) -> MagicMock:
    entry = MagicMock()
    entry.options = {"parcels": [{"tracking_code": DELIVERED_CODE}]}
    entry.runtime_data.coordinator.current_tier_minutes = 15
    entry.runtime_data.coordinator.update_interval = timedelta(minutes=15)
    entry.runtime_data.coordinator.data = parcels
    entry.runtime_data.coordinator.delivered = []
    entry.runtime_data.coordinator.delivered_codes = set()
    return entry


async def test_diagnostics_redacts_and_counts(hass):
    """Diagnostics get pasted into public issues — nothing identifying may survive."""
    entry = _entry([normalize_parcel(delivered_sample(), include_history=True)])

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["counts"] == {
        "incoming_active": 1,
        "delivered": 0,
        "skipped_from_fetch": 0,
    }
    assert result["polling"] == {
        "tier_minutes": 15,
        "update_interval_seconds": 900.0,
        "suspended": False,
    }
    incoming = result["incoming"][0]
    # the reference itself, at every nesting level
    assert result["entry_options"]["parcels"][0]["tracking_code"] == "**REDACTED**"
    assert incoming["barcode"] == "**REDACTED**"
    assert incoming["url"] == "**REDACTED**"
    assert incoming["raw"]["packageParam"]["trackingNumber"] == "**REDACTED**"
    # the destination, the routing ids and the delivery's own evidence
    assert incoming["raw"]["packageParam"]["toZipcode"] == "**REDACTED**"
    assert incoming["raw"]["packageParam"]["outOrderId"] == "**REDACTED**"
    assert incoming["raw"]["cainiaoId"] == "**REDACTED**"
    assert incoming["raw"]["feature"] == "**REDACTED**"
    assert incoming["raw"]["solutionParam"] == "**REDACTED**"
    event = incoming["raw"]["statuses"][0]
    assert event["deliveryLatitude"] == "**REDACTED**"
    assert event["deliveryLongitude"] == "**REDACTED**"
    assert event["imgUrl"] == "**REDACTED**"
    # when a parcel moved says where someone lives, so every instant goes too
    assert event["datetime"] == "**REDACTED**"
    assert event["opTimestamp"] == "**REDACTED**"
    assert incoming["delivered_at"] == "**REDACTED**"
    assert incoming["history"][0]["timestamp"] == "**REDACTED**"
    # the status vocabulary survives, or the diagnostics would be useless
    assert incoming["status"] == "delivered"
    assert event["actionCode"] == "GTMS_SIGNED"
    assert incoming["raw"]["success"] == "true"


async def test_diagnostics_redacts_the_pickup_point(hass):
    """A pickup point is a shop address, and its PIN a collection credential."""
    entry = _entry([normalize_parcel(pickup_sample())])

    result = await async_get_config_entry_diagnostics(hass, entry)

    incoming = result["incoming"][0]
    assert incoming["pickup_point"] == "**REDACTED**"
    assert incoming["raw"]["popStationParam"] == "**REDACTED**"


async def test_diagnostics_reports_suspended_polling(hass):
    """update_interval None (Section 2.1's full stop) must be visible, not just absent."""
    entry = MagicMock()
    entry.options = {"parcels": []}
    entry.runtime_data.coordinator.current_tier_minutes = None
    entry.runtime_data.coordinator.update_interval = None
    entry.runtime_data.coordinator.data = []
    entry.runtime_data.coordinator.delivered = []

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["polling"] == {
        "tier_minutes": None,
        "update_interval_seconds": None,
        "suspended": True,
    }
