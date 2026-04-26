#!/bin/bash
# =============================================================================
# scripts/git_fetch.sh — Pull depuis GitHub (universel Mac + HA SSH)
#
# Usage :
#   ./scripts/git_fetch.sh
# =============================================================================

set -e
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

BRANCH=$(git rev-parse --abbrev-ref HEAD)
echo "[FETCH] Pulling from GitHub ($BRANCH)..."
git pull --rebase origin "$BRANCH"

# Synchro .gitignore → gitignore (visible depuis Finder/Samba)
if [ -f ".gitignore" ]; then
    cp .gitignore gitignore
    echo "[FETCH] .gitignore → gitignore"
fi

echo "[FETCH] Done — v$(jq -r '.version' viessmann_planning/config.json)"
