"""One-shot VPN traffic report.

Pulls bulk state from RCI (HTTP/JSON) and per-interface `stat` from Telnet
(RCI doesn't expose `show interface <name> stat`), then prints a report of:
  * VPN interface health and total/live throughput
  * IP policy routes — which profiles egress via the VPN
  * LAN clients ranked by total traffic, with their current speeds and
    whichever policy they're attached to

Credentials via ROUTER_PASS env var, consistent with the rest of the repo.

Usage:
    ROUTER_PASS='...' python kn_vpn_traffic.py           # default SSTP0
    ROUTER_PASS='...' python kn_vpn_traffic.py --iface Wireguard0
    python kn_vpn_traffic.py --debug                     # dump raw RCI JSON
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
from hashlib import md5, sha256
from typing import Any

import requests

if sys.platform == "win32" and not getattr(sys.stdout, "_utf8", False):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    sys.stdout._utf8 = True  # type: ignore[attr-defined]

from kn_common import DEFAULT_HOST, DEFAULT_USER, KeeneticSession


REPORT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "vpn_traffic_report.txt")


# ── RCI thin client ─────────────────────────────────────────────────────────

class RCI:
    def __init__(self, host: str, user: str, password: str, timeout: float = 8.0):
        self.base = f"http://{host}"
        self.user = user
        self.password = password
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers["Accept"] = "application/json"
        self.authed = False

    def auth(self) -> bool:
        r = self.s.get(f"{self.base}/auth", timeout=self.timeout)
        if r.status_code == 200:
            self.authed = True
            return True
        realm = r.headers.get("X-NDM-Realm", "")
        challenge = r.headers.get("X-NDM-Challenge", "")
        if not realm or not challenge:
            return False
        md5_hash = md5(f"{self.user}:{realm}:{self.password}".encode()).hexdigest()
        sha_hash = sha256(f"{challenge}{md5_hash}".encode()).hexdigest()
        r2 = self.s.post(f"{self.base}/auth",
                         json={"login": self.user, "password": sha_hash},
                         timeout=self.timeout)
        self.authed = r2.status_code == 200
        return self.authed

    def get(self, path: str) -> Any:
        if not self.authed and not self.auth():
            return None
        url = f"{self.base}/rci/" + path.lstrip("/").replace(" ", "/")
        try:
            r = self.s.get(url, timeout=self.timeout)
        except requests.RequestException as e:
            return {"_error": str(e)}
        if r.status_code == 401:
            self.authed = False
            if self.auth():
                r = self.s.get(url, timeout=self.timeout)
            else:
                return None
        if r.status_code != 200:
            return {"_error": f"HTTP {r.status_code}"}
        try:
            return r.json()
        except ValueError:
            return {"_error": "not json", "_text": r.text[:200]}


# ── Formatting ──────────────────────────────────────────────────────────────

def fmt_bytes(n: Any) -> str:
    try:
        x = float(n or 0)
    except (ValueError, TypeError):
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if x < 1024:
            return f"{x:.0f} {unit}" if unit == "B" else f"{x:.2f} {unit}"
        x /= 1024
    return f"{x:.2f} PB"


def fmt_bps_from_bitsps(bps: Any) -> str:
    try:
        x = float(bps or 0)
    except (ValueError, TypeError):
        return "—"
    for unit in ("bps", "Kbps", "Mbps", "Gbps"):
        if x < 1000:
            return f"{x:.0f} {unit}" if unit == "bps" else f"{x:.2f} {unit}"
        x /= 1000
    return f"{x:.2f} Tbps"


def fmt_uptime(sec: Any) -> str:
    try:
        s = int(float(sec or 0))
    except (ValueError, TypeError):
        return "—"
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    return f"{m}m {s}s"


def _num(v: Any, default: int = 0) -> int:
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return default


def _first(d: dict, *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


# ── Collectors ──────────────────────────────────────────────────────────────

def collect_iface_stat(tn: KeeneticSession, iface: str) -> dict:
    """`show interface <iface> stat` via Telnet — RCI doesn't expose it."""
    try:
        raw = tn.run(f"show interface {iface} stat")
    except Exception:
        return {}
    out: dict = {}
    for ln in raw.splitlines():
        if ":" in ln:
            k, _, v = ln.partition(":")
            out[k.strip().lower()] = v.strip()
    return out


