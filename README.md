# keenetic-singbox

Toolkit for deploying and operating a sing-box VPN client on
Keenetic Hopper 4G+ (NC-2312, NDM 5.0.10), with optional SoftEther
bridge for selective routing through `Bridge2`.

## Layout

```
keenetic-singbox/
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
├── SINGBOX_SETUP.md             end-to-end deployment doc
└── requirements.txt
```

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
