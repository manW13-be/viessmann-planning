#!/bin/bash
# =============================================================================
# viessmann_planning/run.sh — Universal entry point
#
# CONTEXTS (auto-detected):
#   mac-launchd    macOS, started by launchd (prod)
#   mac-shell      macOS, interactive shell (dev/test)
#   ha-docker-prod inside prod Docker container (supervisor-managed)
#   ha-docker-test inside test Docker container (docker_test_start.sh)
#   ha-shell       HA Linux SSH, direct local execution (dev/test)
#
# MODES:
#   (no flag)   single scheduler run then exit
#   --loop      scheduler loop + Flask configurator (prod container only)
#   --cfg       Flask configurator only
# =============================================================================

set -euo pipefail

PROD_CONTAINER="addon_viessmann_planning"
TEST_CONTAINER="addon_test_viessmann_planning"
LAUNCHD_LABEL="com.viessmann-planning"

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ "$SELF_DIR" = "/" ]; then
    if hostname 2>/dev/null | grep -q "viessmann_planning"; then
        CONTEXT="ha-docker-prod"
    else
        CONTEXT="ha-docker-test"
    fi
elif [ "$(uname)" = "Darwin" ]; then
    if [ "${LAUNCHED_BY_LAUNCHD:-}" = "1" ]; then
        CONTEXT="mac-launchd"
    else
        CONTEXT="mac-shell"
    fi
else
    CONTEXT="ha-shell"
fi

log()  {
    local msg="[VIESSMANN] $(date '+%d/%m/%Y %H:%M:%S') — $*"
    echo "$msg"
    if [ -n "${LOG_FILE:-}" ]; then
        echo "$msg" >> "$LOG_FILE" 2>/dev/null || true
    fi
}
die()  { echo "[VIESSMANN] ERROR: $*" >&2; exit 1; }
container_running() { docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${1}$"; }

LOOP=false
RUN_CFG=false
PYTHON_ARGS=()

for arg in "$@"; do
    case "$arg" in
        --loop) LOOP=true ;;
        --cfg)  RUN_CFG=true ;;
        --run)  ;;
        *)      PYTHON_ARGS+=("$arg") ;;
    esac
done

if [ "$LOOP" = true ] && [ "$RUN_CFG" = true ]; then
    die "--loop and --cfg are mutually exclusive."
fi

if [ "$LOOP" = true ] && [[ "$CONTEXT" != ha-docker-* ]] && [ "$CONTEXT" != "mac-launchd" ]; then
    die "--loop is only valid inside a Docker container or under launchd."
fi

case "$CONTEXT" in
    mac-shell)
        if [ "$RUN_CFG" != true ] && launchctl list 2>/dev/null | grep -q "$LAUNCHD_LABEL"; then
            die "macOS launchd prod agent is running.
  Stop it first: ./scripts/launchd_uninstall.sh"
        fi
        ;;
    ha-shell)
        if container_running "$PROD_CONTAINER"; then
            die "Prod container '$PROD_CONTAINER' is running. Stop the add-on from the HA UI first."
        fi
        if container_running "$TEST_CONTAINER"; then
            die "Test container '$TEST_CONTAINER' is running. Stop it first: ./scripts/docker_test_stop.sh"
        fi
        ;;
    ha-docker-test)
        if container_running "$PROD_CONTAINER" 2>/dev/null; then
            die "Prod container '$PROD_CONTAINER' is running alongside the test container."
        fi
        ;;
    ha-docker-prod)
        if container_running "$TEST_CONTAINER" 2>/dev/null; then
            die "Test container '$TEST_CONTAINER' is running alongside the prod container."
        fi
        ;;
    mac-launchd)
        ;;
esac

