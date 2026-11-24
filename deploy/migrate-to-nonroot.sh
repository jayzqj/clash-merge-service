#!/usr/bin/env bash
# One-time migration: root service with data next to the code
#   -> user clash-merge, data in /var/lib/clash-merge, token-protected URL.
# Run on the VPS as root, AFTER the new code is in place (DEPLOY.md section 7):
#
#   bash /opt/clash-merge-service/deploy/migrate-to-nonroot.sh [--yes] [--no-wait]
#
# Old data files are COPIED, never deleted. env, unit and the old published
# YAML are backed up to /root/clash-merge-migrate-<time>/. The old published
# YAML in the site root is moved into that backup: the old address stops
# working at once.
#
#   --yes       do not ask for confirmation
#   --no-wait   stop the service at once even if a round is running
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR=/var/lib/clash-merge
ENV_FILE=/etc/clash-merge/env
UNIT_SRC="$APP_DIR/deploy/clash-merge.service"
UNIT_DST=/etc/systemd/system/clash-merge.service
SERVICE=clash-merge.service
SVC_USER=clash-merge
MIHOMO_DST=/usr/local/bin/mihomo
DEFAULT_PUBLIC_BASE=https://xiuyuan.9527653.xyz
WAIT_SECONDS=1800
PY="$APP_DIR/venv/bin/python"

YES=0
WAIT=1
for arg in "$@"; do
  case "$arg" in
    --yes) YES=1 ;;
    --no-wait) WAIT=0 ;;
    -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

die() { echo "ERROR: $*" >&2; exit 1; }
env_get() { { sed -n "s/^$1=//p" "$ENV_FILE" 2>/dev/null || true; } | tail -n 1; }
set_env() {
  if grep -q "^$1=" "$ENV_FILE"; then
    sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"
  else
    printf '%s=%s\n' "$1" "$2" >> "$ENV_FILE"
  fi
}

# --- preflight: nothing is changed yet ------------------------------------
[ "$(id -u)" -eq 0 ] || die "run as root"
[ -x "$PY" ] || die "$PY not found"
grep -q 'def output_root' "$APP_DIR/app/runtime.py" \
  || die "the code in $APP_DIR is still the old version; update it first (DEPLOY.md section 7)"
[ -f "$ENV_FILE" ] || die "$ENV_FILE not found"
[ -f "$UNIT_SRC" ] || die "$UNIT_SRC not found"

NEW_CONFIG="$STATE_DIR/config.json"
NEW_OUTPUT="$STATE_DIR/output/jay_filtered.yaml"
OLD_CONFIG="$(env_get CLASH_MERGE_CONFIG)"
OLD_CONFIG="${OLD_CONFIG:-$APP_DIR/config.json}"
[ "$OLD_CONFIG" != "$NEW_CONFIG" ] || die "already migrated: CLASH_MERGE_CONFIG is $NEW_CONFIG"
[ -f "$OLD_CONFIG" ] || die "old config $OLD_CONFIG not found"
[ ! -e "$NEW_CONFIG" ] || die "$NEW_CONFIG already exists; inspect it and remove it before retrying"
OLD_DIR="$(dirname "$OLD_CONFIG")"
PORT="$(env_get PORT)"
PORT="${PORT:-18080}"
HEALTH="http://127.0.0.1:$PORT/health"
PUBLIC_BASE="$(env_get CLASH_MERGE_PUBLIC_BASE)"
PUBLIC_BASE="${PUBLIC_BASE:-$DEFAULT_PUBLIC_BASE}"