def collect_fqdn_routes_via_vpn(tn: KeeneticSession, vpn_iface: str) -> list[dict]:
    """Find `route object-group <grp> <vpn_iface> ...` bindings (under the
    `dns-proxy` block on Keenetic) and enumerate the FQDN object-groups
    they attach to. Returns list of `{group, line, mode, domains}`."""
    import re
    try:
        cfg = tn.run("show running-config", timeout=30)
    except Exception:
        return []

    bindings: list[dict] = []
    # Two forms seen in the wild:
    #   dns-proxy route object-group <g> <iface> auto [reject]
    #   route object-group <g> <iface> auto [reject]   (inside dns-proxy block)
    line_re = re.compile(
        rf"^\s*(?:dns-proxy\s+)?route\s+object-group\s+(\S+)\s+{re.escape(vpn_iface)}(\b.*)?$",
        re.MULTILINE,
    )
    for m in line_re.finditer(cfg):
        grp = m.group(1)
        tail = (m.group(2) or "").strip()
        bindings.append({
            "group": grp,
            "line": m.group(0).strip(),
            "mode": tail,
            "domains": [],
        })

    # Resolve domains for each group by finding its definition block.
    for b in bindings:
        grp = b["group"]
        block = re.search(
            rf"^\s*object-group fqdn {re.escape(grp)}\s*\n(.*?)(?=^\s*!|\Z)",
            cfg, re.DOTALL | re.MULTILINE,
        )
        if block:
            b["domains"] = re.findall(r"\binclude\s+(\S+)", block.group(1))

    return bindings


# ── Flatten helpers for the ragged shapes Keenetic returns ─────────────────

def _as_iface_list(obj: Any) -> list[tuple[str, dict]]:
    """Normalize `show interface` into [(name, dict), ...]."""
    if isinstance(obj, dict):
        if obj and all(isinstance(v, dict) for v in obj.values()):
            return list(obj.items())
        if "interface" in obj and isinstance(obj["interface"], list):
            return [(i.get("interface-name", i.get("name", "?")), i)
                    for i in obj["interface"]]
    if isinstance(obj, list):
        return [(d.get("interface-name", d.get("name", "?")), d)
                for d in obj if isinstance(d, dict)]
    return []


def _as_host_list(obj: Any) -> list[dict]:
    """Normalize `show ip hotspot` into a flat list of host dicts."""
    if isinstance(obj, dict):
        for key in ("host", "hosts"):
            if isinstance(obj.get(key), list):
                return obj[key]
        for v in obj.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
    if isinstance(obj, list):
        return obj
    return []


# ── Rendering ───────────────────────────────────────────────────────────────

VPN_MARKERS = ("wireguard", "sstp", "l2tp", "ike", "openvpn", "pptp", "proxy")


