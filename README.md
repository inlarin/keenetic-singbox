# keenetic-singbox

Toolkit for deploying and operating a sing-box VPN client on
Keenetic Hopper 4G+ (NC-2312, NDM 5.0.10), with optional SoftEther
bridge for selective routing through `Bridge2`.

## Layout

```
keenetic-singbox/
├── install.sh                   one-shot bootstrap (run on router via curl|sh)
├── deploy.py                    workstation-side push installer (paramiko)
├── kn_common.py                 shared NDM CLI / SSH helpers
├── kn_*.py                      one-shot probe / apply / install scripts
├── sub_to_singbox.py            v2ray subscription -> sing-box config
├── S99singbox-healthcheck       router-side init.d watchdog
├── singbox-healthcheck-watchdog systemd-style supervisor
├── sub-refresh.sh               periodic subscription refresh on router
├── ndm_setup.cmd                OpkgTun0 NDM registration commands
├── softether/                   SoftEther client + br2 bridge wrappers
│   ├── S05vpnclient
│   ├── udhcpc.br2.script
│   ├── udhcpc.vpn_redacted.script
│   └── poc_opkgtap.sh
├── tests/                       pytest for kn_common helpers
├── MANUAL_INSTALL.md            bare command list (alternative to deploy.py)
├── SINGBOX_SETUP.md             full runbook with architecture + gotchas
└── requirements.txt
```

## Quick start

The simplest path runs entirely on the router — nothing pushed from a
workstation. SSH in and pipe the bootstrap into `sh`:

```sh
ssh -p 222 root@<router-ip>
export SUBSCRIPTION_URL='https://<panel>/s/<token>'
curl -fsSL https://raw.githubusercontent.com/inlarin/keenetic-singbox/main/install.sh | sh
```

`install.sh` auto-detects the LAN IP, opkg-installs the prerequisites,
fetches the rest of the scripts from this public repo, generates the
sing-box config locally on the router, applies the NDM-side OpkgTun0
registration via `ndmc`, and starts everything. Idempotent — re-run
for upgrades. Assumes Entware is already installed (run
`kn_install_entware_step1.py` from a workstation first if not).

The healthcheck secret is generated on first run and persisted to
`/opt/etc/sing-box/.healthcheck-secret`, so MetaCubeXD bookmarks
survive re-runs.

### Alternative install paths

- **Workstation push (`deploy.py`)** — for offline installs or when
  the router can't reach GitHub. Requires `paramiko` and an `.env`
  file with `ROUTER_HOST`/`ROUTER_PASS`/`SUBSCRIPTION_URL`/
  `SINGBOX_HEALTHCHECK_SECRET`. Pushes everything via SSH from the
  workstation.
- **Step-by-step (`MANUAL_INSTALL.md`)** — every command spelled out,
  no installer.
- **Full architectural runbook (`SINGBOX_SETUP.md`)** — the why behind
  each step, gotchas, and troubleshooting.

## Credentials

All scripts read secrets from environment variables — nothing is
hard-coded. Real values live one directory up in `../.env` (kept
out of this repo).

```sh
set -a && source ../.env && set +a
```

| Var | Purpose |
|---|---|
| `ROUTER_HOST` | Router IP (default `192.168.1.1`) |
| `ROUTER_PORT` | NDM CLI telnet port (default `23`) |
| `ROUTER_USER` | NDM login (default `admin`) |
| `ROUTER_PASS` | NDM password — required |
| `SUBSCRIPTION_URL` | hynet panel subscription URL (used by `deploy.py` and written to `/opt/etc/sing-box/.subscription-url` on the router) |
| `SINGBOX_HEALTHCHECK_SECRET` | Clash API bearer token used by `sub_to_singbox.py` and the router-side healthcheck |

## Architecture

See `SINGBOX_SETUP.md` for the full walk-through. Short version:

- `sub_to_singbox.py` parses a v2ray-style base64 subscription and
  emits a sing-box config with per-country routing pools — each
  country gets its own `OpkgTunN` interface, urltest group, and
  selector.
- `S99singbox-healthcheck` runs on the router, probing the Clash API,
  pinning `select.now`, and restarting sing-box on stall.
- `softether/S05vpnclient` runs SoftEther client + `Bridge2` wrapper
  so a watcher can patch NDM routing tables to send selected FQDNs
  through SoftEther instead of sing-box.

## Cross-repo dependencies

- `kn_check_install.py` and `kn_probe_storage.py` import from
  `kn_gui.rci_client` (a sibling project at
  [keenetic-fqdn-manager](https://github.com/inlarin/keenetic-fqdn-manager)).
  Clone it next to this repo and add the parent directory to
  `PYTHONPATH` to use them.

## Running tests

```sh
python -m pytest tests/
```
