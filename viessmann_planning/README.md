# Viessmann Planning

> **Branch `local-mqtt`** — this variant talks **directly to the MQTT broker** (state topics `vito/<name>`, availability `vito/LWT`, commands `vito/cmnd`) published by the Optolink reader. No Viessmann cloud, no dependency on Home Assistant entities, so it also runs outside Home Assistant (macOS).
> The backend is in the `BOILER BACKEND` section of `viessmann_planning/viessmann-planning-run.py`; it follows the reader's MQTT contract (`CONTRAT_MQTT.md` in the `vitoconnect-rpi` project).
> Other variants live on the branches `viessmann-cloud`, `local-mqtt` and `home-assistant`; `main` only holds the branch map.

Home Assistant add-on that manages Viessmann boiler setpoints from two-week cycle schedules.

## Features

- **Heating** (5–25 °C) and **DHW / domestic hot water** (55–75 °C) setpoints managed independently
- **Two-week cycle** support (odd/even ISO week)
- **Exception periods** — override the base schedule for holidays, presence changes, etc.
- **Web configurator** — built-in UI to manage plannings, week configs, and settings
- Runs every N minutes (configurable resolution: 1, 5, 10, 20, 30, 60 min)

## Requirements

- The boiler readable and writable locally (Optolink reader publishing to MQTT).

## MQTT broker settings

Inside Home Assistant the add-on asks the Supervisor for the broker settings. If that is not available, or outside Home Assistant, copy `mqtt_credentials.json.template` to `mqtt_credentials.json` and fill it in:

- macOS: at the project root, next to the template
- Home Assistant: `/config/viessmann-planning/mqtt_credentials.json`

When the file exists it takes precedence. It is ignored by git.

## Data

Schedules and settings are stored in `/config/viessmann-planning/schedules/`.

## Options

| Option | Default | Description |
|--------|---------|-------------|
| `verbosity` | `0` | Log verbosity level (0 = normal, 1 = verbose, 2 = very verbose) |
