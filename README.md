# keenetic-singbox

Sing-box VPN client for Keenetic-class routers — single-pool config
with per-country routing pools, an honest healthcheck daemon, daily
subscription refresh, and a fail-closed kill switch.

Tested on **Netcraze Hopper 4G+ (NC-2312), NDM 5.0.10**. Should work on
any Keenetic with the OPKG component and Entware deployed.

## Quick start

You only need an SSH client on your workstation — **the installer
runs on the router**, not on your laptop. Windows 10/11 ships with
OpenSSH built in (`ssh` works in PowerShell / cmd / Windows Terminal /
git-bash). macOS and Linux already have it. PuTTY also works.

From your workstation:

```sh
ssh -p 222 root@<router-ip>
```

You're now in the router's shell. Run:

```sh
curl -fsSL https://raw.githubusercontent.com/inlarin/keenetic-singbox/main/install.sh | sh
```

The installer is interactive — it prompts for your subscription URL,
auto-detects the LAN IP, generates a Clash API secret, and deploys
everything. ~3 minutes later you'll get a MetaCubeXD URL and the API
secret to paste into it.

For the full step-by-step (with verification, FQDN pinning, removal,
troubleshooting) see [INSTALL.md](INSTALL.md). For architecture deep
dive (why single-pool, how the healthcheck daemon works, kill-switch
invariants, NAND wear-reduction) see [ARCHITECTURE.md](ARCHITECTURE.md).

## What you get

- **sing-box 1.13** running as `/opt/etc/init.d/S99sing-box`, exposing
  a TUN interface (`opkgtun0`) NDM treats as a managed iface.
- **Per-country routing pools** — every server tagged stably as
  `<cc>-<proto>-<host>-<port>-<transport>`. MetaCubeXD shows
  `urltest-all`, per-country `urltest-tr` / `urltest-nl` / etc., and
  every individual server for manual pinning.
- **Healthcheck daemon** that probes through `opkgtun0` with real-data
  100 KB downloads (not HEAD/204), rotates dead servers in ≤30 s,
  re-ranks the full pool every 10 min, and survives provider key
  rotations via auto-refresh.
- **Daily subscription refresh** at 04:02 with stable cc-keyed cache
  so server IPs that move between providers keep their slot.
- **Kill switch** — `auto reject` on every `dns-proxy route` makes
  tunneled traffic fail-closed when sing-box is down.
- **MetaCubeXD** web UI auto-deployed at `http://<router-ip>:9090/ui/`.

## Repo layout

```
keenetic-singbox/
├── install.sh                   ← interactive installer (curl|sh entry point)
├── sub_to_singbox.py            ← v2ray subscription → sing-box config
├── S99singbox-healthcheck       ← router init.d daemon
├── singbox-healthcheck-watchdog ← cron.1min watchdog
├── sub-refresh.sh               ← cron.daily subscription refresh
├── softether/                   ← optional SoftEther Bridge2 mode
├── diag/                        ← Keenetic diag + utility scripts (NOT used by install)
│   └── kn_*.py + tests/
├── INSTALL.md                   ← step-by-step
├── ARCHITECTURE.md              ← internals
├── .env.example                 ← workstation-side env vars
└── requirements.txt             ← workstation-only deps
```

## Alternative: SoftEther Bridge2 mode

If you'd rather route through a SoftEther server (different geography,
existing SoftEther infra) instead of sing-box outbounds, see
[`softether/README.md`](softether/README.md). The two modes can co-exist
on the same router — sing-box owns `OpkgTun0`, SoftEther owns `Bridge2`,
and NDM `dns-proxy route` decides which FQDN group goes where.
