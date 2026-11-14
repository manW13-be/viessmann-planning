# viessmann-planning

Scheduler for a Viessmann boiler (two-week cycles, exception periods, independent heating / DHW levels), with a web configurator. It runs as a Home Assistant add-on or on macOS.

**`main` holds no code.** Since 14 Nov 2026 the project is split into three variants, one per branch, depending on how the boiler is reached:

| Branch | Boiler access | State |
|---|---|---|
| [`viessmann-cloud`](../../tree/viessmann-cloud) | Viessmann cloud API (PyViCare) through a Vitoconnect gateway | Working code, v1.1.5. Continues the former `main` (v1.1.4, tag `pre-split-v1.1.4`). |
| [`local-mqtt`](../../tree/local-mqtt) | Direct MQTT: topics `vito/<name>`, `vito/LWT`, commands `vito/cmnd`, published by an Optolink reader | Cloud layer removed, backend to be written. |
| [`home-assistant`](../../tree/home-assistant) | Home Assistant entities `vitocrossal_*` (MQTT discovery) through the Core API | Cloud layer removed, backend to be written. |

## Installing a variant in Home Assistant

Add the repository with the branch as suffix, for example:

```
https://github.com/manW13-be/viessmann-planning#home-assistant
```

The three variants share the add-on slug `viessmann_planning`: install only one at a time. Schedules stay in `/config/viessmann-planning/schedules/` whichever variant is installed.

## Maintaining this page and the branches

- `main` only contains this README, `CLAUDE.md` and the ignore files. Do not merge a variant into it.
- Update the table above when a branch is added, renamed, retired, or when its state changes (for example once a backend is written).
- The variants share everything except the boiler backend: planning resolution, validation, web configurator, `run.sh`, scripts. A fix in the shared code must be cherry-picked to the other two branches.
- In `local-mqtt` and `home-assistant`, the backend is isolated in the `BOILER BACKEND` section of `viessmann_planning/viessmann-planning-run.py` (`connect_backend`, `get_/set_heating_setpoint`, `get_/set_dhw_setpoint`, `BackendError`).
- Each branch carries its own version in `viessmann_planning/config.json`; `scripts/git_push.sh --bump` bumps and pushes the current branch only.
- `viessmann_credentials.json` and `viessmann_token.save` must stay in `gitignore` / `.gitignore` on every branch, including the ones that no longer use them: the files can still be present in a working copy.
