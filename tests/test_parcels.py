"""Tests for the pure parcel-mapping helpers.

These need no Home Assistant instance — the whole point of keeping
``parcels.py`` free of I/O is that the carrier-specific mapping (the part you
rewrite per carrier) can be tested as plain functions.
"""
from datetime import datetime, timedelta, timezone

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoscooting.const import (
    CAPABILITIES,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DOMAIN,
    KNOWN_CAPABILITIES,
    PENDING_CAPABILITIES,
    ParcelStatus,
)
from custom_components.ecoscooting.parcels import (
    apply_delivered_filter,
    build_history,
    map_event_status,
    map_parcel_status,
    normalize_parcel,
    parse_iso,
    sort_parcels_by_ts,
    to_iso_timestamp,
)

from .payloads import (
    ACTIVE_CODE,
    DELIVERED_CODE,
    active_sample,
    collected_sample,
    delivered_sample,
    event,
    failure_sample,
    malformed_timestamp_sample,
    pickup_sample,
    return_sample,
)

# ---------------------------------------------------------------------------
# map_parcel_status / map_event_status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code,expected",
    [
        # Seen on the wire, both code families.
        ("CREATE_ORDER_CES", ParcelStatus.REGISTERED),
        ("create", ParcelStatus.REGISTERED),
        ("Label_created", ParcelStatus.REGISTERED),
        ("GTMS_ACCEPT", ParcelStatus.IN_TRANSIT),
        ("SL_ACCEPT", ParcelStatus.IN_TRANSIT),
        ("TD_TRANS_ARRIVE_C", ParcelStatus.IN_TRANSIT),
        ("TD_TRANSWH_OUTBOUND", ParcelStatus.IN_TRANSIT),
        ("SC_INBOUND", ParcelStatus.IN_TRANSIT),
        ("SC_OUTBOUND", ParcelStatus.IN_TRANSIT),
        ("In_transit", ParcelStatus.IN_TRANSIT),
        ("COMMON_INTRANSIT", ParcelStatus.IN_TRANSIT),
        ("Partner_outbound", ParcelStatus.IN_TRANSIT),
        ("GTMS_DO_DEPART", ParcelStatus.OUT_FOR_DELIVERY),
        ("LM_DELIVERY_DEPART", ParcelStatus.OUT_FOR_DELIVERY),
        ("GTMS_SIGNED", ParcelStatus.DELIVERED),
        ("LM_SIGN_SUCCESS", ParcelStatus.DELIVERED),
        ("GTMS_PUDO_SIGNED", ParcelStatus.DELIVERED),
        ("PUDO_SIGN_SUCCESS", ParcelStatus.DELIVERED),
        ("GTMS_PUDO_INBOUND", ParcelStatus.AT_PICKUP_POINT),
        ("PUDO_INBOUND", ParcelStatus.AT_PICKUP_POINT),
        ("GTMS_DEL_FAILURE", ParcelStatus.PROBLEM),
        ("LM_DELIVERY_FAILURE", ParcelStatus.PROBLEM),
        ("LM_PARCEL_EXCEPTION", ParcelStatus.PROBLEM),
        ("PUDO_OVERDUE", ParcelStatus.RETURNING),
        ("GTMS_PUDO_OVERDUE", ParcelStatus.RETURNING),
        ("GTMS_SIGN_FAILURE", ParcelStatus.RETURNING),
        ("LM_UNREACHABLE_RETURN", ParcelStatus.RETURNING),
        # Source-only codes kept in the map.
        ("PU_PICKUP_SUCCESS", ParcelStatus.IN_TRANSIT),
        ("TRANSFER_ARRIVE", ParcelStatus.IN_TRANSIT),
        ("TRANSFER_DEPART", ParcelStatus.IN_TRANSIT),
    ],
)
def test_map_parcel_status_known(code, expected):
    assert map_parcel_status(code) == expected


