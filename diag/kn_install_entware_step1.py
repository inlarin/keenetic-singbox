"""STEP 1 of Entware-to-internal-NAND install for NC-2312.

Sends ONE command:
    opkg disk storage:/ https://bin.entware.net/aarch64-k3.10/installer/aarch64-installer.tar.gz

This downloads the aarch64 installer archive from bin.entware.net and
unpacks Entware into the router's built-in flash partition `storage:`.

After this we DO NOT call `system configuration save` and DO NOT reboot —
the operator inspects the output and explicitly authorizes the next steps.

If the install fails:
    no opkg disk
    no system mount storage:
    erase storage:
"""
from __future__ import annotations

from kn_common import ANY_PROMPT, KeeneticSession, build_arg_parser

INSTALL_URL = 'https://bin.entware.net/aarch64-k3.10/installer/aarch64-installer.tar.gz'
INSTALL_CMD = f'opkg disk storage:/ {INSTALL_URL}'

# Generous: download ~10 MB + unpack to NAND can take 2-5 minutes on a
# slow link. The kn_common READ_TIMEOUT default of 8s is way too short.
INSTALL_TIMEOUT = 360


def main() -> int:
    parser = build_arg_parser('Entware NAND install — step 1 only (no save, no reboot)')
    parser.add_argument('--log', default='kn_install_entware_step1.log')
    args = parser.parse_args()

    if args.dry_run:
        print(f'[dry-run] would send to {args.host}:{args.port} →')
        print(f'  {INSTALL_CMD}')
        return 0

    with open(args.log, 'w', encoding='utf-8') as log, \
         KeeneticSession(host=args.host, port=args.port, user=args.user) as kn:

        # Sanity: opkg disk should not be set after the rollback.
        pre = kn.run('show running-config', prompt=ANY_PROMPT, timeout=20)
        residual = [ln for ln in pre.splitlines() if 'opkg' in ln.lower()]
        if residual:
            print('ABORT: residual opkg lines in running-config — rollback first:')
            for ln in residual:
                print(f'  {ln}')
            return 2

        print(f'sending: {INSTALL_CMD}')
        print(f'(timeout {INSTALL_TIMEOUT}s — download + unpack into NAND)')
        text = kn.run(INSTALL_CMD, prompt=ANY_PROMPT, timeout=INSTALL_TIMEOUT)
        log.write(f'===== {INSTALL_CMD} =====\n{text}\n')
        print('\n--- output ---')
        print(text)

        # Post-checks: did running-config gain `opkg disk storage:`? Did
        # `show media` start reporting the mounted partition?
        post_cfg = kn.run('show running-config', prompt=ANY_PROMPT, timeout=20)
        log.write(f'\n===== show running-config (post) =====\n{post_cfg}\n')
        post_media = kn.run('show media', prompt=ANY_PROMPT, timeout=15)
        log.write(f'\n===== show media (post) =====\n{post_media}\n')

        print('\n--- post: opkg lines in running-config ---')
        for ln in post_cfg.splitlines():
            if 'opkg' in ln.lower() or 'storage:' in ln.lower():
                print(f'  {ln}')

        print('\n--- post: show media ---')
        print(post_media or '  (empty)')

    print('\nDONE step 1. Did NOT save and did NOT reboot.')
    print(f'Full transcript: {args.log}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
