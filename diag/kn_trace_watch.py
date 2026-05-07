"""Live per-host connection watcher.

Polls `show ip nat` on a fixed interval, diffs packet counters against the
previous snapshot, and prints a ticker showing where the host is sending
packets *right now*, split by VPN (SSTP0) vs direct PPPoE egress.

NAT table note: each translation row's packet counter is cumulative for as
long as the flow stays alive. Deltas between snapshots give rate; flows
that expire just drop out of the table.

Usage:
    ROUTER_PASS='...' python kn_trace_watch.py --host 192.168.X.10
    python kn_trace_watch.py --host 192.168.X.10 --interval 5 --duration 60 --rdns
"""
from __future__ import annotations

import argparse
import io
import os
import re
import socket
import sys
import time
from collections import defaultdict
from typing import Any

from kn_common import DEFAULT_HOST, DEFAULT_USER, KeeneticSession
from kn_trace_host import (
    WELL_KNOWN,
    parse_nat,
    port_label,
    rdns_one,
)

if sys.platform == "win32" and not getattr(sys.stdout, "_utf8", False):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    sys.stdout._utf8 = True  # type: ignore[attr-defined]


def flow_key(f: dict) -> tuple:
    return (f["proto"], f["lan_ip"], f["lan_port"], f["dst_ip"], f["dst_port"])


def classify(f: dict, vpn_local_ip: str, pppoe_ip: str | None) -> str:
    ext = f.get("wan_ext_ip")
    if ext == vpn_local_ip:
        return "VPN"
    if pppoe_ip and ext == pppoe_ip:
        return "PPPoE"
    if ext is None:
        return "?"
    return "OTHER"


def diff_snapshots(prev: dict, curr: list[dict], host_ip: str) -> list[dict]:
    """Return list of per-flow deltas for `host_ip`.

    For flows present in both: delta = curr.pkt - prev.pkt.
    For brand-new flows: delta = curr.pkt (everything since it appeared,
    which is still a useful lower-bound rate signal on the first tick a
    flow shows up)."""
    out: list[dict] = []
    for f in curr:
        if f["lan_ip"] != host_ip:
            continue
        k = flow_key(f)
        p_out_prev = prev.get(k, {}).get("packets_out", 0)
        p_in_prev  = prev.get(k, {}).get("packets_in", 0)
        d_out = max(0, f["packets_out"] - p_out_prev)
        d_in  = max(0, f["packets_in"]  - p_in_prev)
        if d_out == 0 and d_in == 0:
            continue
        out.append({**f, "d_out": d_out, "d_in": d_in})
    return out


def aggregate_by_dst(flows: list[dict]) -> list[dict]:
    agg: dict[str, dict] = defaultdict(
        lambda: {"d_out": 0, "d_in": 0, "ports": set(), "protos": set(), "flows": 0}
    )
    for f in flows:
        a = agg[f["dst_ip"]]
        a["d_out"] += f["d_out"]
        a["d_in"]  += f["d_in"]
        a["ports"].add(f["dst_port"])
        a["protos"].add(f["proto"])
        a["flows"] += 1
    result = []
    for dst, a in agg.items():
        result.append({
            "dst_ip": dst,
            "d_out": a["d_out"],
            "d_in":  a["d_in"],
            "ports": sorted(a["ports"]),
            "protos": sorted(p for p in a["protos"] if p),
            "flows": a["flows"],
        })
    result.sort(key=lambda x: x["d_out"] + x["d_in"], reverse=True)
    return result


def fmt_pps(pkts: int, interval: float) -> str:
    rate = pkts / interval if interval > 0 else 0
    if rate >= 1000:
        return f"{rate/1000:.1f}K/s"
    if rate >= 10:
        return f"{rate:.0f}/s"
    if rate >= 1:
        return f"{rate:.1f}/s"
    return f"{rate:.2f}/s"


def format_ports(ports: list[int]) -> str:
    if not ports:
        return ""
    labels = []
    for p in ports[:4]:
        labels.append(port_label(p))
    if len(ports) > 4:
        labels.append(f"+{len(ports)-4}")
    return ",".join(labels)