@pytest.mark.parametrize(
    "code",
    [
        "GTMS_STA_SIGNED",
        "PUDO_DELIVERY",
    ],
)
def test_delivered_to_the_shop_is_not_delivered(code):
    """"Delivered to PUDO" is the shop signing for it, not the recipient."""
    assert map_parcel_status(code) == ParcelStatus.AT_PICKUP_POINT


@pytest.mark.parametrize(
    "code",
    [
        "RT_LM_SIGN_SUCCESS",
        "RT_SIGNIN_SUCCESS",
        "RT_LM_DELIVERY_DEPART",
        "RT_DO_ARRIVE",
        "RT_WH_OUTBOUND",
        "RT_SC_INBOUND",
        "RT_SOMETHING_WE_HAVE_NEVER_SEEN",
    ],
)
def test_the_whole_return_leg_is_returning(code):
    """The return leg reuses the forward-leg code names behind an ``RT_``
    prefix, so the prefix must win over the map: a parcel signed for back at
    the sender is a return, never a delivery."""
    assert map_parcel_status(code) == ParcelStatus.RETURNING


def test_status_codes_are_matched_case_insensitively():
    """The gateway mixes casing within one vocabulary."""
    assert map_parcel_status("gtms_signed") == ParcelStatus.DELIVERED
    assert map_parcel_status("CREATE") == ParcelStatus.REGISTERED
    assert map_parcel_status("rt_lm_sign_success") == ParcelStatus.RETURNING


def test_map_parcel_status_missing_is_unknown():
    assert map_parcel_status(None) == ParcelStatus.UNKNOWN
    assert map_parcel_status("") == ParcelStatus.UNKNOWN


def test_map_parcel_status_unmapped_falls_back_to_newest_mappable_event():
    """One new code must not drop a parcel to ``unknown``."""
    events = [
        event("LM_SOMETHING_NEW", "1790600000000", "?"),
        event("LM_DELIVERY_DEPART", "1790580000000", "Out for Delivery"),
        event("SC_INBOUND", "1790354511000", "Sorting Center Inbound"),
    ]
    assert (
        map_parcel_status("LM_SOMETHING_NEW", events[1:])
        == ParcelStatus.OUT_FOR_DELIVERY
    )


def test_map_parcel_status_unmapped_without_usable_history_is_unknown():
    assert map_parcel_status("LM_SOMETHING_NEW", []) == ParcelStatus.UNKNOWN
    assert (
        map_parcel_status("LM_SOMETHING_NEW", ["not-a-dict"]) == ParcelStatus.UNKNOWN
    )


def test_map_event_status_missing_and_unmapped_are_none():
    """History keeps ``null`` rather than ``unknown`` so consumers can tell
    "no mapping" from "mapped to unknown"."""
    assert map_event_status(None) is None
    assert map_event_status("SOMETHING_NEW") is None
    assert map_event_status("GTMS_SIGNED") == ParcelStatus.DELIVERED


def test_unmapped_status_warns_only_once(caplog):
    assert map_parcel_status("LM_ABDUCTED") == ParcelStatus.UNKNOWN
    assert map_parcel_status("LM_ABDUCTED") == ParcelStatus.UNKNOWN
    assert caplog.text.count("LM_ABDUCTED") == 1
    assert "issues/new" in caplog.text


def test_unmapped_status_warning_carries_no_payload_values(caplog):
    """The reply embeds a postcode and door coordinates; logs carry codes only."""
    raw = active_sample()
    raw["statuses"][0]["actionCode"] = "LM_BRAND_NEW"
    normalize_parcel(raw)

    assert "LM_BRAND_NEW" in caplog.text
    assert raw["packageParam"]["toZipcode"] not in caplog.text
    assert ACTIVE_CODE not in caplog.text


# ---------------------------------------------------------------------------
# timestamp helpers
# ---------------------------------------------------------------------------


def test_parse_iso_handles_z_naive_and_garbage():
    assert parse_iso("2026-04-29T13:12:42Z").tzinfo is not None
    # A naive value is assumed UTC so mixed lists still sort.
    assert parse_iso("2026-04-29T13:12:42").tzinfo == timezone.utc
    assert parse_iso("not-a-date") is None
    assert parse_iso(None) is None


