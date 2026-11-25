"""Read side of mwan3 sticky + the validation for editing it (/api/sticky,
/api/traffic).

Pure functions over command output text: the daemon runs `ipset list`,
`iptables -t mangle -S`, `dnsmasq --version` and the sysfs reads through its
injected Runner and hands the text here. Tests feed anonymized fixture output
(tests/fixtures/sticky).

mwan3 marks a flow with the interface id shifted into mmx_mask (default
0x3f00). The id is the 1-based position of the interface among the `interface`
sections of /etc/config/mwan3 -- /lib/mwan3/mwan3.sh `mwan3_update_iface_to_table`
numbers them in `config_foreach ... interface` order and `mwan3_get_iface_id`
looks the number up; /lib/mwan3/common.sh `mwan3_id2mask` spreads the id's bits
over the mask's set bits (so id 1 -> 0x100, id 5 -> 0x500 with 0x3f00).
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from . import sqm
from .config import Globals, normalize_cidr

MMX_MASK = 0x3F00
TIMEOUT_CHOICES = (300, 600, 1800, 3600)
UNSUPPORTED_DOMAINS_MSG = "dnsmasq 未编译 ipset，暂不支持域名"

MANGLE_CMD = "iptables -t mangle -S"
DNSMASQ_CMD = "dnsmasq --version 2>&1"

_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
_DEVICE_RE = re.compile(r"^dial[0-9]+$")
_DOMAIN_RE = re.compile(r"^(?=.{3,253}$)([a-z0-9-]+\.)+[a-z]{2,}$", re.I)  # = proto isDomain
_MEMBER_RE = re.compile(
    r"^(\d+\.\d+\.\d+\.\d+),(0x[0-9a-fA-F]+)(?:\s+timeout\s+(\d+))?")
_COMMENT_RE = re.compile(r'--comment "(\S+) \d+ \d+"')
_XMARK_RE = re.compile(r"--set-xmark (0x[0-9a-fA-F]+)/(0x[0-9a-fA-F]+)")


def _natural(dev: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", dev)]


def ipset_cmd(rule: str) -> str:
    return f"ipset list mwan3_rule_ipv4_{rule} 2>/dev/null"


def traffic_cmd(devices: List[str]) -> str:
    """One line per dial: `<dial> <ifb4dial tx_bytes> <dial tx_bytes>` (`-` = unreadable).

    The ingress IFB's tx is what was delivered to the LAN side (download); the
    dial's own tx is what left toward the campus (upload)."""
    parts = []
    for dev in devices:
        if not _DEVICE_RE.match(dev):
            continue
        rd = lambda d: (f"$(cat /sys/class/net/{d}/statistics/tx_bytes "
                        "2>/dev/null || echo -)")
        parts.append(f'echo "{dev} {rd(sqm.ifb_name(dev))} {rd(dev)}"')
    return "; ".join(parts) or ":"


def parse_traffic(text: str) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for raw in (text or "").splitlines():
        tok = raw.split()
        if len(tok) != 3 or not _DEVICE_RE.match(tok[0]):
            continue
        entry = {}
        if tok[1].isdigit():
            entry["down_bytes"] = int(tok[1])
        if tok[2].isdigit():
            entry["up_bytes"] = int(tok[2])
        if entry:
            out[tok[0]] = entry
    return out


# --- mwan3 config / mark mapping ------------------------------------------
def _uci_sections(uci_text: str) -> List[Tuple[str, str, Dict[str, str]]]:
    """(name, type, options) per `mwan3.<sec>` of `uci show mwan3`, in file order."""
    order: List[str] = []
    types: Dict[str, str] = {}
    opts: Dict[str, Dict[str, str]] = {}
    for raw in (uci_text or "").splitlines():
        line = raw.strip()
        if "=" not in line:
            continue
        lhs, rhs = line.split("=", 1)
        parts = lhs.split(".")
        if len(parts) < 2 or parts[0] != "mwan3":
            continue
        sec = parts[1]
        if sec not in types:
            order.append(sec)
            types[sec] = ""
            opts[sec] = {}
        val = rhs.strip()
        if len(val) >= 2 and val[0] == "'" and val[-1] == "'":
            val = val[1:-1]
        if len(parts) == 2:
            types[sec] = val
        elif len(parts) == 3:
            opts[sec][parts[2]] = val
    return [(s, types[s], opts[s]) for s in order]


