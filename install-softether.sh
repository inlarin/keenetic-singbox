#!/bin/sh
# install-softether.sh — interactive bootstrap of the SoftEther bridge mode.
# Run on the router itself over an SSH session:
#
#   ssh -p 222 root@<router-ip>
#   curl -fsSL https://raw.githubusercontent.com/inlarin/keenetic-singbox/main/install-softether.sh | sh
#
# Prompts for SoftEther server / port / HUB / username / password / profile,
# installs SoftEther + iptables/ipset, runs vpncmd to create the connection,
# sets up NDM Bridge2, captures HUB_GW from DHCP, writes
# /opt/etc/softether-bridge.conf, downloads the watcher + DHCP scripts from
# this public repo, and starts everything.
#
# Idempotent for the package + script-deploy steps; vpncmd account creation
# checks for existence first.

set -eu

REPO_URL="https://raw.githubusercontent.com/inlarin/keenetic-singbox/main"
CONF_FILE=/opt/etc/softether-bridge.conf
NDM_BRIDGE=br2
NDM_BRIDGE_NAME=Bridge2
DHCP_SCRIPT=/opt/etc/udhcpc.${NDM_BRIDGE}.script

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

ask_silent() {
    # Read without echoing characters. Restore stty state on any path.
    prompt=$1
    printf '  %s: ' "$prompt" >/dev/tty
    saved_stty=$(stty -g </dev/tty 2>/dev/null || true)
    stty -echo </dev/tty 2>/dev/null || true
    read -r reply </dev/tty || reply=""
    [ -n "$saved_stty" ] && stty "$saved_stty" </dev/tty 2>/dev/null || true
    printf '\n' >/dev/tty
    printf '%s' "$reply"
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

ndmc_try() {
    line=$1
    if ! out=$(ndmc -c "$line" 2>&1); then
        err "ndmc rejected: $line
  $out"
    fi
}

TOTAL_STEPS=8
N=0; nstep() { N=$((N + 1)); step "$N" "$1"; }

# ── banner ──────────────────────────────────────────────────────────────────
cat <<EOF
${BOLD}╔══════════════════════════════════════════════════════════════════╗
║       keenetic-singbox — SoftEther Bridge2 mode installer        ║
╚══════════════════════════════════════════════════════════════════╝${RESET}
${DIM}Sets up SoftEther vpnclient + NDM Bridge2 so you can route specific
FQDN groups through a SoftEther HUB. This is independent of the
sing-box stack — they can coexist on the same router.${RESET}
EOF

# ── 1. preflight ────────────────────────────────────────────────────────────
nstep "preflight"

[ -d /opt/bin ] || err "Entware not detected at /opt. Install Entware first."
ok "Entware present"

if ! command -v curl >/dev/null 2>&1; then
    info "installing curl prerequisite"
    opkg update >/dev/null
    opkg install curl >/dev/null
fi
ok "curl available"

if ! ndmc -c "show version" >/dev/null 2>&1; then
    err "ndmc not responding — Keenetic NDM CLI is required."
fi

# Required NDM components: openvpn (for kernel-side TAP support on this
# firmware) + opkg.
COMPONENTS=$(ndmc -c "show version" 2>/dev/null | awk '/^ *components: / {sub(/^ *components: /,""); print}')
case " $COMPONENTS " in
    *" openvpn "*|*",openvpn,"*|*",openvpn "*|*" openvpn,"*) ok "openvpn component enabled" ;;
    *) err "The 'openvpn' component is required for kernel-side TAP/TUN.
Enable it in NDM web UI → System settings → Component options → OpenVPN client,
then reboot the router and re-run this installer." ;;
esac

# ── 2. SoftEther connection inputs ──────────────────────────────────────────
nstep "SoftEther connection details"

info "All values are stored locally on the router only. The repo never sees them."
echo

SE_SERVER=$(ask "SoftEther server hostname or IP")
[ -n "$SE_SERVER" ] || err "server is required"

SE_PORT=$(ask "SoftEther server port" "443")

SE_HUB=$(ask "HUB name on the server")
[ -n "$SE_HUB" ] || err "HUB name is required"

SE_USER=$(ask "VPN username")
[ -n "$SE_USER" ] || err "username is required"

SE_PASS=$(ask_silent "VPN password")
[ -n "$SE_PASS" ] || err "password is required"

SE_PROFILE=$(ask "local profile / NicName (will become vpn_<profile>)" "vpn")
case "$SE_PROFILE" in
    *[!a-zA-Z0-9_]*) err "profile must contain only [A-Za-z0-9_]" ;;
esac

VPN_IFACE="vpn_${SE_PROFILE}"

echo
info "Will connect to ${BOLD}${SE_SERVER}:${SE_PORT}${RESET} HUB=${BOLD}${SE_HUB}${RESET}"
info "kernel TAP iface will be ${BOLD}${VPN_IFACE}${RESET}"
confirm "proceed" || err "aborted by user"