def test_to_iso_timestamp_converts_the_gateways_string_epoch_milliseconds():
    assert to_iso_timestamp("1784203767167") == "2026-07-16T12:09:27.167000+00:00"
    assert to_iso_timestamp(1784203767167) == "2026-07-16T12:09:27.167000+00:00"


def test_to_iso_timestamp_refuses_a_display_datetime():
    """The display string's offset lives in a separate field, so using it
    would silently shift an event by an hour or two. Better no instant."""
    assert to_iso_timestamp("2026-10-09 20:35:06 UTC+1") is None
    assert to_iso_timestamp("2026-10-09 20:35:06") is None


def test_to_iso_timestamp_handles_missing_and_out_of_range():
    assert to_iso_timestamp(None) is None
    assert to_iso_timestamp("") is None
    assert to_iso_timestamp(True) is None
    assert to_iso_timestamp({"ms": 1}) is None
    assert to_iso_timestamp(10**20) is None  # out of range -> None, never raises


# ---------------------------------------------------------------------------
# build_history
# ---------------------------------------------------------------------------


def test_build_history_reverses_the_gateways_newest_first_order():
    history = build_history(delivered_sample()["statuses"])
    assert len(history) == 5
    assert history[0]["status"] == ParcelStatus.IN_TRANSIT
    assert history[0]["raw_status"] == "Sorting Center Inbound"
    assert history[-1]["status"] == ParcelStatus.DELIVERED
    assert history[-1]["raw_status"] == "Delivery Success"


def test_build_history_caps_to_max_events():
    events = [
        event("SC_INBOUND", str(1790000000000 + day * 86400000), "moved")
        for day in range(25)
    ]
    assert len(build_history(events, max_events=20)) == 20


def test_build_history_drops_events_without_a_usable_instant():
    """A display-only or missing ``opTimestamp`` costs the event, not the poll."""
    history = build_history(malformed_timestamp_sample()["statuses"])
    assert [entry["raw_status"] for entry in history] == [
        "create",
        "Sorting Center Inbound",
    ]


def test_build_history_handles_missing_and_malformed():
    assert build_history(None) == []
    assert build_history([{"actionCode": "SC_INBOUND"}]) == []  # no timestamp
    assert build_history(["not-a-dict"]) == []


def test_build_history_falls_back_through_label_then_code():
    history = build_history(
        [
            event("SC_INBOUND", "1790000000000", "", description="arrived"),
            event("SC_OUTBOUND", "1790100000000", "", description=""),
        ]
    )
    assert [entry["raw_status"] for entry in history] == ["arrived", "SC_OUTBOUND"]


# ---------------------------------------------------------------------------
# normalize_parcel — the canonical contract
# ---------------------------------------------------------------------------

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
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


def test_normalize_publishes_exactly_the_canonical_keys():
    """The aggregator and cross-carrier dashboards depend on this key set."""
    assert list(normalize_parcel(delivered_sample())) == CANONICAL_KEYS


def test_capabilities_are_known_values():
    """A typo here would silently misreport this carrier on the docs site."""
    assert CAPABILITIES <= KNOWN_CAPABILITIES
    assert PENDING_CAPABILITIES <= KNOWN_CAPABILITIES


def test_a_capability_is_never_both_populated_and_pending():
    """The docs site would have to pick one; "awaiting data" must not hide a confirmed field."""
    assert not CAPABILITIES & PENDING_CAPABILITIES


