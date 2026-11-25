"""Actual Linux packets, routes, NAT, nft priority and tc counters in CI.

NSS firmware is absent on the runner. This suite establishes the Linux half
of the contract and deliberately does not translate NSS qdiscs into software
qdiscs and claim a hardware result.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

from multidial.nss import Profile, upload_rules

pytestmark = pytest.mark.skipif(os.environ.get("MULTIDIAL_NETWORK_TESTS") != "1",
                                reason="requires isolated privileged CI network job")


class Lab:
    def __init__(self):
        self.prefix = "md" + uuid.uuid4().hex[:8]
        self.names = {role: f"{self.prefix}-{role}" for role in ("dut", "lan", "up")}
        self.processes = []

    def run(self, role, *args, data=None, check=True):
        result = subprocess.run(["ip", "netns", "exec", self.names[role], *args],
                                input=data, text=True, capture_output=True,
                                timeout=10)
        if check and result.returncode:
            pytest.fail(f"{role} {args}: {result.stderr}\ninput: {data}")
        return result

    def ip(self, role, *args):
        return self.run(role, "ip", *args)

    def close(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
        for name in self.names.values():
            subprocess.run(["ip", "netns", "del", name], capture_output=True, timeout=5)

    def create(self):
        for name in self.names.values():
            subprocess.run(["ip", "netns", "add", name], check=True, timeout=5)
        for role in self.names:
            self.ip(role, "link", "set", "lo", "up")
        for left, right, a, b in [("dut", "lan", "lan", "eth0"),
                                   ("dut", "up", "phy", "eth0")]:
            self.ip(left, "link", "add", a, "type", "veth", "peer", "name", "peer0")
            self.ip(left, "link", "set", "peer0", "netns", self.names[right])
            self.ip(right, "link", "set", "peer0", "name", b)
            self.ip(left, "link", "set", a, "up")
            self.ip(right, "link", "set", b, "up")
        self.ip("dut", "addr", "add", "198.18.0.1/24", "dev", "lan")
        self.ip("lan", "addr", "add", "198.18.0.2/24", "dev", "eth0")
        self.ip("lan", "addr", "add", "198.18.0.3/24", "dev", "eth0")
        self.ip("lan", "route", "add", "default", "via", "198.18.0.1")
        self.ip("up", "addr", "add", "192.0.2.1/24", "dev", "eth0")
        self.run("dut", "sysctl", "-qw", "net.ipv4.ip_forward=1")
        self.run("dut", "sysctl", "-qw", "net.ipv4.conf.all.rp_filter=0")
        for slot in (0, 1):
            dev, addr, table = f"dial{slot}", f"192.0.2.{slot + 2}", str(100 + slot)
            self.ip("dut", "link", "add", dev, "link", "phy", "type", "macvlan", "mode", "private")
            self.ip("dut", "link", "set", dev, "up")
            self.ip("dut", "addr", "add", f"{addr}/24", "dev", dev)
            self.run("dut", "sysctl", "-qw", f"net.ipv4.conf.{dev}.rp_filter=0")
            self.ip("dut", "route", "add", "table", table, "192.0.2.0/24", "dev", dev)
            self.ip("dut", "route", "add", "table", table, "198.18.0.0/24", "dev", "lan")
            self.ip("dut", "route", "add", "table", table, "default", "via", "192.0.2.1", "dev", dev)
            self.ip("dut", "rule", "add", "pref", table, "fwmark", f"{(slot+1)*256}/0x3f00", "table", table)
        self.run("dut", "nft", "-f", "-", data='''
add table ip route_test
add chain ip route_test prerouting { type filter hook prerouting priority mangle; policy accept; }
add rule ip route_test prerouting ct state established,related meta mark set ct mark
add rule ip route_test prerouting ip saddr 198.18.0.2 meta mark set 0x100 ct mark set meta mark
add rule ip route_test prerouting ip saddr 198.18.0.3 meta mark set 0x200 ct mark set meta mark
add chain ip route_test nat_out { type nat hook postrouting priority srcnat; policy accept; }
add rule ip route_test nat_out oifname "dial0" masquerade
add rule ip route_test nat_out oifname "dial1" masquerade
''')
        profile = Profile.parse({"physical": "phy", "ifb": "ifb-test",
            "default_download_kbit": 1000, "default_upload_kbit": 1000,
            "uplinks": [{"slot": slot, "device": f"dial{slot}", "address": f"192.0.2.{slot+2}",
                         "download_kbit": 30000, "upload_kbit": 10000} for slot in (0, 1)]})
        self.run("dut", "nft", "-f", "-", data=upload_rules(profile))
        # Inspect at the physical egress, after macvlan and NAT. These are real
        # packet counters and are independent of the generated upload rules.
        rules = '''add table netdev inspect
add chain netdev inspect egress { type filter hook egress device "phy" priority 0; policy accept; }
'''
        for slot in (0, 1):
            rules += (f'add counter netdev inspect line{slot}\n'
                      f'add rule netdev inspect egress ip saddr 192.0.2.{slot+2} '
                      f'meta mark 0x{(slot+1)*256:x} meta priority 0x{(0x100+slot)<<16:08x} '
                      f'counter name line{slot}\n')
        self.run("dut", "nft", "-f", "-", data=rules)
        self.run("dut", "tc", "qdisc", "add", "dev", "phy", "root", "handle", "1:", "htb", "default", "10")
        self.run("dut", "tc", "class", "add", "dev", "phy", "parent", "1:",
                 "classid", "1:10", "htb", "rate", "100mbit")
        self.run("dut", "tc", "qdisc", "add", "dev", "phy", "parent", "1:10", "handle", "10:", "fq_codel")


@pytest.fixture
def lab():
    current = Lab()
    try:
        current.create()
        yield current
    finally:
        current.close()


def counter(lab, slot):
    result = lab.run("dut", "nft", "-j", "list", "counter", "netdev", "inspect", f"line{slot}")
    return next(item["counter"]["packets"] for item in json.loads(result.stdout)["nftables"]
                if "counter" in item)


@pytest.mark.parametrize("slot", [0, 1])
def test_forwarded_nat_reply_preserves_route_mark_and_physical_priority(lab, slot):
    # An external UDP echo server reports the translated source address.
    server = '''import socket
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.bind(("192.0.2.1",42001));s.settimeout(8)
print("ready",flush=True)
for i in range(5):
 data,peer=s.recvfrom(2048);s.sendto(peer[0].encode()+b":"+data,peer)
'''
    process = subprocess.Popen(["ip", "netns", "exec", lab.names["up"], sys.executable,
                                "-u", "-c", server], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    lab.processes.append(process)
    assert process.stdout.readline().strip() == "ready"
    before = [counter(lab, n) for n in (0, 1)]
    client = f'''import socket
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
s.bind(("198.18.0.{slot+2}",0));s.settimeout(3)
for i in range(5):
 payload=("packet-%s"%i).encode();s.sendto(payload,("192.0.2.1",42001))
 data,peer=s.recvfrom(2048)
 assert data==b"192.0.2.{slot+2}:"+payload,(data,peer)
print("five round trips")
'''
    assert lab.run("lan", sys.executable, "-c", client).stdout.strip() == "five round trips"
    assert process.wait(timeout=3) == 0
    after = [counter(lab, n) for n in (0, 1)]
    assert after[slot] - before[slot] == 5
    assert after[1-slot] == before[1-slot]
    stats = json.loads(lab.run("dut", "tc", "-s", "-j", "qdisc", "show", "dev", "phy").stdout)
    assert any(q.get("kind") == "fq_codel" and q.get("packets", 0) >= 5 for q in stats)
    print(json.dumps({"slot": slot, "physical_packets": after[slot]-before[slot],
                      "nat_reply_packets": 5, "other_line_delta": after[1-slot]-before[1-slot]}))
