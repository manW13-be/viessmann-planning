#!/usr/bin/env bash
# =============================================================================
# scripts/list_features.sh — Liste les features/setpoints Viessmann disponibles
#
# Fonctionne sur macOS et Home Assistant (SSH).
# Utilise les mêmes credentials que viessmann-planning-run.py.
#
# Usage :
#   ./scripts/list_features.sh
# =============================================================================

set -euo pipefail

# --- Couleurs ----------------------------------------------------------------
GREEN='\033[0;32m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

ADDON_CONTAINER="addon_viessmann_planning"

# --- Détection du contexte ---------------------------------------------------
if [ -f "/.dockerenv" ]; then
    CONTEXT="docker"
elif [ "$(uname)" = "Darwin" ]; then
    CONTEXT="mac"
else
    CONTEXT="linux"
fi

# --- Chemins selon contexte --------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# --- En-tête -----------------------------------------------------------------
echo ""
echo -e "${CYAN}${BOLD}╔══════════════════════════════════════════════╗${RESET}"
echo -e "${CYAN}${BOLD}║   viessmann-planning — Device Features       ║${RESET}"
echo -e "${CYAN}${BOLD}╚══════════════════════════════════════════════╝${RESET}"
echo ""

# ---------------------------------------------------------------------------
# Snippet Python partagé
# ---------------------------------------------------------------------------
PYTHON_SCRIPT='
import os, sys, json

CREDS_FILE = os.environ.get("VIESSMANN_CREDS_FILE", "")
TOKEN_FILE  = os.environ.get("VIESSMANN_TOKEN_FILE", "")

if not CREDS_FILE or not os.path.isfile(CREDS_FILE):
    print(f"[ERROR] Credentials file not found: {CREDS_FILE}")
    print("  Create viessmann_credentials.json with username, password, client_id.")
    sys.exit(1)

try:
    from PyViCare.PyViCare import PyViCare
except ImportError:
    print("[ERROR] PyViCare not installed. Run: pip3 install PyViCare")
    sys.exit(1)

with open(CREDS_FILE) as f:
    creds = json.load(f)

username  = creds["username"]
password  = creds["password"]
client_id = creds["client_id"]

print(f"[AUTH] Connecting as {username}...")
vicare = PyViCare()
vicare.initWithCredentials(username, password, client_id, TOKEN_FILE or "/tmp/viessmann_token.save")

devices = vicare.devices
if not devices:
    print("[ERROR] No devices found on this account.")
    sys.exit(1)

device = devices[0]
print(f"[OK]   Device: {device.getModel()} (ID: {getattr(device, \"id\", \"?\")})\n")

# --- Heating circuits -------------------------------------------------------
print("Heating circuits:")
try:
    circuits = device.circuits
    for c in circuits:
        cid = getattr(c, "id", "?")
        try:
            comfort = c.getHeatingProgramDesiredTemperatureForProgram("comfort")
            print(f"  circuit[{cid}] comfort temp : {comfort} °C")
        except Exception:
            pass
        try:
            normal = c.getHeatingProgramDesiredTemperatureForProgram("normal")
            print(f"  circuit[{cid}] normal temp  : {normal} °C")
        except Exception:
            pass
        try:
            reduced = c.getHeatingProgramDesiredTemperatureForProgram("reduced")
            print(f"  circuit[{cid}] reduced temp : {reduced} °C")
        except Exception:
            pass
        try:
            active_prog = c.getActiveProgram()
            print(f"  circuit[{cid}] active prog  : {active_prog}")
        except Exception:
            pass
        try:
            mode = c.getActiveMode()
            print(f"  circuit[{cid}] active mode  : {mode}")
        except Exception:
            pass
except Exception as e:
    print(f"  (Could not enumerate circuits: {e})")

# --- DHW --------------------------------------------------------------------
print()
print("DHW (domestic hot water):")
try:
    dhw = device.dhw
    try:
        temp = dhw.getDesiredTemperature()
        print(f"  DHW target temperature: {temp} °C")
    except Exception:
        pass
    try:
        actual = dhw.getStorageTemperature()
        print(f"  DHW actual temperature: {actual} °C")
    except Exception:
        pass
    try:
        mode = dhw.getActiveMode()
        print(f"  DHW active mode       : {mode}")
    except Exception:
        pass
except Exception as e:
    print(f"  (Could not access DHW: {e})")

# --- Raw features (first 40) ------------------------------------------------
print()
print("Raw features (first 40):")
try:
    raw = device.service.fetch_all_features()
    features = raw.get("data", []) if isinstance(raw, dict) else []
    for i, f in enumerate(features[:40]):
        fid = f.get("feature", "?")
        props = list(f.get("properties", {}).keys())
        print(f"  {fid:<55}  {props}")
    if len(features) > 40:
        print(f"  ... ({len(features) - 40} more features not shown)")
except Exception as e:
    print(f"  (Could not fetch raw features: {e})")

print()
print("  Hint: set client_id and register at https://app.developer.viessmann.com")
print()
'

# ---------------------------------------------------------------------------
# Exécution selon le contexte
# ---------------------------------------------------------------------------
case "$CONTEXT" in
    mac)
        PYTHON=$(which python3.11 2>/dev/null || which python3)
        CREDS_FILE="$PROJECT_DIR/viessmann_credentials.json"
        TOKEN_FILE="$PROJECT_DIR/viessmann_token.save"
        VIESSMANN_CREDS_FILE="$CREDS_FILE" \
        VIESSMANN_TOKEN_FILE="$TOKEN_FILE" \
        $PYTHON -c "$PYTHON_SCRIPT"
        ;;

    linux)
        # Sur HA : PyViCare n'est disponible que dans le container du add-on
        CREDS_FILE="/config/viessmann-planning/viessmann_credentials.json"
        TOKEN_FILE="/config/viessmann-planning/viessmann_token.save"

        if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${ADDON_CONTAINER}$"; then
            echo -e "${CYAN}ℹ  Add-on container not running — starting it...${RESET}"
            ha addons start viessmann_planning 2>/dev/null || true
            sleep 4
        fi

        if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${ADDON_CONTAINER}$"; then
            docker exec \
                -e "VIESSMANN_CREDS_FILE=$CREDS_FILE" \
                -e "VIESSMANN_TOKEN_FILE=$TOKEN_FILE" \
                "$ADDON_CONTAINER" python3 -c "$PYTHON_SCRIPT"
        else
            echo "ERROR: Could not start the add-on container." >&2
            echo "  Start the add-on from the HA UI first, then retry." >&2
            exit 1
        fi
        ;;

    docker)
        CREDS_FILE="/config/viessmann-planning/viessmann_credentials.json"
        TOKEN_FILE="/config/viessmann-planning/viessmann_token.save"
        VIESSMANN_CREDS_FILE="$CREDS_FILE" \
        VIESSMANN_TOKEN_FILE="$TOKEN_FILE" \
        python3 -c "$PYTHON_SCRIPT"
        ;;
esac
