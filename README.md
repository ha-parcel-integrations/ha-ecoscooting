# Ecoscooting Parcel Tracker

[![Release](https://img.shields.io/github/v/release/ha-parcel-integrations/ha-ecoscooting.svg)](https://github.com/ha-parcel-integrations/ha-ecoscooting/releases)
[![Downloads](https://img.shields.io/github/downloads/ha-parcel-integrations/ha-ecoscooting/total.svg)](https://github.com/ha-parcel-integrations/ha-ecoscooting/releases)
[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> 💬 Questions or feedback? Join the discussion on the [Home Assistant community](https://community.home-assistant.io/t/packages-postnl-dhl-nl-dpd-and-gls-parcel-integration/112433/).

A custom Home Assistant integration that tracks your [Ecoscooting](https://www.ecoscooting.com) parcels in Spain and Portugal. Ecoscooting delivers e-commerce parcels to the door and to pickup points. No account is needed — you enter the tracking reference yourself, just like on the Ecoscooting website.

Part of the [ha-parcel-integrations](https://ha-parcel-integrations.github.io/) family: it publishes the same canonical parcel format, statuses and events as the other carrier integrations, so it plugs straight into the [Parcel Aggregator](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) and cross-carrier automations.

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Options](#options)
- [Removal](#removal)
- [Sensors](#sensors)
- [Parcel status reference](#parcel-status-reference)
- [Events](#events)
- [Services](#services)
- [Examples](#examples)
- [Debugging](#debugging)
- [Troubleshooting](#troubleshooting)
- [Related integrations](#related-integrations)
- [Disclaimer](#disclaimer)
- [Contributing](#contributing)
- [License](#license)

## Features

- Track any number of Ecoscooting parcels by tracking code — no account needed
- Per-parcel sensor with the canonical status (`registered` / `in_transit` / `out_for_delivery` / `delivered` / …), the carrier's own status text, the parcel weight, the pickup point where applicable and a tracking deep-link
- Summary sensors: incoming parcels, next delivery, en route to a pickup point, awaiting pickup, recently delivered parcels
- `ecoscooting.track_parcel` / `ecoscooting.untrack_parcel` services, so a dashboard button can add a parcel
- Events + device triggers for no-code automations (parcel registered, status changed, delivered)
- Opt-in per-parcel status history
- Manual refresh button and a diagnostic last-update sensor

## Requirements

- Home Assistant 2025.12 or newer
- An Ecoscooting parcel and its tracking reference (from the shipping
  confirmation email, the webshop's order page or the missed-delivery card) —
  no account needed. Both reference formats Ecoscooting uses work: an
  18-digit number, or `CNPRT`/`CNESP` followed by 20 digits

## Installation

### HACS (recommended)

1. In HACS, choose the three-dot menu → **Custom repositories**.
2. Add `https://github.com/ha-parcel-integrations/ha-ecoscooting` as an **Integration**.
3. Install **Ecoscooting** and restart Home Assistant.

### Manual

Copy `custom_components/ecoscooting` into your `config/custom_components/` folder and restart Home Assistant.

## Configuration

Add the integration via **Settings → Devices & Services → Add Integration → Ecoscooting**. There is nothing to fill in: the hub is created immediately (Ecoscooting tracking needs no account).

Then add parcels via the integration's **Configure** dialog, the [`ecoscooting.track_parcel`](#services) service, or a [dashboard button](examples/dashboards/add_parcel_card.yaml). The tracking reference is on your shipping confirmation email, the webshop's order page or the missed-delivery card.

## Options

Open **Configure** on the integration entry:

| Section | Option | Default | Description |
|---|---|---|---|
| Parcels | Add / remove | — | Manage the tracked tracking codes. Changes apply immediately, no restart. |
| Delivered parcels | Filter by / amount | last 7 days | How long delivered parcels stay visible on the delivered sensor. |
| Parcel history | Include status history | off | Adds a `history` attribute per parcel with each status update. |

Polling isn't one of these settings: the integration polls on a dynamic,
status-driven schedule with nothing to configure.

## Dynamic polling

Polling isn't a setting here — the integration adjusts its own cadence to
what your tracked parcels are actually doing:

- **Quiet hours** — no polling between 00:00–06:00 local time, aside from one
  catch-up check at each end of that window (around midnight and around 6
  AM), so an overnight update is never missed.
- **Hot (every 15 minutes)** — while any tracked parcel is out for delivery
  today, starting an hour before its delivery window opens (or immediately if
  no window is known yet).
- **Normal (every 45 minutes)** — for anything else still on its way.
- **Fully paused** — once every tracked parcel has been delivered, or nothing
  is tracked at all, polling stops until you add a parcel back (adding one
  always triggers an immediate check, regardless of the pause).
- A small, fixed per-hub offset is added on top, so not every Ecoscooting
  hub out there polls at exactly the same second.

Ecoscooting publishes no expected delivery time, so a parcel that is out for delivery is always in the 15-minute tier until it is delivered.

## Removal

Standard HA removal applies: **Settings → Devices & Services → Ecoscooting → ⋮ → Delete**. Nothing is stored on Ecoscooting's side.

## Sensors

| Entity | Description |
|---|---|
| `sensor.ecoscooting_incoming_parcels` | Number of active tracked parcels, full list under the `parcels` attribute |
| `sensor.ecoscooting_parcel_<code>` | One per tracked parcel; state is the canonical status, attributes carry the full normalised parcel |
| `sensor.ecoscooting_next_delivery` | Earliest expected delivery moment across all active parcels. Ecoscooting publishes no expected delivery time, so this fills only while a parcel is out for delivery — the day it is on the van |
| `sensor.ecoscooting_awaiting_pickup` | Parcels waiting for you at a pickup point |
| `sensor.ecoscooting_delivered_parcels` | Recently delivered parcels (see the retention option) |
| `sensor.ecoscooting_last_successful_update` | Diagnostic: when Ecoscooting was last polled successfully |
| `calendar.ecoscooting_deliveries` | Read-only calendar; a parcel appears on the day it goes out for delivery |

A delivered parcel moves from its per-parcel sensor to the delivered sensor automatically.

## Parcel status reference

The `status` field is the carrier-agnostic enum shared by the whole integration family:

| Status | Meaning |
|---|---|
| `registered` | The shipment was created, Ecoscooting does not have it yet |
| `in_transit` | In the sorting network or accepted by the last-mile station |
| `out_for_delivery` | With the courier today |
| `at_pickup_point` | At a pickup point — including "delivered to PUDO", which means the shop signed for it, not you |
| `delivered` | Handed over to you, at your door or at the pickup point |
| `returning` | On its way back to the sender, including a parcel left uncollected past its deadline |
| `problem` | Ecoscooting reports an exception, such as a failed delivery attempt or a lost parcel |
| `unknown` | Not yet scanned, or a status we have not mapped yet |

The carrier's own human-readable text is always available as `raw_status`.

## Events

The integration fires these on the event bus (also available as device triggers on the Ecoscooting device):

| Event | When |
|---|---|
| `ecoscooting_parcel_registered` | A new parcel appears in the active list |
| `ecoscooting_parcel_status_changed` | A parcel's canonical status changes (`old_status` / `new_status` in the payload), except the final hop to delivered |
| `ecoscooting_parcel_delivered` | A parcel is delivered |
| `ecoscooting_parcel_delivery_time_changed` | The expected delivery window changes — Ecoscooting publishes no delivery time, so this fires only when a parcel goes out for delivery, or goes out again on a later day |

Every payload is the full normalised parcel plus the hub's `device_id`. Events are suppressed on the first refresh after start-up.

## Services

| Service | Fields | Description |
|---|---|---|
| `ecoscooting.track_parcel` | `tracking_code` | Start tracking a parcel |
| `ecoscooting.untrack_parcel` | `tracking_code` | Stop tracking a parcel |

## Examples

Ready-to-paste automations and dashboard snippets live in [`examples/`](examples/), including tracking a new parcel straight from a dashboard.

### Community Lovelace cards

Third-party cards that work with this integration's sensors:

- [jonisnet/hki-parcels-card](https://github.com/jonisnet/hki-parcels-card)
- [klaptafel/ha-package-tracker-card](https://github.com/klaptafel/ha-package-tracker-card)

## Debugging

```yaml
logger:
  logs:
    custom_components.ecoscooting: debug
```

## Troubleshooting

- **A parcel shows `unknown`** — Ecoscooting has not scanned it yet, or the reference is wrong. Its tracking answers the same "not found" either way, so the two cannot be told apart; a valid reference picks up automatically once scanned.
- **Every parcel stops updating at once, for everyone** — this is not something a setting can fix. Please [open an issue](https://github.com/ha-parcel-integrations/ha-ecoscooting/issues/new); it needs a code change.
- **A status logs "Unrecognised Ecoscooting status"** — please [open an issue](https://github.com/ha-parcel-integrations/ha-ecoscooting/issues/new) with the logged line so the mapping can be extended.

## Related integrations

This integration is part of [**ha-parcel-integrations**](https://ha-parcel-integrations.github.io/) — a family of
parcel-carrier integrations that all publish the same canonical parcel format,
statuses and events.

- [**Parcel Aggregator**](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) rolls every installed carrier
  up into one set of sensors.
- Browse [the organisation](https://ha-parcel-integrations.github.io/) for the current list of supported carriers.

## Disclaimer

This is an independent, community-built project. It is not affiliated with, endorsed by, sponsored by, or supported by Ecoscooting, Home Assistant, or any other third party referenced in this project. Please don't contact Ecoscooting for support with this integration.

All third-party trademarks, trade names, product names, logos, and other brand assets are the property of their respective owners. References to them are solely to identify the relevant carrier or service and do not imply affiliation, sponsorship, or endorsement. Nothing in this project grants or implies any licence or right to use third-party brand assets.

This integration may rely on public, unofficial, or undocumented carrier interfaces, accessed with your own account or API key where required. These may change or be withdrawn without notice and may be subject to Ecoscooting's terms. Data is sent only to Ecoscooting's own services or those of its group; this project operates no servers of its own. You are responsible for ensuring that your use complies with applicable law and those terms. Use is at your own risk; see the [licence](LICENSE) for warranty limitations.

This integration uses the same public tracking endpoint as the Ecoscooting consumer website. The delivery, pickup-point, failed-delivery and return paths are all confirmed against real parcels; any status Ecoscooting reports that this integration does not know yet is logged with a link to report it.

## Contributing

Pull requests and issues are welcome. Please open an issue before
submitting a large change.

## License

[MIT](LICENSE)
