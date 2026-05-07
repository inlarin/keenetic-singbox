"""One-shot: push a fixed list of `ip host` + `ip route` commands.

This is the original quick-and-dirty pusher; for the cleaner structured
version see kn_apply.py. Kept because it prints per-command verify output.
"""
from __future__ import annotations

import sys

from kn_common import KeeneticSession, build_arg_parser, is_error_output

COMMANDS = [
    'more off',
    'ip host telegram.org sstp-redacted',
    'ip host telegram.me sstp-redacted',
    'ip host t.me sstp-redacted',
    'ip host telesco.pe sstp-redacted',
    'ip host telegra.ph sstp-redacted',
    'ip host tdesktop.com sstp-redacted',
    'ip host web.telegram.org sstp-redacted',
    'ip host k.web.telegram.org sstp-redacted',
    'ip host z.web.telegram.org sstp-redacted',
    'ip host a.web.telegram.org sstp-redacted',
    'ip host venus.web.telegram.org sstp-redacted',
    'ip host aurora.web.telegram.org sstp-redacted',
    'ip host vesta.web.telegram.org sstp-redacted',
    'ip host flora.web.telegram.org sstp-redacted',
    'ip host pluto.web.telegram.org sstp-redacted',
    'ip host api.telegram.org sstp-redacted',
    'ip host core.telegram.org sstp-redacted',
    'ip host my.telegram.org sstp-redacted',
    'ip host updates.tdesktop.com sstp-redacted',
    'ip host desktop.telegram.org sstp-redacted',
    'ip route 91.108.4.0/22 sstp-redacted auto',
    'ip route 91.108.8.0/22 sstp-redacted auto',
    'ip route 91.108.12.0/22 sstp-redacted auto',
    'ip route 91.108.16.0/22 sstp-redacted auto',
    'ip route 91.108.20.0/22 sstp-redacted auto',
    'ip route 91.108.56.0/22 sstp-redacted auto',
    'ip route 149.154.160.0/20 sstp-redacted auto',
    'system configuration save',
]


def main() -> int:
    parser = build_arg_parser('Push legacy Telegram routing commands via sstp-redacted')
    args = parser.parse_args()

    if args.dry_run:
        print(f'[dry-run] would execute {len(COMMANDS)} commands on {args.host}:{args.port}:')
        for c in COMMANDS:
            print(f'  > {c}')
        return 0

    errors = 0
    with KeeneticSession(host=args.host, port=args.port, user=args.user) as kn:
        print('[+] connected', flush=True)
        for cmd in COMMANDS:
            text = kn.run(cmd)
            bad = is_error_output(text)
            marker = 'ERR' if bad else 'OK'
            print(f'[{marker}] {cmd}')
            if bad:
                errors += 1
                print('      >>>', text.strip()[-200:])

        verify = kn.run('show ip route | include sstp-redacted')
        print('\n--- show ip route (sstp-redacted) ---')
        print(verify)

    print('[+] done')
    return 0 if errors == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
