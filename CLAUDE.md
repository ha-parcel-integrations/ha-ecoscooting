# Working in this repository

Home Assistant custom integration for **Ecoscooting** parcel tracking.
Distributed via HACS; not part of HA core. One carrier in the
[ha-parcel-integrations](https://github.com/ha-parcel-integrations) suite,
**generated from ha-carrier-template** — everything outside *Carrier-specific
notes* is suite-wide; when in doubt check the template or a sibling repo.
No DTO layer.

API mechanics — endpoints, parameters, status vocabularies — live in the
private `carrier-research/ecoscooting/api/` and are **never** copied here.

## Shared conventions — fetch when relevant

Suite-wide rules live in
[`.github/CONVENTIONS.md`](https://github.com/ha-parcel-integrations/.github/blob/main/CONVENTIONS.md)
and are **not** repeated here. Don't fetch it every session — fetch it **before**
you act in one of these areas:

| Before you … | Fetch `CONVENTIONS.md` § |
|---|---|
| touch entities, sensors, config/options flow, coordinator, diagnostics, translations | *Home Assistant developer docs* (its table points on to the canonical HA page — don't rely on memory) |
| add/rename a parcel field, a `ParcelStatus`, or a bus event; change the sort/first-refresh; touch unmapped-status logging | *Parcel contract* — exact key set, units, sort, events + suppression; `test_parcels.py::test_normalize_publishes_exactly_the_canonical_keys` guards the key set |
| change which optional field this carrier populates vs. always returns `None` | Update `const.py`'s `CAPABILITIES` in the same commit — it feeds the comparison table on the docs site, so a field that starts (or stops) coming back non-null and isn't reflected there is a wrong claim on the website, not just a stale comment. If this carrier has more than one backend (a country-specific transport, not just a config option) with genuinely different field support, `CAPABILITIES` should be a `CAPABILITIES_BY_VARIANT` dict instead — one frozenset per backend, so a field only some backends populate doesn't get silently intersected away or overclaimed for the rest. A field you could not verify (no real parcel yet) goes in `PENDING_CAPABILITIES` instead of being left out — the site then says "awaiting data" rather than claiming the API never exposes it; move it into `CAPABILITIES` once confirmed |
| ship anything while below 1.0.0 (unconfirmed data) | *Pre-1.0 releases* — one-shot WARNINGs for every guessed shape/code |
| consider "fixing" a lint/pattern the skill flags (poll interval, inline client, sync requests) | *Deliberate skill divergences* — likely intentional, don't re-flag |
| commit, bump, tag, release, or write release notes; add a feature without a test | *Workflow / Commits / Versioning / Testing* |

**Suite-wide tripwires, kept inline on purpose:**
- **First refresh in `__init__.py`, before `async_forward_entry_setups`** — from
  a forwarded platform HA can't catch `ConfigEntryNotReady` and half-sets-up the
  entry. Runtime-only; tests don't catch a regression.
- **Setup stale-entity sweep is scoped to `domain == "sensor"` and skips
  `non_parcel_unique_ids`** — else it deletes the refresh button / the
  summary+diagnostic sensors. Add a new non-parcel sensor's unique_id to the set.
- **Per-parcel sensors are removed by the summary sensor** via
  `entity_registry.async_remove` (self-removal races and leaves ghosts).
- **The optional pickup-point summaries are a pair.** A carrier that can tell
  a parcel is destined for a pickup point exposes
  `en_route_to_pickup_point` for `pickup is true` before it arrives; one that
  can reach `ParcelStatus.AT_PICKUP_POINT` also exposes `awaiting_pickup`.
  See *Parcel contract* in `CONVENTIONS.md`. Say "pickup point", not
  "ServicePoint"/"parcel shop"/"locker", for the generic concept; the
  example carrier demonstrates both canonical sensors. **This carrier ships
  only `awaiting_pickup`:** it learns a parcel is going to a pickup point no
  earlier than its arrival there, so `pickup` is never true before
  `at_pickup_point` and `en_route_to_pickup_point` could only ever read zero.
  Do not restore it from the template.

## Carrier-specific notes

Ecoscooting is a **code-based, account-less** carrier: one tracking reference
per parcel, no second factor, no login. Everything below is an integration
decision; the endpoint mechanics live in the private research notes.

**The lookup cannot prove a parcel does not exist.** An unknown reference and
a reference whose shipment is merely unavailable come back identically, so
`api.py` reports that case as not-found (`None`) and the coordinator keeps a
parcel's last-good state instead of blanking it. Never surface it to the user
as "wrong code".

**Nothing may be inferred from the HTTP status.** Every reply is `200`,
including the rejections. `api.py` branches on the body alone, and treats a
reply whose echoed reference is not the one we asked for as an error rather
than as that parcel's data.

**Status comes from the newest event, and the return leg wins.** The event
vocabulary has two families that both occur on the same references, and the
whole return leg is the forward-leg code names behind an `RT_` prefix — so
`RT_LM_SIGN_SUCCESS` is the *sender* signing for it. `parcels.py` therefore
tests the prefix **before** the status map; reordering that would publish a
returned parcel as delivered. "Delivered to PUDO" is likewise the shop
signing, not the recipient, so it maps to `at_pickup_point`. An unmapped code
falls back to the newest event below it that does map, and warns once.

**Instants come only from the events' epoch field.** Each event also carries
a display string whose UTC offset lives in a *separate* field, and that
offset varies per event within one parcel. An event without a usable epoch
value is dropped from the history rather than dated from the display string.

**The only ETA signal is the delivery round itself.** The reply carries no
expected-delivery field, but `out_for_delivery` means the parcel is on a van
that day, so `normalize_parcel` derives the window from that departure scan —
the newest one, so a second delivery day does not report the first day's.
`planned_from` is its instant and `planned_to` the end of its own local day,
read from that one event's offset rather than from the parcel: the offset
varies per event, but on the event that opens the window it is the courier's
own clock. Without a usable offset `planned_to` is left empty rather than
spanning the wrong day. The derivation is scoped to that one status, so a
delivered or returning parcel reports no ETA even though the scan is still in
its history. A whole-day end is also what keeps the parcel on the calendar
for the rest of the delivery day — a from-only window would drop off it an
hour after the van left.

**`None` on purpose:** no sender, no receiver and no dimensions — the reply
has no such fields. Weight is grams; a unit other than grams warns once and
publishes nothing rather than a number off by a factor of a thousand.

**The pickup point is kept after collection** so a collected parcel still
shows where it was collected. Its PIN, phone number and opening hours are
never read: the PIN is a collection credential. Address parts the carrier has
no value for arrive spelled `NaN` and are left out.

**Diagnostics redact more than usual.** A reference embeds the destination
postcode, and the delivery event carries the coordinates of the door and a
link to a photo of the drop-off, so the redaction list covers the references,
the routing ids, the pickup-point block, the coordinates, the photo link,
every timestamp — the derived delivery window included, since it says when a
courier set out for this address — and the per-event offset, which narrows
down a region. `raw` itself is never trimmed — privacy lives in the
redaction list. For the same reason a reference never reaches a log line
either: `coordinator.py`'s `_log_key` logs a short digest of it, which is
enough to match two lines about one parcel without naming it.

**Troubleshooting a sudden, universal failure.** Every parcel for every user
failing at once, with replies that are not the per-parcel not-found, is never
a parcel problem and never something a user-side setting can work around — it
needs a code change in `api.py`. A single parcel failing while others succeed
is never this. The private research notes say what to check.

**Not supported, deliberately:** ICP references (a different normalisation),
other `CN…` reference families that belong to other networks, discovery or
bulk guessing of references, and any write operation. Outgoing parcels are
out of scope: Ecoscooting is a last-mile delivery partner for webshops, with
no consumer-sending surface.

## Options and reloads

For code-based carriers, the options flow starts with exactly `Parcels` and
`Settings` — or, where the carrier supports outgoing parcels, `Incoming
parcels` / `Outgoing parcels` / `Settings` (see the next section).
`Parcels` is one editable multi-code list; `Settings` is
a flat form — some carriers use one sectioned form
(`data_entry_flow.section`) instead; both are generator variants, not carrier
decisions. Changes apply without a restart. Two models, **do not mix them**:
- **Account-less carriers** (the default, and the `--auth byo-key` build) apply
  changes live: an update listener calls `async_request_refresh()`, so
  added/removed parcel sensors appear immediately (this is also the resume
  path after polling has fully suspended — see "Dynamic polling" below).
- **Account-based carriers** call `async_schedule_reload` on submit and register
  **no** update listener. Combining a listener with a reload-on-update flow is
  deprecated, an error in HA 2026.12+.

## Auth models

This repo is the keyless build: the config flow asks for nothing but the
tracking codes themselves, there is no credential to store, and there is no
reauth flow. The template's other two builds (`--auth byo-key`, which adds a
user-supplied API key, and `--auth credentials`, which replaces user-entered
codes with an account feed) do not apply here — see `ha-carrier-template` if
you ever need them.

## Incoming and outgoing parcels

**Add outgoing support whenever the carrier lets a consumer send a parcel** —
a C2C shipment, a locker drop-off, a marketplace or returns label. It is not
an optional extra to bolt on later: without it a parcel the user sent counts
towards `incoming_active` and sits on their dashboard next to the ones they
are waiting for. A carrier that genuinely has no consumer-sending surface is
exempt — say so in *Carrier-specific notes* so the gap reads as a decision.

Where the direction comes from has exactly two answers, and which one applies
follows from the carrier, not from taste:

- **Account-based** — the account feed distinguishes them, so derive it and
  never ask the user. A shipment matching neither side logs a one-shot
  warning and defaults to incoming rather than disappearing from every list.
  References: `ha-ppl-cz` (one call, split on a field), `ha-dhl-nl` (a
  separate "sent" endpoint).
- **Account-less** — the payload cannot reveal it (the user's own parcel and
  a stranger's look alike, and there is no account identity to compare a
  party against), so the **user declares it per parcel**: two menu entries in
  the options flow, each the same multi-code list, plus a `direction` field on
  `track_parcel`. Store it as `CONF_DIRECTION` on the `CONF_PARCELS` dicts,
  defaulting to incoming, so entries written before the option existed need no
  migration. Re-filing a code under the other direction moves it instead of
  erroring — that is the correction path. Reference: `ha-packeta`.
  **Never infer direction from free-text event wording**: a handover sentence
  that happens to name the drop-off point is not a structured field, says
  nothing before handover, and silently ties the split to one locale.

Above that split the shape is identical either way, and is suite-wide:
`coordinator.outgoing` / `coordinator.delivered_outgoing` alongside `data` /
`delivered`; `outgoing_parcels` + `outgoing_delivered_parcels` summary
sensors (their unique_ids belong in `non_parcel_unique_ids`); per-parcel
sensors spawned for **both** directions; the
`<domain>_outgoing_parcel_status_changed` / `_outgoing_parcel_delivered`
event pair, with **no** `registered` and no delivery-time event for outgoing;
`awaiting_pickup`, `next_delivery` and the calendar staying incoming-only;
and both new lists in `diagnostics.py`. The aggregator needs no change — it
buckets on the sensor suffix and the event prefix. Also set
`directions: incoming+outgoing` for the carrier in the docs site's
`data/carriers.yml`.

## Tracking-code validation

In code-based carriers (account-based sources have no code entry),
`valid_tracking_code` in `config_flow.py` accepts every non-empty code — no
format regex. This is a suite-wide convention, not a per-carrier TODO: real
tracking-number formats vary too much across carriers, and are often not
fully confirmed even for this one, to gate on a guessed shape. A too-strict
regex risks rejecting a genuinely valid code; an actually-bad code just comes
back "not found" on the next poll, which is a far cheaper failure mode. Do
not add one back in, even once the format is confirmed.

## Dynamic polling

There is no user-facing polling interval — this is a deliberate suite-wide
choice, not a gap. `coordinator.py`'s `_hottest_tier_minutes` /
`_next_update_interval` recompute `update_interval` at the end of every
refresh. The reference carrier in `ha-carrier-template` (its `coordinator.py`)
is the canonical implementation every carrier mirrors; the design rationale (quiet window, tiers, stagger,
backoff, delivered-skip) is spelled out below.

- **Quiet window:** no polling 00:00–06:00 local time, except two daily
  anchors (~00:00 and ~06:00) for overnight / end-of-day catch-up.
- **Tiers while polling:** *hot* (15 min) when a tracked, not-yet-delivered
  parcel is `out_for_delivery` within an hour of its `planned_from` (or has no
  `planned_from` at all); *mid* (45 min) for anything else still in flight —
  `problem`/`returning` included, deliberately not hot. Account-based carriers
  never fully stop even with nothing hot or in transit: the mid-tier poll is
  also how a new shipment gets discovered.
- **Full stop (account-less carriers only):** `update_interval = None` when
  nothing is tracked or every tracked parcel is delivered. Resumes the moment
  a parcel is added back, via the options-flow refresh above.
- **Stagger:** a small, stable per-install offset (hash of the config entry
  id) is added to every computed interval so installs don't all hit an anchor
  or tier boundary at the same second.
- **429 backoff:** a 429 anywhere in a poll raises `UpdateFailed` with
  `retry_after` — the carrier's own `Retry-After` header if present, otherwise
  an exponential backoff tracked per-coordinator. `api.py`'s
  `…ApiError.status_code` / `.retry_after` carry this from the HTTP layer.
- **Delivered codes are skipped from the fetch (account-less carriers only):**
  once a tracking code's payload comes back `delivered`, `coordinator.py`
  excludes it from the next cycle's fetch — its payload can never change
  again. `self._delivered_codes` (keyed on the tracking code, not the barcode)
  is rebuilt from each cycle's results and intersected with the tracked set on
  untrack. The code stays in the options list, keeps its sensor and its
  cached payload, and still shows under the retention window — it just costs
  no more requests. `coordinator.delivered_codes` surfaces the count in
  diagnostics. Account-based carriers have nothing to skip here — one account
  call already returns everything, so they either have no `delivered_codes` or
  it is always empty.

A carrier that genuinely throttles or soft-bans traffic harder than the 429
backoff handles is a documented, local divergence from this in that one
repo's own `CLAUDE.md` — not a generator flag.

## Module layout

| File | Carrier-specific? |
|---|---|
| `api.py` (HTTP client, error types; `account/` and `tracking/` packages instead when a repo has two sources) | **yes** |
| `const.py` (domain, URLs, `ParcelStatus`, option keys) | partly (URLs) |
| `parcels.py` (status map, `normalize_parcel`, history, sort, filters — pure, no I/O) | partly (the status map, `normalize_parcel`) |
| `coordinator.py` (fetch, cache, event firing) | mostly not |
| `config_flow.py` | partly (code validation; key/credential validation on `--auth byo-key`/`credentials`) |
| `sensor.py` / `button.py` / `calendar.py` / `device_trigger.py` | no |
| `device.py` (shared device-info helper) | no |
| `diagnostics.py` | partly (`TO_REDACT`) |
| `services.py` (`track_parcel` / `untrack_parcel`, account-less only) | no |

`parcels.py` is deliberately free of I/O and HA objects so the per-carrier part
stays unit-testable without Home Assistant. Config: `ConfigEntry.runtime_data`
(typed, no `hass.data`), `PARALLEL_UPDATES = 0`, coordinator takes
`config_entry=entry`. `aiohttp.ClientError` is caught **per parcel** in the gather
loop (one bad parcel doesn't fail the poll) but **not** around the whole update
(the coordinator wraps that). Entities: `has_entity_name` + `translation_key`,
`icons.json`, translated units, `_attr_attribution`, `_unrecorded_attributes` on
anything with a parcel list or `raw`. Over-redact diagnostics — they get pasted
into public issues.

## Running tests

```
python -m pytest tests/ --cov=custom_components.ecoscooting
```

Coverage must stay **above 95%** (silver `test-coverage` rule). Run before
committing. A code change updates the README + this file + `docs/` in the same
commit; the API reference lives in your own private research notes, never in
this repo.
