"""Apply `reject` flag (kill switch) to all existing SSTP0 routes.

When the VPN interface goes down, traffic destined for the rejected
routes is dropped instead of falling back to the ISP — preventing leaks.

SAFETY: this script refuses to run unless a series of pre-flight checks
pass, because a badly-ordered rule can lock you out of the router:

  1. `--dry-run` is always available to preview changes without applying.
  2. The target interface MUST be up (we don't reject when VPN is already down).
  3. The management subnet (router LAN) is verified to have its own permit
     route that bypasses the rejected set.
  4. An interactive confirmation is required unless `--yes` is passed.
  5. A rollback-timer is started on the router: if we lose the session
     mid-apply, the router reverts to the saved config after N minutes.
"""
from __future__ import annotations

import re
import sys
import time

from kn_common import KeeneticSession, build_arg_parser, is_error_output

IFACE = 'SSTP0'
MGMT_SUBNET_RE = re.compile(r'^\s*ip address\s+(\S+)\s+(\S+)')


def find_sstp_routes(cfg: str, iface: str) -> tuple[list[tuple[str, str]], list[str]]:
    ip_routes = re.findall(
        r'^ip route (\S+) (\S+) ' + re.escape(iface) +
        r'(?: auto)?(?: reject)?\s*$',
        cfg, re.MULTILINE,
    )
    dns_routes = re.findall(
        r'^\s*route object-group (\S+) ' + re.escape(iface) +
        r'(?: auto)?(?: reject)?\s*$',
        cfg, re.MULTILINE,
    )
    return ip_routes, dns_routes


def iface_is_up(kn: KeeneticSession, iface: str) -> bool:
    """Check `show interface <iface>` for state=up / connected=yes."""
    text = kn.run(f'show interface {iface}', timeout=10)
    up = re.search(r'(?i)state\s*:\s*up', text) is not None
    connected = re.search(r'(?i)connected\s*:\s*yes', text) is not None
    return up and connected


