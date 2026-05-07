"""Per-host connection trace from Keenetic NAT table.

`show ip nat` is the only place on stock Keenetic firmware that exposes
per-connection L4 tuples. Each translation entry ties a LAN src (IP:port)
to a WAN dst (IP:port), plus packet count, and critically, the external
IP used by the router — which distinguishes VPN egress (SSTP0 local IP)
from the normal PPPoE WAN.

Usage:
    ROUTER_PASS='...' python kn_trace_host.py --host <lan-client-ip>
    python kn_trace_host.py --host <lan-client-ip> --rdns --top 30
    python kn_trace_host.py --host <lan-client-ip> --vpn-local-ip <vpn-local-ip>
"""
from __future__ import annotations

import argparse
import io
import os
import re
import socket
import sys
from collections import defaultdict
from typing import Iterable

from kn_common import DEFAULT_HOST, DEFAULT_USER, KeeneticSession

if sys.platform == "win32" and not getattr(sys.stdout, "_utf8", False):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    sys.stdout._utf8 = True  # type: ignore[attr-defined]


# `show ip nat` line shape (column-aligned):
#   Type | In  | Source           Port     Destination      Port     Packets
#   UDP          <lan-client-ip>  28328    <upstream-ip>    55899    3394
#
# Each "translation" = two rows (forward + reverse), separated by `---`.
# We only care about the forward row: src is LAN IP, and the reverse row's
# source IP tells us which WAN IP the router used as external.

ROW_RE = re.compile(
    r"^\s*(?P<proto>TCP|UDP|ICMP)?\s+"
    r"(?P<src>\d+\.\d+\.\d+\.\d+)\s+"
    r"(?P<sport>\d+)\s+"
    r"(?P<dst>\d+\.\d+\.\d+\.\d+)\s+"
    r"(?P<dport>\d+)\s+"
    r"(?P<packets>\d+)"
)


def parse_nat(text: str) -> list[dict]:
    """Return list of translations. Each translation is a dict with forward
    and reverse rows fused:
        {proto, lan_ip, lan_port, wan_ext_ip, wan_ext_port,
         dst_ip, dst_port, packets_out, packets_in}

    The NAT table emits two lines per flow: forward (LAN src → dst) and
    reverse (dst → router-external IP). The two lines are adjacent and
    separated from the next flow by a dashed ruler line.
    """
    blocks: list[list[dict]] = []
    cur: list[dict] = []
    carry_proto: str | None = None
    for line in text.splitlines():
        if "---" in line:
            if cur:
                blocks.append(cur)
                cur = []
                carry_proto = None
            continue
        m = ROW_RE.match(line)
        if not m:
            continue
        proto = m.group("proto") or carry_proto
        if m.group("proto"):
            carry_proto = proto
        cur.append({
            "proto":   proto,
            "src":     m.group("src"),
            "sport":   int(m.group("sport")),
            "dst":     m.group("dst"),
            "dport":   int(m.group("dport")),
            "packets": int(m.group("packets")),
        })
    if cur:
        blocks.append(cur)

    flows: list[dict] = []
    for blk in blocks:
        if not blk:
            continue
        fwd = blk[0]
        rev = blk[1] if len(blk) > 1 else None
        flows.append({
            "proto":        fwd["proto"] or (rev["proto"] if rev else None),
            "lan_ip":       fwd["src"],
            "lan_port":     fwd["sport"],
            "dst_ip":       fwd["dst"],
            "dst_port":     fwd["dport"],
            "packets_out":  fwd["packets"],
            # On the reverse row, "src" is the dst (from router's POV the
            # remote replying), and "dst" is the router's external IP —
            # that's what tells us which WAN the flow egressed through.
            "wan_ext_ip":   rev["dst"] if rev else None,
            "wan_ext_port": rev["dport"] if rev else None,
            "packets_in":   rev["packets"] if rev else 0,
        })
    return flows


# ── Well-known port labels — small, cheap, avoids external deps ─────────────

