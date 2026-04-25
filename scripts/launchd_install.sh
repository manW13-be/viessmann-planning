#!/usr/bin/env bash
# =============================================================================
# scripts/launchd_install.sh — Installe et active le LaunchAgent pour viessmann-planning
# =============================================================================

set -euo pipefail

# --- Dry-run flag ------------------------------------------------------------
DRY_RUN=0
for arg in "$@"; do
    [[ "$arg" == "--dry-run" ]] && DRY_RUN=1
done

# --- Couleurs ----------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

LABEL="com.viessmann-planning"
PLIST_NAME="${LABEL}.plist"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
PLIST_PATH="${LAUNCH_AGENTS_DIR}/${PLIST_NAME}"

# --- Détection du répertoire projet ------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# --- Détection de Python -----------------------------------------------------
detect_python() {
    local candidates=(
        "/opt/homebrew/bin/python3.11"
        "/usr/local/bin/python3.11"
        "$(command -v python3.11 2>/dev/null || true)"
    )

    for candidate in "${candidates[@]}"; do
        if [[ -n "$candidate" && -x "$candidate" ]]; then
            echo "$candidate"
            return 0
        fi
    done

    return 1
}

check_python_version() {
    local python_bin="$1"
    local version
    version=$("$python_bin" --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+')
    local major minor
    major=$(echo "$version" | cut -d. -f1)
    minor=$(echo "$version" | cut -d. -f2)

    if [[ "$major" -lt 3 || ( "$major" -eq 3 && "$minor" -lt 10 ) ]]; then
        echo "${RED}✗ Python $version détecté — version 3.10 minimum requise.${RESET}"
        return 1
    fi
    echo "$version"
    return 0
}

# --- En-tête -----------------------------------------------------------------
echo ""
echo -e "${CYAN}${BOLD}╔══════════════════════════════════════════════╗${RESET}"
if [[ $DRY_RUN -eq 1 ]]; then
echo -e "${CYAN}${BOLD}║ viessmann-planning — Installation (DRY RUN) ║${RESET}"
else
echo -e "${CYAN}${BOLD}║    viessmann-planning — Installation macOS  ║${RESET}"
fi
echo -e "${CYAN}${BOLD}╚══════════════════════════════════════════════╝${RESET}"
echo ""

# --- Détection Python --------------------------------------------------------
echo -e "${BOLD}🔍 Détection de Python...${RESET}"
if ! PYTHON_BIN=$(detect_python); then
    echo -e "${RED}✗ python3.11 introuvable.${RESET}"
    echo -e "  Installez-le via Homebrew : ${YELLOW}brew install python@3.11${RESET}"
    exit 1
fi

PYTHON_VERSION=$(check_python_version "$PYTHON_BIN") || exit 1
echo -e "  ${GREEN}✓ Python $PYTHON_VERSION${RESET} → ${PYTHON_BIN}"

# --- Chemins détectés --------------------------------------------------------
RUN_SCRIPT="${PROJECT_DIR}/viessmann_planning/run.sh"
SCHEDULES_DIR="${PROJECT_DIR}/schedules"
LOG_OUT="${SCHEDULES_DIR}/viessmann-planning.log"
LOG_ERR="${SCHEDULES_DIR}/viessmann-planning-err.log"
CFG_PORT=$(jq -r '.ingress_port // 8098' "${PROJECT_DIR}/viessmann_planning/config.json" 2>/dev/null || echo "8098")

echo ""
echo -e "${BOLD}📂 Configuration détectée :${RESET}"
echo -e "  Répertoire projet  : ${CYAN}${PROJECT_DIR}${RESET}"
echo -e "  Script             : ${CYAN}${RUN_SCRIPT}${RESET}"
echo -e "  Schedules dir      : ${CYAN}${SCHEDULES_DIR}${RESET}"
echo -e "  Port configurateur : ${CYAN}${CFG_PORT}${RESET}"
echo -e "  Log stdout         : ${CYAN}${LOG_OUT}${RESET}"
echo -e "  Log stderr         : ${CYAN}${LOG_ERR}${RESET}"
echo -e "  Plist destination  : ${CYAN}${PLIST_PATH}${RESET}"
echo -e "  Mode               : ${CYAN}--loop (scheduler + configurateur web)${RESET}"

# --- Vérifications -----------------------------------------------------------
echo ""
WARNINGS=0

if [[ ! -f "$RUN_SCRIPT" ]]; then
    echo -e "${RED}✗ Script introuvable : ${RUN_SCRIPT}${RESET}"
    WARNINGS=$((WARNINGS + 1))
fi

if [[ ! -d "$SCHEDULES_DIR" ]]; then
    echo -e "${YELLOW}⚠ Dossier schedules absent, il sera créé : ${SCHEDULES_DIR}${RESET}"
fi

if [[ $WARNINGS -gt 0 ]]; then
    echo ""
    echo -e "${RED}${BOLD}Des erreurs bloquantes ont été détectées. Installation annulée.${RESET}"
    exit 1
fi

# --- Confirmation ------------------------------------------------------------
echo ""
if [[ $DRY_RUN -eq 1 ]]; then
    echo -e "${YELLOW}${BOLD}[DRY RUN] Aucune modification ne sera effectuée.${RESET}"
else
    echo -e "${YELLOW}${BOLD}Confirmer l'installation ? [o/N]${RESET} \c"
    read -r confirm
    if [[ ! "$confirm" =~ ^[oOyY]$ ]]; then
        echo -e "${YELLOW}Installation annulée.${RESET}"
        exit 0
    fi
fi

# --- Création des dossiers ---------------------------------------------------
if [[ $DRY_RUN -eq 1 ]]; then
    echo -e "  ${YELLOW}[DRY RUN] mkdir -p ${LAUNCH_AGENTS_DIR}${RESET}"
    echo -e "  ${YELLOW}[DRY RUN] mkdir -p ${SCHEDULES_DIR}${RESET}"
else
    mkdir -p "$LAUNCH_AGENTS_DIR"
    mkdir -p "$SCHEDULES_DIR"
fi

# --- Déchargement si déjà actif (avant d'écrire le nouveau plist) -----------
if launchctl print "gui/$(id -u)/${LABEL}" >/dev/null 2>&1; then
    if [[ $DRY_RUN -eq 1 ]]; then
        echo -e "  ${YELLOW}[DRY RUN] Service déjà actif — launchctl bootout gui/$(id -u) serait exécuté${RESET}"
    else
        echo -e "${BOLD}🔄 Service déjà actif, déchargement...${RESET}"
        launchctl bootout "gui/$(id -u)" "$PLIST_PATH" 2>/dev/null || \
        launchctl remove "$LABEL" 2>/dev/null || true
        sleep 1
    fi
fi

# --- Génération du plist -----------------------------------------------------
echo ""
echo -e "${BOLD}📝 Génération du plist...${RESET}"

if [[ $DRY_RUN -eq 1 ]]; then
    echo -e "  ${YELLOW}[DRY RUN] Plist qui serait écrit dans : ${PLIST_PATH}${RESET}"
    PLIST_DEST=/dev/stdout
else
    PLIST_DEST="$PLIST_PATH"
fi
cat > "$PLIST_DEST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${RUN_SCRIPT}</string>
        <string>--loop</string>
    </array>

    <key>WorkingDirectory</key>
    <string>${PROJECT_DIR}</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>LAUNCHED_BY_LAUNCHD</key>
        <string>1</string>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>

    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>${LOG_OUT}</string>
    <key>StandardErrorPath</key>
    <string>${LOG_ERR}</string>

    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
EOF

if [[ $DRY_RUN -eq 1 ]]; then
    echo -e "  ${YELLOW}[DRY RUN] Plist affiché ci-dessus (non écrit sur disque)${RESET}"
else
    echo -e "  ${GREEN}✓ Plist créé${RESET}"
fi

# --- Activation --------------------------------------------------------------
if [[ $DRY_RUN -eq 1 ]]; then
    echo -e "${YELLOW}[DRY RUN] launchctl bootstrap gui/$(id -u) ${PLIST_PATH}${RESET}"
    echo ""
    echo -e "${GREEN}${BOLD}✅ [DRY RUN] Simulation terminée — aucune modification effectuée.${RESET}"
    echo -e "   Relancez sans --dry-run pour installer réellement."
else
    echo -e "${BOLD}🚀 Activation du LaunchAgent...${RESET}"
    if launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"; then
        echo -e "  ${GREEN}✓ Service activé${RESET}"
    else
        echo -e "${RED}✗ Échec de l'activation. Vérifiez le plist : ${PLIST_PATH}${RESET}"
        exit 1
    fi

    # --- Vérification finale -------------------------------------------------
    echo ""
    if launchctl print "gui/$(id -u)/${LABEL}" >/dev/null 2>&1; then
        echo -e "${GREEN}${BOLD}✅ viessmann-planning est installé et actif.${RESET}"
        echo -e "   Scheduler + configurateur web démarrés (--loop)."
        echo -e "   UI  : ${CYAN}http://localhost:${CFG_PORT}${RESET}"
        echo -e "   Logs: ${CYAN}${LOG_OUT}${RESET}"
    else
        echo -e "${YELLOW}⚠ Le service a été chargé mais n'apparaît pas encore dans launchctl list.${RESET}"
        echo -e "  Attendez quelques secondes et vérifiez avec : ${CYAN}launchctl list | grep viessmann${RESET}"
    fi
fi

echo ""