def tick_print(host_ip: str, delta_secs: float,
               vpn_dsts: list[dict], direct_dsts: list[dict],
               other_dsts: list[dict],
               rdns: dict[str, str], top: int,
               vpn_stat_delta: dict, pppoe_stat_delta: dict) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"\n── {ts}  Δ={delta_secs:.1f}s "
          f"──────────────────────────────────────────────")
    if vpn_stat_delta or pppoe_stat_delta:
        parts = []
        if vpn_stat_delta:
            parts.append(f"SSTP0  RX={vpn_stat_delta.get('rx','?')}  "
                         f"TX={vpn_stat_delta.get('tx','?')}")
        if pppoe_stat_delta:
            parts.append(f"PPPoE  RX={pppoe_stat_delta.get('rx','?')}  "
                         f"TX={pppoe_stat_delta.get('tx','?')}")
        print("  " + " │ ".join(parts))

    def show(label: str, items: list[dict]):
        if not items:
            return
        total_out = sum(i["d_out"] for i in items)
        total_in  = sum(i["d_in"]  for i in items)
        print(f"\n  [{label}]  {len(items)} dst  "
              f"Σ ↑{fmt_pps(total_out, delta_secs)}  "
              f"↓{fmt_pps(total_in, delta_secs)}")
        for a in items[:top]:
            name = (rdns.get(a["dst_ip"]) or "")[:34]
            port_str = format_ports(a["ports"])[:16]
            print(f"    {a['dst_ip']:15s} {name:34s} {port_str:16s}  "
                  f"↑{fmt_pps(a['d_out'], delta_secs):>9s}  "
                  f"↓{fmt_pps(a['d_in'], delta_secs):>9s}")

    show(f"VPN via SSTP0", vpn_dsts)
    show(f"DIRECT via PPPoE", direct_dsts)
    if other_dsts:
        show(f"OTHER / LAN-local", other_dsts)


def parse_stat_speeds(text: str) -> dict:
    """Pick rxspeed/txspeed lines out of `show interface X stat` output."""
    out = {}
    for ln in text.splitlines():
        if ":" in ln:
            k, _, v = ln.partition(":")
            k = k.strip().lower()
            if k in ("rxspeed", "txspeed"):
                out[k] = v.strip()
    return out


def fmt_bps(bps_str: Any) -> str:
    try:
        x = float(bps_str or 0)
    except (ValueError, TypeError):
        return "?"
    for unit in ("bps", "Kbps", "Mbps", "Gbps"):
        if x < 1000:
            return f"{x:.0f}{unit}" if unit == "bps" else f"{x:.1f}{unit}"
        x /= 1000
    return f"{x:.1f}Tbps"


