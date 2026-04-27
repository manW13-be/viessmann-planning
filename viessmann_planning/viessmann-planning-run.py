#!/usr/bin/env python3
"""
viessmann-planning-run.py  v1.0
================================
Manages Viessmann Heating and DHW/ECS setpoints from two JSON files:

    plannings.json   — all plannings (standard + exceptions)
    weekconfigs.json — all setpoint configurations

Heating = heating circuit (comfort temperature, 5–25°C)
DHW     = domestic hot water / ECS (target temperature, 55–75°C)

Heating and DHW are independent — DHW never overrides Heating.
A planning can define Heating events, DHW events, or both.

At each run:
  1. Resolve active weekconfig for Heating and DHW (via planning precedence)
  2. Find current time slot → target temperature
  3. Read current setpoint from Viessmann API
  4. Update only if different

Requirements:
    pip install PyViCare flask requests
"""

import sys
import os
import json
import argparse
import datetime
import platform
import time

# ---------------------------------------------------------------------------
# PATHS
# ---------------------------------------------------------------------------

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

if platform.system() == "Darwin":
    _DEFAULT_CREDS_FILE = os.path.join(_SCRIPT_DIR, "..", "viessmann_credentials.json")
    _DEFAULT_TOKEN_FILE = os.path.join(_SCRIPT_DIR, "..", "viessmann_token.save")
    _DEFAULT_DATA_DIR   = os.path.join(_SCRIPT_DIR, "..", "schedules")
else:
    _DEFAULT_CREDS_FILE = "/config/viessmann-planning/viessmann_credentials.json"
    _DEFAULT_TOKEN_FILE = "/config/viessmann-planning/viessmann_token.save"
    _DEFAULT_DATA_DIR   = "/config/viessmann-planning/schedules"

CREDS_FILE = os.environ.get("VIESSMANN_CREDS_FILE",    os.path.abspath(_DEFAULT_CREDS_FILE))
TOKEN_FILE = os.environ.get("VIESSMANN_TOKEN_FILE",    os.path.abspath(_DEFAULT_TOKEN_FILE))
DATA_DIR   = os.environ.get("VIESSMANN_SCHEDULES_DIR", os.path.abspath(_DEFAULT_DATA_DIR))

PLANNINGS_FILE   = os.path.join(DATA_DIR, "plannings.json")
WEEKCONFIGS_FILE = os.path.join(DATA_DIR, "weekconfigs.json")
SETTINGS_FILE    = os.path.join(DATA_DIR, "settings.json")
STATS_FILE       = os.path.join(DATA_DIR, "api_stats.json")

# ---------------------------------------------------------------------------
# CONSTANTS
# ---------------------------------------------------------------------------

TIMETABLE_MON_SUN         = "Mon-Sun"
TIMETABLE_MON_FRI_SAT_SUN = "Mon-Fri, Sat, Sun"
TIMETABLE_MON_TO_SUN      = "Mon, ..., Sun"
VALID_TIMETABLES = (TIMETABLE_MON_SUN, TIMETABLE_MON_FRI_SAT_SUN, TIMETABLE_MON_TO_SUN)

TIMETABLE_REQUIRED_KEYS = {
    TIMETABLE_MON_SUN:         ["Mon-Sun"],
    TIMETABLE_MON_FRI_SAT_SUN: ["Mon-Fri", "Sat", "Sun"],
    TIMETABLE_MON_TO_SUN:      ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
}

DAY_NAMES = {
    "monday": 0, "tuesday": 1, "wednesday": 2,
    "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
}

DAY_NAMES_EN = {
    0: "Monday", 1: "Tuesday", 2: "Wednesday",
    3: "Thursday", 4: "Friday", 5: "Saturday", 6: "Sunday",
}

VALID_CYCLES = ("one-week", "two-weeks-iso", "two-weeks-seq")

VALID_RESOLUTIONS = (1, 5, 10, 20, 30, 60)

HEATING_TEMP_MIN = 5
HEATING_TEMP_MAX = 25
DHW_TEMP_MIN     = 55
DHW_TEMP_MAX     = 75

VERBOSITY = 0


