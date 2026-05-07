"""Remove stray test artifacts from running-config.

Historical bug: the CLI once produced ACLs named `?` (probably from
someone pressing ? while the router was expecting a name). This script
evicts those, plus the `tg_test` probe group.
"""
from __future__ import annotations

from kn_common import ANY_PROMPT, KeeneticSession, build_arg_parser


def main() -> int:
    parser = build_arg_parser('Remove stray test object-groups and malformed ACLs')
    parser.add_argument('--log', default='kn_cleanup.log', help='Transcript log file')
    args = parser.parse_args()

    commands = [
        'no object-group fqdn tg_test',
        'no access-list ?',
        'no access-list \\?',
        'show running-config | grep route',
        'show ip route',
        'system configuration save',
    ]

    if args.dry_run:
        print(f'[dry-run] would run {len(commands)} cleanup commands on {args.host}:{args.port}')
        for c in commands:
            print(f'  > {c}')
        return 0

    with open(args.log, 'w', encoding='utf-8') as log:
        with KeeneticSession(host=args.host, port=args.port, user=args.user) as kn:
            for cmd in commands:
                text = kn.run(cmd, prompt=ANY_PROMPT)
                log.write(f'\n===== {cmd} =====\n{text}\n')

    print('done')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
