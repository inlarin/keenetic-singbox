"""Initial CLI reconnaissance: dump version, interfaces, and help trees.

Used once to survey what commands the router exposes.
"""
from __future__ import annotations

from kn_common import ANY_PROMPT, KeeneticSession, build_arg_parser


def main() -> int:
    parser = build_arg_parser('Dump router version/interfaces/help trees')
    parser.add_argument('--log', default='kn_probe.log', help='Transcript log file')
    args = parser.parse_args()

    commands = [
        'show version',
        'show interface',
        'show running-config',
        'ip ?',
        'ip route ?',
        'ip host ?',
        'show interface Sstp0',
        'show interface SstpVpn0',
    ]

    if args.dry_run:
        print(f'[dry-run] would run {len(commands)} recon commands on {args.host}:{args.port}')
        return 0

    with open(args.log, 'w', encoding='utf-8') as log:
        with KeeneticSession(host=args.host, port=args.port, user=args.user) as kn:
            for cmd in commands:
                text = kn.run(cmd, prompt=ANY_PROMPT, timeout=15)
                log.write(f'\n===== {cmd} =====\n{text}\n')

    print(f'wrote {args.log}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
