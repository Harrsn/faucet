#!/usr/bin/env bash
# faucet deploy — pull, rebuild the image, recreate the stack.
#
# Runs as root (docker + compose). Git operations drop to the account that
# owns the checkout so the working tree doesn't end up root-owned, which would
# break every subsequent non-root git command in it.
#
# Install root-owned to /usr/local/sbin/faucet-deploy -- NOT sudo'd from its
# path in the repo. A NOPASSWD sudoers rule pointing at a script the invoking
# account can write is a root shell with extra steps.
set -euo pipefail

REPO_DIR="${FAUCET_REPO_DIR:-/opt/faucet}"
STACK_FILE="${FAUCET_STACK_FILE:-$REPO_DIR/deploy/stack.yml}"
PROJECT="${FAUCET_PROJECT:-cascade}"
IMAGE="${FAUCET_IMAGE:-faucet:local}"
REPO_USER="${FAUCET_REPO_USER:-mcpops}"
SERVICE="${FAUCET_SERVICE:-faucet}"

log() { printf '==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "must run as root (needs docker)"
[[ -d $REPO_DIR/.git ]] || die "$REPO_DIR is not a git checkout"
[[ -f $STACK_FILE ]] || die "stack file not found: $STACK_FILE"
command -v docker >/dev/null || die "docker not found"
docker compose version >/dev/null 2>&1 || die "docker compose plugin not available"

git_as() { sudo -u "$REPO_USER" git -C "$REPO_DIR" "$@"; }

# --- 1. update the checkout ------------------------------------------------
log "fetching in $REPO_DIR"
before="$(git_as rev-parse HEAD)"
git_as fetch --prune

# ff-only: a diverged or dirty checkout stops here rather than producing a
# merge commit nobody asked for on a production box.
if ! git_as merge --ff-only '@{u}'; then
    die "cannot fast-forward $REPO_DIR -- diverged or has local commits. Resolve by hand."
fi

after="$(git_as rev-parse HEAD)"
if [[ $before == "$after" ]]; then
    log "already at $after (no new commits)"
else
    log "$before -> $after"
    git_as --no-pager log --oneline "$before..$after"
fi

# --- 2. rebuild ------------------------------------------------------------
log "building $IMAGE from $REPO_DIR"
docker build -t "$IMAGE" "$REPO_DIR"

# --- 3. recreate -----------------------------------------------------------
# --force-recreate is required: the tag is unchanged, so without it compose
# sees no reason to replace a running container and the deploy silently no-ops.
log "recreating $SERVICE in project $PROJECT"
docker compose -p "$PROJECT" -f "$STACK_FILE" up -d --force-recreate "$SERVICE"

# --- 4. report -------------------------------------------------------------
log "result"
docker ps -a --filter "name=^/${SERVICE}$" \
  --format 'name={{.Names}} image={{.Image}} state={{.State}} status={{.Status}}'

state="$(docker inspect -f '{{.State.Status}}' "$SERVICE" 2>/dev/null || echo unknown)"
[[ $state == running ]] || die "$SERVICE is '$state' after deploy -- check: docker logs $SERVICE"

log "deployed $after"
