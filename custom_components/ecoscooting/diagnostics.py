"""Diagnostics support for the Ecoscooting parcel tracker integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import EcoscootingConfigEntry

# Diagnostics are pasted into public issues, so redact anything that
# identifies a person, an address, a parcel or where and when it was handed
# over. The reply's references embed the destination postcode, and its
# delivery event carries the coordinates of the door and a photo of the
# drop-off, so over-redacting here is not optional.
TO_REDACT = {
    # canonical fields we publish ourselves
    "tracking_code",
    "barcode",
    "sender",
    "receiver",
    "url",
    "pickup_point",
    "delivered_at",
    "planned_from",
    "planned_to",
    "timestamp",
    # carrier payload fields
    "trackingNumber",
    "toZipcode",
    "toCity",
    "outOrderId",
    "cainiaoId",
    "feature",
    "solutionParam",
    "popStationParam",
    "deliveryLatitude",
    "deliveryLongitude",
    "imgUrl",
    "datetime",
    "opTimestamp",
    "timezone",
    "opRemark",
    # generic personal fields, in case a reply grows one
    "recipient",
    "address",
    "detailAddress",
    "postalCode",
    "city",
    "street",
    "email",
    "name",
    "phone",
    "pinCode",
    "driver",
    "signature",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: EcoscootingConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the Ecoscooting config entry."""
    coordinator = entry.runtime_data.coordinator

    return {
        "entry_options": async_redact_data(dict(entry.options), TO_REDACT),
        "counts": {
            "incoming_active": len(coordinator.data or []),
            "delivered": len(coordinator.delivered or []),
            "skipped_from_fetch": len(coordinator.delivered_codes),
        },
        "polling": {
            "tier_minutes": coordinator.current_tier_minutes,
            "update_interval_seconds": (
                coordinator.update_interval.total_seconds()
                if coordinator.update_interval
                else None
            ),
            "suspended": coordinator.update_interval is None,
        },
        "incoming": async_redact_data(coordinator.data or [], TO_REDACT),
        "delivered": async_redact_data(coordinator.delivered or [], TO_REDACT),
    }
