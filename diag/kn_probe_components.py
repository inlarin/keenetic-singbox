"""Dump the full firmware components catalogue from the router.

`(config)> components list` lists every component the firmware sandbox
can offer, with installed/available state. We grep the output for opkg
to see if the router can even install OPKG, plus storage/usb/ssh entries
to confirm what's already in place.
"""
from __future__ import annotations

from kn_common import ANY_PROMPT, KeeneticSession, build_arg_parser


def main() -> int:
    parser = build_arg_parser('Dump firmware components catalogue')
    parser.add_argument('--log', default='kn_probe_components.log')
    args = parser.parse_args()

    if args.dry_run:
        print(f'[dry-run] would list components on {args.host}:{args.port}')
        return 0

    with open(args.log, 'w', encoding='utf-8') as log, \
         KeeneticSession(host=args.host, port=args.port, user=args.user) as kn:
        # Drop into the components sub-config so the subcommands are bare.
        kn.run('components', prompt=ANY_PROMPT, timeout=10)
        for cmd in ('list', 'preview', 'preset'):
            text = kn.run(cmd, prompt=ANY_PROMPT, timeout=30)
            log.write(f'\n===== components {cmd} =====\n{text}\n')

    with open(args.log, 'r', encoding='utf-8') as log:
        content = log.read()

    # Surface lines that mention opkg/storage/ssh/usb/ext.
    print('--- lines mentioning opkg / ssh / storage / usb / ext ---')
    keywords = ('opkg', 'storage', 'ssh', 'usb', 'ext')
    for line in content.splitlines():
        low = line.lower()
        if any(k in low for k in keywords):
            print(line)

    print(f'\nfull transcript: {args.log}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
