#!/bin/sh
# install.sh — bootstrap keenetic-singbox on the router from the public repo.
#
# Run this ON the router (not on the workstation) over an SSH session:
#
#   ssh -p 222 root@<router-ip>
#   export SUBSCRIPTION_URL='https://<panel>/s/<token>'
#   curl -fsSL https://raw.githubusercontent.com/inlarin/keenetic-singbox/main/install.sh | sh
#
# Idempotent — safe to re-run for upgrades. Persists the healthcheck
# secret across reruns so MetaCubeXD bookmarks keep working.

set -eu

REPO_URL="https://raw.githubusercontent.com/inlarin/keenetic-singbox/main"
SING_DIR=/opt/etc/sing-box
SHARE_DIR=/opt/share/sing-box
SECRET_FILE="$SING_DIR/.healthcheck-secret"
URL_FILE="$SING_DIR/.subscription-url"

err()  { echo "ERROR: $*" >&2; exit 1; }
info() { echo "[install] $*"; }

# ── 0. preflight ────────────────────────────────────────────────────────────
[ -d /opt/bin ] || err "Entware not detected at /opt. Install it first \
(kn_install_entware_step1.py from workstation)."

# busybox wget segfaults on chunked HTTPS — get curl from Entware before
# any network fetches.
if ! command -v curl >/dev/null 2>&1; then
    info "installing curl prerequisite"
    opkg update >/dev/null
    opkg install curl >/dev/null
fi

# ── 1. detect router LAN IP ─────────────────────────────────────────────────
ROUTER_IP="${ROUTER_HOST:-}"
if [ -z "$ROUTER_IP" ]; then
    # br0 is the standard NDM LAN bridge
    ROUTER_IP=$(ip -4 addr show br0 2>/dev/null | awk '/inet / {print $2}' | cut -d/ -f1 | head -1)
fi
[ -n "$ROUTER_IP" ] || err "could not detect router LAN IP. Set ROUTER_HOST env."
info "router LAN IP: $ROUTER_IP"

# ── 2. subscription URL ─────────────────────────────────────────────────────
if [ -z "${SUBSCRIPTION_URL:-}" ] && [ -f "$URL_FILE" ]; then
    SUBSCRIPTION_URL=$(cat "$URL_FILE")
    info "reusing subscription URL from $URL_FILE"
fi
if [ -z "${SUBSCRIPTION_URL:-}" ]; then
    printf 'Subscription URL: '
    read -r SUBSCRIPTION_URL
fi
[ -n "$SUBSCRIPTION_URL" ] || err "SUBSCRIPTION_URL required"

# ── 3. install Entware deps ─────────────────────────────────────────────────
info "opkg update + install (sing-box-go, python3, cron, curl)"
opkg update >/dev/null
opkg install sing-box-go python3 python3-urllib python3-codecs cron curl
/opt/etc/init.d/S10cron start 2>/dev/null || true

# ── 4. healthcheck secret (persist across reruns) ───────────────────────────
mkdir -p "$SING_DIR"
if [ -z "${SINGBOX_HEALTHCHECK_SECRET:-}" ] && [ -f "$SECRET_FILE" ]; then
    SINGBOX_HEALTHCHECK_SECRET=$(cat "$SECRET_FILE")
    info "reusing Clash API secret from $SECRET_FILE"
fi
if [ -z "${SINGBOX_HEALTHCHECK_SECRET:-}" ]; then
    SINGBOX_HEALTHCHECK_SECRET=$(/opt/bin/python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
    info "generated new Clash API secret"
fi
printf '%s' "$SINGBOX_HEALTHCHECK_SECRET" > "$SECRET_FILE"; chmod 600 "$SECRET_FILE"
printf '%s' "$SUBSCRIPTION_URL"          > "$URL_FILE";    chmod 600 "$URL_FILE"

# ── 5. directory layout ─────────────────────────────────────────────────────
mkdir -p /opt/var/lib/sing-box "$SHARE_DIR/ui" \
         /opt/etc/cron.1min /opt/etc/cron.daily

# ── 6. download scripts from public repo ────────────────────────────────────
fetch() {
    src=$1; dst=$2; mode=${3:-0644}
    info "fetch $(basename "$dst")"
    curl -fsSL "$src" -o "$dst.tmp" || err "download failed: $src"
    chmod "$mode" "$dst.tmp"
    mv "$dst.tmp" "$dst"
}

fetch "$REPO_URL/S99singbox-healthcheck"        "/opt/etc/init.d/S99singbox-healthcheck"          0755
fetch "$REPO_URL/singbox-healthcheck-watchdog"  "/opt/etc/cron.1min/singbox-healthcheck-watchdog" 0755
fetch "$REPO_URL/sub-refresh.sh"                "/opt/etc/cron.daily/sub-refresh"                 0755
fetch "$REPO_URL/sub_to_singbox.py"             "$SHARE_DIR/sub_to_singbox.py"                    0644

# ── 7. generate sing-box config locally on the router ───────────────────────
info "generating sing-box config"
export SINGBOX_HEALTHCHECK_SECRET ROUTER_HOST="$ROUTER_IP"
/opt/bin/python3 "$SHARE_DIR/sub_to_singbox.py" "$SUBSCRIPTION_URL" \
    --out "$SING_DIR/config.json" \
    --ndm-setup /tmp/ndm_setup.cmd \
    --router-ip "$ROUTER_IP"

info "validating sing-box config"
/opt/bin/sing-box check -C "$SING_DIR/" || err "config validation failed"

# ── 8. apply NDM-side OpkgTun0 registration ─────────────────────────────────
info "applying NDM setup"
/opt/etc/init.d/S99sing-box stop 2>/dev/null || true
sleep 2
while IFS= read -r line; do
    case "$line" in \!*|"") continue ;; esac
    ndmc -c "$line" >/dev/null
done < /tmp/ndm_setup.cmd
ndmc -c 'system configuration save' >/dev/null

# ── 9. start services ───────────────────────────────────────────────────────
info "starting sing-box"
/opt/etc/init.d/S99sing-box start
sleep 6
info "starting healthcheck daemon"
/opt/etc/init.d/S99singbox-healthcheck start

# ── 10. smoke test ──────────────────────────────────────────────────────────
echo
info "===== smoke test ====="
pgrep -af sing-box | head -3 || err "sing-box not running"
ip a show opkgtun0 2>/dev/null | head -3 || echo "WARN: opkgtun0 missing"
netstat -tln 2>/dev/null | grep ':9090' | head -1 || echo "WARN: Clash API not listening"

cat <<EOF

[install] DONE
  MetaCubeXD:                http://$ROUTER_IP:9090/ui/
  Clash API secret:          $SECRET_FILE
  Subscription URL:          $URL_FILE
  Healthcheck status:        /opt/etc/init.d/S99singbox-healthcheck status
  Daily subscription refresh runs at /opt/etc/cron.daily/sub-refresh
EOF
