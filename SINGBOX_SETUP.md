# Sing-box on Keenetic NDM (aarch64 + Entware) — runbook

End-to-end install of sing-box as a second VPN client on a Keenetic-class
router that already has Entware installed in internal flash. Tested on
**Netcraze Hopper 4G+ (NC-2312), NDM 5.0.10, kernel 4.9 aarch64,
Entware → /opt UBIFS, dropbear on tcp/222**. Should reproduce on any other
Keenetic with the `opkg` component enabled and Entware deployed.

## TL;DR — one-shot deploy

If you have Entware up and an `.env` file with `ROUTER_HOST`,
`ROUTER_PASS`, `SUBSCRIPTION_URL`, `SINGBOX_HEALTHCHECK_SECRET`:

```sh
pip install -r requirements.txt   # paramiko + requests
python deploy.py                  # uses ../.env, deploys everything
```

`deploy.py` does §1–§6 below automatically. Read on for the manual
runbook (useful for debugging or stepping through the install).

The end state: sing-box 1.13.x running as a daemon, exposing a TUN
interface (`opkgtun0`, `172.19.0.1/32`), 42 v2ray outbounds parsed from a
base64 subscription, MetaCubeXD web dashboard on
`http://<router-lan-ip>:9090/ui/`, NDM keeps owning the routing tables
(`auto_route` / `strict_route` are off so we don't fight `kn_gui`).

## 0. Prerequisites

- Entware installed in internal flash (`mount | grep /opt` → `/dev/ubi0_X
  on /opt type ubifs`). On NC-2312 the partition is `mtd18 "Storage"`,
  ~98 MiB usable. Verify ≥30 MiB free: `df -h /opt`.
- Dropbear SSH on tcp/222, root login (factory-default password from
  Keenetic docs — change it on first login).
- A v2ray-style subscription URL that returns base64-of-newline-separated
  `vless://`/`vmess://`/`trojan://`/`ss://` URIs. The hynet panel does **not**
  honour `User-Agent: clash` / `?flag=clash` etc — it always returns
  base64-URI-list, so a converter is mandatory.

## 1. Install sing-box from Entware

```sh
ssh -p 222 root@<router-lan-ip>
opkg update
opkg install sing-box-go            # 1.13.3-2 at time of writing
sing-box version                    # confirm
```

Footprint: bundled binary is **~54 MiB** (`/opt/bin/sing-box`) — large
because the Entware build ships with every feature flag (`with_grpc`,
`with_quic`, `with_gvisor`, `with_tailscale`, `with_wireguard`,
`with_naive_outbound`, `with_v2ray_api`, `with_clash_api`, `with_acme`,
`with_dhcp`, `with_embedded_tor`, etc.). Brings in `libatomic` (~50 KiB).

Files it writes:
- `/opt/bin/sing-box` — binary
- `/opt/etc/sing-box/config.json` — default sample (rename to `.dist`)
- `/opt/etc/init.d/S99sing-box` — Entware init wrapper

```sh
mv /opt/etc/sing-box/config.json /opt/etc/sing-box/config.json.dist
mkdir -p /opt/var/lib/sing-box /opt/share/sing-box/ui
```

`/opt/var/lib/sing-box` is the sing-box working directory (cache, downloaded
rule-sets); `/opt/share/sing-box/ui` is where `external_ui` will land
MetaCubeXD on first run.

## 2. Generate sing-box config from the subscription

The subscription is base64-URI-list, sing-box can't ingest that natively.
Convert with `sub_to_singbox.py` (in this repo). Run on your workstation:

```sh
python sub_to_singbox.py "https://<panel>/s/<token>" --out hynet_singbox.json
```

What the converter emits:
- **Outbounds:** one per URI (vless/vmess/trojan/ss), with REALITY/uTLS
  flags, gRPC/WS/HTTP transports, xudp packet encoding.
- **`selector`** outbound `select` + **`urltest`** outbound (`generate_204`
  every 3 min, 50 ms tolerance).
- **TUN inbound** `tunhynet`, `172.19.0.1/30`, MTU 1420, `auto_route: false`,
  `strict_route: false`, `stack: system`. Critical: NDM keeps owning all
  routing tables and policy rules — sing-box just publishes an interface.
- **DNS**: local domains via `192.168.1.1` (the router's NDM dns-proxy),
  everything else via `1.1.1.1` through `select`.
- **Clash API** on `192.168.1.1:9090` with bearer-token auth and
  `external_ui` pointing at MetaCubeXD (auto-downloaded on first start).
- **`route`** rules: hijack DNS to sing-box, bypass private nets to direct,
  everything else through `select`. `default_domain_resolver: local`
  silences the 1.13 deprecation warning.

Generate a fresh API secret if you don't have one, then export so the
converter picks it up via env (no editing source):

```sh
export SINGBOX_HEALTHCHECK_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export ROUTER_HOST=192.168.X.1   # your router's LAN IP
python sub_to_singbox.py "$SUBSCRIPTION_URL" --out hynet_singbox.json --ndm-setup ndm_setup.cmd
```

`sub_to_singbox.py` reads `SINGBOX_HEALTHCHECK_SECRET` and `ROUTER_HOST`
from the environment (or `--secret` / `--router-ip` flags). Keep the
real values in `monitoring/.env` — `keenetic-singbox/` itself never
holds them.

## 3. Push config to the router

Dropbear on Keenetic does **not** ship with SFTP, so use a base64-piped
fallback rather than `scp -O`/`sftp`. Example with paramiko (Python):

```python
import paramiko, base64
def push(c, local, remote):
    data = open(local, 'rb').read()
    b64 = base64.b64encode(data).decode()
    ch = c.get_transport().open_session()
    ch.exec_command(f"base64 -d > {remote}")
    for i in range(0, len(b64), 4096):
        ch.sendall(b64[i:i+4096])
    ch.shutdown_write(); ch.recv_exit_status()

c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect('192.168.1.1', port=222, username='root',
          password=os.environ['ROUTER_PASS'],
          allow_agent=False, look_for_keys=False)
push(c, 'hynet_singbox.json', '/opt/etc/sing-box/config.json')
```

Validate before starting:

```sh
/opt/bin/sing-box check -C /opt/etc/sing-box/
```

Common 1.13 gotchas that the converter already handles (and that you'll hit
if you copy a 1.10/1.11-era config from a tutorial):

| Symptom | Cause | Fix |
|---|---|---|
| `legacy DNS servers is deprecated` | DNS server uses `address` field | use `type: udp` + `server:` |
| `dns outbound is deprecated and removed` | `outbounds: [{"type":"dns"}]` | drop it, use `route.rules` with `action: hijack-dns` |
| `block outbound is deprecated and removed` | `outbounds: [{"type":"block"}]` | drop it, use `action: reject` in route rule |
| `missing default_domain_resolver` | DNS rules without resolver | set `route.default_domain_resolver: <tag>` |
| `detour to an empty direct outbound makes no sense` | DNS server sets `detour: direct` while the direct outbound has no fields | omit `detour` (default IS direct) |

## 4. Start

```sh
/opt/etc/init.d/S99sing-box start
sleep 6
pgrep -af sing-box
ip a show opkgtun0 | head -5
netstat -tln | grep 9090
```

Expected:
- one `sing-box run -D /opt/var/lib/sing-box -C /opt/etc/sing-box` process,
- `opkgtun0` interface UP with `172.19.0.1/32`,
- `192.168.1.1:9090` LISTEN.

**Critical config alignment:** the converter sets `interface_name: "opkgtun0"`
and `address: ["172.19.0.1/32"]`. Both are required for NDM-side registration
in step 6:
- `opkgtun0` (lowercase) is the kernel iface name; NDM exposes it as
  `OpkgTun0` (uppercase noun). Other names will not be picked up.
- The address mask must match what NDM stores (`/32`); a `/30` configured in
  earlier docs runs at runtime but creates a mask mismatch with NDM's noun.

The init script is the stock Entware `rc.func` wrapper with `ENABLED=yes`,
`PROCS=sing-box`, `ARGS="run -D /opt/var/lib/sing-box -C /opt/etc/sing-box"`.
No edits needed.

## 5. Web GUI (MetaCubeXD)

On first start sing-box hits `external_ui_download_url`
(`https://github.com/MetaCubeX/metacubexd/archive/refs/heads/gh-pages.zip`)
through the `direct` outbound and unpacks ~2.2 MiB into
`/opt/share/sing-box/ui`. Open in any LAN browser:

```
http://<router-lan-ip>:9090/ui/
```

Auth host: `<router-lan-ip>:9090`, secret: the bearer token you baked
into the config. The dashboard exposes:
- per-outbound latency & manual selection,
- realtime traffic graph,
- live log stream,
- active connections (with kill),
- routing rules.

If sing-box has no internet at first start, the UI dir stays empty — the
fix is `rm -rf /opt/share/sing-box/ui/*; /opt/etc/init.d/S99sing-box restart`
once connectivity is back, or unzip manually:

```sh
cd /tmp && wget https://github.com/MetaCubeX/metacubexd/archive/refs/heads/gh-pages.zip
unzip gh-pages.zip && cp -r metacubexd-gh-pages/* /opt/share/sing-box/ui/
```

## 6. Register the single OpkgTun0 in NDM

Single-pool architecture (since 2026-04-28): one TUN, one selector,
country switching via the web dashboard. The converter emits a
ready-to-paste NDM-side bundle:

```sh
python sub_to_singbox.py <SUB_URL> --out hynet_singbox.json --ndm-setup ndm_setup.cmd
```

`ndm_setup.cmd` (mirrors
[TrustTunnel-Keenetic](https://github.com/artemevsevev/TrustTunnel-Keenetic)):

```
interface OpkgTun0
interface OpkgTun0 description "sing-box hynet TUN"
interface OpkgTun0 ip address 172.19.0.1 255.255.255.255
interface OpkgTun0 ip global auto
interface OpkgTun0 ip mtu 1420
interface OpkgTun0 ip tcp adjust-mss pmtu
interface OpkgTun0 security-level public
interface OpkgTun0 up
ip route default 172.19.0.1 OpkgTun0
system configuration save
```

**Apply order — chicken-egg with kernel iface ownership:**

```sh
# 1. Stop sing-box so kernel iface is released
/opt/etc/init.d/S99sing-box stop

# 2. Apply ndm_setup.cmd line-by-line via ndmc (NDM creates the
#    OpkgTun0 noun + kernel iface)
while IFS= read -r line; do
    [ -z "$line" ] || [ "${line#!}" != "$line" ] && continue
    ndmc -c "$line"
done < /tmp/ndm_setup.cmd

# 3. Start sing-box — it re-attaches to existing iface by name
/opt/etc/init.d/S99sing-box start
```

After registration, NDM treats `opkgtun0` like any other managed
interface — FORWARD ACCEPT, fwmark tables, SNAT/MASQUERADE are auto.

**Error `0xcffd009f`**: kernel iface still held by sing-box. Stop
sing-box first, then apply ndm_setup. Subsequent restart cycles don't
trip on this.

## 6.1 Pin a service to the tunnel

All FQDN groups bind to the **same** `OpkgTun0`. Country choice happens
later, inside sing-box (selector). Example for YouTube:

```
object-group fqdn youtube
include youtube.com
include www.youtube.com
include googlevideo.com
exit
dns-proxy route object-group youtube OpkgTun0 auto reject
system configuration save
```

NDM auto-creates ipset `_NDM_OGDN_4_@youtube`, mangle MARK, ip rule, and
populates the dedicated routing table with `default via 172.19.0.1 dev
opkgtun0` from step 6's `ip route default …`. LAN traffic to YouTube
then enters opkgtun0 → sing-box → `select` → currently-active outbound.

Other things you might want bound: `chatgpt.com`, `claude.ai`,
`telegram.org`, `youtube.com`, etc. — manage via kn_gui v3.6.1+ which
shows `OpkgTun0` in the iface dropdown.

```sh
# Verify a binding works:
curl -4 https://ipinfo.io/ip            # default → PPPoE IP
# … add ipinfo.io to a kn_gui binding mapped to OpkgTun0 …
curl -4 https://ipinfo.io/ip            # → currently selected tunnel exit IP
```

## 6.2 Switch country (or specific server) via MetaCubeXD

Open `http://<router-lan-ip>:9090/ui/`, authenticate (host
`<router-lan-ip>:9090`, secret from `experimental.clash_api.secret`).

> **Clash API binding** — sing-box listens on `0.0.0.0:9090` so
> MetaCubeXD is reachable from any LAN browser at `<router-lan-ip>:9090`,
> while router-side scripts (healthcheck, sub-refresh) hit
> `127.0.0.1:9090`. No router IP is hardcoded into either side.

Click the `select` group → pick:
| Option | Behaviour |
|---|---|
| `urltest-all` | Sing-box auto-best across all 42 servers (note: HEAD-probe-based, not always honest — see §8) |
| `urltest-tr` / `-us` / `-de` / `-nl` / `-gb` / `-ru` | Sing-box auto-best inside one country |
| `tr-vless-198.51.100.2-2053-grpc` (or any other tag) | Manual pin to a specific server |

Persists across sing-box restarts via `experimental.cache_file`. The
healthcheck daemon's auto-pin (§8) overrides this on every sweep — to
keep a manual pin sticky, stop the daemon
(`S99singbox-healthcheck stop`).

## 7. Subscription refresh

Daily refresh script `/opt/etc/cron.daily/sub-refresh` (deployed from
`sub-refresh.sh` in this repo). The subscription URL is read from
`/opt/etc/sing-box/.subscription-url` (or the `SUBSCRIPTION_URL` env)
— never hardcoded. Requires Entware packages:

```sh
opkg install python3 python3-urllib python3-codecs cron curl
/opt/etc/init.d/S10cron start
```

The script:
1. Pulls the subscription (curl, **not** busybox wget — segfaults on
   HTTPS with chunked transfer).
2. Re-runs the converter (`sub_to_singbox.py`) with persistent
   `country_cache.json` so the same IP→cc lookup doesn't hit ip-api.com
   on every refresh.
3. Validates with `sing-box check`.
4. If `ndm_setup.cmd` line count changed (mainly during architecture
   migrations), re-applies it.
5. Replaces the active config and restarts sing-box.
6. Re-applies the previous `select.now` pick via Clash API.

Logs to `/opt/var/log/sub-refresh.log`.

## 8. Honest health-check daemon + watchdog

`/opt/etc/init.d/S99singbox-healthcheck` runs in the background and
owns `select.now`. Source of truth for which server traffic actually
goes through (sing-box's built-in urltest-all is unreliable — it only
HEAD-pings gstatic over the proxy chain, missing REALITY/Vision TLS
failures).

### Probe / rotation parameters

| Setting | Value | Meaning |
|---|---|---|
| `LIVENESS_INTERVAL` | 30 s | how often the daemon probes opkgtun0 |
| `FAIL_THRESHOLD` | 1 | one failed probe = immediate rotation |
| `PROBE_TIMEOUT` | 8 s | per-probe wall-clock cutoff |
| `RANK_INTERVAL` | 600 s | full sweep cadence (re-rank all 42 servers) |
| `ROTATIONS_BEFORE_FORCE_SWEEP` | 3 | safety net: force sweep instead of round-robin through stale ranking |

### Behaviour

1. **Liveness:** every 30 s, probe `https://www.gstatic.com/generate_204`
   through `opkgtun0` (kernel-bind-to-iface curl). Real traffic, full
   pipeline (mangle MARK → tun read → sing-box → outbound → server's
   REALITY/Vision/etc.).
2. **Single fail → rotate** `select.now` to next ranked outbound via
   Clash API PUT.
3. **3 consecutive rotations without success → force sweep** instead of
   round-robining 42 servers (which would take ~21 min worst case).
4. **Full sweep** every 10 min: tmp-pin each of 42 outbounds, probe,
   record latency to `/opt/var/lib/sing-box/server-ranking.json`.
5. **Auto-pin** after every sweep: writes the lowest-latency outbound
   into `select.now`.

### Watchdog

`/opt/etc/cron.1min/singbox-healthcheck-watchdog` (deployed from
`singbox-healthcheck-watchdog` in this repo) runs every minute. If the
daemon's PID file points at a dead process, the watchdog calls `start`
to bring it back. Worst case downtime: 60 s.

### Operating

```sh
/opt/etc/init.d/S99singbox-healthcheck start    # start daemon (no-op if already running)
/opt/etc/init.d/S99singbox-healthcheck status   # PID + last 20 log + top-10 ranking
/opt/etc/init.d/S99singbox-healthcheck sweep    # force a full sweep now
/opt/etc/init.d/S99singbox-healthcheck stop     # release select.now to manual control
```

If you want a sticky manual pin (e.g. testing one specific server),
`stop` the daemon — otherwise auto-pin will overwrite it on the next
sweep. **Note:** the watchdog will then bring it back within a minute.
For long-term sticky pin, also disable the watchdog by `chmod -x
/opt/etc/cron.1min/singbox-healthcheck-watchdog` (and `chmod +x` to
re-enable).

### Resilience cheatsheet (recovery times)

| Failure | Recovery | Why |
|---|---|---|
| Currently-active server dies | ≤ 30 s | Single fail → rotate to next ranked |
| Top 5-10 servers die | ≤ ~7 min | 3×30 s rotations → force sweep (~5 min) → auto-pin |
| 41 of 42 dead | ~7 min | Force sweep finds the lone live one |
| **All 42 dead** | Indefinite hold | `auto reject` kill switch drops traffic via fwmark blackhole. **No PPPoE leak.** Auto-recovers when any server returns. |
| Healthcheck daemon crash | ≤ 60 s | cron.1min watchdog restarts it |
| Sing-box crash | Indefinite hold | `opkgtun0` gone → blackhole still drops marked packets. **No leak**, but failover is gone — needs manual `S99sing-box restart` (a similar watchdog can be added) |
| PPPoE drops | Hold for WAN | Same as "all dead" |
| Sing-box TUN inbound hangs (gvisor stack stuck) — **theoretical, not observed** | n/a | Documented sing-box/gVisor failure mode in the wider community, but never seen in this deployment as of 2026-04-28. Listed for honesty. If it ever happens: probe-through-opkgtun0 fails identically to "all servers dead", daemon can't tell the difference, no auto-recovery — user must `S99sing-box restart`. Optional hardening if it starts happening: switch TUN `stack: gvisor` → `stack: system`. |
| NDM ipset misses a destination IP | INSTANT leak | Packet without fwmark goes through PPPoE. Refreshes when an FQDN gets re-resolved through dns-proxy. Cannot fix without breaking caching. |
| Client uses DoH/DoT bypassing NDM | INSTANT leak | Browser/app encrypts DNS over its own channel; NDM never sees the query. Mitigation: firewall block on tcp/853 + known DoH endpoint IPs (separate task). |

### Kill-switch invariants (must hold)

- Every `dns-proxy route ... <iface> auto` MUST end with `reject`.
  Audit:
  ```sh
  ndmc -c "show running-config" | grep "route object-group" | grep -v reject
  ```
  Output must be empty. Without `reject`, a momentarily-empty fwmark
  table falls through to PPPoE = leak.
- `from all fwmark 0xffffXXX blackhole` rules (priority 101/103/...)
  MUST stay in `ip rule list`. NDM owns these; never observed missing,
  but worth checking if anything seems wrong.

## 9. Disk / RAM accounting + NAND wear reduction (2026-04-28)

### Disk on `/opt` (UBIFS NAND, 98 MiB total)

| Path | Size |
|---|---|
| `/opt/bin/sing-box` | 54.2 MiB |
| `/opt/bin/python3` + libs (urllib, codecs) | ~7 MiB |
| `/opt/bin/curl` + libcurl + ca-bundle | ~2 MiB |
| `/opt/share/sing-box/ui/` (MetaCubeXD) | 2.2 MiB |
| `/opt/etc/sing-box/config.json` (42 outbounds) | ~30 KiB |
| Entware `cron` | ~50 KiB |

`/opt`: ~52 MiB used / ~41 MiB free.

### Runtime state on tmpfs (RAM, lost on reboot)

To stop hammering NAND with per-connection writes, **all** runtime
state lives on `/tmp` (tmpfs). Logs, PIDs, ranking, sing-box cache —
none of it touches UBIFS during normal operation.

| File | Purpose |
|---|---|
| `/tmp/sing-box.log` | sing-box log (level=`warn`, not `info`) |
| `/tmp/sing-box-cache.db` | sing-box selector cache |
| `/tmp/singbox-healthcheck.log` | healthcheck daemon log |
| `/tmp/singbox-healthcheck.pid` | daemon PID |
| `/tmp/singbox-server-ranking.json` | ranking output |
| `/tmp/singbox-state/{fail,rotations,_sweep.txt}` | counters + scratch |
| `/tmp/sub-refresh.log` | refresh script log |

Confirm zero NAND writes for our stack:

```sh
ls /opt/var/log/ /opt/var/lib/sing-box/ 2>&1
# Expected: empty or "No such directory"
```

Trade-off: state lost on reboot.
- Sing-box's `select.now` cache: gone → defaults to `urltest-all` for
  ~60 s, then healthcheck daemon's first sweep auto-pins best (~5 min).
  Worst case ~5 min of sub-optimal selection after reboot.
- Logs gone — if you need post-incident debugging, hook a remote
  syslog forwarder.

### RAM (497 MiB total)

~190 MiB available, ~250 MiB used (sing-box ~30 MiB + python3 ~10 MiB +
NDM + everything else). Single-pool gvisor stack costs ~15 MiB; the
per-country variant from 2026-04-27 burned ~60 MiB extra for the same
end-user UX.

## 10. Removing it

NDM-side first (over telnet, repeat for every OpkgTunN registered):

```
no dns-proxy route object-group <group> OpkgTun0
no object-group fqdn <group>
no ip route default 172.19.0.1 OpkgTun0
no interface OpkgTun0
system configuration save
```

```sh
/opt/etc/init.d/S99singbox-healthcheck stop
/opt/etc/init.d/S99sing-box stop
rm /opt/etc/cron.1min/singbox-healthcheck-watchdog
rm /opt/etc/cron.daily/sub-refresh
rm /opt/etc/init.d/S99singbox-healthcheck
opkg remove sing-box-go
rm -rf /opt/etc/sing-box /opt/var/lib/sing-box /opt/share/sing-box
# Optional, if no other Entware app needs them:
opkg remove cron python3 python3-urllib python3-codecs curl
```

The kernel `opkgtunN` interfaces disappear when sing-box exits.