# ── 3. install Entware packages ─────────────────────────────────────────────
nstep "install SoftEther + iptables/ipset packages"

opkg update >/dev/null
opkg install softethervpn5-libs softethervpn5-client iptables ipset ip-full
ok "packages installed"

# Start vpnclient daemon (needed before vpncmd can talk to it).
/opt/etc/init.d/S05vpnclient start 2>/dev/null || true
sleep 3

# ── 4. SoftEther account via vpncmd ─────────────────────────────────────────
nstep "configure SoftEther account"

VPNCMD=/opt/bin/vpncmd

# NicCreate is idempotent: if iface already exists, vpncmd reports
# "The specified Virtual Network Adapter already exists" and exits non-zero.
# Detect via ip-link and skip.
if ip link show "$VPN_IFACE" >/dev/null 2>&1; then
    ok "TAP $VPN_IFACE already exists"
else
    info "creating virtual TAP '$SE_PROFILE'"
    "$VPNCMD" /CLIENT localhost /CMD NicCreate "$SE_PROFILE" >/dev/null
fi

# AccountCreate with retries: if account already exists, AccountSet to update.
if "$VPNCMD" /CLIENT localhost /CMD AccountList 2>/dev/null \
        | grep -q "VPN Connection Setting Name +| ${SE_PROFILE}\$"; then
    info "account '$SE_PROFILE' exists — updating server/HUB/user"
    "$VPNCMD" /CLIENT localhost /CMD AccountSet "$SE_PROFILE" \
        "/SERVER:${SE_SERVER}:${SE_PORT}" "/HUB:${SE_HUB}" >/dev/null
    "$VPNCMD" /CLIENT localhost /CMD AccountUsernameSet "$SE_PROFILE" \
        "/USERNAME:${SE_USER}" >/dev/null
else
    info "creating account '$SE_PROFILE'"
    "$VPNCMD" /CLIENT localhost /CMD AccountCreate "$SE_PROFILE" \
        "/SERVER:${SE_SERVER}:${SE_PORT}" "/HUB:${SE_HUB}" \
        "/USERNAME:${SE_USER}" "/NICNAME:${SE_PROFILE}" >/dev/null
fi

info "setting password"
"$VPNCMD" /CLIENT localhost /CMD AccountPasswordSet "$SE_PROFILE" \
    "/PASSWORD:${SE_PASS}" /TYPE:standard >/dev/null

info "enabling autoconnect on daemon start"
"$VPNCMD" /CLIENT localhost /CMD AccountStartupSet "$SE_PROFILE" >/dev/null

info "connecting"
"$VPNCMD" /CLIENT localhost /CMD AccountDisconnect "$SE_PROFILE" >/dev/null 2>&1 || true
"$VPNCMD" /CLIENT localhost /CMD AccountConnect "$SE_PROFILE" >/dev/null
sleep 5

if "$VPNCMD" /CLIENT localhost /CMD AccountStatusGet "$SE_PROFILE" 2>/dev/null \
        | grep -q "Connection Completed"; then
    ok "SoftEther session established"
else
    warn "session not yet 'Completed' — may take longer; continuing"
fi

# ── 5. NDM Bridge2 ──────────────────────────────────────────────────────────
nstep "NDM Bridge2 setup"

if ip link show "$NDM_BRIDGE" >/dev/null 2>&1; then
    ok "$NDM_BRIDGE_NAME already exists"
else
    info "creating $NDM_BRIDGE_NAME"
    ndmc_try "interface $NDM_BRIDGE_NAME"
    ndmc_try "interface $NDM_BRIDGE_NAME description \"SoftEther $VPN_IFACE via L2 bridge\""
    ndmc_try "interface $NDM_BRIDGE_NAME role misc"
    ndmc_try "interface $NDM_BRIDGE_NAME security-level public"
    ndmc_try "interface $NDM_BRIDGE_NAME ip mtu 1500"
    ndmc_try "interface $NDM_BRIDGE_NAME ip tcp adjust-mss pmtu"
    ndmc_try "interface $NDM_BRIDGE_NAME ip global auto"
    ndmc_try "interface $NDM_BRIDGE_NAME up"
    ndmc_try "system configuration save"
    sleep 3
    ok "$NDM_BRIDGE_NAME created"
fi

# ── 6. fetch watcher + DHCP scripts ─────────────────────────────────────────
nstep "fetch bridge scripts from public repo"

mkdir -p /opt/etc/init.d /opt/etc

fetch() {
    src=$1; dst=$2; mode=${3:-0644}
    info "fetch $(basename "$dst")"
    curl -fsSL "$src" -o "$dst.tmp" || err "download failed: $src"
    chmod "$mode" "$dst.tmp"
    mv "$dst.tmp" "$dst"
}

