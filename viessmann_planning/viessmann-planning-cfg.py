#!/usr/bin/env python3
"""
viessmann-planning-cfg.py  v1.0
=================================
Web configurator for viessmann-planning.
Manages:
    weekconfigs.json  — heating and DHW schedule configurations
    plannings.json    — all plannings (standard + exceptions)
    settings.json     — resolution (minutes), default temps

Usage:
    python3 viessmann-planning-cfg.py
    python3 viessmann-planning-cfg.py --port 8098 --host 0.0.0.0 --no-browser
"""

import sys
import os
import json
import copy
import argparse
import platform
import datetime
import subprocess
import threading
import webbrowser
import time

from flask import Flask, jsonify, request, render_template


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

CREDS_FILE        = os.environ.get("VIESSMANN_CREDS_FILE",    os.path.abspath(_DEFAULT_CREDS_FILE))
TOKEN_FILE        = os.environ.get("VIESSMANN_TOKEN_FILE",    os.path.abspath(_DEFAULT_TOKEN_FILE))
DATA_DIR          = os.environ.get("VIESSMANN_SCHEDULES_DIR", os.path.abspath(_DEFAULT_DATA_DIR))
VIESSMANN_CONTEXT = os.environ.get("VIESSMANN_CONTEXT", "unknown")

PLANNINGS_FILE    = os.path.join(DATA_DIR, "plannings.json")
WEEKCONFIGS_FILE  = os.path.join(DATA_DIR, "weekconfigs.json")
SETTINGS_FILE     = os.path.join(DATA_DIR, "settings.json")
LOOP_STATUS_FILE  = os.path.join(DATA_DIR, "loop_status.json")
LOOP_TRIGGER_FILE = os.path.join(DATA_DIR, "loop_trigger")
LOG_FILE          = os.path.join(DATA_DIR, "viessmann-planning.log")
LOG_FILE_PREV     = os.path.join(DATA_DIR, "viessmann-planning.log.1")

_PROJECT_DIR    = os.path.dirname(_SCRIPT_DIR)
PLANNING_SCRIPT = os.path.join(_SCRIPT_DIR, "viessmann-planning-run.py")

# ---------------------------------------------------------------------------
# FLASK APP
# ---------------------------------------------------------------------------

app = Flask(__name__,
            template_folder=os.path.join(_SCRIPT_DIR, "templates"),
            static_folder=os.path.join(_SCRIPT_DIR, "static"))
app.config["JSON_SORT_KEYS"] = False

# ---------------------------------------------------------------------------
# FILE I/O
# ---------------------------------------------------------------------------

