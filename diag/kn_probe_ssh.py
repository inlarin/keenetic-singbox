"""Probe SSH server configuration on the router via Telnet.

Asks the NDM CLI:
  - is the `ip ssh` service enabled?
  - what port is it listening on?
  - what's its access policy?

Writes a transcript to kn_probe_ssh.log.
"""
from __future__ import annotations

from kn_common import ANY_PROMPT, KeeneticSession, build_arg_parser


def main() -> int:
    parser = build_arg_parser('Probe SSH server configuration on the router')
    parser.add_argument('--log', default='kn_probe_ssh.log', help='Transcript log file')
    args = parser.parse_args()

    commands = [
        'show running-config',
        'ip ssh ?',
        'ip ssh port ?',
    ]

    if args.dry_run:
        print(f'[dry-run] would probe SSH config on {args.host}:{args.port}')
        return 0

    with open(args.log, 'w', encoding='utf-8') as log:
        with KeeneticSession(host=args.host, port=args.port, user=args.user) as kn:
            for cmd in commands:
                text = kn.run(cmd, prompt=ANY_PROMPT, timeout=15)
                log.write(f'\n===== {cmd} =====\n{text}\n')

    # Grep the captured running-config for ssh-related lines.
    with open(args.log, 'r', encoding='utf-8') as log:
        content = log.read()
    print('--- SSH-related lines from running-config ---')
    for line in content.splitlines():
        if 'ssh' in line.lower():
            print(line)
    print(f'\nfull transcript: {args.log}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
