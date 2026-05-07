"""Probe: try `reject` / `exclusive` variants of `ip route` to find the
syntax that Keenetic's CLI actually persists.

Result (historically): `ip route <net> <mask> <iface> auto reject` is the
supported form. Used by kn_reject_all.py.
"""
from __future__ import annotations

from kn_common import ANY_PROMPT, KeeneticSession, build_arg_parser


def main() -> int:
    parser = build_arg_parser('Probe ip route reject/exclusive syntax')
    parser.add_argument('--log', default='kn_probe_exclusive.log',
                        help='Transcript log file')
    args = parser.parse_args()

    if args.dry_run:
        print(f'[dry-run] would probe ip route reject syntax on {args.host}:{args.port}')
        return 0

    commands = [
        # Help
        'ip route ?',
        # Try reject syntax variants
        'ip route 198.51.100.0 255.255.255.0 SSTP0 auto reject',
        'ip route 198.51.100.0 255.255.255.0 SSTP0 reject',
        'ip route 198.51.100.0 255.255.255.0 reject',
        'ip route 198.51.100.0 255.255.255.0 SSTP0 auto exclusive',
        # Check how UI saves it
        'show running-config | grep 198.51.100',
        'show ip route | grep 198.51',
        # Try exclusive-related tokens in policy context
        'ip policy EXTEST',
        '?',
        'standalone',
        '?',
        'exit',
        'no ip policy EXTEST',
        # Cleanup any stray test routes
        'no ip route 198.51.100.0 255.255.255.0 SSTP0',
        'no ip route 198.51.100.0 255.255.255.0',
        # dns-proxy route template
        'dns-proxy route ?',
    ]

    with open(args.log, 'w', encoding='utf-8') as log:
        with KeeneticSession(host=args.host, port=args.port, user=args.user) as kn:
            for cmd in commands:
                text = kn.run(cmd, prompt=ANY_PROMPT)
                log.write(f'\n===== {cmd} =====\n{text}\n')

    print('done')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
