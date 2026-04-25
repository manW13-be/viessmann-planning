#!/usr/bin/env bash
# =============================================================================
# scripts/launchd_uninstall.sh — Désactive et supprime le LaunchAgent viessmann-planning
# =============================================================================

set -euo pipefail

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

# --- En-tête -----------------------------------------------------------------
echo ""
echo -e "${CYAN}${BOLD}╔══════════════════════════════════════════════╗${RESET}"
echo -e "${CYAN}${BOLD}║  viessmann-planning — Désinstallation macOS  ║${RESET}"
echo -e "${CYAN}${BOLD}╚══════════════════════════════════════════════╝${RESET}"
echo ""

# --- Vérification de l'état actuel -------------------------------------------
SERVICE_ACTIVE=false
PLIST_EXISTS=false

if launchctl print "gui/$(id -u)/${LABEL}" >/dev/null 2>&1; then
    SERVICE_ACTIVE=true
fi

if [[ -f "$PLIST_PATH" ]]; then
    PLIST_EXISTS=true
fi

if [[ "$SERVICE_ACTIVE" == false && "$PLIST_EXISTS" == false ]]; then
    echo -e "${YELLOW}⚠ Le service ${LABEL} n'est pas installé.${RESET}"
    echo -e "  Rien à désinstaller."
    echo ""
    exit 0
fi

# --- Résumé de ce qui va être supprimé ---------------------------------------
echo -e "${BOLD}🗑  Ce qui va être supprimé :${RESET}"

if [[ "$SERVICE_ACTIVE" == true ]]; then
    echo -e "  ${CYAN}●${RESET} Service LaunchAgent actif → sera désactivé"
fi

if [[ "$PLIST_EXISTS" == true ]]; then
    echo -e "  ${CYAN}●${RESET} Fichier plist : ${PLIST_PATH}"
fi

echo ""
echo -e "${YELLOW}Note : les fichiers du projet (scripts, schedules, logs, token) ne sont${RESET}"
echo -e "${YELLOW}pas supprimés — uniquement la configuration launchd.${RESET}"

# --- Confirmation ------------------------------------------------------------
echo ""
echo -e "${YELLOW}${BOLD}Confirmer la désinstallation ? [o/N]${RESET} \c"
read -r confirm
if [[ ! "$confirm" =~ ^[oOyY]$ ]]; then
    echo -e "${YELLOW}Désinstallation annulée.${RESET}"
    exit 0
fi

echo ""

# --- Désactivation -----------------------------------------------------------
if [[ "$SERVICE_ACTIVE" == true ]]; then
    echo -e "${BOLD}🛑 Désactivation du service...${RESET}"
    if [[ -f "$PLIST_PATH" ]]; then
        launchctl bootout "gui/$(id -u)" "$PLIST_PATH" 2>/dev/null || true
    else
        launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
    fi
    echo -e "  ${GREEN}✓ Service désactivé${RESET}"
fi

# --- Suppression du plist ----------------------------------------------------
if [[ "$PLIST_EXISTS" == true ]]; then
    echo -e "${BOLD}🗑  Suppression du plist...${RESET}"
    rm -f "$PLIST_PATH"
    echo -e "  ${GREEN}✓ Plist supprimé${RESET}"
fi

# --- Vérification finale -----------------------------------------------------
echo ""
if ! launchctl print "gui/$(id -u)/${LABEL}" >/dev/null 2>&1 && [[ ! -f "$PLIST_PATH" ]]; then
    echo -e "${GREEN}${BOLD}✅ viessmann-planning a été désinstallé proprement.${RESET}"
else
    echo -e "${YELLOW}⚠ Vérification manuelle recommandée :${RESET}"
    echo -e "  ${CYAN}launchctl list | grep viessmann${RESET}"
    echo -e "  ${CYAN}ls ~/Library/LaunchAgents/ | grep viessmann${RESET}"
fi

echo ""