def render(vpn_iface: str, iface_info: dict, iface_stat: dict,
           hotspot_t0: Any, hotspot_t1: Any, delta_sec: float,
           routes_via_vpn: list[tuple[str, str]],
           fqdn_bindings: list[dict],
           all_ifaces: Any) -> str:
    lines: list[str] = []
    p = lines.append
    sep = "=" * 78

    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    p(sep)
    p(f"VPN TRAFFIC REPORT  —  {vpn_iface}")
    p(f"Generated: {ts}")
    p(sep)
    p("")

    # ── Section: VPN interface ──
    p(f"[VPN interface: {vpn_iface}]")
    if not iface_info or "_error" in iface_info:
        err = iface_info.get("_error") if isinstance(iface_info, dict) else "no data"
        p(f"  (нет данных: {err})")
    else:
        p(f"  State / connected:   {iface_info.get('state','—')} / "
          f"{iface_info.get('connected','—')}")
        p(f"  Description:         {iface_info.get('description','')}")
        p(f"  Type:                {iface_info.get('type','—')}")
        p(f"  Local address:       {_first(iface_info,'address','ip','ipv4', default='—')}")
        p(f"  Remote / peer:       {_first(iface_info,'remote','peer-address', default='—')}")
        p(f"  Remote endpoint:     {iface_info.get('remote-endpoint-address','—')}")
        p(f"  Local endpoint:      {iface_info.get('local-endpoint-address','—')}")
        p(f"  Via:                 {iface_info.get('via','—')}")
        p(f"  Uptime:              {fmt_uptime(iface_info.get('uptime'))}")
        p(f"  MTU:                 {iface_info.get('mtu','—')}")
    if iface_stat:
        p("")
        p("  ── live stat (show interface … stat) ──")
        p(f"  RX total:            {fmt_bytes(_first(iface_stat,'rxbytes','rx-bytes'))}")
        p(f"  TX total:            {fmt_bytes(_first(iface_stat,'txbytes','tx-bytes'))}")
        p(f"  RX packets:          {_first(iface_stat,'rxpackets','rx-packets', default='—')}")
        p(f"  TX packets:          {_first(iface_stat,'txpackets','tx-packets', default='—')}")
        p(f"  Current RX speed:    {fmt_bps_from_bitsps(iface_stat.get('rxspeed'))}")
        p(f"  Current TX speed:    {fmt_bps_from_bitsps(iface_stat.get('txspeed'))}")
        p(f"  Errors:              rx={iface_stat.get('rxerrors','—')}  "
          f"tx={iface_stat.get('txerrors','—')}")
        p(f"  Dropped:             rx={iface_stat.get('rxdropped','—')}  "
          f"tx={iface_stat.get('txdropped','—')}")
    else:
        p("  (stat через Telnet недоступен — totals и current-speed не получены)")
    p("")

    # ── Section: other VPN interfaces ──
    others = []
    for name, d in _as_iface_list(all_ifaces):
        low = name.lower()
        if name != vpn_iface and any(m in low for m in VPN_MARKERS):
            if isinstance(d, dict) and d.get("state"):
                others.append((name, d))
    if others:
        p("[Other VPN interfaces present]")
        for name, d in others:
            p(f"  {name:18s} state={d.get('state','?'):6s} "
              f"connected={str(d.get('connected','?')):4s} "
              f"desc='{d.get('description','')}'")
        p("")

    # ── Section: destinations routed via VPN ──
    p(f"[Static routes through {vpn_iface} (show ip route)]")
    if not routes_via_vpn:
        p("  (нет статических маршрутов через VPN)")
    else:
        for dst, src in routes_via_vpn[:20]:
            p(f"  {dst:40s} via {src}")
    p("")

    # ── Section: FQDN-based routing (dns-proxy route) ──
    p(f"[FQDN routing through {vpn_iface} (dns-proxy route)]")
    p("  Трафик к этим доменам уходит в VPN при DNS-резолве. Это основной")
    p("  способ направления клиентов в VPN на данном роутере.")
    p("")
    if not fqdn_bindings:
        p("  (не найдено dns-proxy route bindings для этого интерфейса)")
    else:
        total_domains = sum(len(b.get("domains", [])) for b in fqdn_bindings)
        p(f"  Всего групп: {len(fqdn_bindings)}   суммарно доменов: {total_domains}")
        p("")
        for b in fqdn_bindings:
            grp = b.get("group") or "(inline)"
            doms = b.get("domains", [])
            p(f"  ▸ {grp}  ({len(doms)} доменов)")
            p(f"      cfg: {b['line']}")
            if doms:
                preview = ", ".join(doms[:8])
                suffix = f"  (+{len(doms)-8} ещё)" if len(doms) > 8 else ""
                p(f"      {preview}{suffix}")
    p("")

    # ── Section: LAN clients by traffic, with live speed from delta ──
    hosts_now = _as_host_list(hotspot_t1)
    hosts_prev_by_mac = {h.get("mac"): h for h in _as_host_list(hotspot_t0)
                         if isinstance(h, dict) and h.get("mac")}

    for h in hosts_now:
        if not isinstance(h, dict):
            continue
        h["_rx"] = _num(_first(h, "rxbytes", "rx-bytes"))
        h["_tx"] = _num(_first(h, "txbytes", "tx-bytes"))
        h["_total"] = h["_rx"] + h["_tx"]
        prev = hosts_prev_by_mac.get(h.get("mac"), {})
        p_rx = _num(_first(prev, "rxbytes", "rx-bytes"))
        p_tx = _num(_first(prev, "txbytes", "tx-bytes"))
        if delta_sec > 0 and prev:
            h["_rx_bps"] = max(0, (h["_rx"] - p_rx)) * 8 / delta_sec
            h["_tx_bps"] = max(0, (h["_tx"] - p_tx)) * 8 / delta_sec
        else:
            h["_rx_bps"] = 0.0
            h["_tx_bps"] = 0.0

    hosts_now = [h for h in hosts_now if isinstance(h, dict)]
    hosts_now.sort(key=lambda h: h.get("_total", 0), reverse=True)

    p(f"[LAN clients — top {min(50, len(hosts_now))} by total traffic]")
    p(f"  Current speeds computed from {delta_sec:.1f}s window between two hotspot snapshots.")
    p("")
    if not hosts_now:
        p("  (show ip hotspot вернул пусто)")
    else:
        header = (f"  {'IP':15s} {'Hostname':22s} {'MAC':18s} "
                  f"{'RX total':>11s} {'TX total':>11s} {'Total':>11s} "
                  f"{'RX now':>11s} {'TX now':>11s} {'Act':>4s}")
        p(header)
        p("  " + "-" * (len(header) - 2))
        for h in hosts_now[:50]:
            ip = str(h.get("ip", "—"))[:15]
            name = (str(h.get("hostname") or h.get("name") or ""))[:22]
            mac = str(h.get("mac", "—"))[:18]
            active = "yes" if h.get("active") is True else "no"
            p(f"  {ip:15s} {name:22s} {mac:18s} "
              f"{fmt_bytes(h['_rx']):>11s} {fmt_bytes(h['_tx']):>11s} "
              f"{fmt_bytes(h['_total']):>11s} "
              f"{fmt_bps_from_bitsps(h['_rx_bps']):>11s} "
              f"{fmt_bps_from_bitsps(h['_tx_bps']):>11s} "
              f"{active:>4s}")

        # Loaders right now
        loaders = [h for h in hosts_now
                   if h["_rx_bps"] + h["_tx_bps"] > 100_000]  # >100 Kbps
        if loaders:
            p("")
            p("  Активно в канале сейчас (>100 Kbps суммарно):")
            for h in loaders:
                p(f"    • {h.get('ip','—'):15s} "
                  f"{(h.get('hostname') or h.get('name') or '—')[:22]:22s}  "
                  f"↓ {fmt_bps_from_bitsps(h['_rx_bps'])}  "
                  f"↑ {fmt_bps_from_bitsps(h['_tx_bps'])}")
    p("")

    p(sep)
    p("Как читать:")
    p(f"  • «RX total/TX total» на {vpn_iface} — сколько всего прошло через VPN")
    p("    за время его аптайма. Это и есть объём, который «убегает в VPN».")
    p("  • «Current RX/TX speed» — текущая скорость VPN-туннеля, отсюда")
    p("    видно, забит ли канал сейчас.")
    p("  • Таблица LAN-клиентов — их ОБЩИЙ трафик (не только VPN). Чтобы")
    p("    понять кто нагружает VPN — ищи клиентов с высокой «RX now»,")
    p("    которые одновременно обращаются к destination-префиксам из")
    p("    секции выше.")
    p("  • Keenetic без SNMP/Netflow не отдаёт per-client per-interface")
    p("    разбивку — это ограничение прошивки, не скрипта.")
    p(sep)
    return "\n".join(lines)


