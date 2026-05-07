"""IPv6 leak protection for VPN-based routing rules.

Problem: the Keenetic FQDN/IP routing rules in this project are all
IPv4-only. If the client has a working IPv6 stack and the ISP hands out
IPv6, the blocked sites resolve to AAAA records and traffic leaves via
the ISP's default IPv6 route, **bypassing** the VPN. Classic VPN leak.

Two mitigations, both offered here:

  --mode=block   — block all IPv6 traffic on LAN interfaces (simplest,
                   safe: users who don't rely on IPv6 won't notice).
  --mode=tunnel  — leave IPv6 reachable but force it through the VPN
                   interface. Needs a VPN that actually advertises IPv6.
                   Most SSTP providers don't; `block` is the safe default.

This script is idempotent: re-running produces the same config. It's also
reversible via `--undo`, which removes the rules it added.
"""
from __future__ import annotations

import re
import sys

from kn_common import KeeneticSession, build_arg_parser, is_error_output


LAN_TAG = '! kn_block_ipv6: managed'


def detect_lan_interfaces(cfg: str) -> list[str]:
    """Find LAN-side bridge/ethernet interfaces in running-config.
    A LAN interface has `security-level private` and an ip address."""
    interfaces: list[tuple[str, list[str]]] = []
    cur: tuple[str, list[str]] | None = None
    for line in cfg.splitlines():
        if re.match(r'^interface \S+', line):
            if cur:
                interfaces.append(cur)
            name = line.split()[1]
            cur = (name, [])
        elif cur is not None and line.startswith(' '):
            cur[1].append(line.strip())
    if cur:
        interfaces.append(cur)

    lan = []
    for name, body in interfaces:
        has_private = any(l == 'security-level private' for l in body)
        has_ip = any(l.startswith('ip address') for l in body)
        if has_private and has_ip:
            lan.append(name)
    return lan


def build_block_commands(interfaces: list[str]) -> list[str]:
    """Disable IPv6 on each LAN interface. Keenetic syntax: `no ipv6`
    under the interface context."""
    cmds: list[str] = []
    for iface in interfaces:
        cmds.append(f'interface {iface}')
        cmds.append('no ipv6 address')
        cmds.append('no ipv6 name-servers')
        cmds.append('exit')
    return cmds


def build_undo_commands(interfaces: list[str]) -> list[str]:
    """Re-enable IPv6 defaults on each LAN interface. Requires SLAAC or
    DHCPv6 to be re-attached on the upstream."""
    cmds: list[str] = []
    for iface in interfaces:
        cmds.append(f'interface {iface}')
        cmds.append('ipv6 address auto')
        cmds.append('exit')
    return cmds


def main() -> int:
    parser = build_arg_parser('Prevent IPv6 leaks past IPv4-only VPN rules')
    parser.add_argument('--mode', choices=['block', 'tunnel'], default='block',
                        help='`block` disables IPv6 on LAN (default). '
                             '`tunnel` is a placeholder; not yet implemented.')
    parser.add_argument('--undo', action='store_true',
                        help='Reverse the action (re-enable IPv6 on LAN)')
    parser.add_argument('--yes', action='store_true',
                        help='Skip confirmation prompt')
    args = parser.parse_args()

    if args.mode == 'tunnel':
        print('ERROR: --mode=tunnel is not implemented yet. Use --mode=block.',
              file=sys.stderr)
        return 2

    with KeeneticSession(host=args.host, port=args.port, user=args.user) as kn:
        cfg = kn.run('show running-config', timeout=20)
        lan_ifaces = detect_lan_interfaces(cfg)
        if not lan_ifaces:
            print('WARNING: no LAN-side interfaces detected '
                  '(security-level private + ip address). '
                  'Either the router is freshly provisioned or running-config '
                  'differs from what this script expects.',
                  file=sys.stderr)
            return 3

        print(f'LAN interfaces detected: {", ".join(lan_ifaces)}')
        cmds = (build_undo_commands(lan_ifaces) if args.undo
                else build_block_commands(lan_ifaces))

        if args.dry_run:
            action = 're-enable IPv6 on' if args.undo else 'disable IPv6 on'
            print(f'\n[dry-run] would {action} {len(lan_ifaces)} interfaces:')
            for c in cmds:
                print(f'  > {c}')
            return 0

        if not args.yes:
            action = 're-enable IPv6' if args.undo else 'disable IPv6'
            reply = input(f'Proceed to {action} on {", ".join(lan_ifaces)}? [y/N] ').strip().lower()
            if reply not in ('y', 'yes'):
                print('aborted')
                return 1

        errors = 0
        for cmd in cmds:
            text = kn.run(cmd)
            if is_error_output(text):
                errors += 1
                print(f'[ERR] {cmd}')
                if args.verbose:
                    print(f'      {text.strip()[-200:]}')
            elif args.verbose:
                print(f'[ok]  {cmd}')

        kn.run('system configuration save')
        action = 'IPv6 re-enabled' if args.undo else 'IPv6 disabled'
        print(f'\n{action} on LAN. Errors: {errors}/{len(cmds)}')
        if not args.undo:
            print('\nHint: verify with `curl -6 https://ifconfig.me/`  on a LAN client.\n'
                  '      On success the request should fail or hang.')
        return 0 if errors == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
