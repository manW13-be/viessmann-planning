#!/bin/bash
# =============================================================================
# scripts/docker_test_start.sh — Start the test Docker container
#
# Runs 'addon_test_viessmann_planning' with the same data mounts as prod.
# All arguments are forwarded to /run.sh inside the container.
#
# Modes:
#   (no flag)         single scheduler run — container exits after completion
#   --loop            scheduler loop + Flask configurator — stays alive (detached)
#   --cfg             Flask configurator only — stays alive (detached)
#   -vv / -d DATE / … passed through to the Python script
#
# --loop vs --cfg:
#   --loop  starts both the scheduler loop AND the Flask configurator
#           (mirrors prod container behaviour)
#   --cfg   starts only the Flask configurator
#
# Refuses if prod container is running.
#
# Usage:
#   ./scripts/docker_test_start.sh                        # single scheduler run
#   ./scripts/docker_test_start.sh --loop                 # scheduler loop + Flask
#   ./scripts/docker_test_start.sh --cfg                  # Flask only
#   ./scripts/docker_test_start.sh -vv                    # single run, verbosity 2
#   ./scripts/docker_test_start.sh -d 2026-04-10 -vv      # simulate date
#   ./scripts/docker_test_start.sh -p planning_winter.json     # force planning
#   ./scripts/docker_test_start.sh -c normal.json             # force weekconfig
# =============================================================================

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PROD_CONTAINER="addon_viessmann_planning"
TEST_IMAGE="addon_test_viessmann_planning"
TEST_CONTAINER="addon_test_viessmann_planning"
CFG_PORT=8098

CONFIG_DIR="$(realpath /config 2>/dev/null || echo /config)"

log() { echo "[TEST-START] $(date '+%d/%m/%Y %H:%M:%S') — $*"; }
die() { echo "[TEST-START] ERROR: $*" >&2; exit 1; }

[ "$(uname)" = "Darwin" ] && die "This script must be run on HA SSH, not on macOS."

# Refuse if prod is running
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${PROD_CONTAINER}$"; then
    die "Prod container '$PROD_CONTAINER' is running.
  Stop the add-on from the HA UI first."
fi

# Check test image exists
if ! docker images --format '{{.Repository}}' 2>/dev/null | grep -q "^${TEST_IMAGE}$"; then
    die "Test image '$TEST_IMAGE' not found.
  Build it first: ./scripts/docker_test_build.sh"
fi

# Remove stale test container if any
if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q "^${TEST_CONTAINER}$"; then
    log "Removing stale test container..."
    docker rm -f "$TEST_CONTAINER" >/dev/null
fi

# Determine run mode from args
DETACHED=false
for arg in "$@"; do
    case "$arg" in
        --loop|--cfg) DETACHED=true ;;
    esac
done

TZ=$(printenv TZ 2>/dev/null || echo "Europe/Brussels")
HOST_IP=$(ip route get 1.1.1.1 2>/dev/null | awk '/src/{print $7; exit}')
HOST_IP=${HOST_IP:-$(hostname -I 2>/dev/null | awk '{print $1}')}
HOST_IP=${HOST_IP:-localhost}

log "Starting test container '$TEST_CONTAINER' — args: ${*:-<none>}"

if [ "$DETACHED" = true ]; then
    docker run -d \
        --name "$TEST_CONTAINER" \
        --hostname "addon-test-viessmann-planning" \
        -p "${CFG_PORT}:${CFG_PORT}" \
        -v "${CONFIG_DIR}:/config" \
        -e "TZ=${TZ}" \
        "$TEST_IMAGE" \
        /run.sh "$@"

    log "Container started in background."
    log "Logs  : docker logs -f $TEST_CONTAINER"
    log "Exec  : docker exec -it $TEST_CONTAINER /run.sh --run -vv"
    log "Exec  : docker exec -it $TEST_CONTAINER /run.sh --run -d 2026-04-10 -vv"
    log "Stop  : ./scripts/docker_test_stop.sh"
    if [[ " $* " =~ " --cfg " ]] || [[ " $* " =~ " --loop " ]]; then
        log "Flask : http://${HOST_IP}:${CFG_PORT}"
    fi
else
    # Single run — attached, container exits when done
    docker run --rm \
        --name "$TEST_CONTAINER" \
        --hostname "addon-test-viessmann-planning" \
        -v "${CONFIG_DIR}:/config" \
        -e "TZ=${TZ}" \
        "$TEST_IMAGE" \
        /run.sh "$@"
fi
