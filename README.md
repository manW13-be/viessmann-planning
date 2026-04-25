# viessmann-planning

> **Version 1.0.0**

Automated Viessmann heating schedule management — with two-week cycle support, exception periods, and independent heating/DHW control.

---

## Table of contents

- [Why viessmann-planning](#why-viessmann-planning)
- [How it works](#how-it-works)
- [Key concepts](#key-concepts)
- [Repository structure](#repository-structure)
- [Prerequisites](#prerequisites)
- [Viessmann API credentials](#viessmann-api-credentials)
- [Setup — Home Assistant](#setup--home-assistant)
- [Setup — macOS](#setup--macos)
- [Web configurator](#web-configurator)
- [Data file format](#data-file-format)
- [Manual runs and testing](#manual-runs-and-testing)
- [Verbosity levels](#verbosity-levels)
- [Updating](#updating)
- [Troubleshooting](#troubleshooting)
- [For developers](#for-developers)
- [License](#license)

---

## Why viessmann-planning

Viessmann boilers have on-device schedules — but those schedules are limited to a seven-day week. There is no native way to express two-week custody cycles, multi-week exception periods, or independent control of heating setpoints vs. DHW (domestic hot water) setpoints.

viessmann-planning is a purpose-built layer on top of the Viessmann API that adds:

- **Two-week cycle support** — ISO week parity or sequential counting from a reference date
- **Exception periods** — any planning can define a start/end window that overrides the standard cycle
- **Two independent control levels** — L1 (heating setpoint) and L2 (DHW setpoint) are scheduled and applied independently
- **Web configurator** — a built-in Flask UI to manage all configurations without editing JSON files by hand

The boiler is put in **permanent normal mode** by the user; viessmann-planning then adjusts setpoints at each slot boundary.

---

## How it works

```
┌─────────────────────────────────────────────────────┐
│                   GitHub Repository                  │
└───────────────┬─────────────────────┬───────────────┘
                │                     │
         git pull/push           git pull/push
                │                     │
┌───────────────▼───────┐   ┌─────────▼───────────────┐
│      macOS (Mac)      │   │  Home Assistant (HA)     │
│                       │   │                          │
│  viessmann-planning   │   │  viessmann-planning      │
│  via launchd (loop)   │   │  Docker add-on           │
│                       │   │  --loop mode             │
│  viessmann-planning   │   │                          │
│  -cfg.py on demand    │   │  Flask configurator      │
│                       │   │  always running          │
└───────────────────────┘   └──────────────────────────┘
                │                     │
                └──────────┬──────────┘
                           │
                  ┌────────▼────────┐
                  │  Viessmann API  │
                  │   (PyViCare)    │
                  └─────────────────┘
```

Every `n` minutes (configurable, default 10), the scheduler:
1. Determines which planning and weekconfig are active for each level (L1, L2)
2. Looks up the expected setpoint for the current time slot
3. Reads the actual setpoint from the boiler via the Viessmann cloud API
4. Updates the setpoint only if it differs

---

## Key concepts

### Levels

| Level | Controls | Temperature range |
|-------|----------|------------------|
| **L1** | Heating circuit setpoint | 5–25 °C |
| **L2** | DHW (hot water) setpoint | 55–75 °C |

L1 and L2 are completely independent — a planning can have L1 events only, L2 only, or both.

### Plannings

A planning links a weekconfig to a time window (optional start, optional end) and a level. Three precedence groups apply:

1. **Group 1** — has a start date ≤ now (highest priority)
2. **Group 2** — no start date, has an end date > now
3. **Group 3** — no start date, no end date (standard weekly cycle)

The first planning found in the highest-priority group wins.

### Weekconfigs

A weekconfig defines a weekly temperature schedule. The timetable type controls which day grouping is used:

| Timetable | Days |
|-----------|------|
| `Mon-Sun` | Every day has the same schedule |
| `Mon-Fri+Sat+Sun` | Weekdays / Saturday / Sunday |
| `Mon+Tue+Wed+Thu+Fri+Sat+Sun` | Each day individually |

Two-week cycle types: `one-week`, `two-weeks-iso` (ISO week parity), `two-weeks-seq` (sequential from `ref_date`).

### Resolution

The resolution (1, 5, 10, 20, 30, or 60 minutes, default 10) defines the granularity of time slots across all weekconfigs. When changed, all existing slot times are adapted automatically.

---

## Repository structure

```
viessmann-planning/
├── viessmann_planning/
│   ├── run.sh                      # Universal entry point
│   ├── viessmann-planning-run.py   # Scheduler daemon
│   ├── viessmann-planning-cfg.py   # Flask web configurator
│   ├── config.json                 # HA add-on manifest
│   ├── Dockerfile                  # aarch64 Docker image
│   └── templates/
│       └── index.html              # Single-page web UI
├── scripts/
│   ├── launchd_install.sh          # macOS: install LaunchAgent
│   ├── launchd_uninstall.sh        # macOS: uninstall LaunchAgent
│   ├── macos_app_build.sh          # macOS: build .app + DMG
│   ├── docker_test_build.sh        # HA: build test Docker image
│   ├── docker_test_start.sh        # HA: start test container
│   ├── docker_test_stop.sh         # HA: stop test container
│   ├── docker_test_remove.sh       # HA: remove image + container
│   ├── git_push.sh                 # Commit + push to GitHub
│   ├── git_fetch.sh                # Pull from GitHub
│   └── list_features.sh            # List Viessmann device features
├── schedules/                      # Runtime data (gitignored)
│   ├── settings.json
│   ├── *.json                      # Plannings and weekconfigs
│   └── viessmann-planning.log
├── viessmann_credentials.json      # API credentials (gitignored)
├── viessmann_token.save            # OAuth2 token cache (gitignored)
├── repository.json                 # HA repository metadata
└── README.md
```

---

## Prerequisites

### All platforms

- Python 3.10+
- `PyViCare` (`pip install PyViCare`)
- `Flask` (`pip install flask`)
- `requests` (`pip install requests`)
- `jq` (for shell scripts)

### macOS only

- Python 3.11 via Homebrew: `brew install python@3.11`

### Home Assistant only

- HA OS or Supervised installation with add-on support
- SSH access to the HA host (for Docker test scripts)

---

## Viessmann API credentials

viessmann-planning uses the **Viessmann Developer API** (cloud, OAuth2). You need to register a client application:

1. Go to [https://app.developer.viessmann.com](https://app.developer.viessmann.com)
2. Create a new "Client" under your account
3. Set the redirect URI to `vicare://oauth-callback/everest`
4. Note the **Client ID**

Then create `viessmann_credentials.json` at the project root:

```json
{
  "username": "your-viessmann-account@example.com",
  "password": "your-password",
  "client_id": "your-client-id"
}
```

This file is gitignored and never committed.

**API call budget**: The free tier allows ~1,450 API calls per day. With resolution=10min the scheduler runs ~144 times/day. Each run makes at most 2 GET + 2 SET calls, well within budget.

---

## Setup — Home Assistant

### Install the add-on

1. In HA, go to **Settings → Add-ons → Add-on Store**
2. Click the three-dot menu → **Repositories**
3. Add your GitHub repository URL
4. Find **Viessmann Planning** and click **Install**

### First-time configuration

1. Create the config directory on your HA host:
   ```bash
   mkdir -p /config/viessmann-planning
   ```

2. Copy your credentials file:
   ```bash
   # From your Mac via scp, or create directly on HA SSH:
   nano /config/viessmann-planning/viessmann_credentials.json
   ```

3. Start the add-on from the HA UI

4. Open the web UI via the add-on's **Open Web UI** button (ingress, port 8098)

---

## Setup — macOS

### Install as a background service (launchd)

```bash
./scripts/launchd_install.sh
```

This installs a LaunchAgent (`com.viessmann-planning`) that starts automatically at login and runs the scheduler loop plus the web configurator.

```bash
# Uninstall
./scripts/launchd_uninstall.sh

# Build a double-clickable .app (opens the configurator in your browser)
./scripts/macos_app_build.sh
```

The web UI is available at `http://localhost:8098` while the service is running.

### Credentials location (macOS)

Place `viessmann_credentials.json` in the project root (same directory as `README.md`).

---

## Web configurator

The web UI (port 8098) has six sections:

| Section | Description |
|---------|-------------|
| **Active Status / Planning** | Current active planning for L1 and L2, 14-day timeline |
| **Active Status / Weekconfig** | Live L1 + L2 slot tables with current slot highlighted, live boiler setpoints |
| **Configuration / Weekconfigs** | Create and edit weekconfigs |
| **Configuration / Plannings** | Create and edit plannings |
| **Configuration / Settings** | Resolution, default heating temp, default DHW temp |
| **System / Add-on** | Add-on info and version |
| **System / Logs** | Tail the scheduler log |
| **System / Service** | macOS launchd service control |

---

## Data file format

All data files live in `schedules/` (HA: `/config/viessmann-planning/`).

### settings.json

```json
{
  "resolution": 10,
  "default_heating_temp": 20,
  "default_dhw_temp": 60
}
```

### Weekconfig file (`<name>.json`)

```json
{
  "Normal": {
    "timetable": "Mon-Sun",
    "Mon-Sun": [
      {"start": "00:00", "temp": 18},
      {"start": "06:00", "temp": 21},
      {"start": "22:00", "temp": 18}
    ]
  }
}
```

For a `Mon-Fri+Sat+Sun` weekconfig, the keys are `Mon-Fri`, `Sat`, `Sun`.  
For `Mon+Tue+...+Sun`, the keys are `Mon`, `Tue`, `Wed`, `Thu`, `Fri`, `Sat`, `Sun`.

Two-week variant — add `"cycle_type": "two-weeks-iso"` (or `"two-weeks-seq"`) and a second set of day keys:

```json
{
  "CustodyCycle": {
    "timetable": "Mon-Sun",
    "cycle_type": "two-weeks-iso",
    "Mon-Sun-W1": [{"start": "00:00", "temp": 21}],
    "Mon-Sun-W2": [{"start": "00:00", "temp": 18}]
  }
}
```

### Planning file (`<name>.json`)

```json
{
  "name": "Winter 2026",
  "level": "L1",
  "weekconfig": "Normal",
  "start": "2026-11-01T00:00",
  "end": "2027-03-31T23:59"
}
```

`start` and `end` are optional. `level` is `"L1"` or `"L2"`.

---

## Manual runs and testing

```bash
# Single scheduler run (macOS shell)
./viessmann_planning/run.sh

# Simulate a specific date without API connection
./viessmann_planning/run.sh --simulate 2026-12-25

# Run with verbose output
./viessmann_planning/run.sh -v        # level 1
./viessmann_planning/run.sh -vv       # level 2

# Start only the web configurator
./viessmann_planning/run.sh --cfg

# List Viessmann device features / available setpoints
./scripts/list_features.sh

# Docker test (HA SSH)
./scripts/docker_test_build.sh
./scripts/docker_test_start.sh -vv
./scripts/docker_test_start.sh --loop
./scripts/docker_test_stop.sh
```

---

## Verbosity levels

| Flag | Level | Output |
|------|-------|--------|
| (none) | 0 | Changes only |
| `-v` | 1 | + current slot info |
| `-vv` | 2 | + API call details |
| `-vvv` | 3 | + full planning resolution trace |

---

## Updating

```bash
# Pull latest code
./scripts/git_fetch.sh

# On HA: restart the add-on from the UI after pulling
```

---

## Troubleshooting

**"No devices found"**  
Check that your `viessmann_credentials.json` is correct and that the client_id is registered at developer.viessmann.com.

**"API call failed / rate limited"**  
The free tier allows ~1,450 calls/day. Check `schedules/viessmann-planning.log` for API error details.

**Setpoints not changing**  
Make sure the boiler is set to permanent normal mode (not auto/schedule mode). viessmann-planning only adjusts setpoints; it does not control the boiler mode.

**macOS: service not starting**  
Check `schedules/viessmann-planning-err.log`. Verify Python 3.11 is installed: `python3.11 --version`.

---

## For developers

### Scripts reference

| Script | Platform | Description |
|--------|----------|-------------|
| `run.sh` | All | Universal entry point; auto-detects context |
| `scripts/launchd_install.sh` | macOS | Install LaunchAgent |
| `scripts/launchd_uninstall.sh` | macOS | Uninstall LaunchAgent |
| `scripts/macos_app_build.sh` | macOS | Build .app + DMG |
| `scripts/docker_test_build.sh` | HA SSH | Build test Docker image |
| `scripts/docker_test_start.sh` | HA SSH | Start test container |
| `scripts/docker_test_stop.sh` | HA SSH | Stop test container |
| `scripts/docker_test_remove.sh` | HA SSH | Remove image + container |
| `scripts/git_push.sh` | All | Commit + push; `--bump` increments patch version |
| `scripts/git_fetch.sh` | All | Pull from GitHub |
| `scripts/list_features.sh` | All | Inspect Viessmann device capabilities |

### Architecture

```
viessmann-planning-run.py
  load_settings()          → resolution, default temps
  select_config_for_level()→ planning resolution (L1 and L2 independently)
  get_current_slot_temp()  → active temperature for current time
  apply_l1_heating()       → read → compare → set heating setpoint
  apply_l2_dhw()           → read → compare → set DHW setpoint
  verify()                 → post-apply verification

viessmann-planning-cfg.py (Flask)
  GET  /api/status          → active plannings for L1 + L2
  GET  /api/timeline        → 14-day L1 + L2 timeline
  GET  /api/weekconfig-status → current slot highlight + live temps
  GET  /api/weekconfigs     → list all weekconfigs
  POST /api/weekconfigs/<n> → save weekconfig
  ...  (full CRUD for plannings, weekconfigs, settings)
```

### Development workflow

1. Edit `viessmann-planning-run.py` or `viessmann-planning-cfg.py` locally on macOS
2. Test with `./viessmann_planning/run.sh -vv` or `--cfg`
3. Deploy to HA SSH with `./scripts/git_push.sh "message"` + `./scripts/git_fetch.sh`
4. Or rebuild the Docker image: `./scripts/docker_test_build.sh`

---

## License

MIT
