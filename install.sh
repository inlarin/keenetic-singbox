#!/bin/sh
# install.sh — interactive installer for keenetic-singbox.
# Run on the router itself over an SSH session:
#
#   ssh -p 222 root@<router-ip>
#   curl -fsSL https://raw.githubusercontent.com/inlarin/keenetic-singbox/main/install.sh | sh
#
# The installer prompts for everything it needs (subscription URL,
# router LAN IP confirmation, optional re-use of a stored secret) and
# does the full deployment end-to-end. Idempotent — safe to re-run.

set -eu

REPO_URL="https://raw.githubusercontent.com/inlarin/keenetic-singbox/main"
SING_DIR=/opt/etc/sing-box
SHARE_DIR=/opt/share/sing-box
SECRET_FILE="$SING_DIR/.healthcheck-secret"
URL_FILE="$SING_DIR/.subscription-url"
NDM_TMP=/tmp/ndm_setup.cmd

# ── output helpers ──────────────────────────────────────────────────────────
if [ -t 1 ]; then
    BOLD=$(printf '\033[1m'); DIM=$(printf '\033[2m')
    OK=$(printf '\033[32m'); WARN=$(printf '\033[33m')
    FAIL=$(printf '\033[31m'); RESET=$(printf '\033[0m')
else
    BOLD=''; DIM=''; OK=''; WARN=''; FAIL=''; RESET=''
fi

step() { printf '\n%s[%s/%s]%s %s\n' "$BOLD" "$1" "$TOTAL_STEPS" "$RESET" "$2"; }
info() { printf '  %s\n' "$1"; }
ok()   { printf '  %s✓%s %s\n' "$OK" "$RESET" "$1"; }
warn() { printf '  %s!%s %s\n' "$WARN" "$RESET" "$1"; }
err()  { printf '%sERROR:%s %s\n' "$FAIL" "$RESET" "$1" >&2; exit 1; }

# Read with explicit prompt that goes to /dev/tty so curl|sh works:
ask() {
    prompt=$1; default=${2:-}
    if [ -n "$default" ]; then
        printf '  %s [%s]: ' "$prompt" "$default" >/dev/tty
    else
        printf '  %s: ' "$prompt" >/dev/tty
    fi
    read -r reply </dev/tty || reply=""
    [ -n "$reply" ] && printf '%s' "$reply" || printf '%s' "$default"
}

confirm() {
    while :; do
        printf '  %s [y/N]: ' "$1" >/dev/tty
        read -r reply </dev/tty || reply="n"
        case "$reply" in
            y|Y|yes|YES) return 0 ;;
            n|N|no|NO|"") return 1 ;;
        esac
    done
}

TOTAL_STEPS=8
N=0; nstep() { N=$((N + 1)); step "$N" "$1"; }

# ── banner ──────────────────────────────────────────────────────────────────
cat <<EOF
${BOLD}╔══════════════════════════════════════════════════════════════════╗
║              keenetic-singbox interactive installer              ║
╚══════════════════════════════════════════════════════════════════╝${RESET}
${DIM}Run this on the router over SSH. It will prompt for everything it
needs and deploy the full sing-box stack (sing-box-go + healthcheck
daemon + watchdog + daily subscription refresh).${RESET}
EOF

# ── 1. preflight ────────────────────────────────────────────────────────────
nstep "preflight"

[ -d /opt/bin ] || err "Entware not detected at /opt. Install Entware first \
(NDM web UI → Components → 'OPKG component', or run \
diag/kn_install_entware_step1.py from a workstation)."
ok "Entware present"

if ! command -v curl >/dev/null 2>&1; then
    info "installing curl (busybox wget can't do chunked HTTPS reliably)"
    opkg update >/dev/null
    opkg install curl >/dev/null
fi
ok "curl available"

# ── 2. detect / confirm router IP ───────────────────────────────────────────
nstep "router LAN IP"

DETECTED_IP=$(ip -4 addr show br0 2>/dev/null | awk '/inet / {print $2}' | cut -d/ -f1 | head -1)
if [ -n "$DETECTED_IP" ]; then
    info "auto-detected from br0: ${BOLD}${DETECTED_IP}${RESET}"
    ROUTER_IP=$(ask "router LAN IP" "$DETECTED_IP")
else
    warn "could not auto-detect from br0 (non-standard LAN bridge?)"
    ROUTER_IP=$(ask "router LAN IP" "192.168.1.1")
fi
[ -n "$ROUTER_IP" ] || err "router IP is required"
ok "using $ROUTER_IP"

# ── 3. subscription URL ─────────────────────────────────────────────────────
nstep "subscription URL"

EXISTING_URL=""
if [ -f "$URL_FILE" ]; then
    EXISTING_URL=$(cat "$URL_FILE")
fi

if [ -n "$EXISTING_URL" ]; then
    SHORT="${EXISTING_URL%/*}/...${EXISTING_URL##*/}"
    SHORT="${SHORT:0:60}..."
    info "existing URL on file: ${DIM}${SHORT}${RESET}"
    if confirm "reuse existing subscription URL"; then
        SUBSCRIPTION_URL="$EXISTING_URL"
    else
        SUBSCRIPTION_URL=$(ask "new subscription URL")
    fi
else
    info "v2ray-style URL that returns base64-of-newline-separated"
    info "vless:// / vmess:// / trojan:// / ss:// URIs"
    SUBSCRIPTION_URL=$(ask "subscription URL")