def parse_iface_order(uci_text: str) -> List[str]:
    """Interfaces in mwan3 id order (id = index + 1)."""
    return [n for n, t, _ in _uci_sections(uci_text) if t == "interface"]


def parse_sticky_rules(uci_text: str) -> List[Tuple[str, str]]:
    """(rule, policy) for every mwan3 rule with sticky=1, in rule order."""
    out = []
    for name, typ, opt in _uci_sections(uci_text):
        if typ != "rule" or opt.get("sticky") != "1":
            continue
        policy = opt.get("use_policy", "")
        if _NAME_RE.match(name) and _NAME_RE.match(policy):
            out.append((name, policy))
    return out


def _mask_to_id(mark: int, mask: int) -> int:
    """Inverse of mwan3_id2mask: gather the mark's bits at the mask's set bits."""
    ident = 0
    bit_val = 0
    for bit in range(32):
        if (mask >> bit) & 1:
            if (mark >> bit) & 1:
                ident |= 1 << bit_val
            bit_val += 1
    return ident


def mark_to_device(mark: int, order: List[str], mask: int = MMX_MASK) -> Optional[str]:
    ident = _mask_to_id(mark, mask)
    return order[ident - 1] if 1 <= ident <= len(order) else None


# --- live state parsing ----------------------------------------------------
def parse_pins(rule: str, ipset_text: str, order: List[str]) -> List[dict]:
    """Members of `ipset list mwan3_rule_ipv4_<rule>` (hash:ip,mark): client -> dial.

    A member whose mark maps to no interface (a dial removed since) is dropped:
    it pins nobody anywhere."""
    pins = []
    for raw in (ipset_text or "").splitlines():
        m = _MEMBER_RE.match(raw.strip())
        if not m:
            continue
        device = mark_to_device(int(m.group(2), 16), order)
        if device is None:
            continue
        pins.append({
            "rule": rule,
            "client": m.group(1),
            "device": device,
            "ttl": int(m.group(3)) if m.group(3) is not None else None,
        })
    pins.sort(key=lambda p: tuple(int(x) for x in p["client"].split(".")))
    return pins


def _chain_lines(mangle_text: str, chain: str) -> List[str]:
    prefix = f"-A {chain} "
    return [ln for ln in (mangle_text or "").splitlines() if ln.startswith(prefix)]


def policy_members(mangle_text: str, policy: str) -> List[str]:
    """Interfaces currently in `mwan3_policy_<policy>` (offline ones are removed by
    mwan3; each member rule carries `--comment "<iface> <metric> <weight>"`)."""
    devs = set()
    for ln in _chain_lines(mangle_text, f"mwan3_policy_{policy}"):
        m = _COMMENT_RE.search(ln)
        if m:
            devs.add(m.group(1))
    return sorted(devs, key=_natural)


def restore_members(mangle_text: str, rule: str, order: List[str]) -> List[str]:
    """Interfaces that have a restore rule in `mwan3_rule_<rule>`: the
    `-j MARK --set-xmark <id mark>/<mask>` mwan3_set_sticky_iptables inserts
    (the `--set-xmark 0x0/...` companions only clear a stale mark)."""
    devs = set()
    for ln in _chain_lines(mangle_text, f"mwan3_rule_{rule}"):
        m = _XMARK_RE.search(ln)
        if not m or int(m.group(1), 16) == 0:
            continue
        dev = mark_to_device(int(m.group(1), 16), order, int(m.group(2), 16))
        if dev:
            devs.add(dev)
    return sorted(devs, key=_natural)


