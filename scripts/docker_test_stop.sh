#!/bin/bash
# =============================================================================
# scripts/docker_test_stop.sh — Stop and remove the test container
#
# Keeps the test image intact. Run docker_test_build.sh to rebuild.
#
# Usage:
#   ./scripts/docker_test_stop.sh
# =============================================================================

set -euo pipefail

TEST_CONTAINER="addon_test_viessmann_planning"

log() { echo "[TEST-STOP] $(date '+%d/%m/%Y %H:%M:%S') — $*"; }

[ "$(uname)" = "Darwin" ] && { echo "[TEST-STOP] ERROR: Run this on HA SSH, not macOS." >&2; exit 1; }

if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q "^${TEST_CONTAINER}$"; then
    log "Removing container '$TEST_CONTAINER'..."
    docker rm -f "$TEST_CONTAINER" >/dev/null
    log "Done. Image 'addon_test_viessmann_planning' is preserved."
    log "Rebuild: ./scripts/docker_test_build.sh"
else
    log "Container '$TEST_CONTAINER' is not running."
fi
