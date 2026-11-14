#!/usr/bin/env bash
# Update the deployed service to origin/main. Run on the VPS as root:
#
#   bash /opt/clash-merge-service/deploy/update.sh [--force] [--no-wait]
#
# What it does:
#   1. git fetch, then move the checkout to origin/main. Untracked and
#      gitignored files (config.json, state.json, availability.json,
#      venv/, mihomo/) are never touched.
#   2. pip install when requirements.txt changed.
#   3. Restart the service when Python code or requirements changed,
#      after waiting for a running round to finish, then check /health.
#      A change to app/static/ alone needs no restart (the page is read
#      from disk on every request).
#
# Tracked files that were changed on the VPS (for example by a direct
# file upload) are fine when they already equal origin/main. When they
# differ, the script stops; --force discards those local changes.
#
#   --force     discard local changes / local commits that are not on origin
#   --no-wait   restart at once even if a round is running (the round is
#               lost; the published file stays as it was)
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE=clash-merge.service
BRANCH=main
ENV_FILE=/etc/clash-merge/env
WAIT_SECONDS=1800

FORCE=0
WAIT=1
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    --no-wait) WAIT=0 ;;
    -h|--help) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

cd "$APP_DIR"
PORT="$( { sed -n 's/^PORT=//p' "$ENV_FILE" 2>/dev/null || true; } | tail -n 1)"
HEALTH="http://127.0.0.1:${PORT:-18080}/health"

old="$(git rev-parse HEAD)"
git fetch --quiet origin "$BRANCH"
new="$(git rev-parse "origin/$BRANCH")"

# Files whose content on disk will change: commits old..new plus any
# local edits to tracked files.
changed="$( { git diff --name-only "$old" "$new"; git diff --name-only "$new"; } | sort -u)"

if [ -z "$changed" ]; then
  [ "$old" = "$new" ] || git reset --quiet --hard "$new"
  echo "Already up to date at $(git rev-parse --short HEAD)."
  exit 0
fi

if [ -n "$(git status --porcelain --untracked-files=no)" ] \
    && ! git diff --quiet "$new" && [ "$FORCE" -ne 1 ]; then
  echo "Tracked files were changed on this server and differ from origin/$BRANCH:" >&2
  git status --short --untracked-files=no >&2
  echo "Push those changes first, or run again with --force to discard them." >&2
  exit 1
fi
if ! git merge-base --is-ancestor "$old" "$new" && [ "$FORCE" -ne 1 ]; then
  echo "Local HEAD $old is not an ancestor of origin/$BRANCH." >&2
  echo "Run again with --force to move to origin/$BRANCH anyway." >&2
  exit 1
fi

need_restart=0
if printf '%s\n' "$changed" | grep -qE '\.py$|^requirements\.txt$'; then
  need_restart=1
fi

round_running() {
  curl -fsS -m 5 "$HEALTH" 2>/dev/null | grep -q '"running":true'
}

if [ "$need_restart" -eq 1 ] && [ "$WAIT" -eq 1 ] && round_running; then
  echo "A round is running; waiting for it to finish (up to ${WAIT_SECONDS}s)..."
  waited=0
  while round_running; do
    if [ "$waited" -ge "$WAIT_SECONDS" ]; then
      echo "Still running after ${WAIT_SECONDS}s. Nothing was changed; try again later or use --no-wait." >&2
      exit 1
    fi
    sleep 10
    waited=$((waited + 10))
  done
fi

git reset --quiet --hard "$new"
echo "Updated $(git rev-parse --short "$old") -> $(git rev-parse --short HEAD):"
printf '%s\n' "$changed" | sed 's/^/  /'

if printf '%s\n' "$changed" | grep -qx 'requirements.txt'; then
  venv/bin/pip install --quiet -r requirements.txt
fi

if ! cmp -s deploy/clash-merge.service "/etc/systemd/system/$SERVICE"; then
  echo "NOTE: deploy/clash-merge.service differs from the installed unit. To apply it:"
  echo "  cp deploy/clash-merge.service /etc/systemd/system/$SERVICE && systemctl daemon-reload && systemctl restart $SERVICE"
fi

if [ "$need_restart" -ne 1 ]; then
  echo "No Python code changed; service not restarted."
  exit 0
fi

systemctl restart "$SERVICE"
for _ in $(seq 1 30); do
  if curl -fsS -m 3 "$HEALTH" >/dev/null 2>&1; then
    echo "Service restarted and healthy: $(curl -fsS -m 3 "$HEALTH")"
    exit 0
  fi
  sleep 1
done
echo "Service did not answer $HEALTH within 30s after the restart:" >&2
journalctl -u "$SERVICE" -n 30 --no-pager >&2
exit 1
