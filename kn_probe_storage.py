"""Probe internal-flash size / OPKG install-target options via RCI.

NDM CLI on this firmware doesn't expose `show storage`/`show hardware`,
but the JSON RCI tree often returns more fields. We GET several plausible
endpoints and dump them, plus probe `components` for any "disk"/"target"
hint that would reveal whether internal install is supported.
"""
from __future__ import annotations

import json
import os
import sys

# Make kn_gui importable when running from project root.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'kn_gui'))
from kn_gui.rci_client import RCIClient

HOST = os.environ.get('ROUTER_HOST', '192.168.1.1')
USER = os.environ.get('ROUTER_USER', 'admin')
PASS = os.environ['ROUTER_PASS']

ENDPOINTS = [
    'show/system',
    'show/version',
    'show/defaults',
    'show/media',
    'show/usb',
    'show/components',
    'show/component',
    'show/components/list',
    'show/components/preview',
    'show/storage',
    'show/disk',
    'show/hardware',
    'show/ndm/component',
    'show/ndm/self-test',
    'show/ndm/running',
    'show/ndm/storage',
    'components/list',
    'components/preview',
]

CLI_COMMANDS = [
    'show defaults',
    'show ndm self-test',
    'components preview',
    'show ndm storage',
    'show partitions',
    'show ndss',
]


def main() -> int:
    out_path = 'kn_probe_storage.log'
    with RCIClient(HOST) as rci, open(out_path, 'w', encoding='utf-8') as log:
        rci.login(USER, PASS)

        for ep in ENDPOINTS:
            log.write(f'\n===== GET /rci/{ep} =====\n')
            try:
                resp = rci.get(ep)
                pretty = json.dumps(resp, ensure_ascii=False, indent=2)[:8000]
                log.write(pretty + '\n')
                # Surface only short/non-empty replies on screen.
                if resp not in (None, {}, []) and len(pretty) < 1500:
                    print(f'\n--- /rci/{ep} ---\n{pretty}')
                else:
                    note = 'EMPTY' if resp in (None, {}, []) else f'len={len(pretty)} → see log'
                    print(f'\n--- /rci/{ep} --- {note}')
            except Exception as e:
                log.write(f'ERROR: {e}\n')
                print(f'--- /rci/{ep} --- ERROR: {e}')

        for cmd in CLI_COMMANDS:
            log.write(f'\n===== POST /rci/parse {{ "parse": "{cmd}" }} =====\n')
            try:
                resp = rci.parse(cmd)
                pretty = json.dumps(resp, ensure_ascii=False, indent=2)[:6000]
                log.write(pretty + '\n')
                print(f'\n--- parse: {cmd} ---\n{pretty[:1500]}')
            except Exception as e:
                log.write(f'ERROR: {e}\n')
                print(f'--- parse: {cmd} --- ERROR: {e}')

    print(f'\nfull transcript: {out_path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
