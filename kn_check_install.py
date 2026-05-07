"""Post-install diagnostic — figure out whether Entware actually landed.

Queries via RCI:
  - show/version       → did `opkg` move out of components?
  - show/media         → is the storage: partition mounted?
  - show/system        → free RAM (download might still be running)
  - show/running-config (parse) → is `opkg disk storage:/` persisted?
And via parse:
  - opkg               → does it still expose subcommands?
  - show ndss
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'kn_gui'))
from kn_gui.rci_client import RCIClient

HOST = os.environ.get('ROUTER_HOST', '192.168.1.1')
USER = os.environ.get('ROUTER_USER', 'admin')
PASS = os.environ['ROUTER_PASS']


def main() -> int:
    with RCIClient(HOST) as rci:
        rci.login(USER, PASS)

        for ep in ('show/version', 'show/media', 'show/system'):
            try:
                resp = rci.get(ep)
            except Exception as e:
                print(f'/rci/{ep} -> ERROR {e}')
                continue
            print(f'\n=== /rci/{ep} ===')
            print(json.dumps(resp, ensure_ascii=False, indent=2)[:4000])

        for cmd in ('show running-config', 'opkg', 'show ndss', 'show interface'):
            try:
                resp = rci.parse(cmd)
            except Exception as e:
                print(f'parse {cmd!r} -> ERROR {e}')
                continue
            print(f'\n=== parse: {cmd} ===')
            text = json.dumps(resp, ensure_ascii=False, indent=2)
            if cmd == 'show running-config':
                # too big — show only opkg/storage lines
                for line in text.splitlines():
                    if 'opkg' in line.lower() or 'storage' in line.lower() or 'mount' in line.lower():
                        print(f'  {line}')
                if not any(k in text.lower() for k in ('opkg', 'storage:')):
                    print('  (no opkg/storage entries in config)')
            else:
                print(text[:3000])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