WELL_KNOWN = {
    53: "DNS", 80: "HTTP", 443: "HTTPS", 22: "SSH", 21: "FTP",
    25: "SMTP", 465: "SMTPS", 587: "SMTP-sub", 110: "POP3", 143: "IMAP",
    993: "IMAPS", 995: "POP3S", 123: "NTP", 8080: "HTTP-alt",
    8443: "HTTPS-alt", 3389: "RDP", 5353: "mDNS", 5060: "SIP",
    1900: "SSDP", 445: "SMB", 135: "RPC", 993: "IMAPS",
    9993: "ZeroTier", 4500: "IPsec-NAT", 500: "IKE",
    1194: "OpenVPN", 51820: "WireGuard",
    3478: "STUN", 5349: "STUN-TLS",
}


def port_label(port: int) -> str:
    return WELL_KNOWN.get(port, str(port))


def rdns_one(ip: str, timeout: float = 1.5) -> str:
    """Best-effort reverse DNS. Returns '' on failure."""
    socket.setdefaulttimeout(timeout)
    try:
        host, _, _ = socket.gethostbyaddr(ip)
        return host
    except Exception:
        return ""


def rdns_many(ips: Iterable[str], timeout: float = 1.5) -> dict:
    out = {}
    for ip in ips:
        out[ip] = rdns_one(ip, timeout)
    return out


# ── Reporting ───────────────────────────────────────────────────────────────

