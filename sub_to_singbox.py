"""Convert a v2ray-style base64 subscription (vless/vmess/trojan/ss URIs)
into a sing-box config with **per-country routing pools**.

Architecture:
- Each subscription server is parsed into an outbound with a **stable tag**
  derived from `<cc>-<proto>-<host>-<port>-<transport>` so ranking and
  manual `select` pins survive subscription rotation.
- Servers are grouped by country (resolved via ip-api.com, cached on disk
  in ``country_cache.json``). Each country gets its own TUN inbound
  (``opkgtun<N>``), urltest group, and selector — letting NDM
  ``dns-proxy route ... OpkgTun<N>`` send specific FQDNs through specific
  countries (e.g. YouTube via Turkey, ChatGPT via US).
- A persistent ``country_index_map.json`` keeps cc→OpkgTunN allocation
  stable across regenerations: new countries get the next free index,
  existing ones keep theirs even if a country temporarily disappears.

Usage:
    python sub_to_singbox.py <subscription_url> [--out config.json]
    python sub_to_singbox.py --file dump.txt --out outbounds.json

Side artefacts created next to this script (or in ``--state-dir``):
    country_cache.json       IP→cc mapping (manual edit OK; auto-extends)
    country_index_map.json   cc→OpkgTunN index (manual edit OK; auto-extends)
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import socket
import sys
import urllib.parse
import urllib.request
from typing import Any

# ── State files (cached country lookups + persistent index allocation) ──────

_HERE = os.path.dirname(os.path.abspath(__file__))
COUNTRY_CACHE_PATH = os.path.join(_HERE, "country_cache.json")
COUNTRY_INDEX_PATH = os.path.join(_HERE, "country_index_map.json")


def _load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save_json(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)


# ── Subscription fetch + base64 decode helpers ──────────────────────────────

def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "ClashforAndroid/2.5.12"})
    return urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace").strip()


def b64decode_loose(s: str) -> str:
    s = s.strip()
    pad = (-len(s)) % 4
    return base64.urlsafe_b64decode(s + "=" * pad).decode("utf-8", "replace")


# ── GeoIP via ip-api.com (cached on disk) ───────────────────────────────────
#
# The subscription has at most a few dozen unique IPs; ip-api.com's free tier
# (45 req/min) handles this trivially. Cache permanently — VPS IPs don't
# wander between countries. To force refresh: delete country_cache.json.

def resolve_country(host: str, cache: dict[str, str]) -> str:
    if host in cache:
        return cache[host]
    try:
        ip = socket.gethostbyname(host) if not _is_ip(host) else host
    except OSError:
        ip = host
    if ip in cache:
        cache[host] = cache[ip]
        return cache[ip]
    try:
        url = f"http://ip-api.com/json/{ip}?fields=countryCode"
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        cc = (data.get("countryCode") or "ZZ").upper()
    except Exception as e:
        print(f"[geoip] {host} failed: {e}", file=sys.stderr)
        cc = "ZZ"
    cache[host] = cc
    cache[ip] = cc
    return cc


_IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def _is_ip(s: str) -> bool:
    return bool(_IP_RE.match(s or ""))


# ── Country → OpkgTun index allocation (stable across runs) ─────────────────

def assign_country_indexes(country_codes: set[str],
                           index_map: dict[str, int]) -> dict[str, int]:
    """Assign each country a stable OpkgTun index. Existing entries in
    ``index_map`` are preserved; new countries get the next free integer."""
    used = set(index_map.values())

    def next_free() -> int:
        i = 0
        while i in used:
            i += 1
        used.add(i)
        return i

    # Sort new countries alphabetically for deterministic first-time order.
    for cc in sorted(country_codes - set(index_map)):
        index_map[cc] = next_free()
    return index_map


# ── Stable tag generation ───────────────────────────────────────────────────

def stable_tag(cc: str, proto: str, host: str, port: int, transport: str,
               extra: str = "") -> str:
    """``<cc>-<proto>-<host>-<port>-<transport>[-<extra>]``. Sing-box accepts
    most ASCII in tags but we stick to ``[a-z0-9._-]`` for safety."""
    parts = [cc.lower(), proto, host.replace(":", ""), str(port), transport]
    if extra:
        parts.append(extra)
    raw = "-".join(parts)
    return re.sub(r"[^a-z0-9._-]+", "-", raw.lower()).strip("-")


# ── Per-protocol parsers (return outbound dict, country resolved later) ─────

def parse_vless(uri: str) -> dict[str, Any]:
    p = urllib.parse.urlparse(uri)
    qs = dict(urllib.parse.parse_qsl(p.query))
    ttype = qs.get("type", "tcp")
    flow = qs.get("flow") or ""
    extra = "vision" if "vision" in flow else ""
    o: dict[str, Any] = {
        "type": "vless",
        "_host": p.hostname, "_port": int(p.port),
        "_transport": ttype, "_extra": extra,
        "server": p.hostname,
        "server_port": int(p.port),
        "uuid": p.username,
        "flow": flow,
        "packet_encoding": "xudp",
    }
    sec = qs.get("security", "")
    if sec == "reality":
        o["tls"] = {
            "enabled": True,
            "server_name": qs.get("sni", ""),
            "utls": {"enabled": True, "fingerprint": qs.get("fp", "chrome")},
            "reality": {
                "enabled": True,
                "public_key": qs.get("pbk", ""),
                "short_id": qs.get("sid", ""),
            },
        }
    elif sec == "tls":
        o["tls"] = {
            "enabled": True,
            "server_name": qs.get("sni", p.hostname or ""),
            "utls": {"enabled": True, "fingerprint": qs.get("fp", "chrome")},
        }
    if ttype == "grpc":
        o["transport"] = {"type": "grpc", "service_name": qs.get("serviceName", "")}
    elif ttype == "ws":
        o["transport"] = {"type": "ws", "path": qs.get("path", "/"),
                          "headers": {"Host": qs.get("host", "")} if qs.get("host") else {}}
    elif ttype == "http":
        o["transport"] = {"type": "http", "path": qs.get("path", "/"),
                          "host": [qs.get("host")] if qs.get("host") else []}
    return o


def parse_vmess(uri: str) -> dict[str, Any]:
    payload = uri[len("vmess://"):]
    j = json.loads(b64decode_loose(payload))
    net = j.get("net", "tcp")
    extra = "http" if (net == "tcp" and j.get("type") == "http") else ""
    o: dict[str, Any] = {
        "type": "vmess",
        "_host": j.get("add"), "_port": int(j.get("port", 0)),
        "_transport": net, "_extra": extra,
        "server": j.get("add"),
        "server_port": int(j.get("port", 0)),
        "uuid": j.get("id"),
        "alter_id": int(j.get("aid") or 0),
        "security": j.get("scy") or "auto",
        "packet_encoding": "xudp",
    }
    if j.get("tls") == "tls":
        o["tls"] = {"enabled": True, "server_name": j.get("sni") or j.get("host") or j.get("add")}
    if net == "ws":
        o["transport"] = {"type": "ws", "path": j.get("path") or "/",
                          "headers": {"Host": j.get("host")} if j.get("host") else {}}
    elif net == "grpc":
        o["transport"] = {"type": "grpc", "service_name": j.get("path") or ""}
    elif net == "tcp" and j.get("type") == "http":
        o["transport"] = {"type": "http", "path": j.get("path") or "/",
                          "host": [j.get("host")] if j.get("host") else []}
    return o


def parse_trojan(uri: str) -> dict[str, Any]:
    p = urllib.parse.urlparse(uri)
    qs = dict(urllib.parse.parse_qsl(p.query))
    ttype = qs.get("type", "tcp")
    o: dict[str, Any] = {
        "type": "trojan",
        "_host": p.hostname, "_port": int(p.port),
        "_transport": ttype, "_extra": "",
        "server": p.hostname,
        "server_port": int(p.port),
        "password": urllib.parse.unquote(p.username or ""),
        "tls": {"enabled": True,
                "server_name": qs.get("sni", p.hostname or ""),
                "utls": {"enabled": True, "fingerprint": qs.get("fp") or "chrome"}},
    }
    if ttype == "ws":
        o["transport"] = {"type": "ws", "path": qs.get("path", "/"),
                          "headers": {"Host": qs.get("host", "")} if qs.get("host") else {}}
    elif ttype == "grpc":
        o["transport"] = {"type": "grpc", "service_name": qs.get("serviceName", "")}
    return o


def parse_ss(uri: str) -> dict[str, Any]:
    p = urllib.parse.urlparse(uri)
    method = password = host = port = None
    if p.username and p.hostname and p.port:
        creds = b64decode_loose(p.username) if not p.password else f"{p.username}:{p.password}"
        if ":" in creds:
            method, password = creds.split(":", 1)
        host, port = p.hostname, int(p.port)
    else:
        body = uri[len("ss://"):].split("#", 1)[0]
        decoded = b64decode_loose(body)
        m = re.match(r"([^:]+):([^@]+)@([^:]+):(\d+)", decoded)
        if not m:
            raise ValueError(f"unparsable ss URI: {uri}")
        method, password, host, port = m.group(1), m.group(2), m.group(3), int(m.group(4))
    return {
        "type": "shadowsocks",
        "_host": host, "_port": port, "_transport": "tcp", "_extra": "",
        "server": host, "server_port": port,
        "method": method, "password": password,
    }


PARSERS = {
    "vless": parse_vless,
    "vmess": parse_vmess,
    "trojan": parse_trojan,
    "ss": parse_ss,
}


# ── Top-level subscription parser ───────────────────────────────────────────

def parse_subscription(text: str, country_cache: dict[str, str]) -> list[dict[str, Any]]:
    """Parse subscription body, resolve country for each server, attach
    stable tag. Returns list of outbound dicts (with ``_cc`` field added)."""
    try:
        text = b64decode_loose(text)
    except Exception:
        pass  # already plain
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or "://" not in line:
            continue
        scheme = line.split("://", 1)[0].lower()
        fn = PARSERS.get(scheme)
        if not fn:
            print(f"[skip] unsupported scheme: {scheme}", file=sys.stderr)
            continue
        try:
            o = fn(line)
        except Exception as e:
            print(f"[err] {scheme}: {e}", file=sys.stderr)
            continue
        cc = resolve_country(o["_host"], country_cache)
        proto = {"vless": "vless", "vmess": "vmess", "trojan": "trojan",
                 "shadowsocks": "ss"}[o["type"]]
        o["_cc"] = cc
        o["tag"] = stable_tag(cc, proto, o["_host"], o["_port"],
                              o["_transport"], o.get("_extra") or "")
        if o["tag"] in seen:
            # Hash a few extra bits to disambiguate truly identical entries.
            h = hashlib.sha1(json.dumps(o, sort_keys=True, default=str).encode()).hexdigest()[:6]
            o["tag"] = f"{o['tag']}-{h}"
        seen.add(o["tag"])
        out.append(o)
    return out


# ── Per-country pool builder ────────────────────────────────────────────────

def build_country_pools(outbounds: list[dict[str, Any]]
                        ) -> dict[str, list[dict[str, Any]]]:
    pools: dict[str, list[dict[str, Any]]] = {}
    for o in outbounds:
        pools.setdefault(o["_cc"], []).append(o)
    return pools


# ── Config builder (per-country TUN inbounds + selectors) ───────────────────

# Single TUN address — `interface OpkgTun0` on the NDM side. Country pools
# are exposed via per-country urltest groups but all share the same inbound.
TUN_NAME = "opkgtun0"
TUN_IP = "172.19.0.1"


def build_config(outbounds: list[dict[str, Any]],
                 clash_secret: str,
                 router_ip: str = "192.168.1.1") -> dict[str, Any]:
    """Single-pool config:

    * One TUN inbound (`opkgtun0`).
    * Top-level selector `select` exposes:
        - `urltest-all`     (default, picks fastest across every country)
        - `urltest-<cc>`    (one per country, picks fastest within country)
        - every individual server tag (manual pin for debugging)
      User flips countries from MetaCubeXD by changing `select.now`.
    * `urltest-<cc>` groups stay informational/switchable but route doesn't
      route into them by inbound — everything goes through the single
      `select` and the user's choice decides the country.
    """
    pools = build_country_pools(outbounds)
    all_tags = [o["tag"] for o in outbounds]

    def clean(o: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in o.items() if not k.startswith("_")}

    urltests: list[dict[str, Any]] = []

    # Global urltest — best server across all countries.
    urltests.append({
        "type": "urltest", "tag": "urltest-all", "outbounds": all_tags,
        "url": "https://www.gstatic.com/generate_204",
        "interval": "3m", "tolerance": 50,
    })

    # Per-country urltests — only for countries with >1 server. With a
    # single-server pool there is nothing to choose between, so we skip
    # the empty selector and let the user pin the lone server directly.
    cc_urltest_tags: list[str] = []
    for cc, servers in sorted(pools.items()):
        tags = [s["tag"] for s in servers]
        if len(tags) <= 1:
            continue
        ut_tag = f"urltest-{cc.lower()}"
        urltests.append({
            "type": "urltest", "tag": ut_tag, "outbounds": tags,
            "url": "https://www.gstatic.com/generate_204",
            "interval": "3m", "tolerance": 50,
        })
        cc_urltest_tags.append(ut_tag)

    # Top-level selector. Order in `outbounds` is the order MetaCubeXD
    # shows: keep urltest-all first (auto), per-country urltests next,
    # then individual servers grouped by country for manual pinning.
    grouped_servers: list[str] = []
    for cc, servers in sorted(pools.items()):
        grouped_servers.extend(s["tag"] for s in servers)
    select = {
        "type": "selector", "tag": "select",
        "outbounds": ["urltest-all", *cc_urltest_tags, *grouped_servers],
        "default": "urltest-all",
    }

    return {
        # Log writes to tmpfs, level=warn to silence per-connection spam
        # while keeping handshake errors / config issues visible. Keeping
        # logs on UBIFS would hammer NAND with thousands of writes/min on
        # a busy LAN. Survives a reboot's worth — sing-box recreates the
        # file on start.
        "log": {"level": "warn", "timestamp": True,
                "output": "/tmp/sing-box.log"},
        "dns": {
            "servers": [
                {"type": "udp", "tag": "remote", "server": "1.1.1.1", "detour": "select"},
                {"type": "udp", "tag": "local", "server": router_ip},
            ],
            "rules": [
                {"clash_mode": "direct", "server": "local"},
                {"clash_mode": "global", "server": "remote"},
            ],
            "final": "remote",
            "strategy": "ipv4_only",
        },
        "inbounds": [
            {
                "type": "tun", "tag": "tun-in",
                "interface_name": TUN_NAME,
                "address": [f"{TUN_IP}/32"],
                "mtu": 1420,
                "auto_route": False, "strict_route": False,
                "stack": "gvisor",
            },
        ],
        "outbounds": [
            select,
            *urltests,
            *(clean(o) for o in outbounds),
            {"type": "direct", "tag": "direct"},
        ],
        "route": {
            "rules": [
                {"action": "sniff"},
                {"protocol": "dns", "action": "hijack-dns"},
                {"ip_is_private": True, "outbound": "direct"},
            ],
            "final": "select",
            "auto_detect_interface": False,
            "default_domain_resolver": "local",
        },
        "experimental": {
            # cache.db on tmpfs — sing-box rebuilds urltest history in
            # ~60 s on cold boot, daemon override'ит select.now ещё через
            # ~5 min при первом sweep, так что потеря cache при reboot
            # обходится дешёво по сравнению с UBIFS write wear.
            "cache_file": {"enabled": True, "path": "/tmp/sing-box-cache.db"},
            "clash_api": {
                # Bind on all interfaces so MetaCubeXD is reachable from LAN
                # at <router-ip>:9090 and router-side scripts (healthcheck,
                # sub-refresh) can hit 127.0.0.1:9090 — both work without
                # baking a router-specific IP into the config.
                "external_controller": "0.0.0.0:9090",
                "secret": clash_secret,
                "external_ui": "/opt/share/sing-box/ui",
                "external_ui_download_url":
                    "https://github.com/MetaCubeX/metacubexd/archive/refs/heads/gh-pages.zip",
                "external_ui_download_detour": "direct",
                "default_mode": "rule",
            },
        },
    }


# ── NDM registration helper (emit ndmc commands for current country set) ───

def emit_ndm_setup() -> str:
    """ndmc commands to register the single OpkgTun0 noun in NDM. Idempotent
    — re-running on an already-set-up router just renews the values."""
    lines = [
        "! Run via telnet to NDM (port 23) or `ndmc -c '<line>'`",
        "interface OpkgTun0",
        'interface OpkgTun0 description "sing-box hynet TUN"',
        f"interface OpkgTun0 ip address {TUN_IP} 255.255.255.255",
        "interface OpkgTun0 ip global auto",
        "interface OpkgTun0 ip mtu 1420",
        "interface OpkgTun0 ip tcp adjust-mss pmtu",
        "interface OpkgTun0 security-level public",
        "interface OpkgTun0 up",
        f"ip route default {TUN_IP} OpkgTun0",
        "system configuration save",
    ]
    return "\n".join(lines) + "\n"


# ── CLI entrypoint ──────────────────────────────────────────────────────────

DEFAULT_SECRET = os.environ.get("SINGBOX_HEALTHCHECK_SECRET", "")


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("url", nargs="?", help="subscription URL")
    g.add_argument("--file", help="read raw subscription body from file")
    ap.add_argument("--out", default="-", help="output config path (default stdout)")
    ap.add_argument("--ndm-setup", help="also emit ndmc registration commands to this path")
    ap.add_argument("--secret", default=DEFAULT_SECRET,
                    help="Clash API bearer token (env: SINGBOX_HEALTHCHECK_SECRET)")
    ap.add_argument("--router-ip", default=os.environ.get("ROUTER_HOST", "192.168.1.1"),
                    help="Router LAN IP — baked into the local DNS server tag (env: ROUTER_HOST)")
    args = ap.parse_args()

    text = open(args.file).read() if args.file else fetch(args.url)

    cache = _load_json(COUNTRY_CACHE_PATH, {})

    obs = parse_subscription(text, cache)
    countries = {o["_cc"] for o in obs}
    _save_json(COUNTRY_CACHE_PATH, cache)

    print(f"[+] parsed {len(obs)} outbounds across {len(countries)} countries:",
          file=sys.stderr)
    for cc in sorted(countries):
        n = sum(1 for o in obs if o["_cc"] == cc)
        print(f"      {cc}: {n} outbounds", file=sys.stderr)

    cfg = build_config(obs, args.secret, router_ip=args.router_ip)
    js = json.dumps(cfg, ensure_ascii=False, indent=2)
    if args.out == "-":
        sys.stdout.write(js)
    else:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(js)
        print(f"[+] wrote {args.out}", file=sys.stderr)

    if args.ndm_setup:
        with open(args.ndm_setup, "w", encoding="utf-8") as f:
            f.write(emit_ndm_setup())
        print(f"[+] wrote ndmc setup script → {args.ndm_setup}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
