"""One-shot: push a fixed list of `ip host` + `ip route` commands for
Telegram (legacy SSTP-routing approach, before FQDN object groups).

This is the original quick-and-dirty pusher; for the cleaner structured
version see kn_apply.py. Kept because it prints per-command verify output.

Usage:
    python kn_telnet.py --iface <vpn-iface-name>

The iface name is your SSTP/IPSec/wireguard tunnel as it appears in
`show interface` (e.g. `sstp-mytunnel`).
"""
from __future__ import annotations

import sys

from kn_common import KeeneticSession, build_arg_parser, is_error_output


def build_commands(iface: str) -> list[str]:
    return [
        'more off',
        f'ip host telegram.org {iface}',
        f'ip host telegram.me {iface}',
        f'ip host t.me {iface}',
        f'ip host telesco.pe {iface}',
        f'ip host telegra.ph {iface}',
        f'ip host tdesktop.com {iface}',
        f'ip host web.telegram.org {iface}',
        f'ip host k.web.telegram.org {iface}',
        f'ip host z.web.telegram.org {iface}',
        f'ip host a.web.telegram.org {iface}',
        f'ip host venus.web.telegram.org {iface}',
        f'ip host aurora.web.telegram.org {iface}',
        f'ip host vesta.web.telegram.org {iface}',
        f'ip host flora.web.telegram.org {iface}',
        f'ip host pluto.web.telegram.org {iface}',
        f'ip host api.telegram.org {iface}',
        f'ip host core.telegram.org {iface}',
        f'ip host my.telegram.org {iface}',
        f'ip host updates.tdesktop.com {iface}',
        f'ip host desktop.telegram.org {iface}',
        f'ip route 91.108.4.0/22 {iface} auto',
        f'ip route 91.108.8.0/22 {iface} auto',
        f'ip route 91.108.12.0/22 {iface} auto',
        f'ip route 91.108.16.0/22 {iface} auto',
        f'ip route 91.108.20.0/22 {iface} auto',
        f'ip route 91.108.56.0/22 {iface} auto',
        f'ip route 149.154.160.0/20 {iface} auto',
        'system configuration save',
    ]


def main() -> int:
    parser = build_arg_parser('Push legacy Telegram routing commands via a tunnel iface')
    parser.add_argument('--iface', required=True,
                        help='NDM-side tunnel interface name (e.g. sstp-foo)')
    args = parser.parse_args()

    commands = build_commands(args.iface)

    if args.dry_run:
        print(f'[dry-run] would execute {len(commands)} commands on {args.host}:{args.port}:')
        for c in commands:
            print(f'  > {c}')
        return 0

    errors = 0
    with KeeneticSession(host=args.host, port=args.port, user=args.user) as kn:
        print('[+] connected', flush=True)
        for cmd in commands:
            text = kn.run(cmd)
            bad = is_error_output(text)
            marker = 'ERR' if bad else 'OK'
            print(f'[{marker}] {cmd}')
            if bad:
                errors += 1
                print('      >>>', text.strip()[-200:])

        verify = kn.run(f'show ip route | include {args.iface}')
        print(f'\n--- show ip route ({args.iface}) ---')
        print(verify)

    print('[+] done')
    return 0 if errors == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
