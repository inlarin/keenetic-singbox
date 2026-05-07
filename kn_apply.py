"""Apply per-domain `ip host` and `ip route` rules for Telegram via SSTP0.

Credentials come from the ROUTER_PASS env var (or interactive getpass).
"""
from __future__ import annotations

import argparse

from kn_common import (
    KeeneticSession,
    build_arg_parser,
    is_error_output,
)

IFACE = 'SSTP0'

DOMAINS = [
    'telegram.org', 'telegram.me', 't.me', 'telesco.pe', 'telegra.ph',
    'tdesktop.com',
    'web.telegram.org', 'k.web.telegram.org', 'z.web.telegram.org', 'a.web.telegram.org',
    'venus.web.telegram.org', 'aurora.web.telegram.org', 'vesta.web.telegram.org',
    'flora.web.telegram.org', 'pluto.web.telegram.org',
    'api.telegram.org', 'core.telegram.org', 'my.telegram.org',
    'updates.tdesktop.com', 'desktop.telegram.org',
]

SUBNETS = [
    ('91.108.4.0',    '255.255.252.0'),
    ('91.108.8.0',    '255.255.252.0'),
    ('91.108.12.0',   '255.255.252.0'),
    ('91.108.16.0',   '255.255.252.0'),
    ('91.108.20.0',   '255.255.252.0'),
    ('91.108.56.0',   '255.255.252.0'),
    ('149.154.160.0', '255.255.240.0'),
]


def build_commands(iface: str) -> list[str]:
    cmds = [f'ip route {d} {iface} auto' for d in DOMAINS]
    cmds += [f'ip route {net} {mask} {iface} auto' for net, mask in SUBNETS]
    cmds.append('system configuration save')
    return cmds


def main() -> int:
    parser = build_arg_parser('Apply Telegram ip-host and ip-route rules via SSTP0')
    parser.add_argument('--iface', default=IFACE,
                        help=f'Egress interface (default: {IFACE})')
    parser.add_argument('--log', default='kn_apply.log',
                        help='Transcript log file')
    args = parser.parse_args()

    cmds = build_commands(args.iface)

    if args.dry_run:
        print(f'[dry-run] would execute {len(cmds)} commands on {args.host}:{args.port}:')
        for c in cmds:
            print(f'  > {c}')
        return 0

    ok, err = 0, 0
    with open(args.log, 'w', encoding='utf-8') as log:
        log.write(f'[+] connected to {args.host}:{args.port}\n')
        with KeeneticSession(host=args.host, port=args.port, user=args.user) as kn:
            log.write('[+] logged in\n')
            for cmd in cmds:
                text = kn.run(cmd)
                bad = is_error_output(text)
                log.write(f'\n> {cmd}\n{text}\n')
                if bad:
                    err += 1
                    print(f'[ERR] {cmd}')
                else:
                    ok += 1
                    if args.verbose:
                        print(f'[ok]  {cmd}')

            cfg = kn.run('show running-config', timeout=15)
            log.write('\n\n===== running-config =====\n')
            log.write(cfg)
            routes = [l for l in cfg.splitlines() if l.strip().startswith('ip route')]
            log.write('\n\n===== extracted ip route lines =====\n' + '\n'.join(routes) + '\n')

    print(f'\nSummary: OK={ok}, ERR={err}, routes in config={len(routes)}')
    return 0 if err == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
