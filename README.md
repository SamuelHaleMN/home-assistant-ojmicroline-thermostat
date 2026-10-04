<!-- PROJECT SHIELDS -->
[![hacs_badge][hacs-shield]][hacs-url]
![Project Stage][project-stage-shield]

![Project Maintenance][maintenance-shield]
[![Maintainability][maintainability-shield]][maintainability-url]

# OJ Microline Thermostat Integration for Home Assistant

The OJ Microline Thermostat integration allows you to control your
thermostat from Home Assistant.

It has been tested and developed on the following models:

## Supported models

| Model            |
|------------------|
| OWD5             |
| UWG4             |
| WCD5             |

After installation you can add the thermostat through the integration page. Currently setting a preset mode and temperature is supported. Only the heat HVAC mode is supported. Use the schedule/manual presets for regulation mode; unsupported OFF/AUTO requests fail visibly.

## Warm Tiles owner release

This fork is based on upstream 1.5.0 and keeps its pinned `ojmicroline-thermostat==3.6.0` dependency. Release `1.5.1` adds a small WG4 reliability adapter while WD5 continues to use the upstream client.

Release `1.6.0` adds the WG4 native weekly program sensor, patch action and
dashboard editor. Existing climate entity identities and presets are preserved.

Release `1.6.1` registers the native card as a versioned Lovelace module as well
as a global extra module. This gives cached mobile clients a dashboard-specific
discovery path. The module's existing resource ID is preserved on upgrades;
unrelated resources are unchanged. Narrow editors use two-column event rows.
After updating, reload the app's frontend so its existing card definition is
replaced. YAML-managed resource lists need the native card URL added explicitly
as a `module`; the integration does not edit YAML configuration.

For ESW WiFi Warm Tiles ColorTouch, select **WG4 series**, host **warmtiles.mythermostat.info** (without `https://`), and **Application 13**. The default temperature policy is manual. WG4 uses conservative five-minute account polling in this release; it does not depend on the draft WG4 push implementation.

If migrating this owner's legacy `schluter` entry, first back up HA and disable that entry. Select **Migrate existing Warm Tiles** and choose the saved entry. Credentials are read inside HA and validated against the Warm Tiles cloud. Installation and migration issue no thermostat commands. Rename legacy climate entity IDs to recorded backup names before assigning the desired IDs to the new entities; preserve device/area and external references separately.

The adapter renews expired read sessions once, rejects invalid/empty login responses, serializes authentication and device commands, and bounds complete HTTP/JSON operations. Writes are never replayed automatically after an uncertain response. Rejected commands and failed cloud-readback verification reach the caller. Cloud readback does not prove physical device acknowledgement. Offline/missing devices and failed polling are unavailable, and unsupported WG4 energy is not exposed as a fabricated zero total.

True WG4 remote OFF and vacation control are not established here. WG4 native weekly editing uses the separate action/card described below. Thermostat hardware and local protection settings remain under the device's control. Do not translate OFF into manual heating or a low setpoint.

Before HA or integration upgrades, qualify native setup, authentication, climate units/modes, missing-device handling and a bounded canary; retain the prior versioned artifact. Retire local patches only after an upstream release passes the same checks. This release is owned by SamuelHaleMN; see upstream for general OJ platform development.

### Reviewed request budget and diagnostics

The background inventory is one account request every five minutes, rather than one request per thermostat. WG4 commands and complete poll publication share an account lock so late responses cannot overwrite a newer confirmed device state. A command performs one POST and at most three readback GETs, delayed by 4, 8 and 12 seconds; its full queue/write/verification budget is 90 seconds. Failed or uncertain writes are never automatically replayed.

HTTP 429 pauses the account for five minutes when no usable `Retry-After` is supplied. Integer and HTTP-date headers are honored conservatively within a 30-second to 24-hour bound; 503 also pauses when it supplies a usable header. HA schedules the next poll after the cooldown, and control calls fail locally during it. Avoid repeated reloads/restarts as a workaround for a cooldown; the cooldown belongs to the running client instance.

Changing comfort options reloads the existing entry automatically. The native HA **Download diagnostics** action reports only operational counts and timing: last successful poll, poll failures, inventory counts, authentication count, verified commands and remaining cooldown. It excludes credentials, account/device identifiers, room names and temperatures. Diagnostics do not make a cloud request.

## Requirements

Your thermostat needs to be connected to the internet. For OWD5 model thermostats you will need the API key and customer ID that is used by the app that you currently use to control your thermostat.

## HACS installation

This owner release requires Home Assistant **2026.9.3 or newer**. In HACS,
open **Custom repositories**, add
`https://github.com/SamuelHaleMN/home-assistant-ojmicroline-thermostat`
with category **Integration**, then download its qualified version. The default
catalog's upstream OJ integration uses the same domain; select the owner fork
and do not install both over the same component directory. Restart HA after
the download, then add **OJ Microline Thermostat** through Settings.

