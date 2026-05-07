"""Roll back two opkg mutations that an earlier probe accidentally persisted.

We sent `opkg disk ?` (which set the disk to literal "?") and
`opkg dns-override ?` (which enabled DNS-override). Both ended up in
running-config but were NOT saved to startup-config.

This script:
  1. Connects via telnet
  2. Sends `no opkg disk` and `no opkg dns-override`  (write, but undoing)
  3. Reads `show running-config` and prints any line that still mentions opkg
  4. Does NOT call `system configuration save` — caller decides if/when to persist

Safe to run repeatedly. Idempotent.
"""
from __future__ import annotations

from kn_common import ANY_PROMPT, KeeneticSession, build_arg_parser


def main() -> int:
    parser = build_arg_parser('Undo opkg disk/dns-override mutations from earlier probe')
    args = parser.parse_args()

    if args.dry_run:
        print(f'[dry-run] would send `no opkg disk` + `no opkg dns-override` to {args.host}:{args.port}')
        return 0

    with KeeneticSession(host=args.host, port=args.port, user=args.user) as kn:
        for cmd in ('no opkg disk', 'no opkg dns-override'):
            text = kn.run(cmd, prompt=ANY_PROMPT, timeout=15)
            print(f'\n--- {cmd} ---\n{text.strip()}')

        cfg = kn.run('show running-config', prompt=ANY_PROMPT, timeout=20)

    print('\n--- residual opkg lines in running-config ---')
    found = False
    for line in cfg.splitlines():
        if 'opkg' in line.lower():
            print(f'  {line}')
            found = True
    if not found:
        print('  (none — clean)')

    print('\nNOTE: changes are in running-config only. Reboot would discard them anyway,')
    print('but if you want to persist the rollback now, run:  system configuration save')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
