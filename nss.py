"""Explicit NSS multi-uplink experiment planner.

python -m multidial.nss PROFILE.json prints a JSON execution plan.
It never applies rules. Uplinks remain VEPA macvlans for software routing.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import ipaddress
import json
from pathlib import Path
import re

# Shared with the original standalone planner.
from .tools.nss_ingress_plan import ingress_plan, rate_params


def interface_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.@-]{0,14}", value):
        raise ValueError("invalid interface name")
    return value


@dataclass(frozen=True)
class Uplink:
    slot: int
    device: str
    address: str
    download_kbit: int
    upload_kbit: int

    @property
    def leaf(self):
        return f"{0x100 + self.slot:x}"


@dataclass(frozen=True)
class Profile:
    physical: str
    ifb: str
    default_download_kbit: int
    default_upload_kbit: int
    uplinks: tuple[Uplink, ...]

    @classmethod
    def parse(cls, data):
        def positive_rate(value):
            if type(value) is not int:
                raise ValueError("rates must be integer kbit/s")
            rate_params(value)
            return value

        physical = interface_name(data["physical"])
        ifb = interface_name(data["ifb"])
        download = positive_rate(data["default_download_kbit"])
        upload = positive_rate(data["default_upload_kbit"])
        if physical == ifb:
            raise ValueError("physical and IFB must differ")
        uplinks = []
        devices, slots, addresses = {physical, ifb}, set(), set()
        for item in data["uplinks"]:
            slot = item["slot"]
            if type(slot) is not int or not 0 <= slot <= 254 or slot in slots:
                raise ValueError("uplink slots must be unique integers (0..254)")
            device = interface_name(item["device"])
            address = ipaddress.IPv4Address(item["address"])
            if address.is_unspecified or address.is_multicast or address.is_loopback or int(address) == 0xffffffff:
                raise ValueError("uplink needs an assigned unicast IPv4")
            if device in devices or address in addresses:
                raise ValueError("uplink devices and addresses must be unique")
            uplinks.append(Uplink(slot, device, str(address),
                                  positive_rate(item["download_kbit"]),
                                  positive_rate(item["upload_kbit"])))
            devices.add(device)
            addresses.add(address)
            slots.add(slot)
        if not uplinks:
            raise ValueError("at least one uplink required")
        return cls(physical, ifb, download, upload,
                   tuple(sorted(uplinks, key=lambda line: line.slot)))


def upload_rules(profile):
    # Route selection has completed by postrouting. Changing skb priority
    # leaves skb mark (used by mwan3) and the low 16 priority bits intact.
    rules = [
        "add table inet multidial_nss",
        "add chain inet multidial_nss upload { type filter hook postrouting priority -140; policy accept; }",
    ]
    for line in profile.uplinks:
        rules.append(
            f'add rule inet multidial_nss upload meta nfproto ipv4 oifname "{line.device}" '
            f"counter meta priority set (meta priority & 0x0000ffff) | 0x{(0x100 + line.slot) << 16:08x}"
        )
    return "\n".join(rules) + "\n"


def upload_tree(profile):
    dev = profile.physical
    aggregate = profile.default_upload_kbit + sum(line.upload_kbit for line in profile.uplinks)
    opts = "nssfq_codel target 5ms interval 100ms flows 128 limit 256 accel_mode 0"
    result = [
        f"qdisc add dev {dev} root handle 1: nsshtb accel_mode 0",
        f"class add dev {dev} parent 1: classid 1:1 nsshtb {rate_params(aggregate)}",
        f"class add dev {dev} parent 1:1 classid 1:2 nsshtb {rate_params(profile.default_upload_kbit)}",
        f"qdisc add dev {dev} parent 1:2 handle 2: {opts} set_default",
    ]
    for line in profile.uplinks:
        result += [
            f"class add dev {dev} parent 1:1 classid 1:{line.leaf} nsshtb {rate_params(line.upload_kbit)}",
            f"qdisc add dev {dev} parent 1:{line.leaf} handle {line.leaf}: {opts}",
        ]
    return result


def rate_change(profile, slot, download_kbit, upload_kbit):
    line = next((line for line in profile.uplinks if line.slot == slot), None)
    if line is None:
        raise ValueError("unknown uplink slot")
    for value in (download_kbit, upload_kbit):
        if type(value) is not int:
            raise ValueError("rates must be integer kbit/s")
        rate_params(value)
    if download_kbit > line.download_kbit or upload_kbit > line.upload_kbit:
        raise ValueError("rate update exceeds the configured class ceiling")
    return [
        f"class change dev {profile.ifb} parent 1:1 classid 1:{line.leaf} nsshtb {rate_params(download_kbit)}",
        f"class change dev {profile.physical} parent 1:1 classid 1:{line.leaf} nsshtb {rate_params(upload_kbit)}",
    ]


def autorate_fragment(profile, line):
    # Upstream rejects identical dl_if/ul_if. Keep the IGS IFB as dl_if but
    # measure this logical line's RX, rather than the shared IFB total.
    return "\n".join([
        f"dl_if='{profile.ifb}'",
        f"ul_if='{line.device}'",
        f"rx_bytes_path='/sys/class/net/{line.device}/statistics/rx_bytes'",
        f"tx_bytes_path='/sys/class/net/{line.device}/statistics/tx_bytes'",
        f"ping_extra_args='-I {line.device}'",
        f"md_nss_dl_device='{profile.ifb}'",
        f"md_nss_ul_device='{profile.physical}'",
        f"md_nss_leaf='{line.leaf}'",
        ". /usr/lib/multidial/multidial/tools/nss-class-shaper.sh",
        "",
    ])


def plan(profile):
    # Mapping must exist before root NSS qdisc init recognizes an IGS IFB.
    return {
        "mode": "experimental-plan-hardware-validation-failed",
        "requirements": [
            "dedicated NSS physical uplink, outside every bridge and management path",
            "every listed uplink is a VEPA macvlan linked to this physical interface",
            "listed IPv4 addresses match the uplinks' live DHCP addresses",
            "no existing SQM, ingress filters or NSS mapping on the experiment interfaces",
            "independent recovery and cleanup of experiment-owned objects",
        ],
        "setup": [
            ["ip", "link", "add", "name", profile.ifb, "type", "ifb"],
            ["ip", "link", "set", "dev", profile.ifb, "up"],
        ],
        "bind_tc": [
            f"qdisc add dev {profile.physical} handle ffff: ingress",
            f"filter add dev {profile.physical} parent ffff: protocol all pref 49152 "
            f"u32 match u32 0 0 action nssmirred redirect dev {profile.ifb} fromdev {profile.physical}",
        ],
        "upload_tc": upload_tree(profile),
        "download_tc": ingress_plan(profile.ifb,
                                   [(line.slot, line.address, line.download_kbit) for line in profile.uplinks],
                                   profile.default_download_kbit),
        # Install nft batch only after both trees exist.
        "upload_nft": upload_rules(profile),
        "autorate_fragments": {
            line.device: autorate_fragment(profile, line) for line in profile.uplinks
        },
        "verify": [
            ["tc", "-s", "qdisc", "show", "dev", profile.physical],
            ["tc", "-s", "qdisc", "show", "dev", profile.ifb],
            ["nft", "list", "table", "inet", "multidial_nss"],
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", type=Path)
    parser.add_argument("--rate", nargs=3, type=int, metavar=("SLOT", "DOWNLOAD_KBIT", "UPLOAD_KBIT"))
    args = parser.parse_args()
    try:
        profile = Profile.parse(json.loads(args.profile.read_text(encoding="utf-8")))
        result = rate_change(profile, *args.rate) if args.rate else plan(profile)
    except (ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
