"""Apply FQDN object-groups for Claude and Telegram, bind them to an interface.

Creates two object-groups: `claude_ai` and `telegram_aux`. Each is bound
to the egress interface via `dns-proxy route`.
"""
from __future__ import annotations

import re

from kn_common import KeeneticSession, build_arg_parser, is_error_output, validate_fqdns

IFACE = 'SSTP0'

CLAUDE_DOMAINS = [
    # Core
    'claude.ai',
    'api.anthropic.com',
    'console.anthropic.com',
    'claudeusercontent.com',
    # Subdomains of claude.ai
    'www.claude.ai',
    'cdn.claude.ai',
    'assets.claude.ai',
    # Corporate
    'anthropic.com',
    'www.anthropic.com',
    'docs.anthropic.com',
    'support.anthropic.com',
    'status.anthropic.com',
]

TELEGRAM_DOMAINS = [
    't.me', 'telegram.org', 'telegram.me', 'telesco.pe', 'telegra.ph',
    'tdesktop.com',
    'web.telegram.org', 'k.web.telegram.org', 'z.web.telegram.org', 'a.web.telegram.org',
    'venus.web.telegram.org', 'aurora.web.telegram.org', 'vesta.web.telegram.org',
    'flora.web.telegram.org', 'pluto.web.telegram.org',
    'api.telegram.org', 'core.telegram.org', 'my.telegram.org',
    'updates.tdesktop.com', 'desktop.telegram.org',
]

GROUPS = [
    ('claude_ai', CLAUDE_DOMAINS),
    ('telegram_aux', TELEGRAM_DOMAINS),
]


def apply_group(kn: KeeneticSession, log_file, name: str,
                domains: list[str], iface: str) -> tuple[int, int, bool]:
    # Pre-validate so a single typo doesn't poison the whole group.
    # Invalid entries are logged and skipped, not sent to the router.
    good, bad = validate_fqdns(domains)
    for b in bad:
        log_file.write(f'\n[SKIP-invalid-fqdn] {b!r}\n')
        print(f'  [SKIP] invalid FQDN: {b!r}')

    ok_inc, err_inc = 0, len(bad)
    kn.run(f'object-group fqdn {name}')
    for d in good:
        text = kn.run(f'include {d}')
        log_file.write(f'\n> include {d}\n{text}\n')
        if is_error_output(text):
            err_inc += 1
            print(f'  [ERR] include {d}')
        else:
            ok_inc += 1
    kn.run('exit')
    text = kn.run(f'dns-proxy route object-group {name} {iface} auto')
    log_file.write(f'\n> dns-proxy route ...\n{text}\n')
    bound_ok = 'added DNS route' in text or 'Dns::Route' in text
    return ok_inc, err_inc, bound_ok


def main() -> int:
    parser = build_arg_parser('Apply Claude + Telegram FQDN groups and bind to interface')
    parser.add_argument('--iface', default=IFACE, help=f'Egress interface (default: {IFACE})')
    parser.add_argument('--log', default='kn_fqdn.log', help='Transcript log file')
    args = parser.parse_args()

    if args.dry_run:
        print(f'[dry-run] would create {len(GROUPS)} object-groups on {args.host}:{args.port}:')
        for name, doms in GROUPS:
            print(f'  object-group fqdn {name}   ({len(doms)} domains)')
            print(f'  dns-proxy route object-group {name} {args.iface} auto')
        return 0

    summary: list[tuple[str, int, int, int, bool]] = []
    with open(args.log, 'w', encoding='utf-8') as log:
        with KeeneticSession(host=args.host, port=args.port, user=args.user) as kn:
            for name, domains in GROUPS:
                ok_inc, err_inc, bound = apply_group(kn, log, name, domains, args.iface)
                summary.append((name, len(domains), ok_inc, err_inc, bound))
                print(f'[group {name}] domains added: {ok_inc}/{len(domains)}, '
                      f'bound to {args.iface}: {bound}')

            kn.run('system configuration save')

            cfg = kn.run('show running-config', timeout=15)
            groups = re.findall(r'object-group fqdn (\S+)', cfg)
            dns_routes = re.findall(r'route object-group \S+ \S+.*', cfg)
            log.write('\n\n===== detected groups =====\n' + str(groups))
            log.write('\n\n===== detected dns-proxy routes =====\n' + '\n'.join(dns_routes))

    print('\n=== SUMMARY ===')
    for name, total, ok, err, bound in summary:
        print(f'  {name}: {ok}/{total} domains, bound={bound}')
    print(f'\nDetected object-groups in running-config: {groups}')
    print(f'Detected dns-proxy routes: {dns_routes}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
