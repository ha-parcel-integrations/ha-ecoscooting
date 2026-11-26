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

from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta, timezone, tzinfo
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.util import dt as dt_util

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
        parsed = parse_iso(timestamp)
        if parsed is None:
            continue
        entries.append((parsed, entry))
    entries.sort(key=lambda item: item[0])
    return [entry for _, entry in entries][-max_events:]


def tracking_url(tracking_code: str | None) -> str | None:
    """Construct the consumer tracking deep-link for a parcel."""
    if not tracking_code:
        return None
    return TRACKING_URL.format(tracking_code=tracking_code)


def _moment(value: Any, zone: tzinfo) -> tuple[datetime, bool, str] | None:
    """Return ``(aware datetime, date_only, text)`` for a carrier time, or ``None``.

    A naive time or a bare date gets ``zone``; an aware time keeps its offset
    and, when it came in as a string, its own spelling.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        moment = datetime.combine(value, time.min, tzinfo=zone)
        return moment, True, moment.isoformat()
    elif isinstance(value, str) and value:
        try:
            if len(value) == 10:
                moment = datetime.combine(
                    date.fromisoformat(value), time.min, tzinfo=zone
                )
                return moment, True, moment.isoformat()
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo:
            return parsed, False, value
    else:
        return None
    moment = parsed if parsed.tzinfo else parsed.replace(tzinfo=zone)
    return moment, False, moment.isoformat()


def _end_of_day(moment: datetime, zone: tzinfo) -> datetime:
    local = moment.astimezone(zone)
    return datetime.combine(local.date(), time(23, 59, 59), tzinfo=zone)


def planned_window(
    start: Any, end: Any, *, zone: tzinfo
) -> tuple[str | None, str | None]:
    """Return ``(planned_from, planned_to)`` in the canonical window shape.

    ``start`` and ``end`` are ISO strings, dates or datetimes, or ``None``; an
    epoch number must be converted first (see ``to_iso_timestamp``). A window
    keeps both ends. One moment with a time ends at ``23:59:59`` the same local
    day; a date covers ``00:00:00`` to ``23:59:59``; two dates run from the
    first day's start to the last day's end. Equal values are one moment,
    reversed values are swapped, and ``planned_to`` is never set without
    ``planned_from``. ``zone`` is the carrier's own zone: it dates naive times
    and bare dates and decides which day a moment ends on, so a ``Z`` moment
    still ends on the carrier's local day. When nothing is left of that day
    after the moment, there is no ``planned_to``.
    """
    first = _moment(start, zone)
    last = _moment(end, zone)
    if first is None:
        first, last = last, None
    if first is None:
        return None, None

    def closing(moment: tuple[datetime, bool, str]) -> datetime:
        return _end_of_day(moment[0], zone) if moment[1] else moment[0]

    if last is not None and closing(last) < first[0]:
        first, last = last, first
    if last is not None and closing(last) == first[0]:
        last = None

    if last is None:
        end_of_day = _end_of_day(first[0], zone)
        return first[2], end_of_day.isoformat() if end_of_day > first[0] else None
    return first[2], closing(last).isoformat() if last[1] else last[2]


_NO_WINDOW_STATUSES = frozenset(
    {ParcelStatus.AT_PICKUP_POINT, ParcelStatus.RETURNING, ParcelStatus.DELIVERED}
)


def planned_for_status(
    status: ParcelStatus, planned_from: str | None, planned_to: str | None
) -> tuple[str | None, str | None]:
    """Empty the delivery window once the parcel is at its destination or done."""
    if status in _NO_WINDOW_STATUSES:
        return None, None
    return planned_from, planned_to


def is_pickup(
    route_flag: bool | None,
    status: ParcelStatus,
    event_statuses: Iterable[ParcelStatus | None] = (),
) -> bool:
    """Whether the delivery route ends at a pickup point.

    The carrier's own route flag wins; otherwise the parcel having reached
    ``AT_PICKUP_POINT`` at any point counts, so the value survives collection.
    """
    if route_flag:
        return True
    return status is ParcelStatus.AT_PICKUP_POINT or any(
        event is ParcelStatus.AT_PICKUP_POINT for event in event_statuses
    )


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


def _station(raw: dict) -> dict | None:
    """Return the ``popStationParam`` block, or ``None`` when there is none.

    The gateway sends it only for a parcel routed to a pickup point, and keeps
    sending it after collection, so its presence says where the route ends. A
    block that is not an object is reported and ignored.
    """
    station = raw.get("popStationParam")
    if isinstance(station, dict):
        return station or None
    if station is not None:
        _warn_pickup_point_shape(type(station).__name__)
    return None


def _pickup_point(station: dict) -> str | None:
    """Return the pickup point's shop name, or ``None``.

    The address, PIN, phone number and opening hours are deliberately never
    read: the PIN is a collection credential and the rest is noise on a parcel
    sensor. A name spelled ``NaN`` is the carrier having no value.
    """
    name = str(station.get("stationName") or "").strip()
    if not name or name.upper() == "NAN":
        _warn_pickup_point_shape(",".join(sorted(station)))
        return None
    return name


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


def _delivery_zone(event: dict) -> tzinfo:
    """Return the courier's zone for an event: its own display offset.

    The display offset varies per event, so it is read from the event that
    opens the window rather than from the parcel. Without a usable offset the
    parcel is placed in Home Assistant's zone.
    """
    try:
        return timezone(timedelta(hours=int(str(event.get("timezone")).strip())))
    except (TypeError, ValueError):
        return dt_util.get_default_time_zone()


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
                planned_from, planned_to = planned_window(
                    to_iso_timestamp(event.get("opTimestamp")),
                    None,
                    zone=_delivery_zone(event),
                )
                break
    planned_from, planned_to = planned_for_status(status, planned_from, planned_to)

    station = _station(raw)
    pickup = is_pickup(
        station is not None,
        status,
        (_lookup(event.get("actionCode")) for event in events),
    )

    return {
        "carrier": "Ecoscooting",
        "barcode": tracking_code,
        "sender": None,
        "receiver": None,
        "contents": None,
        "status": status,
        "raw_status": newest.get("statusName")
        or newest.get("description")
        or action_code,
        "delivered": delivered,
        "delivered_at": delivered_at,
        "planned_from": planned_from,
        "planned_to": planned_to,
        "pickup": pickup,
        "pickup_point": _pickup_point(station) if pickup and station else None,
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
