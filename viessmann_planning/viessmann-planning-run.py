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
  3. Read current setpoint from the boiler backend
  4. Update only if different

Requirements:
    pip install flask requests paho-mqtt
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
    _DEFAULT_DATA_DIR   = os.path.join(_SCRIPT_DIR, "..", "schedules")
    _DEFAULT_MQTT_FILE  = os.path.join(_SCRIPT_DIR, "..", "mqtt_credentials.json")
else:
    _DEFAULT_DATA_DIR   = "/config/viessmann-planning/schedules"
    _DEFAULT_MQTT_FILE  = "/config/viessmann-planning/mqtt_credentials.json"

DATA_DIR   = os.environ.get("VIESSMANN_SCHEDULES_DIR", os.path.abspath(_DEFAULT_DATA_DIR))
MQTT_FILE  = os.environ.get("VIESSMANN_MQTT_FILE",     os.path.abspath(_DEFAULT_MQTT_FILE))

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
DHW_TEMP_MIN     = 10
DHW_TEMP_MAX     = 60

VERBOSITY = 0

# Dry run: read the boiler and log what would be written, without writing anything.
# Set by --dry-run, VIESSMANN_DRY_RUN=true, or the add-on option "dry_run".
DRY_RUN = os.environ.get("VIESSMANN_DRY_RUN", "").strip().lower() in ("1", "true", "yes", "on")


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
                   resolution: int = 10, temp_min: int = None, temp_max: int = None) -> str | None:
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
    t_min = HEATING_TEMP_MIN if temp_min is None else temp_min
    t_max = DHW_TEMP_MAX     if temp_max is None else temp_max
    try:
        temp = int(slot["temp"])
        if not (t_min <= temp <= t_max):
            return (f"[VALIDATION] '{cfg_name}'/'{day_key}' slot #{idx}: "
                    f"temp {temp} out of allowed range {t_min}–{t_max}")
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

    level = cfg.get("level", "L1")
    t_min = DHW_TEMP_MIN if level == "L2" else HEATING_TEMP_MIN
    t_max = DHW_TEMP_MAX if level == "L2" else HEATING_TEMP_MAX

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
                err = _validate_slot(s, cfg_name, key, i, resolution, t_min, t_max)
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
# BOILER BACKEND  — local MQTT
# ---------------------------------------------------------------------------
# Talks to the Optolink reader through the MQTT broker. The only reference is
# the contract kept with the reader (CONTRAT_MQTT.md, version 1):
#   <prefix>/LWT          "online" / "offline"   (retained)
#   <prefix>/<name>       current value          (retained, republished on change only)
#   <prefix>/<name>/set   write a value by name  (same unit as the value read)
#   <prefix>/resp         "<code>;<address>;<value>" after each write, code 1 = accepted
# The rest of this script (planning resolution above, apply / verify / HA push
# below) only depends on the five functions of this section.

BACKEND_NAME = "local-mqtt"

MQTT_PREFIX       = os.environ.get("VIESSMANN_MQTT_PREFIX", "vito").strip("/")
DP_HEATING        = "consigne_confort_circuit_1"   # heating circuit 1, comfort room setpoint (°C, integer)
DP_DHW            = "consigne_ecs"                 # DHW setpoint (°C, integer)
MQTT_CONNECT_WAIT = 10    # seconds to get a broker answer
MQTT_DATA_WAIT    = 5     # seconds to receive the retained values after subscribing
MQTT_WRITE_WAIT   = 20    # seconds for the reader to publish the value just written


