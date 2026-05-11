#!/bin/sh
# Daily refresh of the v2ray subscription → sing-box config.
#
# Architecture:
#   - Pulls fresh subscription bytes via wget
#   - Runs sub_to_singbox.py (Entware python3) to regenerate config.json
#     + ndm_setup.cmd; persistent country_cache.json + country_index_map.json
#     ensure stable opkgtunN allocation across rotations.
#   - sing-box check the new config; bail out if invalid (keeps old).
#   - Diff old vs new; if outbound set / inbound set / route rules changed
#     in non-trivial ways, restart sing-box.
#   - If new countries appeared (i.e. ndm_setup.cmd has more lines than
#     last run), re-apply ndmc registration so NDM gets the new
#     OpkgTun{N} nouns + ip route default.
#   - Restore the previous Clash select-<cc>.now picks (best-effort) so
#     manual pins survive a refresh as long as the chosen tag still
#     exists in the new config.
#
# Install: /opt/etc/cron.daily/sub-refresh -> this file (chmod +x)

set -u

# Subscription URL: read from external file (deployed by install) or env.
# Never hardcoded — keeps the token out of source control.
URL_FILE=/opt/etc/sing-box/.subscription-url
URL="${SUBSCRIPTION_URL:-}"
if [ -z "$URL" ] && [ -f "$URL_FILE" ]; then
    URL=$(cat "$URL_FILE")
fi
if [ -z "$URL" ]; then
    echo "FAIL: no subscription URL (set SUBSCRIPTION_URL env or write $URL_FILE)" >&2
    exit 3
fi

DIR=/opt/share/sing-box
CONV="$DIR/sub_to_singbox.py"
INJECT_HY2="$DIR/inject-hy2.sh"        # optional; runs only if HY2_URI set
HY2_URI_FILE=/opt/etc/sing-box/.hy2-uri
LOG=/tmp/sub-refresh.log              # tmpfs to spare NAND wear; rotates on reboot
ACTIVE=/opt/etc/sing-box/config.json
SECRET_FILE="$DIR/clash_secret"     # optional override; falls back to grep-from-config
CLASH_API="${CLASH_API:-http://127.0.0.1:9090}"

# Optional: load HY2_URI (used by inject-hy2.sh) from file. Same pattern
# as .subscription-url -- keeps the credential off env / process listing.
HY2_URI="${HY2_URI:-}"
if [ -z "$HY2_URI" ] && [ -f "$HY2_URI_FILE" ]; then
    HY2_URI=$(cat "$HY2_URI_FILE")
fi
export HY2_URI

# Optional runtime knobs from /opt/etc/sing-box/.env (HEALTHCHECK_ENABLED
# already lives here). Lets inject-hy2.sh pick up HY2_UP_MBPS /
# HY2_DOWN_MBPS for Brutal CC bandwidth advertisement.
if [ -f /opt/etc/sing-box/.env ]; then
    . /opt/etc/sing-box/.env
fi
export HY2_UP_MBPS HY2_DOWN_MBPS

mkdir -p "$DIR"

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*" >> "$LOG"; }

log "=== refresh start ==="

TMP=$(mktemp -d /tmp/subrefresh.XXXXXX)
trap 'rm -rf "$TMP"' EXIT

if ! curl -fsSL --max-time 30 -o "$TMP/sub.txt" -A "ClashforAndroid/2.5.12" "$URL"; then
    # Use curl, not busybox wget — wget on Keenetic is unreliable on HTTPS
    # for non-trivial transfers.
    log "FAIL: curl subscription"
    exit 1
fi
SIZE=$(wc -c < "$TMP/sub.txt")
log "fetched subscription, ${SIZE} bytes"

# Reuse existing state file (IP→cc cache) so we don't re-hit ip-api.com.
# country_index_map.json is no longer relevant in single-pool architecture
# (kept on disk for backward compat — converter ignores it).
cp -f "$DIR/country_cache.json"     "$TMP/country_cache.json"     2>/dev/null || true

# Run converter from $TMP so it reads/writes state next to its module path
# semantic. Easiest: copy the script + state in, run there, copy back.
cp -f "$CONV" "$TMP/sub_to_singbox.py"

cd "$TMP" || exit 1
if ! /opt/bin/python3 sub_to_singbox.py --file sub.txt --out new.json \
        --ndm-setup new_ndm_setup.cmd >> "$LOG" 2>&1; then
    log "FAIL: converter failed"
    exit 1