def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def load_weekconfigs() -> dict:
    if not os.path.exists(WEEKCONFIGS_FILE):
        return {}
    with open(WEEKCONFIGS_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_weekconfigs(data: dict):
    _ensure_data_dir()
    with open(WEEKCONFIGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_plannings() -> list:
    if not os.path.exists(PLANNINGS_FILE):
        return []
    with open(PLANNINGS_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_plannings(data: list):
    _ensure_data_dir()
    with open(PLANNINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_settings() -> dict:
    if not os.path.exists(SETTINGS_FILE):
        return {"resolution": 10, "default_heating_temp": 20, "default_dhw_temp": 60}
    with open(SETTINGS_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_settings(data: dict):
    _ensure_data_dir()
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# CONSTANTS
# ---------------------------------------------------------------------------

VALID_TIMETABLES = ("Mon-Sun", "Mon-Fri, Sat, Sun", "Mon, ..., Sun")
TIMETABLE_KEYS   = {
    "Mon-Sun":           ["Mon-Sun"],
    "Mon-Fri, Sat, Sun": ["Mon-Fri", "Sat", "Sun"],
    "Mon, ..., Sun":     ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
}
VALID_CYCLES      = ("one-week", "two-weeks-iso", "two-weeks-seq")
DAY_NAMES         = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
VALID_RESOLUTIONS = (1, 5, 10, 20, 30, 60)

# ---------------------------------------------------------------------------
# VALIDATION HELPERS
# ---------------------------------------------------------------------------

def validate_planning_conflicts(plannings: list, exclude_name: str = None) -> list:
    errors = []
    seen   = {}
    for p in plannings:
        if p.get("name") == exclude_name:
            continue
        key  = (p.get("start"), p.get("end"))
        name = p.get("name", "?")
        if key in seen:
            other = seen[key]
            s, e  = key
            if s is None and e is None:
                errors.append(f"'{other}' and '{name}': both have no start and no end")
            elif s is None:
                errors.append(f"'{other}' and '{name}': both have no start, same end '{e}'")
            elif e is None:
                errors.append(f"'{other}' and '{name}': both have same start '{s}', no end")
            else:
                errors.append(f"'{other}' and '{name}': identical start and end")
        else:
            seen[key] = name
    return errors


def validate_planning(p: dict, weekconfigs: dict, all_plannings: list,
                      exclude_name: str = None) -> list:
    errors = []
    name   = p.get("name", "")

    if not name:
        errors.append("Name is required")

    cycle = p.get("cycle")
    if cycle not in VALID_CYCLES:
        errors.append(f"Invalid cycle '{cycle}'")

    if cycle == "two-weeks-seq" and not p.get("ref_date"):
        errors.append("ref_date is required for two-weeks-seq cycle")

    for field in ("start", "end"):
        val = p.get(field)
        if val is not None:
            try:
                datetime.datetime.strptime(val, "%Y-%m-%d %H:%M")
            except ValueError:
                errors.append(f"Invalid {field} format (expected YYYY-MM-DD HH:MM)")

    start, end = p.get("start"), p.get("end")
    if start and end:
        try:
            if datetime.datetime.strptime(start, "%Y-%m-%d %H:%M") >= \
               datetime.datetime.strptime(end,   "%Y-%m-%d %H:%M"):
                errors.append("start must be before end")
        except ValueError:
            pass

    test_list = [p] + [x for x in all_plannings if x.get("name") != exclude_name]
    errors.extend(validate_planning_conflicts(test_list))

    events = p.get("events", [])
    if not isinstance(events, list) or len(events) == 0:
        errors.append("At least one event is required")
        return errors

    for i, ev in enumerate(events):
        if not isinstance(ev, dict):
            continue
        if ev.get("day", "").lower() not in DAY_NAMES:
            errors.append(f"Event #{i+1}: invalid day '{ev.get('day')}'")
        if ev.get("level") not in (1, 2):
            errors.append(f"Event #{i+1}: level must be 1 or 2")
        cfg = ev.get("config", "")
        if cfg and cfg not in weekconfigs:
            errors.append(f"Event #{i+1}: config '{cfg}' not found in weekconfigs")

    return errors


def _adapt_slots_to_resolution(slots: list, resolution: int) -> list:
    """Round slot times to nearest multiple of resolution minutes."""
    adapted = []
    seen_times = set()
    for slot in slots:
        try:
            h, m = map(int, slot["start"].split(":"))
            total = h * 60 + m
            rounded = round(total / resolution) * resolution
            rounded = min(rounded, 23 * 60 + (60 - resolution))  # cap at last valid time
            nh, nm = divmod(rounded, 60)
            new_start = f"{nh:02d}:{nm:02d}"
            if new_start not in seen_times:
                seen_times.add(new_start)
                adapted.append({**slot, "start": new_start})
        except (ValueError, KeyError):
            continue
    return sorted(adapted, key=lambda s: s["start"])


def adapt_weekconfigs_to_resolution(weekconfigs: dict, resolution: int) -> dict:
    """Adapt all slot times in all weekconfigs to the new resolution."""
    adapted = {}
    for name, cfg in weekconfigs.items():
        new_cfg = dict(cfg)
        for key in TIMETABLE_KEYS.get(cfg.get("timetable", ""), []):
            if key in cfg:
                slots = _adapt_slots_to_resolution(cfg[key], resolution)
                if slots and slots[0]["start"] != "00:00":
                    slots = [{"start": "00:00", "temp": slots[0]["temp"]}] + slots
                new_cfg[key] = slots
        adapted[name] = new_cfg
    return adapted


# ---------------------------------------------------------------------------
# PLANNING LOGIC (shared helpers)
# ---------------------------------------------------------------------------

def _parse_dt(s):
    return datetime.datetime.strptime(s, "%Y-%m-%d %H:%M") if s else None


def _week_parity(t: datetime.datetime, planning: dict) -> str:
    cycle    = planning.get("cycle", "two-weeks-iso")
    ref_date = planning.get("ref_date")
    if cycle == "two-weeks-iso":
        return "odd" if t.isocalendar()[1] % 2 == 1 else "even"
    elif cycle == "two-weeks-seq" and ref_date:
        ref     = datetime.datetime.strptime(ref_date, "%Y-%m-%d")
        ref_mon = ref - datetime.timedelta(days=ref.weekday())
        t_mon   = t   - datetime.timedelta(days=t.weekday())
        weeks   = int((t_mon - ref_mon).days / 7)
        return "odd" if weeks % 2 == 0 else "even"
    return "odd"


_DAY_MAP = {"monday":0,"tuesday":1,"wednesday":2,
            "thursday":3,"friday":4,"saturday":5,"sunday":6}
_DAY_ABR = {0:"Mon",1:"Tue",2:"Wed",3:"Thu",4:"Fri",5:"Sat",6:"Sun"}


def _active_plannings_at(plannings: list, t: datetime.datetime) -> list:
    group1, group2, group3 = [], [], []
    for p in plannings:
        s = _parse_dt(p.get("start"))
        e = _parse_dt(p.get("end"))
        if s is not None:
            if s <= t and (e is None or e > t):
                group1.append(p)
        elif e is not None:
            if e > t:
                group2.append(p)
        else:
            group3.append(p)

    def g1_key(p):
        s = _parse_dt(p["start"])
        e = _parse_dt(p.get("end"))
        return (-s.timestamp(), e.timestamp() if e else float("inf"))

    group1.sort(key=g1_key)
    group2.sort(key=lambda p: _parse_dt(p["end"]))
    return group1 + group2 + group3


def _resolve_config_for_level(level: int,
                               plannings_by_precedence: list,
                               t: datetime.datetime,
                               weekconfigs: dict | None = None) -> tuple[str | None, str | None]:
    if weekconfigs is None:
        weekconfigs = load_weekconfigs()
    monday = (t - datetime.timedelta(days=t.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)

    for planning in plannings_by_precedence:
        cycle  = planning.get("cycle", "two-weeks-iso")
        events = [e for e in planning.get("events", [])
                  if isinstance(e, dict) and e.get("level") == level
                  and e.get("config") is not None]
        if not events:
            continue

        parity = _week_parity(t, planning)
        is_odd = (parity == "odd")
        if is_odd:
            odd_mon, even_mon = monday, monday + datetime.timedelta(weeks=1)
            p_odd, p_even = monday - datetime.timedelta(weeks=2), monday - datetime.timedelta(weeks=1)
        else:
            even_mon, odd_mon = monday, monday + datetime.timedelta(weeks=1)
            p_even, p_odd = monday - datetime.timedelta(weeks=2), monday - datetime.timedelta(weeks=1)

        candidates = []
        for ev in events:
            week = ev.get("week", "both").lower()
            d    = _DAY_MAP.get(ev.get("day", "monday").lower(), 0)
            h, m = map(int, ev.get("time", "00:00").split(":"))
            if cycle == "one-week":
                for mon in [odd_mon, even_mon, p_odd, p_even]:
                    candidates.append((mon + datetime.timedelta(days=d, hours=h, minutes=m),
                                       ev["config"]))
            else:
                if week in ("odd", "both"):
                    candidates.append((odd_mon + datetime.timedelta(days=d, hours=h, minutes=m), ev["config"]))
                    candidates.append((p_odd   + datetime.timedelta(days=d, hours=h, minutes=m), ev["config"]))
                if week in ("even", "both"):
                    candidates.append((even_mon + datetime.timedelta(days=d, hours=h, minutes=m), ev["config"]))
                    candidates.append((p_even   + datetime.timedelta(days=d, hours=h, minutes=m), ev["config"]))

        if not candidates:
            continue

        past = [(dt, cfg) for dt, cfg in candidates if t >= dt]
        _, cfg = max(past, key=lambda x: x[0]) if past else min(candidates, key=lambda x: x[0])

        if cfg in weekconfigs:
            return cfg, planning.get("name")

    return None, None


def _day_key_for_timetable(tt: str, now: datetime.datetime) -> str:
    wd = now.weekday()
    if tt == "Mon-Sun":
        return "Mon-Sun"
    elif tt == "Mon-Fri, Sat, Sun":
        return "Mon-Fri" if wd <= 4 else ("Sat" if wd == 5 else "Sun")
    else:
        return ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][wd]


def _current_slot_temp(cfg: dict, now: datetime.datetime) -> int | None:
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
        active = slots[-1]
    try:
        return int(active["temp"])
    except (TypeError, ValueError, KeyError):
        return None


def _current_slot_index(cfg: dict, now: datetime.datetime) -> int | None:
    tt = cfg.get("timetable")
    if not tt:
        return None
    day_key = _day_key_for_timetable(tt, now)
    slots   = cfg.get(day_key, [])
    if not slots:
        return None
    now_minutes = now.hour * 60 + now.minute
    active_idx  = None
    for i, slot in enumerate(slots):
        try:
            h, m = map(int, slot["start"].split(":"))
            if h * 60 + m <= now_minutes:
                active_idx = i
        except (ValueError, KeyError):
            continue
    return active_idx if active_idx is not None else len(slots) - 1


# ---------------------------------------------------------------------------
# STATUS
# ---------------------------------------------------------------------------

def get_status() -> dict:
    try:
        plannings   = load_plannings()
        weekconfigs = load_weekconfigs()
        now         = datetime.datetime.now()
        iso_week    = now.isocalendar()[1]
        parity      = "odd" if iso_week % 2 == 1 else "even"

        planning_status = []
        for p in plannings:
            s = _parse_dt(p.get("start"))
            e = _parse_dt(p.get("end"))
            if s is not None and s > now:
                status = "upcoming"
            elif e is not None and e <= now:
                status = "ended"
            else:
                status = "active"

            cycle      = p.get("cycle", "two-weeks-iso")
            cycle_info = None
            if status == "active":
                if cycle == "two-weeks-iso":
                    cycle_info = f"ISO week {iso_week} — {parity}"
                elif cycle == "two-weeks-seq":
                    par = _week_parity(now, p)
                    cycle_info = f"week {'1' if par == 'odd' else '2'} of 2"
                else:
                    cycle_info = "week 1 of 1"

            if s or e:
                parts = [s.strftime("%d/%m %H:%M") if s else "…", "→",
                         e.strftime("%d/%m %H:%M") if e else "…"]
                if e and status == "active":
                    delta = int((e - now).days)
                    parts.append(f"· ends in {delta} days" if delta >= 0 else f"· ended {-delta} days ago")
                period_str = " ".join(parts)
            else:
                period_str = "always active — no period"

            planning_status.append({
                "name":       p.get("name"),
                "status":     status,
                "cycle":      cycle,
                "cycle_info": cycle_info,
                "period":     period_str,
                "start":      p.get("start"),
                "end":        p.get("end"),
            })

        active_pls = _active_plannings_at(plannings, now)
        l1_cfg, l1_pl = _resolve_config_for_level(1, active_pls, now, weekconfigs)
        l2_cfg, l2_pl = _resolve_config_for_level(2, active_pls, now, weekconfigs)

        l1_temp = _current_slot_temp(weekconfigs[l1_cfg], now) if l1_cfg else None
        l2_temp = _current_slot_temp(weekconfigs[l2_cfg], now) if l2_cfg else None

        return {
            "now":      now.strftime("%A %d/%m/%Y %H:%M"),
            "iso_week": iso_week,
            "parity":   parity,
            "plannings": planning_status,
            "l1": {"config": l1_cfg, "planning": l1_pl, "temp": l1_temp} if l1_cfg else None,
            "l2": {"config": l2_cfg, "planning": l2_pl, "temp": l2_temp} if l2_cfg else None,
        }

    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# TIMELINE
# ---------------------------------------------------------------------------

def get_timeline(days: int = 14) -> dict:
    try:
        plannings   = load_plannings()
        weekconfigs = load_weekconfigs()
        now         = datetime.datetime.now()
        end         = now + datetime.timedelta(days=days)

        moments    = set()
        moments.add(now.replace(second=0, microsecond=0))
        boundaries = {}

        for p in plannings:
            for field, btype in (("start", "starts"), ("end", "ends")):
                val = p.get(field)
                if val:
                    dt = _parse_dt(val)
                    if now <= dt <= end:
                        moments.add(dt)
                        boundaries.setdefault(dt, []).append(
                            {"planning": p["name"], "type": btype})

        for p in plannings:
            cycle   = p.get("cycle", "two-weeks-iso")
            events  = [e for e in p.get("events", []) if isinstance(e, dict)]
            p_start = _parse_dt(p.get("start"))
            p_end   = _parse_dt(p.get("end"))
            scan    = now - datetime.timedelta(days=7)
            while scan <= end + datetime.timedelta(days=7):
                monday = (scan - datetime.timedelta(days=scan.weekday())).replace(
                    hour=0, minute=0, second=0, microsecond=0)
                parity = _week_parity(monday + datetime.timedelta(hours=12), p)
                for ev in events:
                    week = ev.get("week", "both").lower()
                    d    = _DAY_MAP.get(ev.get("day", "monday").lower(), 0)
                    h, m = map(int, ev.get("time", "00:00").split(":"))
                    if cycle != "one-week":
                        if week == "odd"  and parity != "odd":  continue
                        if week == "even" and parity != "even": continue
                    dt = monday + datetime.timedelta(days=d, hours=h, minutes=m)
                    if p_start and dt < p_start: continue
                    if p_end   and dt >= p_end:  continue
                    if now <= dt <= end:
                        moments.add(dt)
                scan += datetime.timedelta(weeks=1)

        sorted_moments = sorted(moments)

        columns = []
        for i, t in enumerate(sorted_moments):
            bds = boundaries.get(t, [])
            columns.append({
                "dt":         t.strftime("%Y-%m-%d %H:%M"),
                "label":      f"{_DAY_ABR[t.weekday()]} {t.strftime('%d/%m')}  {t.strftime('%H:%M')}",
                "now":        (i == 0),
                "boundaries": bds,
            })

        active_pls_per_t = [_active_plannings_at(plannings, t) for t in sorted_moments]

        l1_prev = l2_prev = None
        l1_row  = []
        l2_row  = []
        has_l1  = False
        has_l2  = False

        for t, active_pls in zip(sorted_moments, active_pls_per_t):
            l1_cfg, l1_pl = _resolve_config_for_level(1, active_pls, t, weekconfigs)
            l2_cfg, l2_pl = _resolve_config_for_level(2, active_pls, t, weekconfigs)

            l1_entry = {"config": l1_cfg, "planning": l1_pl} if l1_cfg else None
            l2_entry = {"config": l2_cfg, "planning": l2_pl} if l2_cfg else None

            l1_row.append(l1_entry if l1_cfg != l1_prev else None)
            l2_row.append(l2_entry if l2_cfg != l2_prev else None)

            if l1_entry: has_l1 = True
            if l2_entry: has_l2 = True

            l1_prev = l1_cfg
            l2_prev = l2_cfg

        levels = {}
        if has_l1:
            levels["l1"] = l1_row
        if has_l2:
            levels["l2"] = l2_row

        return {"columns": columns, "levels": levels}

    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# WEEKCONFIG STATUS (for Active Status/Weekconfig tab)
# ---------------------------------------------------------------------------

def get_weekconfig_status() -> dict:
    """
    Returns current-day schedule for Heating and DHW, plus active slot index.
    Optionally fetches live Viessmann setpoints.
    """
    try:
        plannings   = load_plannings()
        weekconfigs = load_weekconfigs()
        now         = datetime.datetime.now()

        active_pls = _active_plannings_at(plannings, now)
        l1_cfg_name, l1_pl = _resolve_config_for_level(1, active_pls, now, weekconfigs)
        l2_cfg_name, l2_pl = _resolve_config_for_level(2, active_pls, now, weekconfigs)

        def build_level_info(cfg_name, planning_name):
            if cfg_name is None:
                return None
            cfg     = weekconfigs.get(cfg_name)
            if not cfg:
                return None
            tt      = cfg.get("timetable")
            day_key = _day_key_for_timetable(tt, now) if tt else None
            slots   = cfg.get(day_key, []) if day_key else []
            idx     = _current_slot_index(cfg, now)
            return {
                "config":       cfg_name,
                "planning":     planning_name,
                "timetable":    tt,
                "day_key":      day_key,
                "slots":        slots,
                "current_slot_idx": idx,
                "current_temp": _current_slot_temp(cfg, now),
            }

        return {
            "now":  now.strftime("%A %d/%m/%Y %H:%M"),
            "l1":   build_level_info(l1_cfg_name, l1_pl),
            "l2":   build_level_info(l2_cfg_name, l2_pl),
        }

    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# ROUTES — MAIN
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    ingress_path = request.headers.get("X-Ingress-Path", "")
    return render_template("index.html", ingress_path=ingress_path)


@app.route("/api/status")
def api_status():
    return jsonify(get_status())


@app.route("/api/timeline")
def api_timeline():
    days = int(request.args.get("days", 14))
    return jsonify(get_timeline(days))


@app.route("/api/weekconfig-status")
def api_weekconfig_status():
    return jsonify(get_weekconfig_status())


# ---------------------------------------------------------------------------
# API — WEEKCONFIGS
# ---------------------------------------------------------------------------

@app.route("/api/weekconfigs")
def api_weekconfigs_list():
    wc = load_weekconfigs()
    return jsonify({"names": list(wc.keys()), "weekconfigs": wc})


@app.route("/api/weekconfigs/<name>")
def api_weekconfig_get(name):
    wc = load_weekconfigs()
    if name not in wc:
        return jsonify({"error": "Not found"}), 404
    return jsonify(wc[name])


@app.route("/api/weekconfigs/<name>", methods=["POST"])
def api_weekconfig_save(name):
    data = request.get_json()
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid data"}), 400
    overwrite = request.args.get("overwrite", "false").lower() == "true"
    wc = load_weekconfigs()
    if name in wc and not overwrite:
        return jsonify({"exists": True, "error": f"'{name}' already exists"}), 409
    wc[name] = data
    save_weekconfigs(wc)
    return jsonify({"ok": True})


@app.route("/api/weekconfigs/<name>", methods=["DELETE"])
def api_weekconfig_delete(name):
    wc = load_weekconfigs()
    if name not in wc:
        return jsonify({"error": "Not found"}), 404
    plannings = load_plannings()
    refs = [p["name"] for p in plannings
            for ev in p.get("events", [])
            if isinstance(ev, dict) and ev.get("config") == name]
    if refs:
        return jsonify({
            "error": f"Config '{name}' is referenced in planning(s): {', '.join(set(refs))}. "
                     f"Remove those references first."}), 409
    del wc[name]
    save_weekconfigs(wc)
    return jsonify({"ok": True})


@app.route("/api/weekconfigs/<name>/rename", methods=["POST"])
def api_weekconfig_rename(name):
    body    = request.get_json() or {}
    newname = body.get("newname", "").strip()
    if not newname:
        return jsonify({"error": "newname is required"}), 400
    wc = load_weekconfigs()
    if name not in wc:
        return jsonify({"error": "Not found"}), 404
    if newname in wc:
        return jsonify({"error": f"'{newname}' already exists"}), 409
    wc[newname] = wc.pop(name)
    plannings = load_plannings()
    for p in plannings:
        for ev in p.get("events", []):
            if isinstance(ev, dict) and ev.get("config") == name:
                ev["config"] = newname
    save_weekconfigs(wc)
    save_plannings(plannings)
    return jsonify({"ok": True})


@app.route("/api/weekconfigs/<name>/copy", methods=["POST"])
def api_weekconfig_copy(name):
    body    = request.get_json() or {}
    newname = body.get("newname", "").strip()
    if not newname:
        return jsonify({"error": "newname is required"}), 400
    wc = load_weekconfigs()
    if name not in wc:
        return jsonify({"error": "Source not found"}), 404
    if newname in wc:
        return jsonify({"error": f"'{newname}' already exists"}), 409
    wc[newname] = copy.deepcopy(wc[name])
    save_weekconfigs(wc)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# API — PLANNINGS
# ---------------------------------------------------------------------------

@app.route("/api/plannings")
def api_plannings_list():
    return jsonify({"plannings": load_plannings()})


@app.route("/api/plannings/<name>")
def api_planning_get(name):
    plannings = load_plannings()
    for p in plannings:
        if p.get("name") == name:
            return jsonify(p)
    return jsonify({"error": "Not found"}), 404


@app.route("/api/plannings/<name>", methods=["POST"])
def api_planning_save(name):
    data = request.get_json()
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid data"}), 400
    overwrite = request.args.get("overwrite", "false").lower() == "true"
    data["name"] = name

    plannings   = load_plannings()
    weekconfigs = load_weekconfigs()
    existing    = next((p for p in plannings if p.get("name") == name), None)

    if existing and not overwrite:
        return jsonify({"exists": True, "error": f"'{name}' already exists"}), 409

    errors = validate_planning(data, weekconfigs, plannings, exclude_name=name)
    if errors:
        return jsonify({"error": "; ".join(errors)}), 422

    if existing:
        plannings[plannings.index(existing)] = data
    else:
        plannings.append(data)
    save_plannings(plannings)
    return jsonify({"ok": True})


@app.route("/api/plannings/<name>", methods=["DELETE"])
def api_planning_delete(name):
    plannings = load_plannings()
    match = next((p for p in plannings if p.get("name") == name), None)
    if not match:
        return jsonify({"error": "Not found"}), 404
    remaining  = [p for p in plannings if p.get("name") != name]
    has_standard = any(p.get("start") is None and p.get("end") is None for p in remaining)
    if not has_standard and match.get("start") is None and match.get("end") is None:
        return jsonify({
            "error": "Cannot delete the only standard planning (no start, no end)."}), 409
    save_plannings(remaining)
    return jsonify({"ok": True})


@app.route("/api/plannings/<name>/rename", methods=["POST"])
def api_planning_rename(name):
    body    = request.get_json() or {}
    newname = body.get("newname", "").strip()
    if not newname:
        return jsonify({"error": "newname is required"}), 400
    plannings = load_plannings()
    match = next((p for p in plannings if p.get("name") == name), None)
    if not match:
        return jsonify({"error": "Not found"}), 404
    if any(p.get("name") == newname for p in plannings):
        return jsonify({"error": f"'{newname}' already exists"}), 409
    match["name"] = newname
    save_plannings(plannings)
    return jsonify({"ok": True})


@app.route("/api/plannings/<name>/copy", methods=["POST"])
def api_planning_copy(name):
    body    = request.get_json() or {}
    newname = body.get("newname", "").strip()
    if not newname:
        return jsonify({"error": "newname is required"}), 400
    plannings = load_plannings()
    match = next((p for p in plannings if p.get("name") == name), None)
    if not match:
        return jsonify({"error": "Source not found"}), 404
    if any(p.get("name") == newname for p in plannings):
        return jsonify({"error": f"'{newname}' already exists"}), 409
    new_p = copy.deepcopy(match)
    new_p["name"] = newname
    plannings.append(new_p)
    save_plannings(plannings)
    return jsonify({"ok": True})


@app.route("/api/plannings/validate", methods=["POST"])
def api_planning_validate():
    data        = request.get_json() or {}
    plannings   = load_plannings()
    weekconfigs = load_weekconfigs()
    exclude     = data.pop("_exclude_name", None)
    errors      = validate_planning(data, weekconfigs, plannings, exclude_name=exclude)
    return jsonify({"valid": len(errors) == 0, "errors": errors})


# ---------------------------------------------------------------------------
# API — SETTINGS
# ---------------------------------------------------------------------------

@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    return jsonify(load_settings())


@app.route("/api/settings", methods=["POST"])
def api_settings_save():
    data = request.get_json()
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid data"}), 400

    resolution = data.get("resolution")
    if resolution is not None:
        try:
            resolution = int(resolution)
            if resolution not in VALID_RESOLUTIONS:
                raise ValueError
        except (TypeError, ValueError):
            return jsonify({"error": f"resolution must be one of {list(VALID_RESOLUTIONS)}"}), 422
        data["resolution"] = resolution

        current = load_settings()
        if int(current.get("resolution", 10)) != resolution:
            wc      = load_weekconfigs()
            adapted = adapt_weekconfigs_to_resolution(wc, resolution)
            save_weekconfigs(adapted)

    for field in ("default_heating_temp", "default_dhw_temp"):
        v = data.get(field)
        if v is not None:
            try:
                data[field] = int(v)
            except (TypeError, ValueError):
                return jsonify({"error": f"{field} must be an integer"}), 422

    save_settings(data)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# API — CREDENTIALS
# ---------------------------------------------------------------------------

@app.route("/api/credentials", methods=["GET"])
def api_credentials_get():
    if not os.path.exists(CREDS_FILE):
        return jsonify({"exists": False, "username": "", "password": "", "client_id": ""})
    try:
        with open(CREDS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return jsonify({"exists": True, "username": data.get("username", ""),
                        "password": data.get("password", ""), "client_id": data.get("client_id", "")})
    except Exception as e:
        return jsonify({"exists": False, "error": str(e), "username": "", "password": "", "client_id": ""})


@app.route("/api/credentials", methods=["POST"])
def api_credentials_save():
    data = request.get_json()
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid data"}), 400
    username  = str(data.get("username",  "")).strip()
    password  = str(data.get("password",  "")).strip()
    client_id = str(data.get("client_id", "")).strip()
    if not username or not password or not client_id:
        return jsonify({"error": "All fields are required"}), 422
    try:
        with open(CREDS_FILE, "w", encoding="utf-8") as f:
            json.dump({"username": username, "password": password, "client_id": client_id},
                      f, indent=2, ensure_ascii=False)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# API — LOGS
# ---------------------------------------------------------------------------

@app.route("/api/logs")
def api_logs():
    try:
        lines = int(request.args.get("lines", 200))
        lines = max(1, min(lines, 2000))
        result = []
        for path in (LOG_FILE_PREV, LOG_FILE):
            if os.path.exists(path):
                with open(path, encoding="utf-8", errors="replace") as f:
                    result.extend(f.readlines())
        tail = result[-lines:] if len(result) > lines else result
        return jsonify({
            "lines":   [l.rstrip("\n") for l in tail],
            "total":   len(result),
            "has_log": os.path.exists(LOG_FILE),
        })
    except Exception as e:
        return jsonify({"error": str(e), "lines": [], "total": 0, "has_log": False})


@app.route("/api/logs/clear", methods=["POST"])
def api_logs_clear():
    try:
        for path in (LOG_FILE, LOG_FILE_PREV):
            if os.path.exists(path):
                os.remove(path)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# API — LOOP STATUS & TRIGGER
# ---------------------------------------------------------------------------

@app.route("/api/loop-status")
def api_loop_status():
    if not os.path.exists(LOOP_STATUS_FILE):
        return jsonify({"running": False})
    try:
        with open(LOOP_STATUS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        pid = data.get("pid")
        if pid:
            if pid == 1 and not VIESSMANN_CONTEXT.startswith("ha-docker"):
                return jsonify({"running": False})
            try:
                os.kill(pid, 0)
            except (ProcessLookupError, PermissionError):
                return jsonify({"running": False})
        return jsonify({**data, "running": True})
    except Exception as e:
        return jsonify({"running": False, "error": str(e)})


def _loop_is_alive() -> bool:
    if not os.path.exists(LOOP_STATUS_FILE):
        return False
    try:
        with open(LOOP_STATUS_FILE, encoding="utf-8") as f:
            pid = json.load(f).get("pid")
        if pid:
            if pid == 1 and not VIESSMANN_CONTEXT.startswith("ha-docker"):
                return False
            os.kill(pid, 0)
            return True
    except Exception:
        pass
    return False


def _run_scheduler_subprocess():
    env = os.environ.copy()
    env["VIESSMANN_SCHEDULES_DIR"] = DATA_DIR
    env["VIESSMANN_CREDS_FILE"]    = CREDS_FILE
    env["VIESSMANN_TOKEN_FILE"]    = TOKEN_FILE
    try:
        _rotate_log()
        proc = subprocess.Popen(
            [sys.executable, PLANNING_SCRIPT],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        with open(LOG_FILE, "a", encoding="utf-8") as lf:
            for line in proc.stdout:
                print(line, end="", flush=True)
                lf.write(line)
                lf.flush()
        proc.wait(timeout=300)
    except Exception as e:
        print(f"[run-now] subprocess error: {e}", flush=True)


def _rotate_log():
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 512_000:
            if os.path.exists(LOG_FILE_PREV):
                os.remove(LOG_FILE_PREV)
            os.rename(LOG_FILE, LOG_FILE_PREV)
    except Exception:
        pass


@app.route("/api/run-now", methods=["POST"])
def api_run_now():
    try:
        if _loop_is_alive():
            _ensure_data_dir()
            with open(LOOP_TRIGGER_FILE, "w") as f:
                f.write(str(datetime.datetime.now()))
            return jsonify({"ok": True, "mode": "trigger"})
        else:
            if not os.path.exists(PLANNING_SCRIPT):
                return jsonify({"error": f"Planning script not found: {PLANNING_SCRIPT}"}), 500
            t = threading.Thread(target=_run_scheduler_subprocess, daemon=True)
            t.start()
            return jsonify({"ok": True, "mode": "direct"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# API — VIESSMANN STATUS
# ---------------------------------------------------------------------------

@app.route("/api/viessmann/status")
def api_viessmann_status():
    try:
        result = subprocess.run(
            [sys.executable, PLANNING_SCRIPT, "--viessmann-status"],
            capture_output=True, text=True, timeout=40,
        )
        if result.returncode != 0:
            err = result.stderr.strip() or result.stdout.strip() or "Script failed"
            return jsonify({"error": err}), 500
        return jsonify(json.loads(result.stdout))
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Viessmann read timed out"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/simulate")
def api_simulate():
    try:
        date_str = request.args.get("date")
        cmd = [sys.executable, PLANNING_SCRIPT, "--simulate"]
        if date_str:
            cmd += ["-d", date_str]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            err = result.stderr.strip() or result.stdout.strip() or "Script failed"
            return jsonify({"error": err}), 500
        return jsonify(json.loads(result.stdout))
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Simulation timed out"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# API — CONTEXT & SERVICE
# ---------------------------------------------------------------------------

import re as _re

def _strip_ansi(text):
    return _re.sub(r'\x1b\[[0-9;]*m', '', text)


@app.route("/api/context")
def api_context():
    return jsonify({"context": VIESSMANN_CONTEXT})


@app.route("/api/service/status")
def api_service_status():
    if not VIESSMANN_CONTEXT.startswith("mac-"):
        return jsonify({"error": "not macOS"}), 400
    result = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/com.viessmann-planning"],
        capture_output=True, text=True
    )
    active = result.returncode == 0
    plist  = os.path.expanduser("~/Library/LaunchAgents/com.viessmann-planning.plist")
    return jsonify({"active": active, "plist_exists": os.path.isfile(plist)})


@app.route("/api/service/install", methods=["POST"])
def api_service_install():
    if not VIESSMANN_CONTEXT.startswith("mac-"):
        return jsonify({"error": "not macOS"}), 400
    script = os.path.join(_PROJECT_DIR, "scripts", "launchd_install.sh")
    result = subprocess.run(["bash", script], input="o\n", capture_output=True, text=True)
    return jsonify({"ok": result.returncode == 0,
                    "output": _strip_ansi(result.stdout + result.stderr)})


@app.route("/api/service/uninstall", methods=["POST"])
def api_service_uninstall():
    if not VIESSMANN_CONTEXT.startswith("mac-"):
        return jsonify({"error": "not macOS"}), 400
    script = os.path.join(_PROJECT_DIR, "scripts", "launchd_uninstall.sh")
    result = subprocess.run(["bash", script], input="o\n", capture_output=True, text=True)
    return jsonify({"ok": result.returncode == 0,
                    "output": _strip_ansi(result.stdout + result.stderr)})


# ---------------------------------------------------------------------------
# API — ADD-ON (HA Supervisor)
# ---------------------------------------------------------------------------

_HA_BASE  = "http://supervisor/core/api"
_SUP_BASE = "http://supervisor"


def _ha_headers():
    return {"Authorization": f"Bearer {os.environ.get('SUPERVISOR_TOKEN', '')}",
            "Content-Type": "application/json"}


@app.route("/api/addon")
def api_addon_info():
    token = os.environ.get("SUPERVISOR_TOKEN")
    out   = {"ha_available": bool(token)}
    if not token:
        return jsonify(out)
    try:
        import requests as _req
        hdrs = _ha_headers()
        r = _req.get(f"{_SUP_BASE}/addons/self/info", headers=hdrs, timeout=5)
        if r.ok:
            d = r.json().get("data", {})
            out["version"]          = d.get("version")
            out["version_latest"]   = d.get("version_latest")
            out["update_available"] = d.get("update_available", False)
    except Exception as e:
        out["error"] = str(e)
    return jsonify(out)


@app.route("/api/addon/check-update", methods=["POST"])
def api_addon_check_update():
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return jsonify({"error": "Not running inside HA"}), 400
    try:
        import requests as _req
        hdrs = _ha_headers()
        _req.post(f"{_SUP_BASE}/store/reload", headers=hdrs, timeout=15)
        r = _req.get(f"{_SUP_BASE}/addons/self/info", headers=hdrs, timeout=5)
        if not r.ok:
            return jsonify({"error": r.text}), 500
        d = r.json().get("data", {})
        return jsonify({"version": d.get("version"), "version_latest": d.get("version_latest"),
                        "update_available": d.get("update_available", False)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/addon/update", methods=["POST"])
def api_addon_update():
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return jsonify({"error": "Not running inside HA"}), 400
    try:
        import requests as _req
        hdrs   = _ha_headers()
        r_info = _req.get(f"{_SUP_BASE}/addons/self/info", headers=hdrs, timeout=5)
        slug   = r_info.json().get("data", {}).get("slug") if r_info.ok else None
        url    = f"{_SUP_BASE}/addons/{slug}/update" if slug else f"{_SUP_BASE}/addons/self/update"
        r = _req.post(url, headers=hdrs, json={"backup": False}, timeout=60)
        if r.ok:
            return jsonify({"ok": True})
        return jsonify({"error": r.text}), 500
    except _req.exceptions.ConnectionError:
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/addon/verbosity", methods=["GET"])
def api_addon_verbosity_get():
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return jsonify({"verbosity": load_settings().get("verbosity", 0)})
    try:
        import requests as _req
        r = _req.get(f"{_SUP_BASE}/addons/self/info", headers=_ha_headers(), timeout=5)
        v = r.json().get("data", {}).get("options", {}).get("verbosity", 0) if r.ok else 0
        return jsonify({"verbosity": int(v)})
    except Exception as e:
        return jsonify({"verbosity": 0, "error": str(e)})


@app.route("/api/addon/verbosity", methods=["POST"])
def api_addon_verbosity_set():
    body = request.get_json() or {}
    try:
        level = int(body.get("verbosity", 0))
        if not 0 <= level <= 4:
            raise ValueError
    except (ValueError, TypeError):
        return jsonify({"error": "verbosity must be 0–4"}), 422

    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        s = load_settings()
        s["verbosity"] = level
        save_settings(s)
        return jsonify({"ok": True})
    try:
        import requests as _req
        r    = _req.get(f"{_SUP_BASE}/addons/self/info", headers=_ha_headers(), timeout=5)
        opts = r.json().get("data", {}).get("options", {}) if r.ok else {}
        opts["verbosity"] = level
        r2   = _req.post(f"{_SUP_BASE}/addons/self/options",
                         headers=_ha_headers(), json={"options": opts}, timeout=10)
        if r2.ok:
            return jsonify({"ok": True})
        return jsonify({"error": r2.text}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# HA MONITOR THREAD
# ---------------------------------------------------------------------------

def _ha_monitor():
    import requests as _req
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return

    hdrs = _ha_headers()
    for eid, fname, icon in [
        ("input_boolean.viessmann_planning_run_now",   "Viessmann Planning — run now",   "mdi:play-circle"),
        ("input_boolean.viessmann_planning_do_update", "Viessmann Planning — do update", "mdi:update"),
    ]:
        try:
            _req.post(f"{_HA_BASE}/states/{eid}", headers=hdrs, timeout=5,
                      json={"state": "off", "attributes": {"friendly_name": fname, "icon": icon}})
        except Exception:
            pass

    while True:
        try:
            r = _req.get(f"{_HA_BASE}/states/input_boolean.viessmann_planning_run_now",
                         headers=hdrs, timeout=5)
            if r.ok and r.json().get("state") == "on":
                subprocess.Popen([sys.executable, PLANNING_SCRIPT],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                _req.post(f"{_HA_BASE}/services/input_boolean/turn_off", headers=hdrs,
                          timeout=5, json={"entity_id": "input_boolean.viessmann_planning_run_now"})
        except Exception:
            pass
        time.sleep(30)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="viessmann-planning configurator")
    parser.add_argument("--port",       type=int, default=8098)
    parser.add_argument("--host",       default="0.0.0.0")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    url = f"http://localhost:{args.port}"
    print(f"\n  viessmann-planning configurator  v1.0")
    print(f"  ──────────────────────────────────────")
    print(f"  URL      : {url}")
    print(f"  Data dir : {DATA_DIR}")
    print(f"  Creds    : {CREDS_FILE}")
    print(f"\n  Press Ctrl+C to stop.\n")

    threading.Thread(target=_ha_monitor, daemon=True).start()

    if not args.no_browser and platform.system() == "Darwin":
        def open_browser():
            time.sleep(1)
            webbrowser.open(url)
        threading.Thread(target=open_browser, daemon=True).start()

    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
