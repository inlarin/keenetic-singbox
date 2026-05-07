#!/bin/sh
# PoC: rename vpn_redacted → opkgtap0 and check whether NDM auto-registers it.
# Runs as root inside the dropbear shell on the Keenetic router.
#
# Will not save NDM config (`system configuration save`) — caller decides
# whether the rename should survive a reboot. Reboot in current state
# would simply recreate vpn_redacted with the original name.

set -u

NEW_IFACE=opkgtap0
OLD_IFACE=vpn_redacted
HUB_GW=10.X.0.1
TEST_GROUP=pbr_poc_test
TEST_DOMAIN=httpbin.org

say() { printf '\n=== %s ===\n' "$*"; }

say "0. Snapshot: current state of vpn_redacted + watcher"
ip a show $OLD_IFACE 2>&1 || echo "($OLD_IFACE not present)"
echo "watcher pid: $(cat /opt/var/run/${OLD_IFACE}_watcher.pid 2>/dev/null || echo none)"

say "1. Pause watcher so it doesn't fight the rename"
if [ -f /opt/var/run/${OLD_IFACE}_watcher.pid ]; then
    kill "$(cat /opt/var/run/${OLD_IFACE}_watcher.pid)" 2>/dev/null
    rm -f /opt/var/run/${OLD_IFACE}_watcher.pid
fi

say "2. Down + rename + up: $OLD_IFACE → $NEW_IFACE"
ip link set "$OLD_IFACE" down 2>&1
ip link set "$OLD_IFACE" name "$NEW_IFACE" 2>&1
ip link set "$NEW_IFACE" up 2>&1
sleep 2
ip a show "$NEW_IFACE" 2>&1

say "3. Wait 5s for NDM watchdog to detect opkgtap0"
sleep 5
ndmc -c 'show interface' 2>&1 | grep -i -E "opkg|name = \"Opkg" | head -10
echo "(if nothing above, NDM did NOT pick it up automatically)"

say "4. Try ndmc-side configuration of OpkgTap0"
for cmd in \
    'interface OpkgTap0 description "SoftEther tap (vpn_redacted) bridged via opkgtap0"' \
    'interface OpkgTap0 ip address 10.X.0.11 255.255.255.0' \
    'interface OpkgTap0 ip global auto' \
    'interface OpkgTap0 ip mtu 1500' \
    'interface OpkgTap0 ip tcp adjust-mss pmtu' \
    'interface OpkgTap0 security-level public' \
    'interface OpkgTap0 up'
do
    echo "--> ndmc: $cmd"
    ndmc -c "$cmd" 2>&1 | head -3
done

say "5. show interface OpkgTap0 — does NDM now know it?"
ndmc -c 'show interface OpkgTap0' 2>&1 | head -25

say "6. Re-apply IP via udhcpc (kernel level)"
udhcpc -i "$NEW_IFACE" -t 4 -T 4 -n -q -s /opt/etc/udhcpc.vpn_redacted.script 2>&1 | tail -5
ip a show "$NEW_IFACE" 2>&1

say "7. Create a test FQDN object-group"
ndmc -c "object-group fqdn $TEST_GROUP" 2>&1 | head -3
ndmc -c "object-group fqdn $TEST_GROUP include $TEST_DOMAIN" 2>&1 | head -3

say "8. CRITICAL TEST — bind dns-proxy route to OpkgTap0"
ndmc -c "dns-proxy route object-group $TEST_GROUP OpkgTap0 auto reject" 2>&1 | head -5
echo "(if NDM accepted that, we win)"

say "9. Verify the route is in running-config"
ndmc -c "show running-config" 2>&1 | grep -E "$TEST_GROUP|OpkgTap0" | head -5

say "10. CLEANUP — drop test group + route (rename is KEPT)"
ndmc -c "no dns-proxy route object-group $TEST_GROUP OpkgTap0" 2>&1 | head -3
ndmc -c "no object-group fqdn $TEST_GROUP" 2>&1 | head -3

say "11. Final state"
ip a show "$NEW_IFACE"
ndmc -c 'show interface OpkgTap0' 2>&1 | grep -E "type|state|address|mtu" | head -10
echo
echo "DONE. Did NOT call 'system configuration save'."
echo "If anything looks wrong: reboot router, vpnclient will recreate vpn_redacted as before."