def test_capabilities_match_what_normalize_parcel_actually_returns():
    """Every declared CAPABILITIES entry must come true somewhere in a sample."""
    delivered = normalize_parcel(delivered_sample())
    active = normalize_parcel(active_sample())
    pickup = normalize_parcel(pickup_sample())
    with_history = normalize_parcel(delivered_sample(), include_history=True)

    if "weight" in CAPABILITIES:
        assert delivered["weight"] is not None
    if "dimensions" in CAPABILITIES:
        assert delivered["dimensions"] is not None
    if "delivery_window" in CAPABILITIES:
        assert active["planned_from"] is not None or active["planned_to"] is not None
    if "pickup_point" in CAPABILITIES:
        assert pickup["pickup_point"] is not None
    if "url" in CAPABILITIES:
        assert delivered["url"] is not None
    if "history" in CAPABILITIES:
        assert with_history["history"] is not None


def test_fields_the_gateway_does_not_carry_stay_none():
    """No sender, receiver or dimensions exist in the reply."""
    parcel = normalize_parcel(active_sample())
    assert parcel["sender"] is None
    assert parcel["receiver"] is None
    assert parcel["dimensions"] is None


def test_out_for_delivery_spans_the_rest_of_the_delivery_day():
    """The departure scan opens the window; the end of its own day closes it."""
    parcel = normalize_parcel(active_sample())
    assert parcel["status"] == ParcelStatus.OUT_FOR_DELIVERY
    assert parcel["planned_from"] == "2026-09-28T07:35:27+00:00"
    assert parcel["planned_to"] == "2026-09-28T23:59:59+01:00"


def test_an_envelope_that_is_not_an_object_warns_instead_of_crashing(caplog):
    """Normalisation runs outside the per-parcel error handling: a raise kills the poll."""
    parcel = normalize_parcel({"packageParam": "x", "statuses": "y", "trackingNumber": "A"})
    assert parcel["barcode"] == "A"
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["weight"] is None
    assert caplog.text.count("packageParam") == 1
    assert caplog.text.count("statuses") == 1
    assert "issues/new" in caplog.text


def test_a_null_envelope_field_is_absence_not_a_surprise(caplog):
    """The gateway sends these keys with a null value; that is not worth a warning."""
    parcel = normalize_parcel({"packageParam": None, "statuses": None})
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["barcode"] is None
    assert caplog.text == ""


def test_a_departure_scan_without_an_instant_reports_no_window():
    """An event the history drops cannot date an ETA either."""
    sample = active_sample()
    sample["statuses"][0]["opTimestamp"] = "2026-09-28 09:35:27"
    parcel = normalize_parcel(sample)
    assert parcel["status"] == ParcelStatus.OUT_FOR_DELIVERY
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None


def test_an_unusable_offset_leaves_the_window_open():
    """A day boundary without a trustworthy offset would span the wrong day."""
    sample = active_sample()
    sample["statuses"][0]["timezone"] = "somewhere"
    parcel = normalize_parcel(sample)
    assert parcel["planned_from"] == "2026-09-28T07:35:27+00:00"
    assert parcel["planned_to"] is None


def test_planned_from_takes_the_newest_out_for_delivery_scan():
    """A second delivery day must not report the first day's departure."""
    sample = active_sample()
    sample["statuses"].insert(
        0,
        event(
            "LM_DELIVERY_DEPART",
            "1790667327000",
            "Out for Delivery",
            description="Your shipment is out for delivery",
            timezone_hours="1",
        ),
    )
    parcel = normalize_parcel(sample)
    assert parcel["planned_from"] == "2026-09-29T07:35:27+00:00"


@pytest.mark.parametrize("sample", [delivered_sample, return_sample])
def test_planned_from_is_dropped_once_the_parcel_is_no_longer_out(sample):
    """An out-for-delivery scan earlier in the history is not an ETA any more."""
    parcel = normalize_parcel(sample())
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None


def test_normalize_delivered_parcel():
    parcel = normalize_parcel(delivered_sample())
    assert parcel["carrier"] == "Ecoscooting"
    assert parcel["barcode"] == DELIVERED_CODE
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["raw_status"] == "Delivery Success"
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-10-09T19:35:06+00:00"
    assert parcel["url"] == (
        f"https://www.ecoscooting.com/tracking/{DELIVERED_CODE}"
    )
    assert parcel["history"] is None  # opt-in, default off


