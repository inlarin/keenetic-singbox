"""Deep-dive probe for Entware / opkg / USB-storage state on the router.

Beyond `kn_probe_opkg.py` we also try:
  - `show media`              -> USB media slots & mount state
  - `show usb`                -> USB devices attached
  - `show defaults`           -> default firmware paths (incl. opkg)
  - `show components`         -> the full component catalogue (installed + available)
  - `show ndm component`      -> alternate component listing
  - `show last-change`        -> when was the last config change
  - `tools mtr ?` / `tools ?` -> what tools are exposed (some hint at opkg)
"""
from __future__ import annotations

from kn_common import ANY_PROMPT, KeeneticSession, build_arg_parser


def main() -> int:
    parser = build_arg_parser('Deep-dive Entware/opkg/USB probe')
    parser.add_argument('--log', default='kn_probe_entware.log', help='Transcript log file')
    args = parser.parse_args()

    commands = [
        'show media',
        'show usb',
        'show defaults',
        'show components',
        'show ndm component',
        'show ndm running',
        'tools ?',
        'opkg dns-override ?',
        'opkg disk ?',
        'no opkg ?',
        'opkg ?',
        'show running-config | grep opkg',
    ]

    if args.dry_run:
        print(f'[dry-run] would run {len(commands)} probes on {args.host}:{args.port}')
        return 0

    with open(args.log, 'w', encoding='utf-8') as log:
        with KeeneticSession(host=args.host, port=args.port, user=args.user) as kn:
            for cmd in commands:
                text = kn.run(cmd, prompt=ANY_PROMPT, timeout=15)
                log.write(f'\n===== {cmd} =====\n{text}\n')
                print(f'\n--- {cmd} ---')
                print(text[-1500:] if len(text) > 1500 else text)

    print(f'\nfull transcript: {args.log}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