fetch "$REPO_URL/softether/S05vpnclient"        /opt/etc/init.d/S05vpnclient   0755
fetch "$REPO_URL/softether/udhcpc.br2.script"   /opt/etc/udhcpc.br2.script     0755
fetch "$REPO_URL/softether/udhcpc.vpn.script"   /opt/etc/udhcpc.vpn.script     0755

# ── 7. capture HUB_GW + write conf ──────────────────────────────────────────
nstep "capture HUB gateway"

# Bridge our TAP into br2 (needed before DHCP can succeed)
if [ "$(ip -o link show "$VPN_IFACE" 2>/dev/null | grep -oE 'master [a-z_0-9]+' | awk '{print $2}')" != "$NDM_BRIDGE" ]; then
    info "bridging $VPN_IFACE into $NDM_BRIDGE"
    brctl addif "$NDM_BRIDGE" "$VPN_IFACE" 2>&1 | head -1
fi

# udhcpc one-shot to get HUB_GW. Use a custom script that just writes the
# router var to a temp file so we can read it from this shell.
DHCP_PROBE=/tmp/.softether-dhcp-probe.sh
cat > "$DHCP_PROBE" <<'SH'
#!/bin/sh
case "$1" in
    bound|renew)
        echo "$router" > /tmp/.softether-hub-gw
        echo "$ip"     > /tmp/.softether-hub-ip
        ;;
esac
SH
chmod +x "$DHCP_PROBE"

info "asking HUB DHCP for a lease"
udhcpc -i "$NDM_BRIDGE" -t 5 -T 3 -n -q -s "$DHCP_PROBE" >/dev/null 2>&1 || true

if [ ! -s /tmp/.softether-hub-gw ]; then
    rm -f "$DHCP_PROBE"
    err "DHCP did not return a router/gateway. Confirm the HUB has DHCP enabled
and the SoftEther session is established (AccountStatusGet $SE_PROFILE)."
fi

HUB_GW=$(cat /tmp/.softether-hub-gw)
HUB_IP=$(cat /tmp/.softether-hub-ip)
rm -f "$DHCP_PROBE" /tmp/.softether-hub-gw /tmp/.softether-hub-ip
ok "HUB gateway: $HUB_GW (we got IP $HUB_IP)"

info "writing $CONF_FILE"
cat > "$CONF_FILE" <<EOF
# Generated by install-softether.sh on $(date '+%Y-%m-%d %H:%M:%S')
VPN_IFACE=$VPN_IFACE
HUB_GW=$HUB_GW
EOF
chmod 600 "$CONF_FILE"

# Apply the real udhcpc script for ongoing DHCP renewals
cp "$DHCP_SCRIPT" "${DHCP_SCRIPT}" 2>/dev/null  # already in place from step 6

# ── 8. start watcher + smoke test ───────────────────────────────────────────
nstep "start watcher + smoke test"

/opt/etc/init.d/S05vpnclient restart
sleep 5

if /opt/etc/init.d/S05vpnclient status 2>&1 | grep -q "watcher.*running"; then
    ok "watcher running"
else
    warn "watcher status uncertain — check '/opt/etc/init.d/S05vpnclient status'"
fi

if ip a show "$NDM_BRIDGE" 2>/dev/null | grep -q 'inet '; then
    ok "$NDM_BRIDGE has IP"
else
    warn "$NDM_BRIDGE has no IP yet — DHCP may take a moment"
fi

if iptables -t nat -nvL POSTROUTING 2>/dev/null | grep -q "SNAT.*$NDM_BRIDGE"; then
    ok "SNAT rule installed"
else
    warn "SNAT not yet visible — watcher will install it within 10 s"
fi

cat <<EOF

${BOLD}${OK}═══════════════════════════════════════════════════════════════════
  ✓ SoftEther bridge mode installed
═══════════════════════════════════════════════════════════════════${RESET}

  ${BOLD}TAP iface:${RESET}              $VPN_IFACE
  ${BOLD}NDM Bridge:${RESET}             $NDM_BRIDGE_NAME (kernel: $NDM_BRIDGE)
  ${BOLD}HUB gateway:${RESET}            $HUB_GW
  ${BOLD}Config file:${RESET}            $CONF_FILE (chmod 600)
  ${BOLD}Status:${RESET}                 /opt/etc/init.d/S05vpnclient status
  ${BOLD}Manual patch:${RESET}           /opt/etc/init.d/S05vpnclient patch-now

${DIM}Bind FQDN groups to this bridge with:
  ndmc -c "dns-proxy route object-group <name> $NDM_BRIDGE_NAME auto reject"
or via kn_gui (the Bridge2 entry will appear in the iface dropdown).${RESET}
EOF
