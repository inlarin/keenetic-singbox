# Installation guide

Step-by-step deployment of `keenetic-singbox` on a Keenetic-class router.

The router runs the entire stack — sing-box itself, the healthcheck
daemon, and the daily subscription refresh job. You don't need anything
on the workstation beyond an SSH client.

## Where commands run

This guide has two kinds of code blocks:

- **From your workstation** — the line starting with `ssh ...`. This
  is the only command you run from your PC. It opens a remote shell on
  the router.
- **On the router** — every other command. They execute over the SSH
  session you just opened. Once you see the router's prompt
  (`Hopper4G+ #` or similar), you're in the router shell.

You'll see the same pattern throughout the guide.

### SSH client

Any SSH client works:

| OS | Recommended |
|---|---|
| Windows 10 / 11 | Built-in OpenSSH — `ssh` in PowerShell, cmd, Windows Terminal, or git-bash. |
| macOS | Built-in `ssh` in Terminal / iTerm. |
| Linux | Built-in `ssh`. |
| Anywhere | PuTTY (set port to 222, host = router IP, login = `root`). |

The installer prompts for input via `/dev/tty`, which means the SSH
terminal handles the prompts directly. ANSI colour works in modern
Windows terminals (Windows Terminal, PowerShell ISE, git-bash); the
script detects a non-tty environment and falls back to plain output.

## What you need

| | |
|---|---|
| Router | Keenetic with **NDM 3.0+** and the **OPKG component** enabled (Hopper, Giga, Ultra, Duo, etc.). Tested on Netcraze Hopper 4G+ (NC-2312) running NDM 5.0.10. |
| NDM features | `OpkgTunN` interface support, `object-group fqdn`, `dns-proxy` with the `route ... auto reject` extension. All present on stable NDM 3.0+; the installer probes for them and aborts with a clear error if missing. |
| Storage | At least 60 MiB free on `/opt`. Internal flash works (NC-2312 has ~98 MiB usable); a USB stick also works. |
| Subscription | A v2ray-style subscription URL that returns a base64-encoded list of `vless://` / `vmess://` / `trojan://` / `ss://` URIs. |
| LAN access | SSH connection to the router on **tcp/222** (dropbear) as `root`. |
| 5 minutes | The installer takes ~3 min on a typical link. |

## Step 1 — Install Entware (one-time)

Skip this step if Entware is already up. Verify with:

```sh
ssh -p 222 root@<router-ip> 'mount | grep /opt'
# should print: /dev/ubi0_X on /opt type ubifs (rw,...)
```

If it isn't installed, the standard path is via Keenetic's own UI:

1. NDM web UI → **System settings** → **Component options** → enable
   **OPKG (Entware)**.
2. Reboot.
3. SSH into the router and `opkg update`.

(Or use `diag/kn_install_entware_step1.py` from a workstation if you
prefer scripting — it sends the right `opkg disk storage:/...` command
over the NDM telnet CLI.)

## Step 2 — Run the installer

**From your workstation:**

```sh
ssh -p 222 root@<router-ip>
```

