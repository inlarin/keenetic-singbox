"""Probe whether opkg / Entware is installed on the router.

Checks several signals:
  - `show version`            -> what firmware components are baked in
  - `show storage`            -> is a USB drive mounted (Entware lives there)
  - `show ndm service`        -> is the `opkg` service registered with ndm
  - `opkg ?`                  -> is opkg exposed as a CLI command
  - `opkg list-installed`     -> if available, dump the installed packages

Writes a transcript to kn_probe_opkg.log so we can inspect the raw output.
"""
from __future__ import annotations

from kn_common import ANY_PROMPT, KeeneticSession, build_arg_parser


def main() -> int:
    parser = build_arg_parser('Probe opkg / Entware availability on the router')
    parser.add_argument('--log', default='kn_probe_opkg.log', help='Transcript log file')
    args = parser.parse_args()

    commands = [
        'show version',
        'show storage',
        'show ndm service',
        'opkg ?',
        'opkg list-installed',
        'show running-config opkg',
    ]

    if args.dry_run:
        print(f'[dry-run] would probe opkg on {args.host}:{args.port}')
        return 0

    with open(args.log, 'w', encoding='utf-8') as log:
        with KeeneticSession(host=args.host, port=args.port, user=args.user) as kn:
            for cmd in commands:
                text = kn.run(cmd, prompt=ANY_PROMPT, timeout=15)
                log.write(f'\n===== {cmd} =====\n{text}\n')
                print(f'--- {cmd} ---')
                print(text)

    print(f'\nwrote {args.log}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