read_cfg() {
  "$PY" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get(sys.argv[2]) or "")' \
    "$OLD_CONFIG" "$1"
}
OLD_OUTPUT="$(read_cfg output_path)"
OLD_WORKDIR="$(read_cfg mihomo_workdir)"
OLD_WORKDIR="${OLD_WORKDIR:-$APP_DIR/mihomo}"
OLD_BIN="$(read_cfg mihomo_bin)"
OLD_BIN="${OLD_BIN:-mihomo}"
if [[ "$OLD_BIN" != /* ]]; then
  OLD_BIN="$(command -v "$OLD_BIN" || true)"
fi
[ -n "$OLD_BIN" ] && [ -x "$OLD_BIN" ] || die "mihomo binary (mihomo_bin in $OLD_CONFIG) not found"
case "$OLD_BIN" in
  /usr/bin/*|/usr/local/bin/*) NEW_BIN="$OLD_BIN" ;;
  *) NEW_BIN="$MIHOMO_DST" ;;
esac

echo "Migration plan:"
echo "  config          $OLD_CONFIG -> $NEW_CONFIG (copy)"
echo "  state files     $OLD_DIR/{state,availability,purity_history}.json -> $STATE_DIR/ (copy)"
echo "  mihomo workdir  $OLD_WORKDIR -> $STATE_DIR/mihomo (copy)"
echo "  mihomo binary   $OLD_BIN -> $NEW_BIN"
echo "  published YAML  ${OLD_OUTPUT:-<none>} -> $NEW_OUTPUT"
echo "                  the old file goes into the backup; the old URL stops working"
echo "  service user    $SVC_USER"
echo "  public base     $PUBLIC_BASE"
if [ -n "$(env_get ADMIN_PASSWORD_FILE)" ] && [ -z "$(env_get ADMIN_PASSWORD)" ]; then
  echo "  WARNING: the admin password is read from $(env_get ADMIN_PASSWORD_FILE)."
  echo "           User $SVC_USER must be able to read it, and it must not be under /root or /home,"
  echo "           otherwise /admin answers 503. Setting ADMIN_PASSWORD in $ENV_FILE is simpler."
fi
if [ "$YES" -ne 1 ]; then
  read -r -p "Continue? [y/N] " answer
  case "$answer" in y|Y) ;; *) die "aborted, nothing changed" ;; esac
fi

# --- stop the old service ---------------------------------------------------
round_running() { curl -fsS -m 5 "$HEALTH" 2>/dev/null | grep -q '"running":true'; }
if [ "$WAIT" -eq 1 ] && round_running; then
  echo "A round is running; waiting for it to finish (up to ${WAIT_SECONDS}s)..."
  waited=0
  while round_running; do
    [ "$waited" -lt "$WAIT_SECONDS" ] \
      || die "still running after ${WAIT_SECONDS}s; nothing changed. Retry later or use --no-wait"
    sleep 10
    waited=$((waited + 10))
  done
fi
systemctl stop "$SERVICE" || true

# --- backup -------------------------------------------------------------------
BACKUP="/root/clash-merge-migrate-$(date +%Y%m%d-%H%M%S)"
install -d -m 0700 "$BACKUP"
cp -a "$ENV_FILE" "$BACKUP/env"
if [ -f "$UNIT_DST" ]; then cp -a "$UNIT_DST" "$BACKUP/clash-merge.service"; fi
printf '%s\n' "$OLD_OUTPUT" > "$BACKUP/old-output-path"
echo "Backup: $BACKUP"

# --- user, directories, data ----------------------------------------------
if ! id -u "$SVC_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$STATE_DIR" --no-create-home --shell /usr/sbin/nologin "$SVC_USER"
fi
install -d -m 0750 "$STATE_DIR" "$STATE_DIR/output" "$STATE_DIR/mihomo"
cp -p "$OLD_CONFIG" "$NEW_CONFIG"
for name in state.json availability.json purity_history.json; do
  if [ -f "$OLD_DIR/$name" ]; then cp -p "$OLD_DIR/$name" "$STATE_DIR/$name"; fi
done
if [ -d "$OLD_WORKDIR" ]; then
  cp -a "$OLD_WORKDIR/." "$STATE_DIR/mihomo/"
  # The executable must not live in a directory the service can write.
  case "$OLD_BIN" in
    "$OLD_WORKDIR"/*) rm -f "$STATE_DIR/mihomo/${OLD_BIN#"$OLD_WORKDIR"/}" ;;
  esac
fi
if [ "$NEW_BIN" != "$OLD_BIN" ]; then
  install -o root -g root -m 0755 "$OLD_BIN" "$NEW_BIN"
fi
if [ -n "$OLD_OUTPUT" ] && [ -f "$OLD_OUTPUT" ]; then
  # Serve the last good file at once; the shrink guard compares against it.
  cp -p "$OLD_OUTPUT" "$NEW_OUTPUT"
fi
"$PY" - "$NEW_CONFIG" "$NEW_BIN" "$STATE_DIR/mihomo" "$NEW_OUTPUT" <<'PYEOF'
import json, os, sys
path, binary, workdir, output = sys.argv[1:]
with open(path, encoding="utf-8") as f:
    data = json.load(f)
data.update(mihomo_bin=binary, mihomo_workdir=workdir, output_path=output)
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
os.replace(tmp, path)
PYEOF
chown -R "$SVC_USER:$SVC_USER" "$STATE_DIR"
chmod 0600 "$STATE_DIR"/*.json

# --- env + unit -----------------------------------------------------------
set_env CLASH_MERGE_CONFIG "$NEW_CONFIG"
set_env CLASH_MERGE_PUBLIC_BASE "$PUBLIC_BASE"
chown root:root "$ENV_FILE"
chmod 0600 "$ENV_FILE"
install -o root -g root -m 0644 "$UNIT_SRC" "$UNIT_DST"
systemctl daemon-reload
systemctl start "$SERVICE"

# --- verify ---------------------------------------------------------------
healthy=0
for _ in $(seq 1 30); do
  if curl -fsS -m 3 "$HEALTH" >/dev/null 2>&1; then healthy=1; break; fi
  sleep 1
done
if [ "$healthy" -ne 1 ]; then
  journalctl -u "$SERVICE" -n 40 --no-pager >&2 || true
  die "service did not come up; roll back with DEPLOY.md section 7 (backup: $BACKUP)"
fi
[ -f "$STATE_DIR/subscription.token" ] || die "service did not create $STATE_DIR/subscription.token"
SUB_PATH="/subscription/$(cat "$STATE_DIR/subscription.token")/jay_filtered.yaml"
if [ -f "$NEW_OUTPUT" ]; then
  code="$(curl -s -o /dev/null -w '%{http_code}' -m 10 "http://127.0.0.1:$PORT$SUB_PATH" || true)"
  [ "$code" = 200 ] || die "local fetch of the new subscription returned $code (backup: $BACKUP)"
fi

# --- retire the old address -----------------------------------------------
if [ -n "$OLD_OUTPUT" ] && [ -f "$OLD_OUTPUT" ]; then
  mv "$OLD_OUTPUT" "$BACKUP/old-output.yaml"
fi

echo
echo "Done. The service now runs as $SVC_USER."
echo "New subscription URL (contains the token, keep it private):"
echo "  ${PUBLIC_BASE%/}$SUB_PATH"
echo
echo "Next (DEPLOY.md section 7):"
echo "  1. nginx: add 'location /subscription/' and 'location = /jay_filtered.yaml { return 404; }'"
echo "     from deploy/nginx-xiuyuan.conf, then reload OpenResty in 1Panel."
echo "  2. Admin page: run one round, check journalctl -u $SERVICE for mihomo errors."
echo "  3. Switch every Clash Verge client to the new URL."
echo "  4. After a few good rounds, delete the old copies in $OLD_DIR and $OLD_WORKDIR,"
echo "     and $BACKUP when no longer needed."
