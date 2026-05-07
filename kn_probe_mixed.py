"""Probe: can an `object-group fqdn` contain IP subnets/addresses, and does
`dns-proxy route` pick them up like regular FQDN entries?

Useful for unifying IP + FQDN rules into a single group.
"""
from __future__ import annotations

import re

from kn_common import KeeneticSession, build_arg_parser


def main() -> int:
    parser = build_arg_parser('Probe whether object-group fqdn accepts IP subnets')
    parser.add_argument('--log', default='kn_probe_mixed.log', help='Transcript log file')
    args = parser.parse_args()

    if args.dry_run:
        print(f'[dry-run] would probe mixed IP/FQDN object-group on {args.host}:{args.port}')
        return 0

    probe_commands = [
        'object-group fqdn probe_mixed',
        'include example.com',
        'include 198.51.100.0/24',
        'include 198.51.101.5',
        'exit',
        'dns-proxy route object-group probe_mixed SSTP0 auto reject',
    ]

    cleanup_commands = [
        'no dns-proxy route object-group probe_mixed',
        'no object-group fqdn probe_mixed',
        'system configuration save',
    ]

    with open(args.log, 'w', encoding='utf-8') as log:
        with KeeneticSession(host=args.host, port=args.port, user=args.user) as kn:
            for cmd in probe_commands:
                text = kn.run(cmd)
                log.write(f'\n> {cmd}\n{text}\n')

            cfg = kn.run('show running-config', timeout=15)
            log.write(f'\n> show running-config\n{cfg}\n')
            block = re.search(r'object-group fqdn probe_mixed.*?(?=^!|\Z)',
                              cfg, re.MULTILINE | re.DOTALL)
            if block:
                log.write('\n=== BLOCK IN RUNNING-CONFIG ===\n' + block.group(0))

            rtr = kn.run('show ip route', timeout=15)
            for line in rtr.splitlines():
                if '198.51.100' in line or '198.51.101' in line or 'probe_mixed' in line:
                    log.write(f'ROUTE TABLE: {line!r}\n')

            for cmd in cleanup_commands:
                kn.run(cmd)

    print('done -> ' + args.log)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