def test_normalize_converts_grams_to_kilograms():
    raw = delivered_sample()
    raw["packageParam"]["dimWeight"] = {"weight": "197", "weightUnit": "g"}
    assert normalize_parcel(raw)["weight"] == 0.197


@pytest.mark.parametrize(
    "dim_weight",
    [
        None,
        "1000g",
        {},
        {"weight": "", "weightUnit": "g"},
    ],
)
def test_normalize_weight_without_a_usable_value_is_none(dim_weight):
    raw = delivered_sample()
    raw["packageParam"]["dimWeight"] = dim_weight
    assert normalize_parcel(raw)["weight"] is None


@pytest.mark.parametrize(
    "dim_weight",
    [
        {"weight": "2.2", "weightUnit": "kg"},
        {"weight": "heavy", "weightUnit": "g"},
    ],
)
def test_normalize_warns_once_on_an_unreadable_weight(dim_weight, caplog):
    """A unit we have never seen must not be published off by a factor of 1000."""
    raw = delivered_sample()
    raw["packageParam"]["dimWeight"] = dim_weight

    assert normalize_parcel(raw)["weight"] is None
    assert normalize_parcel(raw)["weight"] is None
    assert caplog.text.count("issues/new") == 1


def test_normalize_history_is_opt_in():
    parcel = normalize_parcel(delivered_sample(), include_history=True)
    assert len(parcel["history"]) == 5
    assert parcel["history"][-1]["status"] == ParcelStatus.DELIVERED


def test_normalize_out_for_delivery_parcel():
    parcel = normalize_parcel(active_sample())
    assert parcel["status"] == ParcelStatus.OUT_FOR_DELIVERY
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None
    assert parcel["barcode"] == ACTIVE_CODE


def test_normalize_pickup_parcel():
    parcel = normalize_parcel(pickup_sample())
    assert parcel["status"] == ParcelStatus.AT_PICKUP_POINT
    assert parcel["pickup"] is True
    # Shop name and address, with the carrier's own ``NaN`` filler dropped.
    assert parcel["pickup_point"] == (
        "Example Parcelshop, Example Street, 1, Example Town"
    )


def test_normalize_keeps_the_pickup_point_after_collection():
    """A collected parcel still shows where it was collected."""
    parcel = normalize_parcel(collected_sample())
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["pickup"] is False
    assert parcel["pickup_point"] is not None


def test_normalize_never_surfaces_the_pickup_points_pin_or_phone():
    parcel = normalize_parcel(pickup_sample())
    station = parcel["raw"]["popStationParam"]
    assert station["pinCode"] not in parcel["pickup_point"]
    assert station["phone"] not in parcel["pickup_point"]


def test_normalize_warns_once_on_an_unreadable_pickup_point(caplog):
    raw = pickup_sample()
    raw["popStationParam"] = {"popStationId": "0"}

    assert normalize_parcel(raw)["pickup_point"] is None
    assert normalize_parcel(raw)["pickup_point"] is None
    assert caplog.text.count("issues/new") == 1


def test_normalize_warns_on_a_pickup_point_that_is_not_an_object(caplog):
    raw = pickup_sample()
    raw["popStationParam"] = "Example Parcelshop"

    assert normalize_parcel(raw)["pickup_point"] is None
    assert "issues/new" in caplog.text


def test_normalize_failure_parcel():
    parcel = normalize_parcel(failure_sample())
    assert parcel["status"] == ParcelStatus.PROBLEM
    assert parcel["delivered"] is False


def test_normalize_return_parcel_is_not_delivered():
    """The return leg ends in a "signed" event — for the sender, not the user."""
    parcel = normalize_parcel(return_sample())
    assert parcel["status"] == ParcelStatus.RETURNING
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None


