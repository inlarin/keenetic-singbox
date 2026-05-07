"""Re-probe opkg now that the OPKG firmware component appeared.

Goal: confirm whether opkg CLI is exposed, what disk targets it offers,
and crucially — does it list an internal-memory option?
"""
from __future__ import annotations

from kn_common import ANY_PROMPT, KeeneticSession, build_arg_parser


def main() -> int:
    parser = build_arg_parser('Probe opkg CLI now that the component is installed')
    parser.add_argument('--log', default='kn_probe_opkg2.log')
    args = parser.parse_args()

    commands = [
        # Top-level CLI: is opkg now a command?
        'opkg ?',
        'opkg disk ?',
        'opkg dns-override ?',
        'no opkg ?',
        # Show subcommands
        'show opkg',
        'show ?',
        # Internal-memory keyword
        'opkg disk internal',
        # Running config: did opkg add anything?
        'show running-config',
    ]

    if args.dry_run:
        print(f'[dry-run] would run {len(commands)} probes on {args.host}:{args.port}')
        return 0

    with open(args.log, 'w', encoding='utf-8') as log:
        with KeeneticSession(host=args.host, port=args.port, user=args.user) as kn:
            for cmd in commands:
                # NOTE: `opkg disk internal` is a *destructive* command that
                # would actually configure opkg if accepted. We deliberately
                # send it WITHOUT a save command so even if it succeeds the
                # change isn't persisted to startup-config. Caller can roll
                # back with `no opkg disk` followed by reboot if needed.
                # Since user asked to *probe*, treat success here as "the
                # internal target is recognised, but we won't commit yet".
                if cmd == 'opkg disk internal':
                    # Skip actual execution unless user passes --commit.
                    text = '(skipped — would configure internal storage; rerun with --commit to actually do it)\n'
                else:
                    text = kn.run(cmd, prompt=ANY_PROMPT, timeout=20)
                log.write(f'\n===== {cmd} =====\n{text}\n')
                tail = '\n'.join(text.splitlines()[-30:])
                print(f'\n--- {cmd} ---\n{tail}')

    print(f'\nfull transcript: {args.log}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
