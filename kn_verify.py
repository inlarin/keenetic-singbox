"""Probe Keenetic's FQDN object-group / dns-proxy route syntax.

Creates a test group, adds a domain, binds it, shows the running-config,
then cleans up. Used to verify whether a firmware version supports the
dns-proxy route feature at all.
"""
from __future__ import annotations

from kn_common import ANY_PROMPT, KeeneticSession, build_arg_parser


def main() -> int:
    parser = build_arg_parser('Probe dns-proxy route syntax support')
    parser.add_argument('--log', default='kn_verify.log', help='Transcript log file')
    args = parser.parse_args()

    if args.dry_run:
        print(f'[dry-run] would probe dns-proxy syntax on {args.host}:{args.port}')
        return 0

    commands = [
        # Probe: help
        'dns-proxy route ?',
        'dns-proxy route object-group ?',
        # Test: create group, add domain, link to SSTP0
        'object-group fqdn probe_test',
        'include example.com',
        'exit',
        'dns-proxy route object-group probe_test SSTP0 auto',
        # Verify
        'show running-config',
        # Cleanup
        'no dns-proxy route object-group probe_test',
        'no object-group fqdn probe_test',
        'show running-config',
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
