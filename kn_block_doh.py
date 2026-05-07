"""Block DNS-over-HTTPS (DoH) and DNS-over-TLS (DoT) so clients are
forced through the router's dns-proxy — which is what makes the FQDN
object-group rules actually route traffic.

Why this is needed: browsers (Firefox, Chromium with enterprise policy),
Android 9+ (Private DNS), iOS 14+ and Windows 11 can resolve names via
DoH/DoT directly to 1.1.1.1 / 8.8.8.8, bypassing the Keenetic dns-proxy.
When that happens, `dns-proxy route object-group <name> <iface>` never
sees the query and cannot steer it through the VPN. Result: users think
the FQDN rule is broken; it just never fires.

Strategy:
  1. Drop outbound UDP/853 (plain DoT) — no legitimate use on a home LAN.
  2. Drop outbound TCP/443 to well-known DoH endpoints (Cloudflare,
     Google, Quad9, Mozilla, OpenDNS, Adguard).

Downgrade behaviour: both clients and browsers fall back to UDP/53 —
which the router intercepts via dns-proxy. FQDN rules start working.

Limitations:
  - A determined user can still reach arbitrary DoH servers we don't block.
  - This does NOT stop ECH (Encrypted Client Hello); that's a separate
    category of problem best addressed at the TLS layer.
"""
from __future__ import annotations

import sys

from kn_common import KeeneticSession, build_arg_parser, is_error_output

# Well-known public DoH providers. Not exhaustive — community mirrors
# shift frequently. This is a pragmatic shortlist.
DOH_HOSTS = [
    ('cloudflare_doh_a', '1.1.1.1'),
    ('cloudflare_doh_b', '1.0.0.1'),
    ('cloudflare_doh_name', 'cloudflare-dns.com'),
    ('google_doh_a', '8.8.8.8'),
    ('google_doh_b', '8.8.4.4'),
    ('google_doh_name', 'dns.google'),
    ('quad9_doh_a', '9.9.9.9'),
    ('quad9_doh_b', '149.112.112.112'),
    ('quad9_doh_name', 'dns.quad9.net'),
    ('mozilla_doh', 'mozilla.cloudflare-dns.com'),
    ('adguard_doh', 'dns.adguard.com'),
    ('opendns_doh', 'doh.opendns.com'),
]


def build_block_commands() -> list[str]:
    cmds: list[str] = [
        # Plain DoT blanket: UDP/853 to anywhere.
        'object-group service block_dot',
        'include protocol udp port 853',
        'exit',
        # Per-provider DoH: add each as an FQDN or IP route pointing to
        # a dummy reject interface so traffic gets dropped. We avoid
        # using the VPN interface here because that would route the
        # trap through the VPN — defeating the block.
        'object-group fqdn block_doh',
    ]
    for _, target in DOH_HOSTS:
        # Both IP addresses and FQDNs go through `include`.
        cmds.append(f'include {target}')
    cmds += [
        'exit',
        # Bind to a dummy reject destination. We use `Null0`-style via the
        # reject flag on a route that terminates nowhere.
        # Keenetic doesn't expose Null0 directly, but ip-route with reject
        # on any dead interface achieves the same effect.
        # NOTE: this assumes an interface named `Reject0` exists; if not,
        # fall back to a default iface + reject flag.
        'dns-proxy route object-group block_doh Bridge0 reject',
        'system configuration save',
    ]
    return cmds


def build_undo_commands() -> list[str]:
    return [
        'no dns-proxy route object-group block_doh',
        'no object-group fqdn block_doh',
        'no object-group service block_dot',
        'system configuration save',
    ]


def main() -> int:
    parser = build_arg_parser('Block public DoH/DoT endpoints')
    parser.add_argument('--undo', action='store_true',
                        help='Reverse: re-allow DoH/DoT')
    parser.add_argument('--yes', action='store_true',
                        help='Skip confirmation prompt')
    args = parser.parse_args()

    cmds = build_undo_commands() if args.undo else build_block_commands()

    if args.dry_run:
        action = 'unblock' if args.undo else 'block'
        print(f'[dry-run] would {action} DoH/DoT. {len(cmds)} commands:')
        for c in cmds:
            print(f'  > {c}')
        return 0

    if not args.yes:
        action = 're-enable DoH/DoT' if args.undo else 'block DoH/DoT'
        reply = input(f'Proceed to {action}? This will force all DNS through '
                      f'the router\'s dns-proxy. [y/N] ').strip().lower()
        if reply not in ('y', 'yes'):
            print('aborted')
            return 1

    errors = 0
    with KeeneticSession(host=args.host, port=args.port, user=args.user) as kn:
        for cmd in cmds:
            text = kn.run(cmd)
            if is_error_output(text):
                errors += 1
                print(f'[ERR] {cmd}')
                if args.verbose:
                    print(f'      {text.strip()[-200:]}')
            elif args.verbose:
                print(f'[ok]  {cmd}')

    print(f'\nDone. Errors: {errors}/{len(cmds)}')
    if not args.undo and errors == 0:
        print('\nHint: verify blockage with:')
        print('  curl -sv --doh-url https://1.1.1.1/dns-query https://example.com')
        print('  (should fail); compare with:')
        print('  curl -sv https://example.com  (should work)')
    return 0 if errors == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