def main() -> int:
    ap = argparse.ArgumentParser(description="Live NAT watcher for a single host")
    ap.add_argument("--host", required=True)
    ap.add_argument("--router", default=os.environ.get("ROUTER_HOST", DEFAULT_HOST))
    ap.add_argument("--user", default=os.environ.get("ROUTER_USER", DEFAULT_USER))
    ap.add_argument("--vpn-iface", default="SSTP0")
    ap.add_argument("--interval", type=float, default=5.0,
                    help="Seconds between NAT snapshots (default: 5)")
    ap.add_argument("--duration", type=float, default=0,
                    help="Stop after this many seconds (0 = run until Ctrl+C)")
    ap.add_argument("--top", type=int, default=10,
                    help="Show top-N destinations per section")
    ap.add_argument("--rdns", action="store_true",
                    help="Reverse-DNS new destinations (cached)")
    args = ap.parse_args()

    pw = os.environ.get("ROUTER_PASS")
    if not pw:
        print("ROUTER_PASS is not set", file=sys.stderr)
        return 2

    prev: dict = {}
    rdns: dict[str, str] = {}
    last_tick = time.time()
    started = time.time()

    # Live interface speeds via `show interface … stat`
    def speeds(kn: KeeneticSession, iface: str) -> dict:
        try:
            txt = kn.run(f"show interface {iface} stat", timeout=10)
        except Exception:
            return {}
        s = parse_stat_speeds(txt)
        return {
            "rx": fmt_bps(s.get("rxspeed")),
            "tx": fmt_bps(s.get("txspeed")),
        }

    print(f"Watching {args.host} every {args.interval}s"
          + (f" for {args.duration}s" if args.duration else " (Ctrl+C to stop)")
          + ". Press Ctrl+C anytime.")

    try:
        with KeeneticSession(host=args.router, user=args.user, password=pw) as kn:
            # Discover VPN local IP and PPPoE public IP once.
            info = kn.run(f"show interface {args.vpn_iface}", timeout=10)
            m = re.search(r"address:\s*(\d+\.\d+\.\d+\.\d+)", info)
            vpn_local = m.group(1) if m else ""
            try:
                ppp = kn.run("show interface PPPoE0", timeout=10)
                m = re.search(r"address:\s*(\d+\.\d+\.\d+\.\d+)", ppp)
                pppoe_ip = m.group(1) if m else None
            except Exception:
                pppoe_ip = None

            print(f"  VPN local IP (SSTP0): {vpn_local or '(unknown)'}")
            print(f"  PPPoE public IP:      {pppoe_ip or '(unknown)'}")

            while True:
                tick_start = time.time()
                raw = kn.run("show ip nat", timeout=args.interval + 30)
                curr_flows = parse_nat(raw)

                # Live iface speeds (best-effort)
                vpn_sp = speeds(kn, args.vpn_iface) if vpn_local else {}
                ppp_sp = speeds(kn, "PPPoE0")

                delta_t = tick_start - last_tick if prev else args.interval
                last_tick = tick_start

                # First tick has no baseline — rates would be bogus
                # (= total packets of each flow since it was opened).
                # Capture baseline quietly and render from the 2nd tick.
                if not prev:
                    ts = time.strftime("%H:%M:%S")
                    n = sum(1 for f in curr_flows if f["lan_ip"] == args.host)
                    parts = []
                    if vpn_sp:
                        parts.append(f"SSTP0 RX={vpn_sp.get('rx','?')} TX={vpn_sp.get('tx','?')}")
                    if ppp_sp:
                        parts.append(f"PPPoE RX={ppp_sp.get('rx','?')} TX={ppp_sp.get('tx','?')}")
                    print(f"\n── {ts} warm-up (baseline captured, {n} flows from {args.host}) ──")
                    if parts:
                        print("  " + " │ ".join(parts))
                    prev = {flow_key(f): f for f in curr_flows if f["lan_ip"] == args.host}
                else:
                    diffs = diff_snapshots(prev, curr_flows, args.host)

                    if args.rdns:
                        for f in diffs:
                            ip = f["dst_ip"]
                            if ip not in rdns:
                                rdns[ip] = rdns_one(ip, timeout=0.8)

                    vpn_flows    = [f for f in diffs if classify(f, vpn_local, pppoe_ip) == "VPN"]
                    direct_flows = [f for f in diffs if classify(f, vpn_local, pppoe_ip) == "PPPoE"]
                    other_flows  = [f for f in diffs if classify(f, vpn_local, pppoe_ip) in ("OTHER", "?")]

                    tick_print(
                        args.host, delta_t,
                        aggregate_by_dst(vpn_flows),
                        aggregate_by_dst(direct_flows),
                        aggregate_by_dst(other_flows),
                        rdns, args.top,
                        vpn_sp, ppp_sp,
                    )

                    prev = {flow_key(f): f for f in curr_flows if f["lan_ip"] == args.host}

                if args.duration and (time.time() - started) >= args.duration:
                    break

                sleep_for = args.interval - (time.time() - tick_start)
                if sleep_for > 0:
                    time.sleep(sleep_for)
                elif sleep_for < -1:
                    print(f"  [warn] tick overran interval by {-sleep_for:.1f}s",
                          file=sys.stderr)

    except KeyboardInterrupt:
        print("\n[stopped]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