fi

# Optional hy2 outbound injection (idempotent; no-op without HY2_URI).
# Runs AFTER the converter so it can patch the freshly-generated config,
# and BEFORE `sing-box check` so a malformed URI fails fast.
if [ -n "$HY2_URI" ] && [ -x "$INJECT_HY2" ]; then
    if ! "$INJECT_HY2" "$TMP/new.json" >> "$LOG" 2>&1; then
        log "FAIL: inject-hy2 rejected URI"
        exit 1
    fi
fi

# Validate new config
if ! /opt/bin/sing-box check -c "$TMP/new.json" >> "$LOG" 2>&1; then
    log "FAIL: sing-box check rejected new config; keeping old"
    exit 2
fi

# Persist updated cache back to $DIR (state file might have new IPs even
# if effective config didn't change)
cp -f "$TMP/country_cache.json"     "$DIR/country_cache.json" 2>/dev/null || true

# Detect changes in interesting parts of the config — ignore the cache_file
# path / log timestamps / etc. (none of these vary per refresh anyway).
DIFF_LINES=0
if [ -f "$ACTIVE" ]; then
    DIFF_LINES=$(diff -u "$ACTIVE" "$TMP/new.json" 2>/dev/null | wc -l)
fi
log "diff lines vs active config: $DIFF_LINES"

if [ "$DIFF_LINES" -eq 0 ]; then
    log "no config changes; exit"
    exit 0
fi

# Capture current select.<cc>.now picks so we can restore them after restart
SECRET=""
if [ -f "$SECRET_FILE" ]; then
    SECRET=$(cat "$SECRET_FILE")
elif [ -f "$ACTIVE" ]; then
    SECRET=$(grep -E '"secret"' "$ACTIVE" | head -1 | sed 's/.*"secret": *"//; s/".*//')
fi

PICKS=$TMP/picks.json
> "$PICKS"
if [ -n "$SECRET" ]; then
    # Single-pool: only `select` is the user-facing override. Per-country
    # urltest groups (urltest-tr, urltest-nl, ...) auto-converge after
    # restart, no need to capture/restore them.
    for sel in select; do
        now=$(curl -fsS --max-time 5 -H "Authorization: Bearer $SECRET" \
              "${CLASH_API}/proxies/${sel}" 2>/dev/null \
              | sed 's/.*"now": *"//; s/".*//')
        [ -n "$now" ] && [ "$now" != "{" ] && echo "$sel $now" >> "$PICKS"
    done
    log "captured select picks: $(wc -l < $PICKS) groups"
fi

# Apply NDM registration changes if ndm_setup line count changed (new
# countries appeared) — diffing line-by-line is overkill for a 9-lines-per-
# country block. Re-applying is idempotent.
OLD_NDM=/opt/etc/sing-box/ndm_setup.cmd.applied
NEW_LINES=$(wc -l < "$TMP/new_ndm_setup.cmd")
OLD_LINES=$([ -f "$OLD_NDM" ] && wc -l < "$OLD_NDM" || echo 0)
if [ "$NEW_LINES" -ne "$OLD_LINES" ]; then
    log "ndm_setup lines changed ($OLD_LINES -> $NEW_LINES), re-applying"
    while IFS= read -r line; do
        case "$line" in \!*|"") continue ;; esac
        ndmc -c "$line" >> "$LOG" 2>&1
    done < "$TMP/new_ndm_setup.cmd"
    cp -f "$TMP/new_ndm_setup.cmd" "$OLD_NDM"
fi

# Install new config + restart
cp -f "$TMP/new.json" "$ACTIVE"
log "config replaced, restarting sing-box"
/opt/etc/init.d/S99sing-box restart >> "$LOG" 2>&1
sleep 8

# Restore picks (best-effort; tag may have rotated out)
if [ -n "$SECRET" ] && [ -s "$PICKS" ]; then
    while read -r sel tag; do
        curl -fsS --max-time 5 -X PUT \
             -H "Authorization: Bearer $SECRET" \
             -H 'Content-Type: application/json' \
             -d "{\"name\":\"$tag\"}" \
             "${CLASH_API}/proxies/${sel}" >/dev/null 2>&1 \
             && log "restored $sel = $tag" \
             || log "could not restore $sel = $tag (tag gone?)"
    done < "$PICKS"
fi

log "=== refresh done ==="
exit 0