**On the router** (you'll see the `Hopper4G+ #` prompt). The base
Entware install ships only `opkg` and busybox basics — `curl` isn't
included and the busybox `wget` you'd find in `/sbin` segfaults on
chunked HTTPS. So bootstrap `curl` first, then fetch the installer:

```sh
opkg update && opkg install curl
curl -fsSL https://raw.githubusercontent.com/inlarin/keenetic-singbox/main/install.sh | sh
```

`opkg install` is idempotent, so this two-step pattern is safe to
re-run when you want to upgrade later.

The installer is interactive. It will ask for:

1. **Router LAN IP** — auto-detected from `br0`, you confirm or override.
2. **Subscription URL** — paste your hynet/v2ray panel URL.
   (Reused from `/opt/etc/sing-box/.subscription-url` on re-runs.)
3. **Clash API secret** — generated automatically on first run; on
   re-runs you choose whether to keep it (recommended — preserves
   MetaCubeXD bookmarks) or regenerate.

It then performs 9 steps:

| | |
|---|---|
| 1 | preflight (Entware, curl) |
| 2 | NDM components — verify ndmc CLI, version ≥ 3.0, `object-group fqdn`, `dns-proxy` |
| 3 | router IP |
| 4 | subscription URL |
| 5 | `opkg install sing-box-go python3 cron curl` |
| 6 | Clash API secret (generate or reuse) |
| 7 | download `S99singbox-healthcheck`, watchdog, `sub-refresh`, `sub_to_singbox.py` |
| 8 | generate sing-box config + apply NDM `OpkgTun0` registration |
| 9 | start services + smoke test |

If step 2 finds something missing (e.g. `object-group fqdn` not
supported on a stripped-down firmware), it aborts before touching any
state — nothing has been installed at that point.

When done it prints the MetaCubeXD URL and the path to your secret.

## Step 3 — Verify

The installer's smoke test should report:

- `✓ sing-box running`
- `✓ opkgtun0 has IP`
- `✓ Clash API listening on :9090`

If any of these are warnings (`!`), wait 30 seconds for sing-box to
fully come up and re-check:

```sh
pgrep -af sing-box
ip a show opkgtun0
netstat -tln | grep ':9090'
/opt/etc/init.d/S99singbox-healthcheck status
```

## Step 4 — Open MetaCubeXD

Open `http://<router-ip>:9090/ui/` in any LAN browser. When prompted:

- **Host**: `<router-ip>:9090`
- **Secret**: the value in `/opt/etc/sing-box/.healthcheck-secret`

```sh
ssh -p 222 root@<router-ip> 'cat /opt/etc/sing-box/.healthcheck-secret'
```

The dashboard shows a `select` group with auto-best across all servers
(`urltest-all`), per-country auto-best (`urltest-tr`, `urltest-nl`, …),
and every individual server tag for manual pinning.

## Step 5 — Test the tunnel

Pin a single FQDN to the tunnel and verify the exit IP changes. Example
for `ipinfo.io`:

```sh
ssh -p 222 root@<router-ip>
ndmc -c 'object-group fqdn test_exit'
ndmc -c 'object-group fqdn test_exit include ipinfo.io'
ndmc -c 'dns-proxy route object-group test_exit OpkgTun0 auto reject'
ndmc -c 'system configuration save'
```

Then from a LAN client:

```sh
curl -4 https://ipinfo.io/ip
# expect: an IP belonging to whichever country MetaCubeXD has selected
```

## Step 6 — Pin real services

Repeat the pattern for FQDN groups you actually want tunneled:

```
object-group fqdn youtube
object-group fqdn youtube include youtube.com
object-group fqdn youtube include www.youtube.com
object-group fqdn youtube include googlevideo.com
dns-proxy route object-group youtube OpkgTun0 auto reject
system configuration save
```

The trailing `reject` is the **kill-switch** — if `OpkgTun0` is down,
matched traffic is blackholed instead of leaking through PPPoE.

For larger FQDN catalogues (Telegram, Claude, ChatGPT, Spotify, etc.)
the GUI sister project handles bulk import: see
[keenetic-fqdn-manager](https://github.com/inlarin/keenetic-fqdn-manager).

## Re-running the installer

`install.sh` is idempotent — re-run it whenever you want to:

- Pull a newer version of any router-side script.
- Rotate the subscription URL after a panel migration.
- Regenerate the Clash API secret (you'll be prompted, default is keep).

```sh
curl -fsSL https://raw.githubusercontent.com/inlarin/keenetic-singbox/main/install.sh | sh
```

## Removing it

NDM-side first (over telnet:23 with admin credentials, or `ndmc -c` over SSH):

```
no dns-proxy route object-group <each-group> OpkgTun0
no object-group fqdn <each-group>
no ip route default 172.19.0.1 OpkgTun0
no interface OpkgTun0
system configuration save
```

Then on the router (over SSH:222):

```sh
/opt/etc/init.d/S99singbox-healthcheck stop
/opt/etc/init.d/S99sing-box stop
rm /opt/etc/cron.1min/singbox-healthcheck-watchdog
rm /opt/etc/cron.daily/sub-refresh
rm /opt/etc/init.d/S99singbox-healthcheck
opkg remove sing-box-go
rm -rf /opt/etc/sing-box /opt/var/lib/sing-box /opt/share/sing-box
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Installer fails at "Entware not detected" | OPKG component not enabled or not yet rebooted | Step 1 again |
| Installer fails at "ndmc not responding" | Not actually a Keenetic, or NDM service crashed | Reboot the router; if persistent, this is not a Keenetic-class device |
| Installer fails at "NDM X.Y is too old" | Firmware older than NDM 3.0 | Update via NDM web UI → System → Update; OpkgTun and `dns-proxy route ... auto reject` need 3.0+ |
| Installer fails at "this NDM build does not support object-group fqdn" | Stripped-down or preview firmware | Switch to stable channel via NDM web UI → System → Update channel → "Release" |
| Installer warns "dns-proxy not in running-config" | DNS proxy component disabled | NDM web UI → System settings → Component options → enable DNS proxy / Internet filter |
| `opkg install sing-box-go` fails: not enough space | `/opt` partition full | `df -h /opt`; remove unused packages or move `/opt` to a USB stick |
| `sing-box check` fails with "legacy DNS deprecated" / "block outbound removed" | `sub_to_singbox.py` is older than the sing-box build | Re-run installer (it pulls the latest converter) |
| `opkgtun0` never gets an IP | `0xcffd009f` — kernel iface still held by sing-box | Installer handles this; if you ran `ndmc` manually, stop sing-box first |
| MetaCubeXD asks for secret repeatedly | wrong Clash API secret | Use the value in `/opt/etc/sing-box/.healthcheck-secret` |
| Tunneled traffic still goes through PPPoE | FQDN group missing the trailing `reject`, OR client uses DoH | Audit: `ndmc -c "show running-config" \| grep "dns-proxy route" \| grep -v reject` should be empty |
| Tunnel works but a specific server is slow | healthcheck daemon might have pinned a stale server | `/opt/etc/init.d/S99singbox-healthcheck sweep` to force a fresh ranking |

For deeper context on the architecture (single-pool design, healthcheck
daemon behaviour, kill-switch invariants, NAND wear-reduction, RU pool
politics) see [`ARCHITECTURE.md`](ARCHITECTURE.md).