def rule_health(mangle_text: str, rule: str, policy: str, order: List[str]) -> dict:
    online = policy_members(mangle_text, policy)
    restore = restore_members(mangle_text, rule, order)
    return {"online": online, "restore": restore,
            "ok": bool(online) and online == restore}


def domains_supported(dnsmasq_version: str) -> bool:
    """True when dnsmasq was built with `ipset` (not `no-ipset`; nftset is no use
    to mwan3, whose iptables rules only match kernel ipsets)."""
    for line in (dnsmasq_version or "").splitlines():
        if line.startswith("Compile time options:"):
            return "ipset" in line.split(":", 1)[1].split()
    return False


def build_status(glb: Globals, mwan3_uci: str, mangle_text: str,
                 ipset_texts: Dict[str, str], dnsmasq_version: str) -> dict:
    order = parse_iface_order(mwan3_uci)
    rules = parse_sticky_rules(mwan3_uci)
    pins: List[dict] = []
    health: Dict[str, dict] = {}
    for rule, policy in rules:
        pins.extend(parse_pins(rule, ipset_texts.get(rule, ""), order))
        health[rule] = rule_health(mangle_text, rule, policy, order)
    return {
        "timeout": glb.sticky_timeout,
        "intranet": {"enabled": glb.intranet_sticky,
                     "ranges": list(glb.intranet_ranges)},
        "service": {"ranges": list(glb.sticky_ranges),
                    "domains": list(glb.sticky_domains)},
        "domains_supported": domains_supported(dnsmasq_version),
        "pins": pins,
        "health": health,
    }


# --- POST /api/sticky validation --------------------------------------------
def _cidr_list(key: str, val) -> List[str]:
    if not isinstance(val, list) or not all(isinstance(v, str) for v in val):
        raise ValueError(f"{key} must be a list of CIDR strings")
    out: List[str] = []
    for item in val:
        try:
            cidr = normalize_cidr(item)
        except ValueError:
            raise ValueError(f"invalid CIDR in {key}: {item!r}")
        if cidr not in out:
            out.append(cidr)
    return out


def _domain_list(val, supported: bool) -> List[str]:
    if not isinstance(val, list) or not all(isinstance(v, str) for v in val):
        raise ValueError("sticky_domains must be a list of domain strings")
    out: List[str] = []
    for item in val:
        dom = item.strip().lower()
        if not _DOMAIN_RE.match(dom):
            raise ValueError(f"invalid domain: {item!r}")
        if dom not in out:
            out.append(dom)
    if out and not supported:
        raise ValueError(UNSUPPORTED_DOMAINS_MSG)
    return out


def _timeout(val) -> int:
    if isinstance(val, str) and val.strip().isdigit():
        val = int(val.strip())
    if isinstance(val, bool) or not isinstance(val, int) or val not in TIMEOUT_CHOICES:
        raise ValueError("sticky_timeout must be one of "
                         + ", ".join(str(t) for t in TIMEOUT_CHOICES))
    return val


def validate_update(data: dict, supported: bool) -> dict:
    """Normalised partial update from a POST body; ValueError on anything bad."""
    known = ("intranet_ranges", "sticky_ranges", "sticky_domains", "sticky_timeout")
    unknown = sorted(set(data) - set(known))
    if unknown:
        raise ValueError(f"unknown field(s): {', '.join(unknown)}")
    out: dict = {}
    for key in known:
        if key not in data:
            continue
        if key == "sticky_timeout":
            out[key] = _timeout(data[key])
        elif key == "sticky_domains":
            out[key] = _domain_list(data[key], supported)
        else:
            out[key] = _cidr_list(key, data[key])
    if not out:
        raise ValueError("nothing to update")
    return out
