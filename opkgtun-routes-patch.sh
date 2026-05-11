#!/bin/sh
# opkgtun-routes-patch.sh -- watcher that keeps `default via 172.19.0.1
# dev opkgtun0` populated in every fwmark routing table NDM assigns to an
# OpkgTun0-bound `dns-proxy route object-group`.
#
# Why this exists
# ---------------
# NDM is supposed to write that default route automatically when an
# OpkgTun0 dns-proxy route is enabled. In practice the routes occasionally
# disappear -- observed 2026-05-11 after `system configuration save`
# following edits to `object-group expresslrs/betaflight ... auto reject`.
# Every OpkgTun0 fwmark table emptied; mark'd traffic fell through to the
# unconditional `from all lookup 4098 -> default ppp0` rule at priority
# 104 and either leaked to PPPoE or got blackholed (depending on which
# blackhole rule fired). Bridge2 tables (claude, copilot_ms, python_pypi,
# x_twitter) stayed healthy because S05vpnclient already watches them.
#
# This script does the same thing S05vpnclient does for Bridge2, but for
# OpkgTun0. Group-driven: reads running-config to find OpkgTun0 routes,
# resolves each group's current fwmark via iptables-mangle, finds its
# routing table via `ip rule list`, then restores the default route if
# missing. This survives NDM mark renumbering (observed: same group can
# get a different mark/table number after a config save).
#
# Install:
#   /opt/bin/opkgtun-routes-patch.sh                 (chmod 755) -- this file
#   /opt/etc/cron.1min/opkgtun-routes-patch          -> symlink to it
#   /opt/etc/ndm/ifstatechanged.d/40-opkgtun-routes  -> symlink (NDM event)
#
# Logs:  /tmp/opkgtun-routes-patch.log (tmpfs; rotates on reboot)
#
# Action:  /opt/bin/opkgtun-routes-patch.sh [--once]  (manual one-shot)

set -u

OPKGTUN_IF=opkgtun0
OPKGTUN_GW=172.19.0.1
MANGLE_CHAIN=_NDM_DNSRT_PREROUTING_MANGLE
IPSET_PREFIX=_NDM_OGDN_4_
LOG=/tmp/opkgtun-routes-patch.log

ts()  { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*" >> "$LOG"; }

# Bail quietly if opkgtun0 isn't up (sing-box hasn't created the TUN yet --
# e.g. during early boot or while a restart is in flight). Nothing to do.
if ! ip link show "$OPKGTUN_IF" 2>/dev/null | grep -q LOWER_UP; then
    exit 0
fi

# Pull OpkgTun0-bound object-groups out of the live NDM running-config.
# Format expected: "    route object-group <name> OpkgTun0 auto [reject]"
GROUPS=$(ndmc -c "show running-config" 2>/dev/null | \
    awk '$1=="route" && $2=="object-group" && $4=="OpkgTun0" {print $3}')

if [ -z "$GROUPS" ]; then
    log "no OpkgTun0 object-groups in running-config; nothing to do"
    exit 0
fi

# Snapshot the mangle chain + ip rule output once -- cheap, avoids
# re-running ndmc/iptables for every group.
MANGLE_DUMP=$(iptables -t mangle -S "$MANGLE_CHAIN" 2>/dev/null)
RULE_DUMP=$(ip rule list 2>/dev/null)

CHANGES=0
SKIPPED=0
for grp in $GROUPS; do
    # NDM assigns one fwmark per group, written by the mangle MARK rule
    # whose match-set is _NDM_OGDN_4_@<group>. Marks can renumber between
    # `system configuration save` runs -- group name is the stable key.
    MARK=$(printf '%s\n' "$MANGLE_DUMP" | \
        grep "match-set ${IPSET_PREFIX}@${grp} .* --set-xmark" | \
        head -1 | sed -nE 's/.*--set-xmark (0x[a-f0-9]+).*/\1/p')
    if [ -z "$MARK" ]; then
        log "skip $grp: no mangle MARK rule (ipset not populated yet?)"
        SKIPPED=$((SKIPPED + 1))
        continue
    fi

    TABLE=$(printf '%s\n' "$RULE_DUMP" | \
        awk -v m="$MARK" '$0 ~ ("fwmark " m " lookup") {
            for (i = 1; i <= NF; i++) if ($i == "lookup") { print $(i+1); exit }
        }')
    if [ -z "$TABLE" ]; then
        log "skip $grp: no ip rule for mark $MARK"
        SKIPPED=$((SKIPPED + 1))
        continue
    fi

    # Already healthy?
    if ip route show table "$TABLE" 2>/dev/null | \
       grep -q "default via $OPKGTUN_GW dev $OPKGTUN_IF"; then
        continue
    fi

    # Restore. `replace` is idempotent (insert-or-update); preserves any
    # other routes in the table (none expected, but be safe).
    if ip route replace default via "$OPKGTUN_GW" dev "$OPKGTUN_IF" \
            table "$TABLE" 2>>"$LOG"; then
        log "patched $grp: mark=$MARK table=$TABLE default via $OPKGTUN_GW"
        CHANGES=$((CHANGES + 1))
    else
        log "FAILED to patch $grp (mark=$MARK table=$TABLE)"
    fi
done

if [ "$CHANGES" -gt 0 ]; then
    log "$CHANGES route(s) restored ($SKIPPED skipped)"
fi
exit 0
