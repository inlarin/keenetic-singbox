# diag/ — Keenetic diagnostic + utility scripts

Standalone scripts that helped build and debug the keenetic-singbox
stack. Not invoked by `install.sh` — clone or download only if you
need them.

## Contents

| Group | Scripts |
|---|---|
| Entware install | `kn_install_entware_step1.py`, `kn_opkg_rollback.py`, `kn_check_install.py` |
| NDM CLI probes | `kn_probe.py`, `kn_probe_*.py` (entware, ssh, storage, opkg, hw, components, mixed, exclusive) |
| Verification | `kn_verify.py` |
| FQDN routing helpers | `kn_fqdn.py`, `kn_youtube.py`, `kn_copilot.py`, `kn_apply.py` |
| Leak prevention | `kn_block_doh.py`, `kn_block_ipv6.py`, `kn_reject_all.py` (kill switch) |
| Maintenance | `kn_cleanup.py`, `kn_save_reboot.py`, `kn_telnet.py` |
| Forensics | `kn_trace_host.py`, `kn_trace_watch.py`, `kn_vpn_traffic.py` |
| Library | `kn_common.py` (shared by all kn_*.py — Telnet/NDM CLI session helpers) |
| Tests | `tests/test_kn_common.py`, `tests/conftest.py` |

## Running

All `kn_*.py` scripts read router credentials from environment:

```sh
export ROUTER_HOST=192.168.X.1
export ROUTER_PASS='<router-admin-password>'
python kn_<script>.py [--host X --user X --dry-run -v]
```

`--help` on each script for specifics.

## Cross-repo dependency

`kn_check_install.py` and `kn_probe_storage.py` import from
`kn_gui.rci_client` (sibling repo
[keenetic-fqdn-manager](https://github.com/inlarin/keenetic-fqdn-manager)).
Clone it next to this repo and add the parent directory to
`PYTHONPATH`:

```sh
git clone https://github.com/inlarin/keenetic-fqdn-manager.git ../keenetic-fqdn-manager
PYTHONPATH=..:../keenetic-fqdn-manager python diag/kn_check_install.py
```

The other 26 scripts work standalone with just `kn_common.py`.

## Tests

```sh
cd diag/
python -m pytest tests/
```
