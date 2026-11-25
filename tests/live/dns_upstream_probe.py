#!/usr/bin/env python3
"""Real-router DNS measurement: dnsmasq counters + per-upstream latency/loss.

Run ON the router (python3 is there; dig is not):

  python3 dns_upstream_probe.py stats
      dnsmasq's own CHAOS counters via 127.0.0.1: cache size/insertions/
      evictions/hits/misses and per-upstream "queries sent / failed".
  python3 dns_upstream_probe.py sample SECONDS server [server ...]
      Every second, one A query per upstream (round-robin over the configured sample
      domains, every 5th tick a random never-cached name to force recursion),
      sent in parallel straight to the upstream (bypassing dnsmasq). Prints
      each timeout/SERVFAIL as it happens, then p50/p90/p99/max per upstream.
  python3 dns_upstream_probe.py split NAME server [server ...]
      rcode + A records for NAME from each upstream (split-horizon check).

Set MD_DNS_DOMAINS to space-separated names for sample mode.
"""
from __future__ import annotations

import os
import random
import socket
import struct
import sys
import threading
import time

TIMEOUT = 2.0


def build(name: str, qtype: int, qclass: int, tid: int) -> bytes:
    q = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)
    for label in name.rstrip(".").split("."):
        q += bytes([len(label)]) + label.encode()
    return q + b"\x00" + struct.pack(">HH", qtype, qclass)


def query(server: str, name: str, qtype: int = 1, qclass: int = 1):
    """-> (ms or None on timeout, rcode, raw reply)."""
    tid = random.randrange(65536)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(TIMEOUT)
    t0 = time.monotonic()
    try:
        s.sendto(build(name, qtype, qclass, tid), (server, 53))
        while True:
            data, _ = s.recvfrom(4096)
            if len(data) >= 12 and struct.unpack(">H", data[:2])[0] == tid:
                return (time.monotonic() - t0) * 1000, data[3] & 0x0F, data
    except OSError:
        return None, None, b""
    finally:
        s.close()


def _skip_name(data: bytes, i: int) -> int:
    while True:
        n = data[i]
        if n == 0:
            return i + 1
        if n & 0xC0 == 0xC0:
            return i + 2
        i += n + 1


def answers(data: bytes):
    """-> list of (type, rdata bytes) from the answer section."""
    qd, an = struct.unpack(">HH", data[4:8])
    i = 12
    for _ in range(qd):
        i = _skip_name(data, i) + 4
    out = []
    for _ in range(an):
        i = _skip_name(data, i)
        rtype, _cls, _ttl, rdlen = struct.unpack(">HHIH", data[i:i + 10])
        i += 10
        out.append((rtype, data[i:i + rdlen]))
        i += rdlen
    return out


def txt_strings(rdata: bytes):
    out, i = [], 0
    while i < len(rdata):
        n = rdata[i]
        out.append(rdata[i + 1:i + 1 + n].decode(errors="replace"))
        i += 1 + n
    return out


def cmd_stats() -> None:
    for name in ("cachesize.bind", "insertions.bind", "evictions.bind",
                 "hits.bind", "misses.bind", "servers.bind"):
        ms, rcode, data = query("127.0.0.1", name, qtype=16, qclass=3)
        if ms is None or rcode != 0:
            print(f"{name}: no answer (rcode={rcode})")
            continue
        vals = [" ".join(txt_strings(r)) for t, r in answers(data) if t == 16]
        print(f"{name}: " + (" | ".join(vals) if vals else "-"))


def pct(xs, p):
    if not xs:
        return float("nan")
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def cmd_sample(seconds: int, servers, domains) -> None:
    res = {s: {"ok": [], "cold": [], "fail": 0, "servfail": 0, "n": 0} for s in servers}
    lock = threading.Lock()

    def one(server, name, cold, stamp):
        ms, rcode, _ = query(server, name)
        with lock:
            r = res[server]
            r["n"] += 1
            if ms is None:
                r["fail"] += 1
                print(f"{stamp} TIMEOUT  {server:15} {name}", flush=True)
            elif rcode == 2:
                r["servfail"] += 1
                print(f"{stamp} SERVFAIL {server:15} {name} {ms:.0f}ms", flush=True)
            else:
                r["cold" if cold else "ok"].append(ms)

    end = time.monotonic() + seconds
    tick = 0
    while time.monotonic() < end:
        t_tick = time.monotonic()
        cold = tick % 5 == 4
        base = domains[tick % len(domains)]
        name = f"md{random.randrange(16**8):08x}.{base.split('.', 1)[-1]}" if cold else base
        stamp = time.strftime("%H:%M:%S")
        th = [threading.Thread(target=one, args=(s, name, cold, stamp)) for s in servers]
        for t in th:
            t.start()
        for t in th:
            t.join()
        tick += 1
        time.sleep(max(0.0, 1.0 - (time.monotonic() - t_tick)))

    print(f"\n{'server':15} {'n':>5} {'timeout':>7} {'srvfail':>7} | warm p50/p90/p99/max ms"
          f" | cold p50/p90/max ms")
    for s in servers:
        r = res[s]
        w, c = r["ok"], r["cold"]
        print(f"{s:15} {r['n']:5} {r['fail']:7} {r['servfail']:7} | "
              f"{pct(w,50):.0f}/{pct(w,90):.0f}/{pct(w,99):.0f}/{max(w) if w else float('nan'):.0f} | "
              f"{pct(c,50):.0f}/{pct(c,90):.0f}/{max(c) if c else float('nan'):.0f}")


def cmd_split(name: str, servers) -> None:
    for s in servers:
        ms, rcode, data = query(s, name)
        if ms is None:
            print(f"{s:15} TIMEOUT")
            continue
        ips = [socket.inet_ntoa(r) for t, r in answers(data) if t == 1 and len(r) == 4]
        print(f"{s:15} rcode={rcode} {ms:.0f}ms {' '.join(ips) or '-'}")


def main(argv) -> int:
    if len(argv) >= 2 and argv[1] == "stats":
        cmd_stats()
        return 0
    if len(argv) >= 4 and argv[1] == "sample":
        domains = os.environ.get("MD_DNS_DOMAINS", "").split()
        if not domains:
            print("set MD_DNS_DOMAINS to the sample query names", file=sys.stderr)
            return 64
        cmd_sample(int(argv[2]), argv[3:], domains)
        return 0
    if len(argv) >= 4 and argv[1] == "split":
        cmd_split(argv[2], argv[3:])
        return 0
    print(__doc__.strip().split("\n\n")[1], file=sys.stderr)
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv))