fi
[ -n "$SUBSCRIPTION_URL" ] || err "subscription URL is required"
ok "subscription URL captured"

# ── 4. install Entware deps ─────────────────────────────────────────────────
nstep "install Entware packages"

info "running opkg update + install (this can take 1–3 min)"
opkg update >/dev/null
opkg install sing-box-go python3 python3-urllib python3-codecs cron curl
/opt/etc/init.d/S10cron start 2>/dev/null || true
ok "sing-box-go, python3, cron installed"

# ── 5. healthcheck secret ───────────────────────────────────────────────────
nstep "Clash API secret"

mkdir -p "$SING_DIR"
if [ -f "$SECRET_FILE" ]; then
    info "secret already exists at $SECRET_FILE"
    if confirm "regenerate (existing MetaCubeXD bookmarks will break)"; then
        SECRET=$(/opt/bin/python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
        ok "new secret generated"
    else
        SECRET=$(cat "$SECRET_FILE")
        ok "reusing existing secret"
    fi
else
    SECRET=$(/opt/bin/python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
    ok "secret generated"
fi
printf '%s' "$SECRET" > "$SECRET_FILE"; chmod 600 "$SECRET_FILE"
printf '%s' "$SUBSCRIPTION_URL" > "$URL_FILE"; chmod 600 "$URL_FILE"

# ── 6. fetch scripts from public repo ───────────────────────────────────────
nstep "download stack from public repo"

mkdir -p /opt/var/lib/sing-box "$SHARE_DIR/ui" \
         /opt/etc/cron.1min /opt/etc/cron.daily

fetch() {
    src=$1; dst=$2; mode=${3:-0644}
    info "fetch $(basename "$dst")"
    curl -fsSL "$src" -o "$dst.tmp" || err "download failed: $src"
    chmod "$mode" "$dst.tmp"
    mv "$dst.tmp" "$dst"
}

fetch "$REPO_URL/S99singbox-healthcheck"        /opt/etc/init.d/S99singbox-healthcheck          0755
fetch "$REPO_URL/singbox-healthcheck-watchdog"  /opt/etc/cron.1min/singbox-healthcheck-watchdog 0755
fetch "$REPO_URL/sub-refresh.sh"                /opt/etc/cron.daily/sub-refresh                 0755
fetch "$REPO_URL/sub_to_singbox.py"             "$SHARE_DIR/sub_to_singbox.py"                  0644
ok "4 scripts deployed"

# ── 7. generate config + apply NDM setup ────────────────────────────────────
nstep "configure sing-box + register NDM interface"

info "generating sing-box config from subscription"
export SINGBOX_HEALTHCHECK_SECRET="$SECRET" ROUTER_HOST="$ROUTER_IP"
/opt/bin/python3 "$SHARE_DIR/sub_to_singbox.py" "$SUBSCRIPTION_URL" \
    --out "$SING_DIR/config.json" \
    --ndm-setup "$NDM_TMP" \
    --router-ip "$ROUTER_IP"

info "validating config"
/opt/bin/sing-box check -C "$SING_DIR/" || err "sing-box rejected the generated config"
ok "config valid"

info "applying NDM OpkgTun0 registration"
/opt/etc/init.d/S99sing-box stop 2>/dev/null || true
sleep 2
while IFS= read -r line; do
    case "$line" in \!*|"") continue ;; esac
    ndmc -c "$line" >/dev/null
done < "$NDM_TMP"
ndmc -c 'system configuration save' >/dev/null
ok "NDM updated"

# ── 8. start + smoke test ───────────────────────────────────────────────────
nstep "start services + smoke test"

/opt/etc/init.d/S99sing-box start
sleep 6
/opt/etc/init.d/S99singbox-healthcheck start

if pgrep sing-box >/dev/null; then ok "sing-box running"
else err "sing-box did not start — check /tmp/sing-box.log"; fi

if ip a show opkgtun0 2>/dev/null | grep -q 'inet '; then
    ok "opkgtun0 has IP"
else
    warn "opkgtun0 missing IP (NDM may need a moment — re-check in 30 s)"
fi

if netstat -tln 2>/dev/null | grep -q ':9090 '; then ok "Clash API listening on :9090"
else warn "Clash API not yet listening (sing-box still starting?)"; fi

# ── done ────────────────────────────────────────────────────────────────────
cat <<EOF

${BOLD}${OK}═══════════════════════════════════════════════════════════════════
  ✓ install complete
═══════════════════════════════════════════════════════════════════${RESET}

  ${BOLD}MetaCubeXD UI:${RESET}        http://${ROUTER_IP}:9090/ui/
  ${BOLD}Clash API secret:${RESET}     stored in ${SECRET_FILE} (chmod 600)
                          (paste into MetaCubeXD when it asks)
  ${BOLD}Healthcheck status:${RESET}   /opt/etc/init.d/S99singbox-healthcheck status
  ${BOLD}Daily refresh:${RESET}        /opt/etc/cron.daily/sub-refresh (auto via cron)
  ${BOLD}Re-run installer:${RESET}     curl -fsSL ${REPO_URL}/install.sh | sh

${DIM}Pin services to the tunnel via NDM CLI or kn_gui (see INSTALL.md §6).${RESET}
EOF
