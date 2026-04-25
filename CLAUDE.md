# CLAUDE.md — viessmann-planning

## Project overview

`viessmann-planning` is a Viessmann boiler scheduler that adds two-week cycle support and exception periods on top of the Viessmann cloud API. It is a sibling project to `tado-planning` (same developer, same patterns, different heating API).

**Two levels, completely independent:**
- **L1** — heating circuit setpoint (5–25 °C)
- **L2** — DHW (domestic hot water) setpoint (55–75 °C)

The boiler is left in permanent normal mode; this app adjusts setpoints at each slot boundary.

---

## Architecture

```
viessmann_planning/
├── run.sh                      Universal entry point (context detection, loop, cfg modes)
├── viessmann-planning-run.py   Scheduler: planning resolution → setpoint apply via PyViCare
├── viessmann-planning-cfg.py   Flask REST API + serves index.html
├── config.json                 HA add-on manifest (version, port 8098, aarch64)
├── Dockerfile                  FROM ghcr.io/home-assistant/aarch64-base
└── templates/index.html        Vanilla JS SPA (no build step)

scripts/
├── launchd_install.sh          macOS LaunchAgent (com.viessmann-planning)
├── launchd_uninstall.sh
├── macos_app_build.sh          ViessmannPlanning.app + DMG
├── docker_test_build/start/stop/remove.sh
├── git_push.sh / git_fetch.sh
└── list_features.sh            PyViCare device introspection
```

---

## Key constants and paths

| Item | Value |
|------|-------|
| Port | 8098 |
| launchd label | `com.viessmann-planning` |
| Prod container | `addon_viessmann_planning` |
| Test container | `addon_test_viessmann_planning` |
| Credentials | `viessmann_credentials.json` (gitignored, project root) |
| Token cache | `viessmann_token.save` (gitignored, project root) |
| Schedules dir (macOS) | `<project_root>/schedules/` |
| Schedules dir (HA) | `/config/viessmann-planning/schedules/` |
| Heating range | 5–25 °C |
| DHW range | 55–75 °C |
| Valid resolutions | 1, 5, 10, 20, 30, 60 minutes |

---

## Weekconfig structure (flat, no zones)

```json
{
  "ConfigName": {
    "timetable": "Mon-Sun",
    "Mon-Sun": [
      {"start": "00:00", "temp": 18},
      {"start": "06:00", "temp": 21}
    ]
  }
}
```

Unlike tado-planning (`{config: {zone: {timetable, slots}}}`), here there is no zone nesting.

Two-week variant adds `"cycle_type": "two-weeks-iso"` and `-W1`/`-W2` day key suffixes.

---

## Planning resolution logic

Same three-group precedence as tado-planning, but `select_config_for_level()` is called independently for L1 and L2 — no zone merging. A planning has a `"level"` field (`"L1"` or `"L2"`).

---

## Deployment targets

- **macOS** — launchd `--loop` mode, or `.app` that runs `--cfg` on demand
- **HA** — aarch64 Docker add-on, ingress port 8098, `--loop` mode

---

## PyViCare auth

```python
from PyViCare.PyViCare import PyViCare
vicare = PyViCare()
vicare.initWithCredentials(username, password, client_id, token_file)
device = vicare.devices[0]
circuit = device.circuits[0]
```

Credentials in `viessmann_credentials.json`:
```json
{"username": "...", "password": "...", "client_id": "..."}
```

Register client at https://app.developer.viessmann.com (redirect URI: `vicare://oauth-callback/everest`).

---

## No test suite

There are no automated tests. Verify changes with:
```bash
./viessmann_planning/run.sh --simulate 2026-12-25 -vv
./viessmann_planning/run.sh --cfg
```
