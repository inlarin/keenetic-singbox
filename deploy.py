"""One-shot deploy of the keenetic-singbox stack to a Keenetic router.

What it does
------------
1. Loads secrets from an env file (default ``../.env`` from this script).
2. Generates ``hynet_singbox.json`` + ``ndm_setup.cmd`` locally via
   ``sub_to_singbox.py``.
3. SSH-connects to the router (dropbear on tcp/222), installs Entware
   prerequisites (``sing-box-go``, ``python3``, ``cron``, ``curl``) unless
   ``--skip-opkg`` is set.
4. Pushes the generated config + this repo's router-side scripts
   (``S99singbox-healthcheck``, ``singbox-healthcheck-watchdog``,
   ``sub-refresh.sh``, ``sub_to_singbox.py``) to ``/opt/...`` via a
   base64-pipe (dropbear has no SFTP).
5. Writes the subscription URL to ``/opt/etc/sing-box/.subscription-url``
   (chmod 600) so ``sub-refresh`` can run unattended without holding the
   token in source.
6. Stops sing-box, applies ``ndm_setup.cmd`` line-by-line via ``ndmc``,
   restarts sing-box, starts the healthcheck daemon.
7. Smoke-tests: process running, ``opkgtun0`` up, Clash API listening.

Prerequisites
-------------
- Entware already installed in internal flash (run
  ``kn_install_entware_step1.py`` first if not).
- Dropbear SSH on port 222 reachable.
- Python paramiko on the workstation (added to ``requirements.txt``).
- An env file with: ``ROUTER_HOST``, ``ROUTER_PASS``, ``SUBSCRIPTION_URL``,
  ``SINGBOX_HEALTHCHECK_SECRET``.

Usage
-----
::

    python deploy.py                    # uses ../.env
    python deploy.py --env path/to.env
    python deploy.py --router-ip 192.168.1.1 --skip-opkg
"""
from __future__ import annotations

import argparse
import base64
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    import paramiko
except ImportError:
    sys.exit("paramiko required. pip install paramiko (or `pip install -r requirements.txt`).")


HERE = Path(__file__).resolve().parent
DEFAULT_ENV = HERE.parent / ".env"

ROUTER_FILES = [
    # (local, remote, mode)
    ("S99singbox-healthcheck",        "/opt/etc/init.d/S99singbox-healthcheck",          "0755"),
    ("singbox-healthcheck-watchdog",  "/opt/etc/cron.1min/singbox-healthcheck-watchdog", "0755"),
    ("sub-refresh.sh",                "/opt/etc/cron.daily/sub-refresh",                 "0755"),
    ("sub_to_singbox.py",             "/opt/share/sing-box/sub_to_singbox.py",           "0644"),
]

OPKG_PACKAGES = "sing-box-go python3 python3-urllib python3-codecs cron curl"


def load_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def require(env: dict[str, str], key: str) -> str:
    val = os.environ.get(key) or env.get(key, "")
    if not val:
        sys.exit(f"missing required env var: {key}")
    return val


def generate_config(out_dir: Path, sub_url: str, secret: str, router_ip: str) -> tuple[Path, Path]:
    cfg = out_dir / "hynet_singbox.json"
    ndm = out_dir / "ndm_setup.cmd"
    env = {**os.environ,
           "SINGBOX_HEALTHCHECK_SECRET": secret,
           "ROUTER_HOST": router_ip}
    print(f"[deploy] generating config -> {cfg}")
    subprocess.run(
        [sys.executable, str(HERE / "sub_to_singbox.py"), sub_url,
         "--out", str(cfg), "--ndm-setup", str(ndm),
         "--router-ip", router_ip],
        check=True, env=env)
    return cfg, ndm


def ssh_connect(host: str, password: str) -> paramiko.SSHClient:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, port=222, username="root", password=password,
              allow_agent=False, look_for_keys=False, timeout=15)
    return c


def run(c: paramiko.SSHClient, cmd: str, *, timeout: int = 60, check: bool = True) -> str:
    """Run a command on the router. Returns combined stdout+stderr."""
    stdin, stdout, stderr = c.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    rc = stdout.channel.recv_exit_status()
    combined = (out + err).rstrip()
    if check and rc != 0:
        sys.exit(f"command failed (rc={rc}): {cmd}\n{combined}")
    return combined


def push(c: paramiko.SSHClient, local: Path, remote: str, mode: str) -> None:
    print(f"[deploy] push {local.name} -> {remote}")
    data = local.read_bytes()
    b64 = base64.b64encode(data).decode()
    ch = c.get_transport().open_session()
    ch.exec_command(f"base64 -d > {remote} && chmod {mode} {remote}")
    for i in range(0, len(b64), 4096):
        ch.sendall(b64[i:i + 4096])
    ch.shutdown_write()
    rc = ch.recv_exit_status()
    if rc != 0:
        sys.exit(f"push failed for {remote} (rc={rc})")