## Manual installation

Create a directory called `ojmicroline_thermostat` in the `<config directory>/custom_components/` directory on your Home Assistant instance.
Install this integration by copying all files in `/custom_components/ojmicroline_thermostat/` folder from this repo into the new `<config directory>/custom_components/ojmicroline_thermostat/` directory you just created.

## Configuration

[![ha_badge][ha-add-shield]][ha-add-url]

To configure the integration, add it using [Home Assistant integrations][ha-add-url]. This will provide you with a configuration screen where you select the API family. WG4 uses username, password, host and Application; WD5 additionally uses its customer ID and API key.

## Live updates (WD5 series)

WD5-series thermostats receive live updates through the same notification service the OJ Microline and SWATT apps use, so changes made on the thermostat or in the app show up in Home Assistant within seconds. While this connection is up, the integration polls only every 5 minutes (for energy usage and as a fallback); when it drops, polling returns to every minute and the connection is retried automatically.

## Energy statistics (WD5 series)

For every WD5-series thermostat the integration imports the energy usage history into a long-term statistic named "<thermostat> energy" (`ojmicroline_thermostat:energy_<serial>`): the last 12 months per month, the last 5 weeks per day and the last week per hour, kept up to date per hour from then on. Add it under **Settings â†’ Dashboards â†’ Energy â†’ Individual devices** to see the usage per day, week, month and year, like the apps' statistics screen.

The "Energy Usage" sensor shows today's usage (from local midnight). Use either the statistic or the sensor in the energy dashboard, not both, or the usage is counted twice.

## Native weekly programs (WG4 series)

WG4 devices get a `sensor.<name>_native_schedule` entity with all seven weekdays,
six stable event slots per day, and a `schedule_hash`. Selecting the climate
`schedule` preset activates the thermostat's stored program. Editing that
program preserves the active preset; a manual thermostat stays manual.

Add **OJ Microline native schedule** to a dashboard, or use:

```yaml
type: custom:ojmicroline-native-schedule-card
entity: sensor.kitchen_2_native_schedule
climate_entity: climate.kitchen_2
title: Kitchen 2
```

The editor shows six numbered events for each day, including inactive slots.
The first event must stay active. It uses the thermostat's local wall-clock
times and explicitly labelled Celsius or Fahrenheit. It does not use the
browser clock to infer the current event or change device clock/DST settings.
Untouched native temperatures and dormant events are preserved exactly.

**Preview** validates cached data without cloud requests. **Save** checks fresh
state against the draft's original hash, validates actual device time limits,
saves an original-program backup in HA storage, uploads the full week once,
then verifies cloud readback. A newer program edited in the vendor app rejects
an older draft. A no-change save skips the upload. There is a small unavoidable
race if an external edit occurs between the final read and the write: the
vendor API supplies no atomic revision check.

The action requires one climate target and a response. Slot identifiers are
0–5 (the card labels them Event 1–6). Patches include only changed fields;
unspecified fields, slots, weekdays, and unknown vendor data remain intact.
Nested temperatures do not receive HA climate unit conversion, so `C` or `F`
is mandatory:

```yaml
action: ojmicroline_thermostat.set_native_schedule
target:
  entity_id: climate.kitchen_2
data:
  expected_hash: "<full schedule_hash from native schedule sensor>"
  temperature_unit: F
  dry_run: true
  changes:
    - day: monday
      slot: 0
      time: "06:00"
      temperature: 80
response_variable: preview
```

Review the returned preview, then send the same draft with `dry_run: false` to
save. An unchanged patch is a no-op. Preview may report `limits_checked: false`
before the first save has retrieved account defaults. Device limits are always
checked before a changed upload. Draft editing and polling never fetch defaults;
the first changed save reads them once and caches them.
Cached device limits expire after 24 hours and refresh only on a changed save.

A changed save normally uses one fresh account GET, one thermostat POST, and
one delayed account GET, with at most three bounded readbacks under the same
account lock/cooldown as climate commands. No uncertain write is automatically
replayed. Stored backup history is bounded per device and never restored
automatically over newer app changes. Cloud readback confirms API storage;
physical upload acknowledgement and an observed schedule transition require
separate verification. Ordinary weekly transitions run on the thermostat and
do not require HA to send a cloud command at each event.

The WG4 editor is separate from the following WD5 group editor and vacation
actions. Do not use WD5's `set_schedule` format for a WG4 thermostat.

## Schedule and vacation (WD5 series)

Every WD5-series thermostat gets these extra entities. Like in the apps, schedule and vacation settings belong to the thermostat's group.