def test_normalize_dates_a_delivery_from_the_delivery_event_only():
    """A status reached by history fallback must not borrow the newest
    event's instant for ``delivered_at``."""
    raw = delivered_sample()
    raw["statuses"].insert(
        0, event("GTMS_SOMETHING_NEW", "1791999999000", "?")
    )
    parcel = normalize_parcel(raw)
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["delivered_at"] == "2026-10-09T19:35:06+00:00"


def test_normalize_pending_placeholder():
    """A tracked-but-not-yet-answered code still yields a full parcel dict."""
    parcel = normalize_parcel({"trackingNumber": ACTIVE_CODE})
    assert parcel["barcode"] == ACTIVE_CODE
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["delivered"] is False
    assert parcel["raw_status"] is None
    assert parcel["weight"] is None
    assert parcel["pickup_point"] is None
    assert parcel["history"] is None


def test_normalize_without_any_reference_has_no_url():
    """The coordinator always seeds the code, but the key stays optional."""
    parcel = normalize_parcel({})
    assert parcel["barcode"] is None
    assert parcel["url"] is None


def test_normalize_ignores_a_non_object_event():
    raw = active_sample()
    raw["statuses"].insert(0, "not-a-dict")
    assert normalize_parcel(raw)["status"] == ParcelStatus.OUT_FOR_DELIVERY


def test_normalize_falls_back_through_label_then_code():
    raw = active_sample()
    raw["statuses"][0]["statusName"] = ""
    assert normalize_parcel(raw)["raw_status"] == (
        "Your shipment is out for delivery"
    )
    raw["statuses"][0]["description"] = ""
    assert normalize_parcel(raw)["raw_status"] == "LM_DELIVERY_DEPART"


def test_normalize_keeps_the_whole_reply_as_raw():
    """``raw`` is never trimmed — the full record is the contract."""
    raw = active_sample()
    parcel = normalize_parcel(raw)
    assert parcel["raw"] is raw
    assert parcel["raw"]["packageParam"]["toZipcode"]
    assert parcel["raw"]["solutionParam"]


# ---------------------------------------------------------------------------
# sort_parcels_by_ts
# ---------------------------------------------------------------------------


def test_sort_parcels_ascending_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "planned_from": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "planned_from": None},
        {"barcode": "c", "planned_from": "2026-05-01T10:00:00Z"},
    ]
    ordered = [p["barcode"] for p in sort_parcels_by_ts(parcels, "planned_from")]
    assert ordered == ["c", "a", "b"]


def test_sort_parcels_descending_still_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "delivered_at": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "delivered_at": "nonsense"},
        {"barcode": "c", "delivered_at": "2026-05-01T10:00:00Z"},
    ]
    ordered = [
        p["barcode"]
        for p in sort_parcels_by_ts(parcels, "delivered_at", descending=True)
    ]
    assert ordered == ["a", "c", "b"]


# ---------------------------------------------------------------------------
# apply_delivered_filter
# ---------------------------------------------------------------------------


def _entry(filter_type: str, amount: int) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_DELIVERED_FILTER_TYPE: filter_type,
            CONF_DELIVERED_FILTER_AMOUNT: amount,
        },
        unique_id=DOMAIN,
    )


def _delivered_pair() -> list[dict]:
    now = datetime.now(timezone.utc)
    return [
        {"barcode": "RECENT", "delivered_at": (now - timedelta(days=1)).isoformat()},
        {"barcode": "OLD", "delivered_at": (now - timedelta(days=30)).isoformat()},
    ]


def test_delivered_filter_by_days():
    kept = apply_delivered_filter(_delivered_pair(), _entry("days", 7))
    assert [p["barcode"] for p in kept] == ["RECENT"]


def test_delivered_filter_by_count():
    parcels = _delivered_pair()
    assert apply_delivered_filter(parcels, _entry("parcels", 1)) == parcels[:1]


def test_delivered_filter_keeps_unparseable_timestamp():
    """Better to show a parcel with a broken date than to silently drop it."""
    parcels = [{"barcode": "WEIRD", "delivered_at": "nonsense"}]
    assert apply_delivered_filter(parcels, _entry("days", 7)) == parcels