# ── Main ────────────────────────────────────────────────────────────────────

def collect_routes_via_vpn(rci: RCI, vpn_iface: str) -> list[tuple[str, str]]:
    """Extract destinations whose next-hop interface contains `vpn_iface`.

    Returns list of (destination_label, source_label) tuples."""
    routes = rci.get("show/ip/route")
    if not routes:
        return []

    out: list[tuple[str, str]] = []

    def add(dst: str, src: str) -> None:
        if dst and dst != "0.0.0.0/0":
            out.append((dst, src))

    # The shape varies by firmware — walk defensively.
    items: list[dict] = []
    if isinstance(routes, dict):
        for key in ("route", "routes"):
            if isinstance(routes.get(key), list):
                items = routes[key]
                break
        if not items:
            # Maybe dict is already keyed by destination.
            for k, v in routes.items():
                if isinstance(v, dict):
                    v2 = dict(v)
                    v2.setdefault("destination", k)
                    items.append(v2)
    elif isinstance(routes, list):
        items = routes

    for r in items:
        if not isinstance(r, dict):
            continue
        iface = str(r.get("interface", r.get("via", "")))
        if vpn_iface not in iface:
            continue
        dst = r.get("destination") or r.get("network") or r.get("address") or "?"
        mask = r.get("mask") or r.get("prefix-length")
        if mask and "/" not in str(dst):
            dst = f"{dst}/{mask}"
        src = r.get("source") or r.get("host-name") or r.get("proto") or r.get("comment") or ""
        add(str(dst), str(src))

    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Keenetic VPN traffic one-shot report")
    ap.add_argument("--iface", default=os.environ.get("VPN_IFACE", "SSTP0"),
                    help="VPN interface name (default: SSTP0)")
    ap.add_argument("--host", default=os.environ.get("ROUTER_HOST", DEFAULT_HOST))
    ap.add_argument("--user", default=os.environ.get("ROUTER_USER", DEFAULT_USER))
    ap.add_argument("--sample-sec", type=float, default=3.0,
                    help="Delay between hotspot snapshots for live-speed calc (default: 3s)")
    ap.add_argument("--debug", action="store_true",
                    help="Dump raw RCI payloads instead of rendering")
    args = ap.parse_args()

    pw = os.environ.get("ROUTER_PASS")
    if not pw:
        print("ROUTER_PASS is not set", file=sys.stderr)
        return 2

    rci = RCI(args.host, args.user, pw)
    if not rci.auth():
        print("RCI auth failed", file=sys.stderr)
        return 3

    iface_info = rci.get(f"show/interface/{args.iface}") or {}
    hotspot_t0 = rci.get("show/ip/hotspot") or {}
    t0 = time.time()
    all_ifaces = rci.get("show/interface") or {}
    routes_via_vpn = collect_routes_via_vpn(rci, args.iface)

    if args.debug:
        raw_routes = rci.get("show/ip/route")
        print(json.dumps({
            "iface_info": iface_info,
            "hotspot_sample": _as_host_list(hotspot_t0)[:2],
            "all_iface_names": [n for n, _ in _as_iface_list(all_ifaces)],
            "routes_via_vpn_count": len(routes_via_vpn),
            "routes_via_vpn_sample": routes_via_vpn[:10],
            "routes_raw_shape": (
                type(raw_routes).__name__,
                len(raw_routes) if hasattr(raw_routes, "__len__") else "?",
                (raw_routes[0] if isinstance(raw_routes, list) and raw_routes
                 else dict(list(raw_routes.items())[:1]) if isinstance(raw_routes, dict)
                 else None),
            ),
        }, indent=2, ensure_ascii=False, default=str))
        return 0

    iface_stat: dict = {}
    fqdn_bindings: list[dict] = []
    try:
        with KeeneticSession(host=args.host, user=args.user, password=pw) as tn:
            iface_stat = collect_iface_stat(tn, args.iface)
            fqdn_bindings = collect_fqdn_routes_via_vpn(tn, args.iface)
    except Exception as e:
        print(f"[warn] telnet stat unavailable: {e}", file=sys.stderr)

    # Second hotspot sample for live-speed delta
    to_wait = max(0.0, args.sample_sec - (time.time() - t0))
    if to_wait > 0:
        time.sleep(to_wait)
    hotspot_t1 = rci.get("show/ip/hotspot") or {}
    delta = time.time() - t0

    report = render(args.iface, iface_info, iface_stat,
                    hotspot_t0, hotspot_t1, delta,
                    routes_via_vpn, fqdn_bindings, all_ifaces)
    print(report)
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\n[report saved → {REPORT_FILE}]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