def log(msg: str, level: int = 0):
    if VERBOSITY >= level:
        ts = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        print(f"[{ts}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# FILE LOADING
# ---------------------------------------------------------------------------

def load_json(path: str) -> object:
    if not os.path.exists(path):
        log(f"[ERROR] File not found: {path}")
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as e:
            log(f"[ERROR] JSON parse error in {path}: {e}")
            sys.exit(1)


def load_data_files() -> tuple[list, dict]:
    plannings   = load_json(PLANNINGS_FILE)
    weekconfigs = load_json(WEEKCONFIGS_FILE)
    if not isinstance(plannings, list):
        log(f"[ERROR] {PLANNINGS_FILE}: expected a JSON array at root")
        sys.exit(1)
    if not isinstance(weekconfigs, dict):
        log(f"[ERROR] {WEEKCONFIGS_FILE}: expected a JSON object at root")
        sys.exit(1)
    return plannings, weekconfigs


def load_settings() -> dict:
    if not os.path.exists(SETTINGS_FILE):
        return {"resolution": 10, "default_heating_temp": 20, "default_dhw_temp": 60}
    with open(SETTINGS_FILE, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# VALIDATION — WEEKCONFIGS
# ---------------------------------------------------------------------------

def _validate_slot(slot: object, cfg_name: str, day_key: str, idx: int,
                   resolution: int = 10) -> str | None:
    if not isinstance(slot, dict):
        return f"[VALIDATION] '{cfg_name}'/'{day_key}' slot #{idx}: expected dict"
    for field in ("start", "temp"):
        if field not in slot:
            return f"[VALIDATION] '{cfg_name}'/'{day_key}' slot #{idx}: missing field '{field}'"
    start = slot["start"]
    if not isinstance(start, str) or len(start) != 5 or start[2] != ":":
        return f"[VALIDATION] '{cfg_name}'/'{day_key}' slot #{idx}: 'start' must be HH:MM"
    try:
        h, m = int(start[:2]), int(start[3:])
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
        if m % resolution != 0:
            return (f"[VALIDATION] '{cfg_name}'/'{day_key}' slot #{idx}: "
                    f"time {start} not aligned to {resolution}-minute resolution")
    except ValueError:
        return f"[VALIDATION] '{cfg_name}'/'{day_key}' slot #{idx}: invalid time '{start}'"
    try:
        temp = int(slot["temp"])
        if not (HEATING_TEMP_MIN <= temp <= DHW_TEMP_MAX):
            return (f"[VALIDATION] '{cfg_name}'/'{day_key}' slot #{idx}: "
                    f"temp {temp} out of allowed range {HEATING_TEMP_MIN}–{DHW_TEMP_MAX}")
    except (TypeError, ValueError):
        return f"[VALIDATION] '{cfg_name}'/'{day_key}' slot #{idx}: 'temp' must be an integer"
    return None


def validate_weekconfig(cfg_name: str, cfg: object, resolution: int = 10) -> list[str]:
    errors = []
    if not isinstance(cfg, dict):
        return [f"[VALIDATION] '{cfg_name}': expected dict"]

    tt = cfg.get("timetable")
    if tt not in VALID_TIMETABLES:
        errors.append(f"[VALIDATION] '{cfg_name}': unknown timetable '{tt}'")
        return errors

    required = TIMETABLE_REQUIRED_KEYS[tt]
    for key in required:
        if key not in cfg:
            errors.append(f"[VALIDATION] '{cfg_name}': missing key '{key}' for timetable '{tt}'")
        else:
            slots = cfg[key]
            if not isinstance(slots, list) or len(slots) == 0:
                errors.append(f"[VALIDATION] '{cfg_name}'/'{key}': must be a non-empty list")
                continue
            # First slot must be 00:00
            if slots[0].get("start") != "00:00":
                errors.append(f"[VALIDATION] '{cfg_name}'/'{key}': first slot must start at 00:00")
            for i, s in enumerate(slots):
                err = _validate_slot(s, cfg_name, key, i, resolution)
                if err:
                    errors.append(err)
    return errors


def validate_weekconfigs(weekconfigs: dict, resolution: int = 10) -> list[str]:
    errors = []
    if not weekconfigs:
        errors.append("[VALIDATION] weekconfigs.json is empty")
        return errors
    for name, cfg in weekconfigs.items():
        errors.extend(validate_weekconfig(name, cfg, resolution))
    return errors


# ---------------------------------------------------------------------------
# VALIDATION — PLANNINGS
# ---------------------------------------------------------------------------

def validate_planning(p: dict, weekconfigs: dict) -> list[str]:
    errors = []
    name = p.get("name", "<unnamed>")

    if "name" not in p:
        errors.append("[VALIDATION] planning missing 'name' field")

    cycle = p.get("cycle")
    if cycle not in VALID_CYCLES:
        errors.append(f"[VALIDATION] planning '{name}': invalid cycle '{cycle}'")

    if cycle == "two-weeks-seq" and not p.get("ref_date"):
        errors.append(f"[VALIDATION] planning '{name}': 'two-weeks-seq' requires 'ref_date'")

    if cycle == "two-weeks-seq" and p.get("ref_date"):
        try:
            datetime.datetime.strptime(p["ref_date"], "%Y-%m-%d")
        except ValueError:
            errors.append(f"[VALIDATION] planning '{name}': invalid ref_date '{p['ref_date']}'")

    for field in ("start", "end"):
        val = p.get(field)
        if val is not None:
            try:
                datetime.datetime.strptime(val, "%Y-%m-%d %H:%M")
            except ValueError:
                errors.append(f"[VALIDATION] planning '{name}': invalid {field} '{val}'")

    start_raw, end_raw = p.get("start"), p.get("end")
    if start_raw and end_raw:
        try:
            s = datetime.datetime.strptime(start_raw, "%Y-%m-%d %H:%M")
            e = datetime.datetime.strptime(end_raw,   "%Y-%m-%d %H:%M")
            if s >= e:
                errors.append(f"[VALIDATION] planning '{name}': start must be before end")
        except ValueError:
            pass

    events = p.get("events", [])
    if not isinstance(events, list) or len(events) == 0:
        errors.append(f"[VALIDATION] planning '{name}': at least one event is required")
        return errors

    for i, ev in enumerate(events):
        if not isinstance(ev, dict):
            errors.append(f"[VALIDATION] planning '{name}' event #{i}: expected dict")
            continue
        for field in ("day", "time", "level", "config"):
            if field not in ev:
                errors.append(f"[VALIDATION] planning '{name}' event #{i}: missing '{field}'")
        if "day" in ev and ev["day"].lower() not in DAY_NAMES:
            errors.append(f"[VALIDATION] planning '{name}' event #{i}: invalid day '{ev['day']}'")
        if "level" in ev and ev["level"] not in (1, 2):
            errors.append(f"[VALIDATION] planning '{name}' event #{i}: level must be 1 or 2")
        if "config" in ev and ev["config"] not in weekconfigs:
            errors.append(f"[VALIDATION] planning '{name}' event #{i}: "
                          f"config '{ev['config']}' not found in weekconfigs.json")
        if "week" in ev and cycle in ("two-weeks-iso", "two-weeks-seq"):
            if ev["week"].lower() not in ("odd", "even", "both"):
                errors.append(f"[VALIDATION] planning '{name}' event #{i}: "
                               f"invalid week '{ev['week']}' (expected odd/even/both)")
    return errors


def validate_planning_conflicts(plannings: list) -> list[str]:
    errors = []
    seen = {}
    for p in plannings:
        k = (p.get("start"), p.get("end"))
        name = p.get("name", "<unnamed>")
        if k in seen:
            other = seen[k]
            s, e = k
            if s is None and e is None:
                errors.append(f"[VALIDATION] plannings '{other}' and '{name}': "
                               f"both have no start and no end (only one standard allowed)")
            elif s is None:
                errors.append(f"[VALIDATION] plannings '{other}' and '{name}': "
                               f"both have no start and same end '{e}'")
            elif e is None:
                errors.append(f"[VALIDATION] plannings '{other}' and '{name}': "
                               f"both have same start '{s}' and no end")
            else:
                errors.append(f"[VALIDATION] plannings '{other}' and '{name}': "
                               f"identical start '{s}' and end '{e}'")
        else:
            seen[k] = name
    return errors


def validate_all(plannings: list, weekconfigs: dict) -> bool:
    settings   = load_settings()
    resolution = int(settings.get("resolution", 10))

    all_errors = []
    all_errors.extend(validate_weekconfigs(weekconfigs, resolution))
    for p in plannings:
        all_errors.extend(validate_planning(p, weekconfigs))
    all_errors.extend(validate_planning_conflicts(plannings))

    if all_errors:
        log("[VALIDATION] Errors found:")
        for err in all_errors:
            log(f"  {err}")
        return False

    log(f"[VALIDATION] OK — {len(plannings)} planning(s), {len(weekconfigs)} weekconfig(s).")
    return True


# ---------------------------------------------------------------------------
# PLANNING SELECTION
# ---------------------------------------------------------------------------

def _parse_dt_safe(s: str | None) -> datetime.datetime | None:
    if s is None:
        return None
    return datetime.datetime.strptime(s, "%Y-%m-%d %H:%M")


def active_plannings_at(plannings: list, now: datetime.datetime) -> list:
    group1, group2, group3 = [], [], []
    for p in plannings:
        s = _parse_dt_safe(p.get("start"))
        e = _parse_dt_safe(p.get("end"))
        if s is not None:
            if s <= now and (e is None or e > now):
                group1.append(p)
        elif e is not None:
            if e > now:
                group2.append(p)
        else:
            group3.append(p)

    def g1_key(p):
        s = _parse_dt_safe(p["start"])
        e = _parse_dt_safe(p.get("end"))
        return (-s.timestamp(), e.timestamp() if e else float("inf"))

    group1.sort(key=g1_key)
    group2.sort(key=lambda p: _parse_dt_safe(p["end"]))
    return group1 + group2 + group3


def _week_parity(now: datetime.datetime, planning: dict) -> str:
    cycle    = planning.get("cycle", "two-weeks-iso")
    ref_date = planning.get("ref_date")
    if cycle == "two-weeks-iso":
        return "odd" if now.isocalendar()[1] % 2 == 1 else "even"
    elif cycle == "two-weeks-seq" and ref_date:
        ref     = datetime.datetime.strptime(ref_date, "%Y-%m-%d")
        ref_mon = ref - datetime.timedelta(days=ref.weekday())
        now_mon = now - datetime.timedelta(days=now.weekday())
        weeks   = int((now_mon - ref_mon).days / 7)
        return "odd" if weeks % 2 == 0 else "even"
    return "odd"


def select_config_for_level(plannings_by_precedence: list, level: int,
                             now: datetime.datetime,
                             weekconfigs: dict) -> tuple[str | None, str | None]:
    """
    Return (config_name, planning_name) for the given level at time now.
    Walks plannings by precedence; returns the first with events for this level.
    No zone concept — configs are global.
    """
    monday = (now - datetime.timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)

    for planning in plannings_by_precedence:
        cycle  = planning.get("cycle", "two-weeks-iso")
        events = [e for e in planning.get("events", [])
                  if isinstance(e, dict) and e.get("level") == level
                  and e.get("config") is not None]
        if not events:
            continue

        parity = _week_parity(now, planning)
        is_odd = (parity == "odd")
        if is_odd:
            odd_mon, even_mon = monday, monday + datetime.timedelta(weeks=1)
            p_odd,  p_even   = monday - datetime.timedelta(weeks=2), monday - datetime.timedelta(weeks=1)
        else:
            even_mon, odd_mon = monday, monday + datetime.timedelta(weeks=1)
            p_even,  p_odd   = monday - datetime.timedelta(weeks=2), monday - datetime.timedelta(weeks=1)

        candidates = []
        for ev in events:
            week = ev.get("week", "both").lower()
            d    = DAY_NAMES.get(ev["day"].lower(), 0)
            h, m = map(int, ev["time"].split(":"))
            if cycle == "one-week":
                for mon in [odd_mon, even_mon, p_odd, p_even]:
                    candidates.append((mon + datetime.timedelta(days=d, hours=h, minutes=m), ev["config"]))
            else:
                if week in ("odd", "both"):
                    candidates.append((odd_mon + datetime.timedelta(days=d, hours=h, minutes=m), ev["config"]))
                    candidates.append((p_odd   + datetime.timedelta(days=d, hours=h, minutes=m), ev["config"]))
                if week in ("even", "both"):
                    candidates.append((even_mon + datetime.timedelta(days=d, hours=h, minutes=m), ev["config"]))
                    candidates.append((p_even   + datetime.timedelta(days=d, hours=h, minutes=m), ev["config"]))

        if not candidates:
            continue

        past = [(dt, cfg) for dt, cfg in candidates if now >= dt]
        if past:
            _, cfg = max(past, key=lambda x: x[0])
        else:
            _, cfg = min(candidates, key=lambda x: x[0])

        if cfg in weekconfigs:
            log(f"[CANDIDATES L{level}] Selected: {cfg} (from {planning.get('name')})", 2)
            return cfg, planning.get("name")

    return None, None


# ---------------------------------------------------------------------------
# SLOT RESOLUTION
# ---------------------------------------------------------------------------

def _day_key_for_timetable(tt: str, now: datetime.datetime) -> str:
    wd = now.weekday()  # 0=Mon … 6=Sun
    if tt == TIMETABLE_MON_SUN:
        return "Mon-Sun"
    elif tt == TIMETABLE_MON_FRI_SAT_SUN:
        return "Mon-Fri" if wd <= 4 else ("Sat" if wd == 5 else "Sun")
    else:
        return ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][wd]


def get_current_slot_temp(cfg: dict, now: datetime.datetime) -> int | None:
    """
    Return the active temperature setpoint at `now` from a weekconfig.
    Finds the last slot with start <= now.
    If before first slot: wrap around to the last slot of the day (assumes prior day's last slot).
    """
    tt = cfg.get("timetable")
    if not tt:
        return None
    day_key = _day_key_for_timetable(tt, now)
    slots   = cfg.get(day_key, [])
    if not slots:
        return None

    now_minutes = now.hour * 60 + now.minute
    active = None
    for slot in slots:
        try:
            h, m = map(int, slot["start"].split(":"))
            if h * 60 + m <= now_minutes:
                active = slot
        except (ValueError, KeyError):
            continue

    if active is None:
        active = slots[-1]  # wrap-around: before first slot

    try:
        return int(active["temp"])
    except (TypeError, ValueError, KeyError):
        return None


# ---------------------------------------------------------------------------
# API STATS
# ---------------------------------------------------------------------------

_api_stats: dict[str, int] = {"GET": 0, "SET": 0}


def _load_api_stats():
    try:
        with open(STATS_FILE, encoding="utf-8") as f:
            d = json.load(f)
            _api_stats["GET"] = int(d.get("GET", 0))
            _api_stats["SET"] = int(d.get("SET", 0))
    except (FileNotFoundError, Exception):
        pass

_load_api_stats()


def log_api_stats():
    total = _api_stats["GET"] + _api_stats["SET"]
    log(f"[API] {total} calls total ({_api_stats['GET']} GET, {_api_stats['SET']} SET) — cumulative since first run")


def save_api_stats():
    try:
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(_api_stats, f)
    except Exception as e:
        log(f"[WARN] Could not save api_stats: {e}", 1)


# ---------------------------------------------------------------------------
# VIESSMANN API
# ---------------------------------------------------------------------------

def load_credentials() -> dict:
    if not os.path.exists(CREDS_FILE):
        log(f"[AUTH] Credentials file not found: {CREDS_FILE}")
        log(f"[AUTH] Create it with: {{\"username\": \"your@email.com\", "
            f"\"password\": \"your_pass\", \"client_id\": \"your_id\"}}")
        sys.exit(1)
    with open(CREDS_FILE, encoding="utf-8") as f:
        return json.load(f)


def get_vicare_client():
    """Return (PyViCare instance, auto-detected device)."""
    try:
        from PyViCare.PyViCare import PyViCare
    except ImportError:
        log("[AUTH] PyViCare not installed. Run: pip install PyViCare")
        sys.exit(1)

    creds  = load_credentials()
    vicare = PyViCare()
    try:
        vicare.initWithCredentials(
            username=creds["username"],
            password=creds["password"],
            client_id=creds["client_id"],
            token_file=TOKEN_FILE,
        )
    except Exception as e:
        log(f"[AUTH] Failed to connect to Viessmann: {e}")
        sys.exit(1)

    if not vicare.devices:
        log("[AUTH] No Viessmann devices found in account")
        sys.exit(1)

    device = vicare.devices[0].asAutoDetectDevice()
    log("[AUTH] Connected to Viessmann.", 1)
    return vicare, device


def get_heating_setpoint(device) -> float | None:
    """Read current heating comfort/normal temperature setpoint."""
    _api_stats["GET"] += 1
    for program in ("comfort", "normal"):
        try:
            val = device.circuits[0].getProgramTemperature(program)
            log(f"[API]  GET heating setpoint ({program}): {val}°C", 3)
            return float(val)
        except Exception:
            continue
    log("[WARN] Could not read heating setpoint", 1)
    return None


def set_heating_setpoint(device, temp: int):
    """Set heating comfort/normal temperature setpoint."""
    _api_stats["SET"] += 1
    for program in ("comfort", "normal"):
        try:
            device.circuits[0].setProgramTemperature(program, temp)
            log(f"[API]  SET heating setpoint ({program}): {temp}°C", 3)
            return
        except Exception:
            continue
    raise RuntimeError(f"Failed to set heating setpoint to {temp}°C")


def get_dhw_setpoint(device) -> float | None:
    """Read current DHW/ECS temperature setpoint."""
    _api_stats["GET"] += 1
    try:
        val = device.getDomesticHotWaterConfiguredTemperature()
        log(f"[API]  GET DHW setpoint: {val}°C", 3)
        return float(val)
    except Exception as e:
        log(f"[WARN] Could not read DHW setpoint: {e}", 1)
        return None


def set_dhw_setpoint(device, temp: int):
    """Set DHW/ECS temperature setpoint."""
    _api_stats["SET"] += 1
    try:
        device.setDomesticHotWaterTemperature(temp)
        log(f"[API]  SET DHW setpoint: {temp}°C", 3)
    except Exception as e:
        raise RuntimeError(f"Failed to set DHW setpoint to {temp}°C: {e}")


# ---------------------------------------------------------------------------
# CHECK & APPLY
# ---------------------------------------------------------------------------

def apply_l1_heating(device, cfg: dict, now: datetime.datetime, config_name: str) -> bool:
    """Check and apply heating setpoint. Returns True if updated."""
    target = get_current_slot_temp(cfg, now)
    if target is None:
        log("[WARN] Heating: could not resolve target temperature")
        return False

    current = get_heating_setpoint(device)
    log(f"[Heating] config={config_name}, target={target}°C"
        + (f", current={current}°C" if current is not None else ", current=?"))

    if current is not None and abs(float(current) - float(target)) <= 0.1:
        log(f"[SKIP] Heating: already at {target}°C")
        return False

    log(f"[UPDATE] Heating: setting to {target}°C")
    set_heating_setpoint(device, target)
    log(f"[OK]   Heating: setpoint → {target}°C")
    return True


def apply_l2_dhw(device, cfg: dict, now: datetime.datetime, config_name: str) -> bool:
    """Check and apply DHW setpoint. Returns True if updated."""
    target = get_current_slot_temp(cfg, now)
    if target is None:
        log("[WARN] DHW: could not resolve target temperature")
        return False

    current = get_dhw_setpoint(device)
    log(f"[DHW]     config={config_name}, target={target}°C"
        + (f", current={current}°C" if current is not None else ", current=?"))

    if current is not None and abs(float(current) - float(target)) <= 0.1:
        log(f"[SKIP] DHW: already at {target}°C")
        return False

    log(f"[UPDATE] DHW: setting to {target}°C")
    set_dhw_setpoint(device, target)
    log(f"[OK]   DHW: setpoint → {target}°C")
    return True


# ---------------------------------------------------------------------------
# VERIFICATION PASS
# ---------------------------------------------------------------------------

def verify(device, l1_cfg: dict | None, l2_cfg: dict | None, now: datetime.datetime):
    """Re-read setpoints from Viessmann and check against expected."""
    log("\n[VERIFY] Re-reading from Viessmann...", 1)
    all_ok = True

    if l1_cfg is not None:
        target  = get_current_slot_temp(l1_cfg, now)
        current = get_heating_setpoint(device)
        if target is not None and current is not None and abs(current - target) > 0.5:
            log(f"[MISMATCH] Heating: expected={target}°C actual={current}°C")
            all_ok = False
        else:
            log(f"[OK]   Heating verified: {current}°C", 1)

    if l2_cfg is not None:
        target  = get_current_slot_temp(l2_cfg, now)
        current = get_dhw_setpoint(device)
        if target is not None and current is not None and abs(current - target) > 0.5:
            log(f"[MISMATCH] DHW: expected={target}°C actual={current}°C")
            all_ok = False
        else:
            log(f"[OK]   DHW verified: {current}°C", 1)

    if all_ok:
        log("[OK]   Verification passed.", 1)


# ---------------------------------------------------------------------------
# HA SENSOR PUSH
# ---------------------------------------------------------------------------

def push_ha_sensors(l1_cfg_name: str | None, l2_cfg_name: str | None,
                    l1_temp: int | None, l2_temp: int | None):
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return
    try:
        import requests as _req
    except ImportError:
        return

    ha_base = "http://supervisor/core/api/states"
    sup_base = "http://supervisor"
    ha_hdr  = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    run_ts  = datetime.datetime.now().astimezone().isoformat()

    try:
        sup_r    = _req.get(f"{sup_base}/addons/self/info", headers=ha_hdr, timeout=5)
        sup_data = sup_r.json().get("data", {}) if sup_r.ok else {}
    except Exception:
        sup_data = {}
    current_v  = sup_data.get("version", "?")
    latest_v   = sup_data.get("version_latest")
    update_av  = sup_data.get("update_available", False)

    sensors = [
        ("sensor.viessmann_planning_last_run", {
            "state": run_ts,
            "attributes": {"friendly_name": "Viessmann Planning — dernier run",
                           "device_class": "timestamp", "icon": "mdi:clock-check"},
        }),
        ("sensor.viessmann_planning_api_calls", {
            "state": str(_api_stats["GET"] + _api_stats["SET"]),
            "attributes": {"friendly_name": "Viessmann Planning — appels API",
                           "unit_of_measurement": "calls", "icon": "mdi:api"},
        }),
        ("sensor.viessmann_planning_version", {
            "state": current_v,
            "attributes": {"friendly_name": "Viessmann Planning — version installée",
                           "icon": "mdi:tag"},
        }),
        ("binary_sensor.viessmann_planning_update_available", {
            "state": "on" if update_av else "off",
            "attributes": {"friendly_name": "Viessmann Planning — update disponible",
                           "device_class": "update"},
        }),
    ]
    if l1_temp is not None:
        sensors.append(("sensor.viessmann_planning_heating_target", {
            "state": str(l1_temp),
            "attributes": {"friendly_name": "Viessmann Planning — chauffage consigne",
                           "unit_of_measurement": "°C", "state_class": "measurement",
                           "icon": "mdi:radiator", "config": l1_cfg_name or "—"},
        }))
    if l2_temp is not None:
        sensors.append(("sensor.viessmann_planning_dhw_target", {
            "state": str(l2_temp),
            "attributes": {"friendly_name": "Viessmann Planning — DHW consigne",
                           "unit_of_measurement": "°C", "state_class": "measurement",
                           "icon": "mdi:water-boiler", "config": l2_cfg_name or "—"},
        }))
    if latest_v:
        sensors.append(("sensor.viessmann_planning_latest_version", {
            "state": latest_v,
            "attributes": {"friendly_name": "Viessmann Planning — version disponible",
                           "icon": "mdi:tag-arrow-up"},
        }))

    for entity_id, payload in sensors:
        try:
            r = _req.post(f"{ha_base}/{entity_id}", headers=ha_hdr, json=payload, timeout=5)
            log(f"[HA] {entity_id} → HTTP {r.status_code}", 1)
        except Exception as e:
            log(f"[HA] Failed to update {entity_id}: {e}", 1)


# ---------------------------------------------------------------------------
# COMMANDS
# ---------------------------------------------------------------------------

def cmd_viessmann_status():
    """Read current setpoints from Viessmann and print as JSON on stdout."""
    import sys as _sys
    _stdout, _sys.stdout = _sys.stdout, _sys.stderr

    _, device = get_vicare_client()
    result = {}

    for prog in ("comfort", "normal"):
        try:
            result["heating_temp"] = float(device.circuits[0].getProgramTemperature(prog))
            result["heating_program"] = prog
            break
        except Exception:
            continue

    try:
        result["dhw_temp"] = float(device.getDomesticHotWaterConfiguredTemperature())
    except Exception as e:
        result["dhw_error"] = str(e)

    try:
        result["device_type"] = type(device).__name__
    except Exception:
        pass

    log_api_stats()
    _sys.stdout = _stdout
    print(json.dumps(result))


def cmd_simulate(date_str: str | None = None):
    """Compute what would be applied without connecting to Viessmann. Output JSON."""
    import sys as _sys
    _stdout, _sys.stdout = _sys.stdout, _sys.stderr

    if date_str:
        try:
            now = datetime.datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            _sys.stdout = _stdout
            print(json.dumps({"error": f"Invalid date '{date_str}'"}))
            return
    else:
        now = datetime.datetime.now()

    plannings, weekconfigs = load_data_files()
    if not validate_all(plannings, weekconfigs):
        _sys.stdout = _stdout
        print(json.dumps({"error": "Validation failed"}))
        return

    active_pls = active_plannings_at(plannings, now)
    l1_cfg, l1_pl = select_config_for_level(active_pls, 1, now, weekconfigs)
    l2_cfg, l2_pl = select_config_for_level(active_pls, 2, now, weekconfigs)

    l1_temp = get_current_slot_temp(weekconfigs[l1_cfg], now) if l1_cfg else None
    l2_temp = get_current_slot_temp(weekconfigs[l2_cfg], now) if l2_cfg else None

    out = {
        "l1": {"config": l1_cfg, "planning": l1_pl, "temp": l1_temp},
        "l2": {"config": l2_cfg, "planning": l2_pl, "temp": l2_temp},
        "meta": {
            "date":     now.strftime("%Y-%m-%d"),
            "weekday":  DAY_NAMES_EN[now.weekday()],
            "iso_week": now.isocalendar()[1],
            "plannings": [p["name"] for p in active_pls],
            "l1_map":   {l1_cfg: l1_pl} if l1_cfg else {},
            "l2_map":   {l2_cfg: l2_pl} if l2_cfg else {},
        },
    }
    _sys.stdout = _stdout
    print(json.dumps(out))


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    global VERBOSITY

    parser = argparse.ArgumentParser(
        description="Applies Viessmann heating/DHW setpoints from plannings.json + weekconfigs.json.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("-d", "--date", metavar="YYYY-MM-DD",
                        help="Simulate a specific date (no API calls)")
    parser.add_argument("--viessmann-status", action="store_true",
                        help="Read current setpoints from Viessmann and output as JSON")
    parser.add_argument("--simulate", action="store_true",
                        help="Compute expected setpoints without connecting to Viessmann")
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help=("-v    : config details\n"
                              "-vv   : + cycle candidates\n"
                              "-vvv  : + API calls\n"
                              "-vvvv : + full debug"))
    args = parser.parse_args()
    VERBOSITY = min(args.verbose, 4)

    if args.viessmann_status:
        cmd_viessmann_status()
        return

    if args.simulate:
        cmd_simulate(args.date)
        return

    if args.date:
        try:
            now = datetime.datetime.strptime(args.date, "%Y-%m-%d")
            log(f"[MODE] Simulated date: {now.strftime('%d/%m/%Y')}")
        except ValueError:
            log(f"[ERROR] Invalid date format '{args.date}' (expected YYYY-MM-DD)")
            sys.exit(1)
    else:
        now = datetime.datetime.now()

    iso_week = now.isocalendar()[1]
    parity   = "odd" if iso_week % 2 == 1 else "even"
    log(f"[INFO] {DAY_NAMES_EN[now.weekday()]} {now.strftime('%d/%m/%Y %H:%M')} "
        f"— ISO week #{iso_week} ({parity})")

    log(f"[INFO] Loading {PLANNINGS_FILE}")
    log(f"[INFO] Loading {WEEKCONFIGS_FILE}")
    plannings, weekconfigs = load_data_files()

    if not validate_all(plannings, weekconfigs):
        sys.exit(1)

    active_pls = active_plannings_at(plannings, now)
    log(f"[INFO] Active plannings: {[p['name'] for p in active_pls]}")

    l1_cfg_name, l1_pl_name = select_config_for_level(active_pls, 1, now, weekconfigs)
    l2_cfg_name, l2_pl_name = select_config_for_level(active_pls, 2, now, weekconfigs)

    log(f"[INFO] Heating: {l1_cfg_name or '—'}"
        + (f" (from {l1_pl_name})" if l1_pl_name else ""))
    log(f"[INFO] DHW:     {l2_cfg_name or '—'}"
        + (f" (from {l2_pl_name})" if l2_pl_name else ""))

    if not l1_cfg_name and not l2_cfg_name:
        log("[WARN] No configs resolved — nothing to apply.")
        return

    l1_cfg = weekconfigs[l1_cfg_name] if l1_cfg_name else None
    l2_cfg = weekconfigs[l2_cfg_name] if l2_cfg_name else None
    l1_temp = get_current_slot_temp(l1_cfg, now) if l1_cfg else None
    l2_temp = get_current_slot_temp(l2_cfg, now) if l2_cfg else None

    log(f"[INFO] Target Heating: {l1_temp}°C" if l1_temp else "[INFO] Target Heating: —")
    log(f"[INFO] Target DHW:     {l2_temp}°C" if l2_temp else "[INFO] Target DHW:     —")

    if args.date:
        log("[MODE] Simulation — skipping API connection.")
        log_api_stats()
        return

    log("[VIESSMANN] Connecting...")
    _, device = get_vicare_client()

    l1_updated = False
    l2_updated = False

    if l1_cfg:
        try:
            l1_updated = apply_l1_heating(device, l1_cfg, now, l1_cfg_name)
        except Exception as e:
            log(f"[ERROR] Heating apply failed: {e}")

    if l2_cfg:
        try:
            l2_updated = apply_l2_dhw(device, l2_cfg, now, l2_cfg_name)
        except Exception as e:
            log(f"[ERROR] DHW apply failed: {e}")

    updated_count = sum([l1_updated, l2_updated])
    unchanged_count = sum([l1_cfg is not None and not l1_updated,
                           l2_cfg is not None and not l2_updated])
    log(f"[OK]   {updated_count} setpoint(s) updated, {unchanged_count} unchanged.")

    verify(device, l1_cfg, l2_cfg, now)

    log_api_stats()
    save_api_stats()
    push_ha_sensors(l1_cfg_name, l2_cfg_name, l1_temp, l2_temp)


if __name__ == "__main__":
    main()