def fmt_pkts(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def render_report(host_ip: str, flows: list[dict], vpn_local_ip: str,
                  pppoe_ip: str | None, rdns_cache: dict, top: int) -> str:
    lines: list[str] = []
    p = lines.append
    sep = "=" * 78

    p(sep)
    p(f"CONNECTION TRACE  —  {host_ip}")
    p(f"VPN local IP (SSTP0): {vpn_local_ip}")
    if pppoe_ip:
        p(f"PPPoE public IP:      {pppoe_ip}")
    p(sep)
    p("")

    mine = [f for f in flows if f["lan_ip"] == host_ip]
    if not mine:
        p(f"(в NAT-таблице нет активных соединений от {host_ip} прямо сейчас)")
        p("Примечание: NAT-таблица живёт только пока соединение активно,")
        p("после закрытия сессии запись исчезает — запусти скрипт повторно.")
        return "\n".join(lines)

    # Classify by egress
    def egress(f: dict) -> str:
        ext = f.get("wan_ext_ip")
        if ext == vpn_local_ip:
            return "VPN (SSTP0)"
        if pppoe_ip and ext == pppoe_ip:
            return "PPPoE (direct)"
        if ext is None:
            return "?"
        return f"other ({ext})"

    for f in mine:
        f["_egress"] = egress(f)

    total = len(mine)
    via_vpn = sum(1 for f in mine if f["_egress"] == "VPN (SSTP0)")
    via_pppoe = sum(1 for f in mine if f["_egress"].startswith("PPPoE"))
    pkt_vpn = sum(f["packets_out"] + f["packets_in"]
                  for f in mine if f["_egress"] == "VPN (SSTP0)")
    pkt_pppoe = sum(f["packets_out"] + f["packets_in"]
                    for f in mine if f["_egress"].startswith("PPPoE"))

    p(f"[Summary]")
    p(f"  Total active flows:  {total}")
    p(f"  Via VPN (SSTP0):     {via_vpn} flows, {fmt_pkts(pkt_vpn)} packets")
    p(f"  Via PPPoE (direct):  {via_pppoe} flows, {fmt_pkts(pkt_pppoe)} packets")
    p("")

    # Group by dst_ip for a flat "where it connects" view
    by_dst: dict[str, list[dict]] = defaultdict(list)
    for f in mine:
        by_dst[f["dst_ip"]].append(f)

    scored = []
    for dst, fl in by_dst.items():
        total_pkts = sum(x["packets_out"] + x["packets_in"] for x in fl)
        scored.append((total_pkts, dst, fl))
    scored.sort(reverse=True)

    # Split into VPN and direct
    for section_name, match in (
        ("Destinations reached through VPN (SSTP0)", lambda f: f["_egress"] == "VPN (SSTP0)"),
        ("Destinations reached directly (PPPoE)",    lambda f: f["_egress"].startswith("PPPoE")),
        ("Other / unclassified",                      lambda f: f["_egress"].startswith("other") or f["_egress"] == "?"),
    ):
        matched = [(pkts, dst, [f for f in fl if match(f)])
                   for pkts, dst, fl in scored]
        matched = [(p_, d, fl) for (p_, d, fl) in matched if fl]
        if not matched:
            continue
        # Recompute per-section packet total for correct sort within section
        matched = [(sum(x["packets_out"] + x["packets_in"] for x in fl), d, fl)
                   for (_, d, fl) in matched]
        matched.sort(reverse=True)

        p(f"[{section_name}]  ({len(matched)} destinations)")
        shown = matched[:top]
        p(f"  {'Destination IP':16s} {'rDNS / hint':38s} {'Ports':18s} {'Pkts out':>10s} {'Pkts in':>10s}")
        p("  " + "-" * 95)
        for pkts, dst, fl in shown:
            rdns = rdns_cache.get(dst, "") or ""
            rdns = rdns[:38]
            ports = sorted({f["dst_port"] for f in fl})
            port_str = ",".join(port_label(p_) for p_ in ports[:5])
            if len(ports) > 5:
                port_str += f",+{len(ports)-5}"
            pout = sum(f["packets_out"] for f in fl)
            pin = sum(f["packets_in"] for f in fl)
            p(f"  {dst:16s} {rdns:38s} {port_str:18s} "
              f"{fmt_pkts(pout):>10s} {fmt_pkts(pin):>10s}")
        if len(matched) > top:
            p(f"  … и ещё {len(matched) - top} destination (используй --top {len(matched)})")
        p("")

    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Trace connections of a single LAN host via NAT")
    ap.add_argument("--host", required=True, help="LAN IP of the client to trace")
    ap.add_argument("--router", default=os.environ.get("ROUTER_HOST", DEFAULT_HOST))
    ap.add_argument("--user", default=os.environ.get("ROUTER_USER", DEFAULT_USER))
    ap.add_argument("--vpn-iface", default="SSTP0")
    ap.add_argument("--vpn-local-ip", default=None,
                    help="VPN interface local IP (auto-detected via show interface)")
    ap.add_argument("--rdns", action="store_true", help="Resolve reverse DNS for destinations")
    ap.add_argument("--top", type=int, default=40,
                    help="Show top-N destinations per section")
    ap.add_argument("--raw-out", help="Write raw NAT table to this file for debugging")
    args = ap.parse_args()

    pw = os.environ.get("ROUTER_PASS")
    if not pw:
        print("ROUTER_PASS is not set", file=sys.stderr)
        return 2

    vpn_local = args.vpn_local_ip
    pppoe_ip: str | None = None

    with KeeneticSession(host=args.router, user=args.user, password=pw) as kn:
        if not vpn_local:
            info = kn.run(f"show interface {args.vpn_iface}", timeout=10)
            m = re.search(r"address:\s*(\d+\.\d+\.\d+\.\d+)", info)
            if m:
                vpn_local = m.group(1)
        # Try to discover PPPoE public IP too.
        try:
            pppoe = kn.run("show interface PPPoE0", timeout=10)
            m = re.search(r"address:\s*(\d+\.\d+\.\d+\.\d+)", pppoe)
            if m:
                pppoe_ip = m.group(1)
        except Exception:
            pass

        # NAT table can be large — give it room to breathe.
        raw = kn.run("show ip nat", timeout=45)

    if not vpn_local:
        print("[warn] could not determine VPN local IP, VPN vs PPPoE split disabled",
              file=sys.stderr)
        vpn_local = "0.0.0.0"  # placeholder that matches nothing

    if args.raw_out:
        with open(args.raw_out, "w", encoding="utf-8") as f:
            f.write(raw)
        print(f"[raw NAT saved → {args.raw_out}]", file=sys.stderr)

    flows = parse_nat(raw)
    print(f"[parsed {len(flows)} total NAT flows; "
          f"{sum(1 for f in flows if f['lan_ip'] == args.host)} from {args.host}]",
          file=sys.stderr)

    rdns_cache: dict[str, str] = {}
    if args.rdns:
        uniq_dsts = sorted({f["dst_ip"] for f in flows if f["lan_ip"] == args.host})
        print(f"[resolving rDNS for {len(uniq_dsts)} destinations...]", file=sys.stderr)
        rdns_cache = rdns_many(uniq_dsts, timeout=1.5)

    report = render_report(args.host, flows, vpn_local, pppoe_ip, rdns_cache, args.top)
    print(report)

    out_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            f"trace_{args.host.replace('.', '_')}.txt")
    with open(out_file, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\n[report saved → {out_file}]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
