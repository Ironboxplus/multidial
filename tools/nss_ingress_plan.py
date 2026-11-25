"""Print a tc batch for per-line IPv4 shaping on a dedicated NSS IGS IFB."""
import argparse
import ipaddress
import re


def rate_params(kbit):
    # The vendor tc parser stores rate in a u32 number of bytes per second.
    if not 1 <= kbit <= (2**32 - 1) // 125:
        raise ValueError("rate outside tc's positive 32-bit byte-rate range")
    burst = max(2048, (kbit * 5 + 7) // 8)  # five milliseconds, min 2 KiB
    return f"rate {kbit}kbit burst {burst} crate {kbit}kbit cburst {burst}"


def ingress_plan(ifb, lines, default_kbit, change_slot=None):
    if not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.@-]{0,14}", ifb):
        raise ValueError("invalid IFB device name")
    fallback = rate_params(default_kbit)
    classes, addresses = {}, set()
    for slot, addr, kbit in lines:
        slot = int(slot)
        addr = ipaddress.IPv4Address(addr)
        if addr.is_unspecified or addr.is_multicast or int(addr) == 0xffffffff:
            raise ValueError("line address must be assigned unicast IPv4")
        if not 0 <= slot <= 254 or slot in classes or addr in addresses:
            raise ValueError("slots must be unique (0..254), IPv4 addresses must be unique")
        classes[slot] = (addr, rate_params(int(kbit)))
        addresses.add(addr)
    if not classes:
        raise ValueError("at least one line required")

    # tc prints handles in hex. Keying by slot keeps the leaf stable across
    # DHCP renewals, reordering of inputs, and removal of a different line.
    if change_slot is not None:
        if change_slot not in classes:
            raise ValueError("unknown slot for rate change")
        minor = f"{0x100 + change_slot:x}"
        return [f"class change dev {ifb} parent 1: classid 1:{minor} nsshtb {classes[change_slot][1]}"]

    leaf_opts = "nssfq_codel target 5ms interval 100ms flows 128 limit 256 accel_mode 0"
    commands = [
        f"qdisc add dev {ifb} root handle 1: nsshtb accel_mode 0",
        f"class add dev {ifb} parent 1: classid 1:2 nsshtb {fallback}",
        f"qdisc add dev {ifb} parent 1:2 handle 2: {leaf_opts} set_default",
    ]
    for slot, (addr, rate) in sorted(classes.items()):
        minor = f"{0x100 + slot:x}"
        commands.extend([
            f"class add dev {ifb} parent 1: classid 1:{minor} nsshtb {rate}",
            f"qdisc add dev {ifb} parent 1:{minor} handle {minor}: {leaf_opts}",
            # nss_qdisc_iterate_fl uses TC_H_MAJ(res.classid) only.
            f"filter add dev {ifb} parent 1: protocol ip pref {100 + slot} "
            f"u32 match ip dst {addr}/32 flowid {minor}:",
        ])
    return commands


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ifb", help="dedicated IFB already bound to the test WAN by nssmirred")
    parser.add_argument("--line", nargs=3, action="append", required=True,
                        metavar=("SLOT", "WAN_IPV4", "KBIT"))
    parser.add_argument("--default-kbit", required=True, type=int, help="unmatched traffic ceiling")
    parser.add_argument("--change-slot", type=int, help="emit only this existing class rate change")
    args = parser.parse_args()
    try:
        commands = ingress_plan(args.ifb, args.line, args.default_kbit, args.change_slot)
    except ValueError as exc:
        parser.error(str(exc))
    print("\n".join(commands))


if __name__ == "__main__":
    main()