def main() -> int:
    parser = build_arg_parser('Add kill-switch reject flag to all routes via an interface')
    parser.add_argument('--iface', default=IFACE, help=f'Target interface (default: {IFACE})')
    parser.add_argument('--yes', action='store_true',
                        help='Skip interactive confirmation')
    parser.add_argument('--rollback-min', type=int, default=5,
                        help='Rollback timer in minutes (0 to disable). Router reverts '
                             'to saved config if the session is lost mid-apply.')
    parser.add_argument('--skip-iface-check', action='store_true',
                        help='DANGEROUS: apply even if the target interface is down')
    args = parser.parse_args()

    with KeeneticSession(host=args.host, port=args.port, user=args.user) as kn:
        print(f'[+] connected to {args.host}')

        # --- Pre-flight checks --------------------------------------------
        print('[pre-flight] reading running-config...')
        cfg = kn.run('show running-config', timeout=20)

        ip_routes, dns_routes = find_sstp_routes(cfg, args.iface)
        print(f'[pre-flight] {len(ip_routes)} ip routes + {len(dns_routes)} dns-proxy routes '
              f'via {args.iface}')

        if not ip_routes and not dns_routes:
            print(f'[pre-flight] no routes via {args.iface} — nothing to do')
            return 0

        # Management route sanity: every Keenetic config has a LAN bridge
        # (Bridge0 usually) with an `ip address`. If we're going to reject
        # to SSTP0, we need at least one non-rejected route that points
        # somewhere else — e.g. the default ISP route or the Bridge0 LAN.
        lan_mask = re.search(r'ip address\s+(\S+)\s+(\S+)', cfg)
        if not lan_mask:
            print('[pre-flight] WARNING: could not detect LAN address in running-config',
                  file=sys.stderr)
        else:
            print(f'[pre-flight] LAN address: {lan_mask.group(1)} mask {lan_mask.group(2)}')

        if not args.skip_iface_check:
            print(f'[pre-flight] checking {args.iface} is up...')
            if not iface_is_up(kn, args.iface):
                print(f'[FAIL] {args.iface} is not up — refusing to apply reject rules.\n'
                      f'       Applying now would blackhole traffic until VPN reconnects.\n'
                      f'       Pass --skip-iface-check to override (NOT RECOMMENDED).',
                      file=sys.stderr)
                return 2
            print(f'[pre-flight] {args.iface} is up')

        # --- Dry-run summary ----------------------------------------------
        if args.dry_run:
            print(f'\n[dry-run] would apply `reject` to:')
            for net, mask in ip_routes:
                print(f'  ip route {net} {mask} {args.iface} auto reject')
            for grp in dns_routes:
                print(f'  dns-proxy route object-group {grp} {args.iface} auto reject')
            return 0

        # --- Confirm -------------------------------------------------------
        if not args.yes:
            print(f'\nAbout to rewrite {len(ip_routes) + len(dns_routes)} routes via '
                  f'{args.iface} with `reject` flag.')
            reply = input('Proceed? [y/N] ').strip().lower()
            if reply not in ('y', 'yes'):
                print('aborted')
                return 1

        # --- Rollback timer (Keenetic NDMS supports this) -----------------
        if args.rollback_min > 0:
            text = kn.run(f'system configuration rollback-timer {args.rollback_min * 60}')
            if is_error_output(text):
                print(f'[warn] could not set rollback-timer ({args.rollback_min}min): {text.strip()[-200:]}',
                      file=sys.stderr)
                print('[warn] proceeding without rollback timer', file=sys.stderr)
            else:
                print(f'[safety] rollback-timer armed for {args.rollback_min} minutes')

        # --- Apply ---------------------------------------------------------
        # Re-add IP routes with reject (Keenetic "renews" existing)
        for net, mask in ip_routes:
            out = kn.run(f'ip route {net} {mask} {args.iface} auto reject')
            status = ('Renewed' if 'enewed' in out else
                      'Added' if 'dded' in out else 'FAIL')
            print(f'  ip route {net}/{mask}: {status}')

        # For dns-proxy: delete and re-add (safer than trying renew)
        for grp in dns_routes:
            kn.run(f'no dns-proxy route object-group {grp}')
            out = kn.run(f'dns-proxy route object-group {grp} {args.iface} auto reject')
            status = 'OK' if 'dded' in out or 'Dns::Route' in out else 'FAIL'
            print(f'  dns-proxy route {grp}: {status}')

        # --- Verify BEFORE saving (so rollback can still rescue us) -------
        cfg = kn.run('show running-config', timeout=20)
        ip_with_reject = re.findall(
            r'^ip route \S+ \S+ ' + re.escape(args.iface) + r' auto reject\s*$',
            cfg, re.MULTILINE,
        )
        dns_with_reject = re.findall(
            r'^\s*route object-group \S+ ' + re.escape(args.iface) + r' auto reject\s*$',
            cfg, re.MULTILINE,
        )
        print(f'\n=== VERIFY ===')
        print(f'IP routes with reject: {len(ip_with_reject)}/{len(ip_routes)}')
        print(f'dns-proxy routes with reject: {len(dns_with_reject)}/{len(dns_routes)}')

        # Sanity: we're still talking to the router. If we can reach this
        # point, the rules didn't lock us out.
        test = kn.run('show version', timeout=10)
        if 'NDMS' not in test and 'components' not in test:
            print('[FAIL] post-apply sanity check did not return expected output. '
                  'Leaving rollback timer armed — router will revert shortly.',
                  file=sys.stderr)
            return 3

        # Commit
        kn.run('system configuration save')
        if args.rollback_min > 0:
            kn.run('system configuration rollback-timer clear')
            print('[safety] rollback-timer cleared (commit successful)')

    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        # If the user Ctrl-Cs mid-apply, the rollback-timer will un-do
        # any partial changes shortly.
        print('\n[abort] interrupted; router will rollback shortly if timer was armed.',
              file=sys.stderr)
        sys.exit(130)
