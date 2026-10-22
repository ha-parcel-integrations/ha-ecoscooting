"""Canonical parcel shape, status mapping and list helpers.

Everything in this module is a **pure function** — no I/O, no Home Assistant
objects beyond the config entry's options. That is deliberate: it keeps the
carrier-specific mapping apart from the coordinator (which is nearly
identical everywhere), and it makes the mapping trivially unit-testable
without spinning up HA.

:data:`_STATUS_MAP` and :func:`normalize_parcel` are the carrier-specific
parts. The sort contract, the delivered filter and the one-shot warning
machinery are suite-wide and should be left alone.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    HISTORY_MAX_EVENTS,
    TRACKING_URL,
    ParcelStatus,
)

_LOGGER = logging.getLogger(__name__)

# Where users report a status we do not map yet. Rewritten by the bootstrap
# script; it must point at the carrier's own repo so the log line is
# copy-pasteable straight into a new issue.
#
# The ``?template=`` parameter matters: without it the link opens a blank form,
# and the report comes back missing the version and the log line we need.
NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-ecoscooting/issues/new"
    "?template=unrecognised_status.yml"
)

# The gateway's ``actionCode`` vocabulary mapped onto ParcelStatus. Two code
# families reach the same references: ``GTMS_*``/``TD_*`` and the last-mile
# ``LM_*``/``SL_*``/``SC_*`` family. Looked up case-insensitively, because the
# gateway mixes casing within one vocabulary (``create``, ``Label_created``,
# ``In_transit``).
_STATUS_MAP: dict[str, ParcelStatus] = {
    "CREATE": ParcelStatus.REGISTERED,
    "LABEL_CREATED": ParcelStatus.REGISTERED,
    "CREATE_ORDER_CES": ParcelStatus.REGISTERED,
    "GTMS_ACCEPT": ParcelStatus.IN_TRANSIT,
    "SL_ACCEPT": ParcelStatus.IN_TRANSIT,
    "PU_PICKUP_SUCCESS": ParcelStatus.IN_TRANSIT,
    "TD_TRANSWH_OUTBOUND": ParcelStatus.IN_TRANSIT,
    "TD_TRANS_ARRIVE_C": ParcelStatus.IN_TRANSIT,
    "IN_TRANSIT": ParcelStatus.IN_TRANSIT,
    "COMMON_INTRANSIT": ParcelStatus.IN_TRANSIT,
    "PARTNER_OUTBOUND": ParcelStatus.IN_TRANSIT,
    "TRANSFER_ARRIVE": ParcelStatus.IN_TRANSIT,
    "TRANSFER_DEPART": ParcelStatus.IN_TRANSIT,
    "SC_INBOUND": ParcelStatus.IN_TRANSIT,
    "SC_OUTBOUND": ParcelStatus.IN_TRANSIT,
    "GTMS_DO_DEPART": ParcelStatus.OUT_FOR_DELIVERY,
    "LM_DELIVERY_DEPART": ParcelStatus.OUT_FOR_DELIVERY,
    # "Delivered to PUDO" means the shop signed for it, not the recipient.
    "GTMS_STA_SIGNED": ParcelStatus.AT_PICKUP_POINT,
    "PUDO_DELIVERY": ParcelStatus.AT_PICKUP_POINT,
    "GTMS_PUDO_INBOUND": ParcelStatus.AT_PICKUP_POINT,
    "PUDO_INBOUND": ParcelStatus.AT_PICKUP_POINT,
    "GTMS_SIGNED": ParcelStatus.DELIVERED,
    "LM_SIGN_SUCCESS": ParcelStatus.DELIVERED,
    "GTMS_PUDO_SIGNED": ParcelStatus.DELIVERED,
    "PUDO_SIGN_SUCCESS": ParcelStatus.DELIVERED,
    # A lost parcel arrives under the same code as a failed hand-over at the
    # last-mile station, so both read as a problem rather than a retry.
    "GTMS_DEL_FAILURE": ParcelStatus.PROBLEM,
    "LM_DELIVERY_FAILURE": ParcelStatus.PROBLEM,
    "LM_PARCEL_EXCEPTION": ParcelStatus.PROBLEM,
    # Past its deadline at a pickup point, or otherwise on its way back.
    "GTMS_PUDO_OVERDUE": ParcelStatus.RETURNING,
    "PUDO_OVERDUE": ParcelStatus.RETURNING,
    "GTMS_SIGN_FAILURE": ParcelStatus.RETURNING,
    "LM_UNREACHABLE_RETURN": ParcelStatus.RETURNING,
}

# The whole return leg is prefixed, and it reuses the forward-leg code names:
# ``RT_LM_SIGN_SUCCESS`` is the parcel signed for *by the sender*, not a
# delivery. The prefix is therefore tested before the map, so a returning
# parcel can never be read as delivered.
_RETURN_PREFIX = "RT_"

# Status codes we have already warned about, so each unmapped one is logged
# only once per HA session instead of on every poll.
_unmapped_statuses_logged: set[str] = set()


def _warn_unmapped_status(code: str) -> None:
    """Log an unmapped carrier status once, with a copy-paste issue link."""
    if code in _unmapped_statuses_logged:
        return
    _unmapped_statuses_logged.add(code)
    _LOGGER.warning(
        "Unrecognised Ecoscooting status — help us map it. Open an issue "
        "and paste this line: %s\n  actionCode=%s",
        NEW_ISSUE_URL,
        code,
    )


_shapes_warned: set[str] = set()


def _warn_shape(what: str, detail: str) -> None:
    """Log a payload shape we have never seen once, with an issue link."""
    key = f"{what}:{detail}"
    if key in _shapes_warned:
        return
    _shapes_warned.add(key)
    _LOGGER.warning(
        "Unrecognised Ecoscooting %s shape — help us map it. Open an issue "
        "and paste this line: %s\n  %s",
        what,
        NEW_ISSUE_URL,
        detail,
    )


def _warn_pickup_point_shape(detail: str) -> None:
    """Report a ``popStationParam`` block we cannot read (keys only)."""
    _warn_shape("pickup point", detail)


def _warn_weight_shape(detail: str) -> None:
    """Report a weight we cannot convert (unit and type only)."""
    _warn_shape("weight", detail)


def _lookup(code: str | None) -> ParcelStatus | None:
    """Return the canonical status for an ``actionCode``, or ``None``."""
    if not code:
        return None
    key = str(code).upper()
    if key.startswith(_RETURN_PREFIX):
        return ParcelStatus.RETURNING
    return _STATUS_MAP.get(key)


def map_parcel_status(
    code: str | None, events: list[dict] | None = None
) -> ParcelStatus:
    """Map the newest event's ``actionCode``; else the newest mappable event.

    ``None`` (a reference with no scans yet) reports ``unknown`` silently. An
    unrecognised code warns once — the map only grows from those reports —
    and then falls back to the newest event below it that does map, so a
    parcel does not drop to ``unknown`` over one new code.
    """
    mapped = _lookup(code)
    if mapped is not None:
        return mapped
    if code:
        _warn_unmapped_status(str(code))
    for event in events or []:
        if not isinstance(event, dict):
            continue
        fallback = _lookup(event.get("actionCode"))
        if fallback is not None:
            return fallback
    return ParcelStatus.UNKNOWN


def map_event_status(code: str | None) -> ParcelStatus | None:
    """Map a history entry's ``actionCode`` to a canonical status, or ``None``.

    Unmapped codes keep ``status: null`` on the history entry (rather than
    ``unknown``, so a consumer can tell "no mapping" from "mapped to
    unknown") and warn once, reusing the parcel-status one-shot set.
    """
    mapped = _lookup(code)
    if mapped is None and code:
        _warn_unmapped_status(str(code))
    return mapped


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string to an aware datetime, or ``None`` on failure.

    Naive values are treated as UTC so a list always sorts without crashing on
    a mixed set.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def to_iso_timestamp(value: Any) -> str | None:
    """Return an ISO 8601 UTC string for an ``opTimestamp``, or ``None``.

    The gateway stamps every event twice: ``opTimestamp`` in epoch
    milliseconds (as a string) and a display ``datetime`` whose offset lives
    in a separate ``timezone`` field. Only the epoch value is used — a
    display string without its offset would be an hour or two wrong, and no
    timestamp at all is better than a wrong one.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text.lstrip("-").isdigit():
            return None
        value = int(text)
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def build_history(
    events: list | None, *, max_events: int = HISTORY_MAX_EVENTS
) -> list[dict]:
    """Build the canonical ``history`` list from the gateway's ``statuses``.

    Each entry is ``{timestamp, status, raw_status}`` — identical across all
    suite carriers, and top-level (not under ``raw``) so it survives the
    aggregator's ``strip_raw()``. ``raw_status`` is the carrier's own text, or
    its action code when neither label is present. The gateway sends events
    newest first; the canonical list is oldest → newest, capped to the most
    recent ``max_events``.
    """
    entries: list[tuple[datetime, dict]] = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        timestamp = to_iso_timestamp(event.get("opTimestamp"))
        if not timestamp:
            continue
        action_code = event.get("actionCode")
        entry = {
            "timestamp": timestamp,
            "status": map_event_status(action_code),
            "raw_status": event.get("statusName")
            or event.get("description")
            or action_code,
        }
        # Every timestamp here came out of to_iso_timestamp, so it parses.
        entries.append((parse_iso(timestamp), entry))
    entries.sort(key=lambda item: item[0])
    return [entry for _, entry in entries][-max_events:]


def tracking_url(tracking_code: str | None) -> str | None:
    """Construct the consumer tracking deep-link for a parcel."""
    if not tracking_code:
        return None
    return TRACKING_URL.format(tracking_code=tracking_code)


def _package(raw: dict) -> dict:
    """Return ``packageParam``, reporting a shape that is not an object.

    ``raw.get(key, {})`` is not enough: the gateway sends the key with a null
    value on some replies, and an ``AttributeError`` here would escape the
    coordinator's per-parcel error handling and fail the whole poll.
    """
    package = raw.get("packageParam")
    if isinstance(package, dict):
        return package
    if package is not None:
        _warn_shape("packageParam", type(package).__name__)
    return {}


def _events(raw: dict) -> list[dict]:
    """Return the reply's events, reporting a ``statuses`` that is not a list."""
    statuses = raw.get("statuses")
    if statuses is None:
        return []
    if not isinstance(statuses, list):
        _warn_shape("statuses", type(statuses).__name__)
        return []
    return [event for event in statuses if isinstance(event, dict)]


def _pickup_point(raw: dict) -> str | None:
    """Return the pickup point's shop name and address, or ``None``.

    The gateway sends a ``popStationParam`` block only for a parcel routed to
    a pickup point, and keeps sending it after collection, so the parcel
    still shows where it was collected. Its PIN, phone number and opening
    hours are deliberately never read: the PIN is a collection credential and
    the rest is noise on a parcel sensor. Address parts come through spelled
    ``NaN`` where the carrier has no value, and are left out.
    """
    station = raw.get("popStationParam")
    if not isinstance(station, dict):
        if station is not None:
            _warn_pickup_point_shape(type(station).__name__)
        return None
    if not station.get("stationName") and not station.get("detailAddress"):
        _warn_pickup_point_shape(",".join(sorted(station)))
        return None
    parts = [
        part.strip()
        for value in (station.get("stationName"), station.get("detailAddress"))
        for part in str(value or "").split(",")
        if part.strip() and part.strip().upper() != "NAN"
    ]
    return ", ".join(parts) or None


def _weight_kg(raw: dict) -> float | None:
    """Return the parcel weight in kilograms, or ``None``.

    The gateway reports grams as a string. A unit other than grams has never
    been seen; it warns once and reports nothing rather than publishing a
    number that is off by a factor of a thousand.
    """
    dim_weight = _package(raw).get("dimWeight")
    if not isinstance(dim_weight, dict):
        return None
    unit = str(dim_weight.get("weightUnit") or "").lower()
    weight = dim_weight.get("weight")
    if weight in (None, ""):
        return None
    try:
        grams = float(weight)
    except (TypeError, ValueError):
        _warn_weight_shape(f"weight:{type(weight).__name__}")
        return None
    if unit != "g":
        _warn_weight_shape(f"weightUnit:{unit or 'missing'}")
        return None
    return round(grams / 1000, 3)


def _end_of_delivery_day(event: dict) -> str | None:
    """Return the end of the day an event happened in, in the courier's zone.

    The round lasts until the end of that day, but "that day" is a local one
    and the display offset varies per event, so it is read from the event
    that opens the window rather than from the parcel. Without a usable
    offset the window would span the wrong day, so it is left open instead.
    """
    start = parse_iso(to_iso_timestamp(event.get("opTimestamp")))
    if start is None:
        return None
    try:
        offset = timezone(timedelta(hours=int(str(event.get("timezone")).strip())))
    except (TypeError, ValueError):
        return None
    local = start.astimezone(offset)
    return local.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()


def normalize_parcel(raw: dict, *, include_history: bool = False) -> dict:
    """Return a carrier-agnostic parcel dict with the payload under ``raw``.

    The **keys of the returned dict are the contract**: every carrier in the
    suite returns exactly these, in this order, and the aggregator and
    cross-carrier dashboards depend on it. ``sender``, ``receiver`` and
    ``dimensions`` are always ``None`` here — the reply simply has no such
    fields.

    The status is the newest event's; the whole reply is retained as ``raw``.
    """
    package = _package(raw)
    # The coordinator seeds a not-yet-answered parcel with the bare code at
    # the top level, so a pending placeholder still yields a barcode.
    tracking_code = package.get("trackingNumber") or raw.get("trackingNumber")

    events = _events(raw)
    newest = events[0] if events else {}
    action_code = newest.get("actionCode")
    status = map_parcel_status(action_code, events[1:])
    delivered = status is ParcelStatus.DELIVERED

    delivered_at = None
    if delivered:
        # Only a delivery event may date a delivery — not whichever event
        # happens to be newest after a fallback.
        for event in events:
            if _lookup(event.get("actionCode")) is ParcelStatus.DELIVERED:
                delivered_at = to_iso_timestamp(event.get("opTimestamp"))
                break

    planned_from = planned_to = None
    if status is ParcelStatus.OUT_FOR_DELIVERY:
        # The gateway carries no ETA field, but OUT_FOR_DELIVERY means "on a
        # delivery vehicle today", so the newest departure scan is a real ETA
        # signal: it opens the window, and the end of its own day closes it.
        for event in events:
            if _lookup(event.get("actionCode")) is ParcelStatus.OUT_FOR_DELIVERY:
                planned_from = to_iso_timestamp(event.get("opTimestamp"))
                planned_to = _end_of_delivery_day(event)
                break

    return {
        "carrier": "Ecoscooting",
        "barcode": tracking_code,
        "sender": None,
        "receiver": None,
        "status": status,
        "raw_status": newest.get("statusName")
        or newest.get("description")
        or action_code,
        "delivered": delivered,
        "delivered_at": delivered_at,
        "planned_from": planned_from,
        "planned_to": planned_to,
        "pickup": status is ParcelStatus.AT_PICKUP_POINT,
        "pickup_point": _pickup_point(raw),
        "url": tracking_url(tracking_code),
        "weight": _weight_kg(raw),
        "dimensions": None,
        "history": build_history(events) if include_history else None,
        "raw": raw,
    }


def sort_parcels_by_ts(
    parcels: list[dict], key_field: str, *, descending: bool = False
) -> list[dict]:
    """Return normalised parcels sorted by the ISO timestamp at ``key_field``.

    The suite's sort contract: incoming/outgoing ascending on ``planned_from``,
    delivered descending on ``delivered_at``. Parcels whose value is missing or
    unparseable always sort to the end, regardless of ``descending``.
    """
    with_ts: list[tuple[datetime, dict]] = []
    without_ts: list[dict] = []
    for parcel in parcels:
        parsed = parse_iso(parcel.get(key_field))
        if parsed is None:
            without_ts.append(parcel)
        else:
            with_ts.append((parsed, parcel))
    with_ts.sort(key=lambda item: item[0], reverse=descending)
    return [parcel for _, parcel in with_ts] + without_ts


def apply_delivered_filter(parcels: list[dict], entry: ConfigEntry) -> list[dict]:
    """Trim the delivered list per the entry's retention option.

    ``parcels`` must already be sorted newest-first. ``days`` keeps deliveries
    from the last N days (an unparseable ``delivered_at`` is kept rather than
    silently dropped); the ``parcels`` type keeps the N most recent. Parcels
    stay *tracked* either way — this only controls what the delivered sensor
    shows.
    """
    options = entry.options
    filter_type = options.get(CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE)
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(UTC) - timedelta(days=amount)
        return [
            parcel
            for parcel in parcels
            if (parsed := parse_iso(parcel.get("delivered_at"))) is None
            or parsed >= cutoff
        ]
    return parcels[:amount]