def write_secret_file(c: paramiko.SSHClient, remote: str, value: str) -> None:
    """Write a secret value via stdin pipe (no echo to log) + chmod 600."""
    print(f"[deploy] write secret -> {remote}")
    ch = c.get_transport().open_session()
    ch.exec_command(f"cat > {remote} && chmod 600 {remote}")
    ch.sendall(value)
    ch.shutdown_write()
    rc = ch.recv_exit_status()
    if rc != 0:
        sys.exit(f"failed to write {remote} (rc={rc})")


def apply_ndm_setup(c: paramiko.SSHClient, ndm_file: Path) -> None:
    print(f"[deploy] applying NDM setup ({ndm_file.name})")
    for line in ndm_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("!"):
            continue
        # ndmc command quoting: single-quote the whole line, escape any
        # embedded single quotes. NDM CLI lines from emit_ndm_setup() do
        # not contain quotes, but defensive shell escape stays cheap.
        escaped = line.replace("'", "'\\''")
        run(c, f"ndmc -c '{escaped}'", timeout=20)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--env", default=str(DEFAULT_ENV),
                    help=f"env file path (default: {DEFAULT_ENV})")
    ap.add_argument("--router-ip", default=None,
                    help="override ROUTER_HOST from env")
    ap.add_argument("--skip-opkg", action="store_true",
                    help="skip Entware package install (assume present)")
    ap.add_argument("--skip-ndm", action="store_true",
                    help="skip ndmc registration (assume already done)")
    ap.add_argument("--keep-tmp", action="store_true",
                    help="don't delete generated config tmp dir on exit")
    args = ap.parse_args()

    env = load_env(Path(args.env))
    router_ip = args.router_ip or require(env, "ROUTER_HOST")
    router_pass = require(env, "ROUTER_PASS")
    sub_url = require(env, "SUBSCRIPTION_URL")
    hc_secret = require(env, "SINGBOX_HEALTHCHECK_SECRET")

    print(f"[deploy] target: root@{router_ip}:222")

    tmp_dir = Path(os.environ.get("TMPDIR", "/tmp")) / f"keenetic-singbox-deploy.{os.getpid()}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    cfg, ndm = generate_config(tmp_dir, sub_url, hc_secret, router_ip)

    c = ssh_connect(router_ip, router_pass)
    try:
        run(c, 'mkdir -p /opt/etc/sing-box /opt/var/lib/sing-box '
               '/opt/share/sing-box/ui /opt/etc/cron.1min /opt/etc/cron.daily')

        if not args.skip_opkg:
            print("[deploy] opkg update + install (this may take a few minutes)")
            run(c, "opkg update", timeout=120)
            run(c, f"opkg install {OPKG_PACKAGES}", timeout=600)
            run(c, "/opt/etc/init.d/S10cron start 2>/dev/null || true", check=False)

        push(c, cfg, "/opt/etc/sing-box/config.json", "0644")
        write_secret_file(c, "/opt/etc/sing-box/.subscription-url", sub_url)
        for local_name, remote, mode in ROUTER_FILES:
            push(c, HERE / local_name, remote, mode)

        print("[deploy] sing-box check")
        run(c, "/opt/bin/sing-box check -C /opt/etc/sing-box/")

        print("[deploy] stopping sing-box (release kernel iface for NDM)")
        run(c, "/opt/etc/init.d/S99sing-box stop 2>/dev/null || true", check=False)
        time.sleep(2)

        if not args.skip_ndm:
            apply_ndm_setup(c, ndm)
            run(c, "ndmc -c 'system configuration save'", timeout=20)

        print("[deploy] starting sing-box")
        run(c, "/opt/etc/init.d/S99sing-box start")
        time.sleep(6)
        print("[deploy] starting healthcheck daemon")
        run(c, "/opt/etc/init.d/S99singbox-healthcheck start")

        print("\n[deploy] ===== smoke test =====")
        print(run(c, "pgrep -af sing-box | head -3"))
        print(run(c, "ip a show opkgtun0 | head -5", check=False))
        print(run(c, "netstat -tln | grep 9090 || true", check=False))

        print(f"\n[deploy] DONE — MetaCubeXD: http://{router_ip}:9090/ui/")
        print(f"[deploy] healthcheck status: ssh -p 222 root@{router_ip} "
              f"'/opt/etc/init.d/S99singbox-healthcheck status'")
    finally:
        c.close()
        if not args.keep_tmp:
            for p in tmp_dir.iterdir():
                p.unlink()
            tmp_dir.rmdir()

    return 0


if __name__ == "__main__":
    sys.exit(main())
