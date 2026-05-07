"""STEP 2 of Entware install: persist opkg config + reboot, then wait for the router back.

Sequence:
  1. telnet → `system configuration save`
  2. telnet → `system reboot`
  3. sleep, then poll TCP 23 (telnet) until the router answers again
  4. confirm with `show version`
  5. probe TCP 222 (dropbear) — Entware should have started its SSH

Steps 1-2 invalidate the existing socket; expect the post-reboot login
to fail a few times before the daemon is back up.
"""
from __future__ import annotations

import socket
import time

from kn_common import (
    ANY_PROMPT, KeeneticSession, build_arg_parser, connect, login, send,
    read_until,
)

REBOOT_GRACE_SEC = 60          # wait this long before first reconnect attempt
RECONNECT_DEADLINE_SEC = 240   # give up after this many seconds total
RECONNECT_INTERVAL_SEC = 5


def _try_telnet(host: str, port: int, timeout: float = 4.0) -> bool:
    """One-shot TCP probe — open + close. True if connection accepted."""
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass
    return True


def main() -> int:
    parser = build_arg_parser('Save running-config and reboot the router; then wait for it back')
    args = parser.parse_args()

    if args.dry_run:
        print(f'[dry-run] would save+reboot {args.host}:{args.port} and wait')
        return 0

    print(f'[1/4] save + reboot via telnet {args.host}:{args.port}')
    try:
        with KeeneticSession(host=args.host, port=args.port, user=args.user) as kn:
            out = kn.run('system configuration save', prompt=ANY_PROMPT, timeout=20)
            print(f'  save: {out.strip().splitlines()[-1] if out.strip() else "(no response)"}')
            # Send reboot but DON'T wait for prompt — the router won't return one.
            send(kn.sock, 'system reboot')
            try:
                # Read for up to 5s: the router usually emits "Rebooting..." or
                # similar before tearing down the socket.
                bye = read_until(kn.sock, ANY_PROMPT, timeout=5).decode('utf-8', 'replace')
                if bye.strip():
                    print(f'  reboot: {bye.strip().splitlines()[-1][:120]}')
            except Exception:
                pass
    except Exception as e:
        # The reboot kicks the connection — graceful exit may raise. Fine.
        print(f'  (session ended: {type(e).__name__})')

    print(f'\n[2/4] sleep {REBOOT_GRACE_SEC}s before first reconnect attempt')
    time.sleep(REBOOT_GRACE_SEC)

    print(f'\n[3/4] poll telnet {args.host}:23 every {RECONNECT_INTERVAL_SEC}s '
          f'(deadline {RECONNECT_DEADLINE_SEC}s)')
    deadline = time.time() + RECONNECT_DEADLINE_SEC
    came_back = False
    while time.time() < deadline:
        if _try_telnet(args.host, 23):
            elapsed = REBOOT_GRACE_SEC + (RECONNECT_DEADLINE_SEC - (deadline - time.time()))
            print(f'  telnet up ({elapsed:.0f}s after reboot was issued)')
            came_back = True
            break
        time.sleep(RECONNECT_INTERVAL_SEC)
    if not came_back:
        print('  TIMEOUT — router did not come back. Check physical state.')
        return 2

    # Give NDM a moment to finish bringing services up before logging in.
    time.sleep(5)

    print('\n[4/4] verify state via fresh login')
    with KeeneticSession(host=args.host, port=args.port, user=args.user) as kn:
        ver = kn.run('show version', prompt=ANY_PROMPT, timeout=15)
        cfg = kn.run('show running-config', prompt=ANY_PROMPT, timeout=20)
    components_line = next((ln for ln in ver.splitlines() if 'components:' in ln), '')
    print(f'  components: {components_line.strip()[:200]}')
    opkg_lines = [ln for ln in cfg.splitlines() if 'opkg' in ln.lower()]
    print('  opkg lines in running-config:')
    for ln in opkg_lines:
        print(f'    {ln.strip()}')
    if not opkg_lines:
        print('    (none — save did NOT persist; investigate)')

    # Probe dropbear
    print('\n[5/5] probe dropbear on tcp/222')
    if _try_telnet(args.host, 222, timeout=4):
        print('  port 222 OPEN — dropbear is up')
    else:
        print('  port 222 closed — dropbear not yet started, may need more time or '
              'manual start. Try `plink -ssh -P 222 root@192.168.1.1` in a minute.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
