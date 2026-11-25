"""Validate one controlled NSS leaf-counter interval; reads tc -s text files."""
import argparse
import json
from pathlib import Path
import re


def counters(text):
    result = {}
    handle = None
    for line in text.splitlines():
        match = re.match(r"qdisc nssfq_codel ([0-9a-f]+): .*parent 1:([0-9a-f]+)", line)
        if line.startswith("qdisc "):
            handle = match.group(1) if match else None
        sent = re.match(r"\s*Sent (\d+) bytes (\d+) pkt", line)
        if handle and sent:
            if handle in result:
                raise ValueError("duplicate leaf counter")
            result[handle] = (int(sent[1]), int(sent[2]))
            handle = None
    return result


def check_interval(before, after, active, monitored, *, leaf_base=0x100):
    if leaf_base not in (0x100, 0x200):
        raise ValueError("unsupported leaf tag space")
    leaves = {f"{leaf_base + n:x}": n for n in monitored}
    if not active or not active <= set(monitored):
        raise ValueError("active slots must be a nonempty subset of monitored slots")
    output = {}
    for handle, slot in leaves.items():
        if handle not in before or handle not in after:
            raise ValueError(f"missing leaf {handle}")
        byte_delta = after[handle][0] - before[handle][0]
        packet_delta = after[handle][1] - before[handle][1]
        if byte_delta < 0 or packet_delta < 0:
            raise ValueError("counter reset during interval")
        if slot in active and (byte_delta == 0 or packet_delta == 0):
            raise ValueError(f"active slot {slot} received no traffic")
        if slot not in active and (byte_delta or packet_delta):
            raise ValueError(f"inactive slot {slot} received traffic")
        output[slot] = {"bytes": byte_delta, "packets": packet_delta}
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("--active", nargs="+", type=int, required=True)
    parser.add_argument("--monitored", nargs="+", type=int, required=True)
    parser.add_argument("--direction", choices=("upload", "download"), default="upload")
    args = parser.parse_args()
    try:
        result = check_interval(counters(args.before.read_text()), counters(args.after.read_text()),
                                set(args.active), args.monitored,
                                leaf_base=0x200 if args.direction == "download" else 0x100)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