class BackendError(Exception):
    """Boiler unreachable, or backend not ready. `reason` is published to HA."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason


def load_mqtt_config() -> dict:
    """Broker settings: MQTT_FILE if present, else the Home Assistant MQTT service (add-on)."""
    if os.path.exists(MQTT_FILE):
        try:
            with open(MQTT_FILE, encoding="utf-8") as f:
                cfg = json.load(f)
            if not isinstance(cfg, dict) or not cfg.get("host"):
                raise ValueError('"host" is missing')
        except Exception as e:
            raise BackendError("MQTT_CONFIG_ERROR", f"{MQTT_FILE}: {e}")
        log(f"[MQTT] Settings from {MQTT_FILE}", 1)
        return cfg

    token = os.environ.get("SUPERVISOR_TOKEN")
    if token:
        try:
            import requests as _req
            r = _req.get("http://supervisor/services/mqtt",
                         headers={"Authorization": f"Bearer {token}"}, timeout=5)
            data = r.json().get("data") or {}
        except Exception as e:
            raise BackendError("MQTT_CONFIG_ERROR", f"Supervisor MQTT service: {e}")
        if not r.ok or not data.get("host"):
            raise BackendError("MQTT_NOT_CONFIGURED",
                               f"Supervisor MQTT service unavailable (HTTP {r.status_code})")
        log("[MQTT] Settings from the Home Assistant MQTT service", 1)
        return data

    raise BackendError("MQTT_NOT_CONFIGURED",
                       f"create {MQTT_FILE} with "
                       '{"host": "...", "port": 1883, "username": "...", "password": "..."}')


class MqttBoiler:
    """Connection to the broker, with the last value seen on each watched topic."""

    def __init__(self, cfg: dict):
        try:
            import paho.mqtt.client as paho
        except ImportError:
            raise BackendError("MQTT_CLIENT_MISSING", "paho-mqtt not installed. Run: pip install paho-mqtt")
        import threading

        self._values: dict[str, str] = {}
        self._last_resp: str | None = None
        self._changed = threading.Condition()
        self._rc = None
        self._watched = [f"{MQTT_PREFIX}/{n}" for n in ("LWT", DP_HEATING, DP_DHW, "resp")]

        client = paho.Client(paho.CallbackAPIVersion.VERSION2,
                             client_id=f"viessmann-planning-{os.getpid()}")
        if cfg.get("username"):
            client.username_pw_set(cfg["username"], cfg.get("password") or None)
        if cfg.get("ssl"):
            client.tls_set()
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        self._client = client

        host, port = cfg["host"], int(cfg.get("port") or 1883)
        log(f"[MQTT] Connecting to {host}:{port}", 1)
        try:
            client.connect(host, port, keepalive=30)
        except Exception as e:
            raise BackendError("MQTT_UNREACHABLE", f"{host}:{port}: {e}")
        client.loop_start()

        with self._changed:
            self._changed.wait_for(lambda: self._rc is not None, timeout=MQTT_CONNECT_WAIT)
        if self._rc is None:
            self.close()
            raise BackendError("MQTT_UNREACHABLE", f"{host}:{port}: no answer in {MQTT_CONNECT_WAIT}s")
        if self._rc.is_failure:
            self.close()
            raise BackendError("MQTT_AUTH_ERROR", f"{host}:{port}: {self._rc}")

        # the retained values arrive right after the subscription
        with self._changed:
            self._changed.wait_for(
                lambda: all(f"{MQTT_PREFIX}/{n}" in self._values for n in ("LWT", DP_HEATING, DP_DHW)),
                timeout=MQTT_DATA_WAIT)
        lwt = self._values.get(f"{MQTT_PREFIX}/LWT")
        if lwt != "online":
            self.close()
            raise BackendError("READER_OFFLINE", f"{MQTT_PREFIX}/LWT = {lwt!r}")

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if not reason_code.is_failure:
            client.subscribe([(t, 1) for t in self._watched])
        with self._changed:
            self._rc = reason_code
            self._changed.notify_all()

    def _on_message(self, client, userdata, msg):
        payload = msg.payload.decode("utf-8", "replace").strip()
        with self._changed:
            if msg.topic == f"{MQTT_PREFIX}/resp":
                self._last_resp = payload
            else:
                self._values[msg.topic] = payload
            self._changed.notify_all()

    def read(self, name: str) -> float | None:
        raw = self._values.get(f"{MQTT_PREFIX}/{name}")
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    def write(self, name: str, value: int):
        """Write by name, then wait until the reader publishes the new value."""
        with self._changed:
            self._last_resp = None
        info = self._client.publish(f"{MQTT_PREFIX}/{name}/set", str(int(value)), qos=1)
        info.wait_for_publish(timeout=MQTT_CONNECT_WAIT)
        with self._changed:
            done = self._changed.wait_for(
                lambda: (r := self.read(name)) is not None and abs(r - value) <= 0.1,
                timeout=MQTT_WRITE_WAIT)
        if not done:
            raise RuntimeError(f"{name}: no confirmation in {MQTT_WRITE_WAIT}s "
                               f"(value read: {self.read(name)}, last response: {self._last_resp})")

    def close(self):
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:
            pass


def connect_backend():
    """Return an opaque handle, passed as `device` to the get_/set_ functions."""
    import atexit
    device = MqttBoiler(load_mqtt_config())
    atexit.register(device.close)
    log("[MQTT] Connected, reader online.", 1)
    return device


def get_heating_setpoint(device) -> float | None:
    """Read current heating comfort temperature setpoint."""
    _api_stats["GET"] += 1
    val = device.read(DP_HEATING)
    if val is None:
        log("[WARN] Could not read heating setpoint", 1)
    else:
        log(f"[MQTT] GET {DP_HEATING}: {val}°C", 3)
    return val


def set_heating_setpoint(device, temp: int):
    """Set heating comfort temperature setpoint."""
    _api_stats["SET"] += 1
    try:
        device.write(DP_HEATING, temp)
        log(f"[MQTT] SET {DP_HEATING}: {temp}°C", 3)
    except Exception as e:
        raise RuntimeError(f"Failed to set heating setpoint to {temp}°C: {e}")


def get_dhw_setpoint(device) -> float | None:
    """Read current DHW/ECS temperature setpoint."""
    _api_stats["GET"] += 1
    val = device.read(DP_DHW)
    if val is None:
        log("[WARN] Could not read DHW setpoint", 1)
    else:
        log(f"[MQTT] GET {DP_DHW}: {val}°C", 3)
    return val


def set_dhw_setpoint(device, temp: int):
    """Set DHW/ECS temperature setpoint."""
    _api_stats["SET"] += 1
    try:
        device.write(DP_DHW, temp)
        log(f"[MQTT] SET {DP_DHW}: {temp}°C", 3)
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

    if DRY_RUN:
        log(f"[DRY-RUN] Heating: would set {target}°C — nothing written")
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

    if DRY_RUN:
        log(f"[DRY-RUN] DHW: would set {target}°C — nothing written")
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

def push_gateway_status(online: bool, reason: str) -> None:
    """Publie dans HA l'accessibilité de la chaudière vue par l'add-on, à chaque run,
    y compris quand la connexion échoue (BackendError.reason) — l'onglet « Vitoconnect » de HA2 s'en sert.
    Les états POSTés ne survivent pas à un redémarrage de HA : ils sont republiés toutes les 10 min."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return
    try:
        import requests as _req
    except ImportError:
        return
    hdr = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    checked = datetime.datetime.now().astimezone().isoformat()
    payloads = [
        ("binary_sensor.viessmann_planning_gateway_online", {
            "state": "on" if online else "off",
            "attributes": {"friendly_name": "Viessmann Planning — passerelle joignable",
                           "device_class": "connectivity", "icon": "mdi:cloud-sync",
                           "reason": reason, "checked_at": checked},
        }),
        ("sensor.viessmann_planning_gateway_status", {
            "state": reason,
            "attributes": {"friendly_name": "Viessmann Planning — statut passerelle",
                           "icon": "mdi:cloud-question", "checked_at": checked},
        }),
    ]
    for entity_id, payload in payloads:
        try:
            r = _req.post(f"http://supervisor/core/api/states/{entity_id}", headers=hdr, json=payload, timeout=5)
            log(f"[HA] {entity_id} → HTTP {r.status_code}", 1)
        except Exception as e:
            log(f"[HA] {entity_id} push failed: {e}", 1)


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
    """Read current setpoints from the boiler backend and print as JSON on stdout."""
    import sys as _sys
    _stdout, _sys.stdout = _sys.stdout, _sys.stderr

    result = {"backend": BACKEND_NAME}
    try:
        device = connect_backend()
        heating = get_heating_setpoint(device)
        if heating is not None:
            result["heating_temp"] = heating
        dhw = get_dhw_setpoint(device)
        if dhw is not None:
            result["dhw_temp"] = dhw
    except BackendError as e:
        result["error"] = e.reason

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
    global VERBOSITY, DRY_RUN

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
    parser.add_argument("--dry-run", action="store_true",
                        help="Read the boiler and log what would be written, without writing")
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help=("-v    : config details\n"
                              "-vv   : + cycle candidates\n"
                              "-vvv  : + API calls\n"
                              "-vvvv : + full debug"))
    args = parser.parse_args()
    VERBOSITY = min(args.verbose, 4)
    DRY_RUN = DRY_RUN or args.dry_run

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

    log(f"[BOILER] Connecting ({BACKEND_NAME})...")
    try:
        device = connect_backend()
    except BackendError as e:
        log(f"[BOILER] Unreachable: {e.reason} ({e})")
        push_gateway_status(False, e.reason)
        log_api_stats()
        save_api_stats()
        return
    push_gateway_status(True, "online")

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

    if DRY_RUN:
        log("[DRY-RUN] Nothing was written to the boiler.")
    else:
        verify(device, l1_cfg, l2_cfg, now)

    log_api_stats()
    save_api_stats()
    push_ha_sensors(l1_cfg_name, l2_cfg_name, l1_temp, l2_temp)


if __name__ == "__main__":
    main()
