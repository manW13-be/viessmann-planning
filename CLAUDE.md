# CLAUDE.md — viessmann-planning

> **Branch `home-assistant`** — this variant drives the boiler through the **Home Assistant entities** `vitocrossal_*` (created by MQTT discovery), using the Core API with the Supervisor token. No Viessmann cloud, no MQTT credentials in the add-on.
> The boiler backend is **not written yet**: see the `BOILER BACKEND` section of `viessmann_planning/viessmann-planning-run.py`.
> Other variants live on the branches `viessmann-cloud`, `local-mqtt` and `home-assistant`; `main` only holds the branch map.

## Project overview

`viessmann-planning` is a Viessmann boiler scheduler that adds two-week cycle support and exception periods for a Viessmann boiler. It is a sibling project to `tado-planning` (same developer, same patterns, different heating API).

**Two levels, completely independent:**
- **L1** — heating circuit setpoint (5–25 °C)
- **L2** — DHW (domestic hot water) setpoint (55–75 °C)

The boiler is left in permanent normal mode; this app adjusts setpoints at each slot boundary.

---

## Architecture

```
viessmann_planning/
├── run.sh                      Universal entry point (context detection, loop, cfg modes)
├── viessmann-planning-run.py   Scheduler: planning resolution → setpoint apply via the boiler backend
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
```

---

## Key constants and paths

| Item | Value |
|------|-------|
| Port | 8098 |
| launchd label | `com.viessmann-planning` |
| Prod container | `addon_viessmann_planning` |
| Test container | `addon_test_viessmann_planning` |
| Schedules dir (macOS) | `<project_root>/schedules/` |
| Schedules dir (HA) | `/config/viessmann-planning/schedules/` |
| Heating range | 5–25 °C |
| DHW range | 10–60 °C (device VScotHO1_40 API max) |
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

## Boiler backend

The cloud layer (PyViCare, credentials, token cache) is removed on this branch. `viessmann-planning-run.py` exposes
`connect_backend()`, `get_/set_heating_setpoint()`, `get_/set_dhw_setpoint()` and `BackendError(reason)`; they are stubs
raising `BACKEND_NOT_IMPLEMENTED` until the `home-assistant` backend is written. A run resolves the planning, logs the target,
then publishes the failure reason to HA (`binary_sensor.viessmann_planning_gateway_online`).

`viessmann_credentials.json` / `viessmann_token.save` stay in `gitignore` on purpose: the files still exist in the
working directory and `scripts/git_push.sh` runs `git add -A`.

---

## No test suite

There are no automated tests. Verify changes with:
```bash
./viessmann_planning/run.sh --simulate 2026-12-25 -vv
./viessmann_planning/run.sh --cfg
```
