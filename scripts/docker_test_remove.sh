#!/bin/bash
# =============================================================================
# scripts/docker_test_remove.sh — Remove test container and image
#
# Use for a clean slate before docker_test_build.sh.
#
# Usage:
#   ./scripts/docker_test_remove.sh
# =============================================================================

set -euo pipefail

TEST_CONTAINER="addon_test_viessmann_planning"
TEST_IMAGE="addon_test_viessmann_planning"

log() { echo "[TEST-REMOVE] $(date '+%d/%m/%Y %H:%M:%S') — $*"; }

[ "$(uname)" = "Darwin" ] && { echo "[TEST-REMOVE] ERROR: Run this on HA SSH, not macOS." >&2; exit 1; }

if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q "^${TEST_CONTAINER}$"; then
    log "Removing container '$TEST_CONTAINER'..."
    docker rm -f "$TEST_CONTAINER" >/dev/null
else
    log "Container '$TEST_CONTAINER' not present."
fi

if docker images --format '{{.Repository}}' 2>/dev/null | grep -q "^${TEST_IMAGE}$"; then
    log "Removing image '$TEST_IMAGE'..."
    docker rmi "$TEST_IMAGE" >/dev/null
    log "Image removed."
else
    log "Image '$TEST_IMAGE' not present."
fi

log "Done. Rebuild from scratch: ./scripts/docker_test_build.sh"
