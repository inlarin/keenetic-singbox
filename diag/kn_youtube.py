"""Create a `youtube` FQDN group and bind it via dns-proxy route."""
from __future__ import annotations

import re

from kn_common import KeeneticSession, build_arg_parser, is_error_output

IFACE = 'SSTP0'
GROUP = 'youtube'
YT_DOMAINS = [
    'youtube.com',
    'youtu.be',
    'ytimg.com',
    'googlevideo.com',
    'youtubei.googleapis.com',
    'ggpht.com',
    'googleusercontent.com',
]


def main() -> int:
    parser = build_arg_parser('Create a YouTube FQDN group and bind via dns-proxy route')
    parser.add_argument('--iface', default=IFACE, help=f'Egress interface (default: {IFACE})')
    parser.add_argument('--group', default=GROUP, help=f'Group name (default: {GROUP})')
    parser.add_argument('--log', default='kn_youtube.log', help='Transcript log file')
    args = parser.parse_args()

    if args.dry_run:
        print(f'[dry-run] would create object-group fqdn {args.group} '
              f'with {len(YT_DOMAINS)} domains, bind to {args.iface}')
        for d in YT_DOMAINS:
            print(f'  include {d}')
        return 0

    ok = 0
    with open(args.log, 'w', encoding='utf-8') as log:
        with KeeneticSession(host=args.host, port=args.port, user=args.user) as kn:
            kn.run(f'object-group fqdn {args.group}')
            for d in YT_DOMAINS:
                text = kn.run(f'include {d}')
                log.write(f'\n> include {d}\n{text}\n')
                if is_error_output(text):
                    print(f'  [ERR] include {d}')
                else:
                    ok += 1
            kn.run('exit')
            text = kn.run(f'dns-proxy route object-group {args.group} {args.iface} auto')
            log.write(f'\n> dns-proxy route ...\n{text}\n')
            bound = 'added DNS route' in text or 'Dns::Route' in text
            kn.run('system configuration save')

            cfg = kn.run('show running-config', timeout=15)
            log.write('\n\n===== running-config =====\n')
            log.write(cfg)

    gblock = re.search(rf'object-group fqdn {re.escape(args.group)}\s*\n(.*?)(?=^!|\Z)',
                       cfg, re.DOTALL | re.MULTILINE)
    routes = re.findall(r'route object-group \S+ \S+.*', cfg)

    print(f'\n{args.group}: {ok}/{len(YT_DOMAINS)} domains added, bound to {args.iface}: {bound}')
    if gblock:
        print('--- group contents in config ---')
        print(gblock.group(0))
    print('\n--- all dns-proxy routes ---')
    for r in routes:
        print('  ' + r.strip())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