case "$CONTEXT" in
    mac-shell|mac-launchd)
        SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
        PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
        SCHEDULES_DIR="$PROJECT_DIR/schedules"
        CREDS_FILE="$PROJECT_DIR/viessmann_credentials.json"
        TOKEN_FILE="$PROJECT_DIR/viessmann_token.save"
        PYTHON=$(which python3.11 2>/dev/null || which python3)
        PLANNING_SCRIPT="$SCRIPT_DIR/viessmann-planning-run.py"
        CFG_SCRIPT="$SCRIPT_DIR/viessmann-planning-cfg.py"
        VERSION=$(jq -r '.version' "$SCRIPT_DIR/config.json" 2>/dev/null || echo "unknown")
        CFG_PORT="${CFG_PORT:-$(jq -r '.ingress_port // 8098' "$SCRIPT_DIR/config.json" 2>/dev/null || echo 8098)}"
        CFG_HOST="127.0.0.1"
        VERBOSITY=0
        ;;
    ha-shell)
        SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
        PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
        SCHEDULES_DIR="/config/viessmann-planning/schedules"
        CREDS_FILE="/config/viessmann-planning/viessmann_credentials.json"
        TOKEN_FILE="/config/viessmann-planning/viessmann_token.save"
        CFG_PORT="${CFG_PORT:-8098}"
        CFG_HOST="0.0.0.0"
        VERBOSITY=0
        VERSION=$(jq -r '.version' "$SCRIPT_DIR/config.json" 2>/dev/null || echo "unknown")
        PLANNING_SCRIPT="$SCRIPT_DIR/viessmann-planning-run.py"
        CFG_SCRIPT="$SCRIPT_DIR/viessmann-planning-cfg.py"

        VENV_DIR="/config/viessmann-planning/venv"
        if [ ! -f "$VENV_DIR/bin/python3" ]; then
            log "Creating Python venv at $VENV_DIR..."
            mkdir -p "$(dirname "$VENV_DIR")"
            python3 -m venv "$VENV_DIR"
        fi
        PYTHON="$VENV_DIR/bin/python3"

        MISSING=()
        "$PYTHON" -c "import PyViCare" 2>/dev/null || MISSING+=("PyViCare")
        "$PYTHON" -c "import flask"    2>/dev/null || MISSING+=("flask")
        "$PYTHON" -c "import requests" 2>/dev/null || MISSING+=("requests")
        if [ ${#MISSING[@]} -gt 0 ]; then
            log "Installing: ${MISSING[*]}"
            "$VENV_DIR/bin/pip" install --quiet "${MISSING[@]}"
        fi
        ;;
    ha-docker-prod|ha-docker-test)
        VERBOSITY=$(jq -r '.verbosity // 0' /data/options.json 2>/dev/null || echo "0")
        SCHEDULES_DIR="/config/viessmann-planning/schedules"
        CREDS_FILE="/config/viessmann-planning/viessmann_credentials.json"
        TOKEN_FILE="/config/viessmann-planning/viessmann_token.save"
        PYTHON="python3"
        PLANNING_SCRIPT="/viessmann-planning-run.py"
        CFG_SCRIPT="/viessmann-planning-cfg.py"
        CFG_PORT=8098
        CFG_HOST="0.0.0.0"
        VERSION=$(jq -r '.version' /config.json.addon 2>/dev/null || echo "unknown")
        ;;
esac

init_schedules() {
    mkdir -p "$SCHEDULES_DIR"
    local count
    count=$(find "$SCHEDULES_DIR" -maxdepth 1 -name "*.json" 2>/dev/null | wc -l | tr -d ' ')
    log "Schedules: ${count} file(s)"
}

next_run_time() {
    local INTERVAL_SEC="${1:-600}"
    local NEXT=$(( INTERVAL_SEC - $(date +%s) % INTERVAL_SEC + 2 ))
    if [ "$(uname)" = "Darwin" ]; then
        date -v+${NEXT}S '+%Y-%m-%dT%H:%M:%S'
    else
        date -d "+${NEXT} seconds" '+%Y-%m-%dT%H:%M:%S'
    fi
}

read_resolution_sec() {
    local SETTINGS="${SCHEDULES_DIR}/settings.json"
    local MINUTES=10
    if [ -f "$SETTINGS" ]; then
        MINUTES=$(python3 -c "
import json, sys
try:
    d = json.load(open('${SETTINGS}'))
    v = int(d.get('resolution', 10))
    print(max(1, v))
except Exception:
    print(10)
" 2>/dev/null || echo 10)
    fi
    echo $(( MINUTES * 60 ))
}

get_cfg_url() {
    local HOST=""
    if [ -n "${SUPERVISOR_TOKEN:-}" ]; then
        HOST=$(curl -sf \
            -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" \
            http://supervisor/core/api/config 2>/dev/null \
            | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('internal_url',''))" \
            2>/dev/null | sed 's|https\?://||' | cut -d'/' -f1 || true)
    fi
    if [ -z "$HOST" ]; then
        if [ "$(uname)" = "Darwin" ]; then
            HOST=$(hostname -f 2>/dev/null || echo "localhost")
        else
            HOST=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")
        fi
    fi
    echo "http://${HOST}:${CFG_PORT}"
}

log "Context : $CONTEXT | v${VERSION}"
log "Schedules: $SCHEDULES_DIR"
log "Creds    : $CREDS_FILE"

LOOP_STATUS_FILE=""
LOOP_TRIGGER_FILE=""
LOG_FILE=""

set_loop_files() {
    LOOP_STATUS_FILE="${SCHEDULES_DIR}/loop_status.json"
    LOOP_TRIGGER_FILE="${SCHEDULES_DIR}/loop_trigger"
    LOG_FILE="${SCHEDULES_DIR}/viessmann-planning.log"
}

log_run_header() {
    log "━━━ START mode=${1} | verbosity=${VERBOSITY} ━━━"
}

rotate_log() {
    local MAX=512000
    if [ -f "$LOG_FILE" ] && [ "$(wc -c < "$LOG_FILE")" -gt $MAX ]; then
        mv "$LOG_FILE" "${LOG_FILE}.1"
    fi
}

tee_log() {
    rotate_log
    tee -a "$LOG_FILE"
}

write_loop_status() {
    local interval_sec="$1"
    local next_ts="$2"
    cat > "$LOOP_STATUS_FILE" << JSON
{
  "pid": $$,
  "interval_sec": ${interval_sec},
  "interval_min": $(( interval_sec / 60 )),
  "last_run": "$(date '+%Y-%m-%dT%H:%M:%S')",
  "next_run": "${next_ts}"
}
JSON
}

check_trigger() {
    if [ -f "$LOOP_TRIGGER_FILE" ]; then
        rm -f "$LOOP_TRIGGER_FILE"
        return 0
    fi
    return 1
}

if [ "$LOOP" = true ]; then
    VFLAG=""
    if [ "${VERBOSITY:-0}" -gt 0 ] 2>/dev/null; then
        VFLAG="-$(printf '%0.sv' $(seq 1 "$VERBOSITY"))"
    fi
    PYTHON_ARGS=($VFLAG)

    log "Mode: loop (scheduler + configurator)"
    init_schedules

    CFG_URL=$(get_cfg_url)
    log "Starting configurator on ${CFG_HOST}:${CFG_PORT} — $CFG_URL"
    VIESSMANN_CONTEXT="$CONTEXT" \
    VIESSMANN_SCHEDULES_DIR="$SCHEDULES_DIR" \
    VIESSMANN_CREDS_FILE="$CREDS_FILE" \
    VIESSMANN_TOKEN_FILE="$TOKEN_FILE" \
    $PYTHON "$CFG_SCRIPT" --host "$CFG_HOST" --port "$CFG_PORT" --no-browser &
    CFG_PID=$!

    set_loop_files
    trap 'rm -f "$LOOP_STATUS_FILE" "$LOOP_TRIGGER_FILE"; exit' INT TERM EXIT
    log "Starting scheduler loop..."
    while true; do
        LOOP_INTERVAL_SEC=$(read_resolution_sec)
        LOOP_INTERVAL_MIN=$(( LOOP_INTERVAL_SEC / 60 ))
        log_run_header "loop (resolution=${LOOP_INTERVAL_MIN}min)"
        VIESSMANN_SCHEDULES_DIR="$SCHEDULES_DIR" \
        VIESSMANN_CREDS_FILE="$CREDS_FILE" \
        VIESSMANN_TOKEN_FILE="$TOKEN_FILE" \
        $PYTHON "$PLANNING_SCRIPT" ${PYTHON_ARGS[@]+"${PYTHON_ARGS[@]}"} 2>&1 \
            | tee_log || true
        LOOP_INTERVAL_SEC=$(read_resolution_sec)
        # Sleep until 2s after next slot boundary
        NOW_SEC=$(date +%s)
        NEXT_BOUNDARY=$(( (NOW_SEC / LOOP_INTERVAL_SEC + 1) * LOOP_INTERVAL_SEC + 2 ))
        SLEEP_SEC=$(( NEXT_BOUNDARY - NOW_SEC ))
        NEXT_RUN_TS=$(next_run_time "$LOOP_INTERVAL_SEC")
        log "Next run at ${NEXT_RUN_TS} (in ${SLEEP_SEC}s)"
        write_loop_status "$LOOP_INTERVAL_SEC" "$NEXT_RUN_TS"
        SLEPT=0
        while [ $SLEPT -lt $SLEEP_SEC ]; do
            if check_trigger; then
                log "Manual trigger received — running scheduler immediately"
                break
            fi
            CHUNK=5
            if [ $(( SLEEP_SEC - SLEPT )) -lt $CHUNK ]; then
                CHUNK=$(( SLEEP_SEC - SLEPT ))
            fi
            sleep $CHUNK
            SLEPT=$(( SLEPT + CHUNK ))
        done
    done

elif [ "$RUN_CFG" = true ]; then
    log "Mode: cfg"
    init_schedules
    LOG_FILE="${SCHEDULES_DIR}/viessmann-planning.log"
    CFG_URL=$(get_cfg_url)
    log "Configurator on ${CFG_HOST}:${CFG_PORT} — $CFG_URL"

    CFG_PID_FILE="${SCHEDULES_DIR}/viessmann-cfg-shell.pid"

    if [[ "$CONTEXT" == mac-shell || "$CONTEXT" == ha-shell || "$CONTEXT" == ha-docker-test ]]; then
        if [ -f "$CFG_PID_FILE" ]; then
            OLD_PID=$(cat "$CFG_PID_FILE" 2>/dev/null || true)
            if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
                if ps -p "$OLD_PID" -o args= 2>/dev/null | grep -q "viessmann-planning-cfg"; then
                    log "Stopping previous cfg server (PID $OLD_PID)..."
                    kill "$OLD_PID" 2>/dev/null || true
                    sleep 1
                fi
            fi
            rm -f "$CFG_PID_FILE"
        fi
        CFG_FLAGS=(--host "$CFG_HOST" --port "$CFG_PORT")
        if [ "$CONTEXT" != "mac-shell" ]; then CFG_FLAGS+=(--no-browser); fi
        VIESSMANN_CONTEXT="$CONTEXT" \
        VIESSMANN_SCHEDULES_DIR="$SCHEDULES_DIR" \
        VIESSMANN_CREDS_FILE="$CREDS_FILE" \
        VIESSMANN_TOKEN_FILE="$TOKEN_FILE" \
        $PYTHON "$CFG_SCRIPT" "${CFG_FLAGS[@]}" &
        CFG_PID=$!
        echo "$CFG_PID" > "$CFG_PID_FILE"
        trap 'rm -f "$CFG_PID_FILE"; exit' INT TERM EXIT
        wait "$CFG_PID"
    else
        VIESSMANN_CONTEXT="$CONTEXT" \
        VIESSMANN_SCHEDULES_DIR="$SCHEDULES_DIR" \
        VIESSMANN_CREDS_FILE="$CREDS_FILE" \
        VIESSMANN_TOKEN_FILE="$TOKEN_FILE" \
        $PYTHON "$CFG_SCRIPT" --host "$CFG_HOST" --port "$CFG_PORT" --no-browser
    fi

else
    init_schedules
    LOG_FILE="${SCHEDULES_DIR}/viessmann-planning.log"
    rotate_log
    log_run_header "run"
    VIESSMANN_SCHEDULES_DIR="$SCHEDULES_DIR" \
    VIESSMANN_CREDS_FILE="$CREDS_FILE" \
    VIESSMANN_TOKEN_FILE="$TOKEN_FILE" \
    $PYTHON "$PLANNING_SCRIPT" ${PYTHON_ARGS[@]+"${PYTHON_ARGS[@]}"} 2>&1 | tee -a "$LOG_FILE"

fi
