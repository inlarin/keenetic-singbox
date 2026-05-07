"""Probe what the router knows about its own hardware/storage.

Tries every show-command we can think of that might leak NAND size, RAM,
mountpoints, or the OPKG-internal-install option.
"""
from __future__ import annotations

from kn_common import ANY_PROMPT, KeeneticSession, build_arg_parser


def main() -> int:
    parser = build_arg_parser('Probe router hardware/storage capabilities')
    parser.add_argument('--log', default='kn_probe_hw.log')
    args = parser.parse_args()

    commands = [
        # Hardware identification
        'show hardware',
        'show system',
        'show kernel',
        'show ndm self-test',
        # Storage / mounts
        'show defaults',
        'show schedule',
        'system mount ?',
        'system storage ?',
        'show storage',
        'show media',
        'show fsboard',
        # Components catalogue (try other forms — earlier "show components" returned empty)
        'show components',
        'show component',
        'components ?',
        # OPKG-related — will fail until the component is added, but tells us if the CLI noun exists at all
        'opkg ?',
        'opkg disk ?',
        'opkg dns-override ?',
        # Random firmware-info commands worth trying
        'show running-config | grep -i flash',
        'show ndss',
        'show scheduler list',
        'show ?',
    ]

    if args.dry_run:
        print(f'[dry-run] would run {len(commands)} probes on {args.host}:{args.port}')
        return 0

    with open(args.log, 'w', encoding='utf-8') as log:
        with KeeneticSession(host=args.host, port=args.port, user=args.user) as kn:
            for cmd in commands:
                text = kn.run(cmd, prompt=ANY_PROMPT, timeout=15)
                log.write(f'\n===== {cmd} =====\n{text}\n')
                # Print only output (skip echoed prompt) so the screen stays readable.
                lines = text.splitlines()
                tail = '\n'.join(lines[-25:]) if len(lines) > 25 else text
                print(f'\n--- {cmd} ---\n{tail}')

    print(f'\nfull transcript: {args.log}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
