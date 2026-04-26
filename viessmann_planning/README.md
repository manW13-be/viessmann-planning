# Viessmann Planning

Home Assistant add-on that manages Viessmann boiler setpoints from two-week cycle schedules.

## Features

- **Heating** (5–25 °C) and **DHW / domestic hot water** (55–75 °C) setpoints managed independently
- **Two-week cycle** support (odd/even ISO week)
- **Exception periods** — override the base schedule for holidays, presence changes, etc.
- **Web configurator** — built-in UI to manage plannings, week configs, and settings
- Runs every N minutes (configurable resolution: 1, 5, 10, 20, 30, 60 min)

## Requirements

- A Viessmann boiler accessible via the Viessmann API
- A Viessmann developer account with a registered `client_id`  
  → Register at [app.developer.viessmann.com](https://app.developer.viessmann.com) (redirect URI: `vicare://oauth-callback/everest`)

## Configuration

After installing the add-on, open the web UI (via the sidebar panel **Viessmann Planning**) and enter your credentials:

| Field | Description |
|-------|-------------|
| Username | Your Viessmann account email |
| Password | Your Viessmann account password |
| Client ID | Your registered API client ID |

## Data

Schedules and settings are stored in `/config/viessmann-planning/schedules/`.

## Options

| Option | Default | Description |
|--------|---------|-------------|
| `verbosity` | `0` | Log verbosity level (0 = normal, 1 = verbose, 2 = very verbose) |
