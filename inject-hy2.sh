#!/bin/sh
# inject-hy2.sh -- append a Hysteria2 outbound from HY2_URI into a sing-box
# config (post-processed by sub-refresh.sh on the freshly converted
# new.json before `sing-box check`). Also pins select.default = the new
# tag, so the hy2 outbound is the active one on every sing-box (re)start.
#
# Why: panel subscription on this stack ships TCP-only outbounds
# (vless+vision/grpc, vmess+http/tcp, trojan+ws, ss+tcp). UDP-over-XUDP-
# over-TCP/TLS adds head-of-line blocking which destroys Telegram voice
# and similar real-time UDP. A native UDP (QUIC) outbound like
# hysteria2 sidesteps that.
#
# Usage:   inject-hy2.sh <config.json>
# Env:     HY2_URI  hy2://user:pass@host:port?obfs=...&obfs-password=...&sni=...&insecure=1[#fragment]
# Deps:    jq (entware: opkg install jq)
#
# Note: pinSHA256 is parsed-and-ignored -- sing-box has no native
# cert-pin mode. With insecure=1 we skip TLS verification entirely (the
# URI itself requests this). Don't drop insecure=1 from the URI without
# supplying the full server cert via tls.certificate.
#
# Idempotent: if TAG already in the config's .outbounds, exits 0
# without touching anything.

set -u

CFG="${1:-/tmp/config.json}"

if [ -z "${HY2_URI:-}" ]; then
    exit 0
fi

if [ ! -f "$CFG" ]; then
    echo "[inject-hy2] FAIL: config $CFG not found" >&2
    exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
    echo "[inject-hy2] FAIL: jq not installed (opkg install jq)" >&2
    exit 1
fi

URI="$HY2_URI"
URI="${URI#hy2://}"
URI="${URI#hysteria2://}"

# Fragment (label after #)
FRAG=""
case "$URI" in
    *\#*) FRAG="${URI##*#}"; URI="${URI%%#*}" ;;
esac

# Query
QUERY=""
case "$URI" in
    *\?*) QUERY="${URI#*\?}"; URI="${URI%%\?*}" ;;
esac

# user:pass@host:port -- auth may itself contain ':'
case "$URI" in
    *@*) AUTH="${URI%@*}"; HOSTPORT="${URI##*@}" ;;
    *)   AUTH=""; HOSTPORT="$URI" ;;
esac
HOST="${HOSTPORT%:*}"
PORT="${HOSTPORT##*:}"

if [ -z "$HOST" ] || [ -z "$PORT" ] || [ -z "$AUTH" ]; then
    echo "[inject-hy2] FAIL: malformed HY2_URI (need user:pass@host:port)" >&2
    exit 1
fi

q_get() {
    echo "$QUERY" | tr '&' '\n' | awk -F= -v k="$1" '$1==k {print substr($0, length(k)+2); exit}'
}
url_decode() {
    printf '%b' "$(echo "$1" | sed 's/+/ /g; s/%/\\x/g')"
}

OBFS="$(url_decode "$(q_get obfs)")"
OBFS_PW="$(url_decode "$(q_get obfs-password)")"
SNI="$(url_decode "$(q_get sni)")"
INSECURE="$(q_get insecure)"

# Tag -- derive from fragment, else host-port
RAW_TAG="${FRAG:-${HOST}-${PORT}}"
TAG="hy2-$(echo "$RAW_TAG" | tr -c 'A-Za-z0-9-' '-' | sed 's/-\{2,\}/-/g; s/^-//; s/-$//')"

# Insecure -> bool
case "$INSECURE" in 1|true|yes|on) INS_BOOL=true ;; *) INS_BOOL=false ;; esac

# Optional Brutal CC bandwidth advertisement. When BOTH HY2_UP_MBPS and
# HY2_DOWN_MBPS are set, sing-box switches the QUIC sender from BBR-like
# to Hysteria2's Brutal CC -- a no-backoff fixed-rate sender that ignores
# packet-loss signals. Measured 2026-05-11 on a Cortex-A53: gives ~15-20%
# higher throughput at the cost of ~20pp more ppp0 overhead (retransmits).
# Server must support Brutal (default for Hysteria2 servers).
UP_MBPS="${HY2_UP_MBPS:-}"
DOWN_MBPS="${HY2_DOWN_MBPS:-}"

# Build the outbound JSON
if [ -n "$OBFS" ]; then
    OUTBOUND=$(jq -n \
        --arg tag "$TAG" --arg server "$HOST" --argjson port "$PORT" \
        --arg pw "$AUTH" --arg obfs "$OBFS" --arg obfs_pw "$OBFS_PW" \
        --arg sni "$SNI" --argjson insecure "$INS_BOOL" \
        '{type:"hysteria2", tag:$tag, server:$server, server_port:$port, password:$pw,
          obfs:{type:$obfs, password:$obfs_pw},
          tls:{enabled:true, server_name:$sni, insecure:$insecure}}')
else
    OUTBOUND=$(jq -n \
        --arg tag "$TAG" --arg server "$HOST" --argjson port "$PORT" \
        --arg pw "$AUTH" --arg sni "$SNI" --argjson insecure "$INS_BOOL" \
        '{type:"hysteria2", tag:$tag, server:$server, server_port:$port, password:$pw,
          tls:{enabled:true, server_name:$sni, insecure:$insecure}}')
fi

if [ -n "$UP_MBPS" ] && [ -n "$DOWN_MBPS" ]; then
    OUTBOUND=$(printf '%s' "$OUTBOUND" | jq \
        --argjson up "$UP_MBPS" --argjson down "$DOWN_MBPS" \
        '. + {up_mbps:$up, down_mbps:$down}')
fi

# Idempotency
if jq -e --arg t "$TAG" '.outbounds | map(.tag) | index($t)' "$CFG" >/dev/null; then
    echo "[inject-hy2] $TAG already present in $CFG, skipping"
    exit 0
fi

TMP="$CFG.injtmp"
jq --argjson outbound "$OUTBOUND" --arg tag "$TAG" '
    .outbounds += [$outbound]
    | (.outbounds[] | select(.tag == "urltest-all").outbounds) |= (. + [$tag] | unique)
    | (.outbounds[] | select(.tag == "select").outbounds)      |= (. + [$tag] | unique)
    | (.outbounds[] | select(.tag == "select").default)        |= $tag
' "$CFG" > "$TMP" && mv "$TMP" "$CFG"

echo "[inject-hy2] injected $TAG ($HOST:$PORT, sni=$SNI, default=$TAG)"