| Entity | Description |
| --- | --- |
| `sensor.<name>_schedule` | The temperature the weekly schedule prescribes right now. The attributes list every weekday's events (`time`, `temperature`, and `next_day` for events after midnight). |
| `date.<name>_vacation_begin` | The first day of the vacation. |
| `date.<name>_vacation_end` | The day normal regulation resumes (at 00:00). Moving one date past the other moves the other along. |
| `switch.<name>_vacation` | Enables the vacation period. If it has already started, vacation mode is activated immediately; switching it off returns to schedule or manual mode, whichever was used last. |

### Schedule card

The integration ships a dashboard card that shows the weekly schedule and highlights the event that is active right now. Tap a day to edit it: change, add or remove events, optionally apply the same events to other days, and save. It is loaded automatically: edit a dashboard, add a card and pick **OJ Microline schedule**, or use YAML:

```yaml
type: custom:ojmicroline-schedule-card
entity: sensor.living_room_schedule
title: Living room  # optional
climate_entity: climate.living_room  # optional; found via the device otherwise
```

### Services

| Service | Description |
| --- | --- |
| `ojmicroline_thermostat.set_schedule` | Set the events of one or more weekdays: up to 6 per day, on the quarter hour, at least 15 minutes apart, 5-40 Â°C. A time earlier than the previous one is after midnight (03:00 at the latest). The first event must be at 22:00 at the latest. |
| `ojmicroline_thermostat.set_vacation` | Set and enable a vacation from `start_date` until `end_date`. |
| `ojmicroline_thermostat.cancel_vacation` | Disable the vacation. |

```yaml
action: ojmicroline_thermostat.set_schedule
target:
  entity_id: climate.living_room
data:
  days: [monday, tuesday, wednesday, thursday, friday]
  events:
    - time: "06:00"
      temperature: 21
    - time: "08:30"
      temperature: 17
    - time: "17:00"
      temperature: 21
    - time: "22:30"
      temperature: 17
```

## Contributing

Please see [CONTRIBUTING](.github/CONTRIBUTING.md) and [CODE_OF_CONDUCT](.github/CODE_OF_CONDUCT.md) for details.

For this owner fork, use Python 3.14 and Poetry 2.3.2. The development lockfile
and HA manifest both pin the published client to 3.6.0. From a clean checkout:

```sh
poetry install --no-interaction
poetry run pytest -q tests
poetry run ruff check custom_components tests scripts
poetry run ruff format --check custom_components tests scripts
poetry run pre-commit run --all-files
```

The WG4 tests use synthetic protocol data and isolated HA interface doubles.
They cover bounded session recovery, authentication failures, command ordering,
readback, unavailable devices and migration/reauthentication. They make no live
cloud or thermostat calls. CI runs these checks from the locked dependencies;
they do not qualify WD5, real HA startup or physical device acknowledgement.
CI and the pre-commit type hook check the WG4 adapter against the pinned client;
namespace package flags distinguish HA's component directory from the client's
package name. Full HA integration typing is not claimed by this isolated check.
Pylint checks production code and qualification scripts; pytest and Ruff govern
the deliberate interface doubles in tests.

In a Linux environment with the intended HA release and the pinned client
installed, run `python -m scripts.native_smoke` from the checkout. This uses
real HA classes and the real client factory to check imports, Fahrenheit
capabilities/state, missing-device behavior and config-flow construction with
synthetic data and no cloud calls. It does not start an HA server or install
the integration. Qualify actual entry setup/reload, cloud authentication and a
bounded control canary separately before promoting a release.

## References & Thanks

- https://community.home-assistant.io/t/mwd5-wifi-thermostat-oj-electronics-microtemp/445601
- https://mdapp.medium.com/the-android-emulator-and-charles-proxy-a-love-story-595c23484e02
- https://github.com/radubacaran/mwd5
- https://github.com/klaasnicolaas
- https://github.com/adamjernst
- https://github.com/ViPeR5000

[maintainability-shield]: https://api.codeclimate.com/v1/badges/d77f7409eb02e331261b/maintainability
[maintainability-url]: https://codeclimate.com/github/robbinjanssen/python-ojmicroline-thermostat
[maintenance-shield]: https://img.shields.io/maintenance/yes/2026.svg
[project-stage-shield]: https://img.shields.io/badge/project%20stage-stable-brightgreen.svg?style=for-the-badge

[hacs-url]: https://github.com/hacs/integration
[hacs-shield]: https://img.shields.io/badge/HACS-Default-orange.svg?style=for-the-badge

[ha-add-url]: https://my.home-assistant.io/redirect/config_flow_start/?domain=ojmicroline_thermostat
[ha-add-shield]: https://my.home-assistant.io/badges/config_flow_start.svg
