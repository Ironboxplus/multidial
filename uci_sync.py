"""Turn DialSpecs into desired UCI `network` + `mwan3` state.

Each dial becomes a macvlan device + DHCP interface (netifd stands it up) and an
mwan3 interface + member (mwan3 load-balances). Everything is named
deterministically (md_dev{slot} / dial{slot} / m_dial{slot}) so a shrunk config
reconciles by deleting the stale sections. All dials sit on one physical link,
each with a fixed MAC — binding stays at the device/MAC layer, never an IP.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Dict, List

from .config import DialSpec, Globals


@dataclass
class Section:
    config: str               # "network" | "mwan3"
    name: str                 # UCI section name
    stype: str                # section type, e.g. "interface", "device", "member"
    options: Dict[str, str] = field(default_factory=dict)
    lists: Dict[str, List[str]] = field(default_factory=dict)


def dev_section_name(slot: int) -> str:
    return f"md_dev{slot}"


def member_name(spec: DialSpec) -> str:
    return f"m_{spec.device}"


POLICY_NAME = "md_balanced"
RULE_NAME = "md_default"
INTRA_RULE_PREFIX = "md_intra"
SVC_RULE_NAME = "md_svc"
METRIC_BASE = 20


def intranet_cidrs(glb: Globals) -> List[str]:
    """Cleaned intranet CIDRs to pin sticky (empty when the feature is off)."""
    if not glb.intranet_sticky:
        return []
    return [str(c).strip() for c in glb.intranet_ranges if str(c).strip()]


def is_managed_rule(name: str) -> bool:
    """mwan3 rule sections this system owns (order-sensitive, recreated)."""
    return name in (RULE_NAME, SVC_RULE_NAME) or name.startswith(INTRA_RULE_PREFIX)


_HOSTNAME_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _base36_suffix(seed: str, length: int = 7) -> str:
    digest = hashlib.sha1(seed.encode("ascii", "ignore")).digest()
    n = int.from_bytes(digest, "big")
    chars = []
    for _ in range(length):
        n, rem = divmod(n, len(_HOSTNAME_ALPHABET))
        chars.append(_HOSTNAME_ALPHABET[rem])
    return "".join(chars)


def dhcp_hostname_for_spec(spec: DialSpec, glb: Globals) -> str:
    """Per-dial DHCP hostname derived from stable MAC identity.

    `DESKTOP-{suffix}` yields Windows-looking, 15-char names while avoiding the
    OpenWrt default hostname being repeated across every macvlan DHCP request.
    """
    suffix = _base36_suffix(spec.macaddr.upper())
    template = glb.dhcp_hostname or "*"
    if template == "*":
        return "*"
    try:
        hostname = template.format(suffix=suffix, slot=spec.slot, device=spec.device)
    except (IndexError, KeyError, ValueError):
        hostname = template
    hostname = re.sub(r"[^A-Za-z0-9-]+", "-", hostname).strip("-").upper()
    return (hostname or "*")[:63]


def desired_sections(specs: List[DialSpec], glb: Globals) -> List[Section]:
    """Compute the full desired UCI state for the given dials."""
    sections: List[Section] = []
    members: List[str] = []

    for spec in specs:
        # --- network: macvlan device + dhcp interface ---
        sections.append(Section(
            config="network", name=dev_section_name(spec.slot), stype="device",
            options={
                "name": spec.device,
                "type": "macvlan",
                "ifname": glb.phy_ifname,
                "macaddr": spec.macaddr,
            },
        ))
        sections.append(Section(
            config="network", name=spec.device, stype="interface",
            options={
                "proto": "dhcp",
                "device": spec.device,
                "defaultroute": "1",
                "peerdns": "1",
                "hostname": dhcp_hostname_for_spec(spec, glb),
                "metric": str(METRIC_BASE + spec.slot),
            },
        ))

        # --- mwan3: tracked interface + balancing member ---
        # Track PUBLIC IPs, not the gateway: the campus AC keeps answering
        # gateway pings for a captive/blocked client, so a gateway-track reports
        # a dead uplink as "online". Pinging the public internet (reliability=1 =>
        # any one IP up) correctly marks a blocked/captive dial offline.
        #
        # Detection latency bounds the window where mwan3 keeps balancing NEW
        # connections onto a dial the campus AC already kicked ("分流到掉线的号 ->
        # 需要登录"). mwan3track removes a dial from the policy only after `down`
        # failed rounds, and a failed round pings EVERY target, each waiting the
        # full `timeout`: latency ~= down x (len(track_ips) x timeout + interval).
        # With 3 targets, timeout=2 measured 22 s on the N60 (2026-10-15), not the
        # ~3 s once assumed; timeout=1 brings it to ~12 s. Campus RTT to the
        # targets is 5-30 ms, so 1 s still leaves a >30x margin.
        sections.append(Section(
            config="mwan3", name=spec.device, stype="interface",
            options={
                "enabled": "1",
                "family": "ipv4",
                "track_method": "ping",
                "reliability": "1",
                "count": "1",
                "timeout": "1",
                "interval": "1",
                "down": "3",
                "up": "3",
            },
            lists={"track_ip": list(glb.track_ips)},
        ))
        m = member_name(spec)
        members.append(m)
        sections.append(Section(
            config="mwan3", name=m, stype="member",
            options={"interface": spec.device, "metric": "1", "weight": "1"},
        ))

    # --- mwan3: one balanced policy across all members + default rule ---
    if members:
        sections.append(Section(
            config="mwan3", name=POLICY_NAME, stype="policy",
            # last_resort=default: if every managed dial is down, fall back to the
            # main routing table (the native wan default route) instead of
            # blackholing LAN traffic. The native wan keeps the overlay alive.
            options={"last_resort": "default"},
            lists={"use_member": members},
        ))

        # Intranet sticky rules — emitted BEFORE the catch-all so mwan3 (first
        # match wins; later rules only touch still-unmarked packets) pins campus
        # dests to one dial => stable source IP => auth survives, while the public
        # internet falls through to md_default and keeps per-flow aggregation.
        # These match only routed destinations. Directly connected networks
        # such as the LAN and the uplink subnets are excluded by mwan3's
        # own connected-network short-circuit, so device-to-device LAN traffic is
        # untouched.
        for i, cidr in enumerate(intranet_cidrs(glb)):
            sections.append(Section(
                config="mwan3", name=f"{INTRA_RULE_PREFIX}{i}", stype="rule",
                options={
                    "dest_ip": cidr,
                    "family": "ipv4",
                    "sticky": "1",
                    "timeout": str(glb.sticky_timeout),
                    "use_policy": POLICY_NAME,
                },
            ))

        # Public services that bind login/risk-control state to the client IP
        # (Aliyun passport/account/console). ONE rule for every range, so they
        # share one sticky set: a client's passport and account requests leave
        # through the same dial. mwan3 passes dest_ip unquoted to
        # iptables-restore, which expands the comma list into one rule per
        # range, all jumping to mwan3_rule_md_svc.
        svc = [str(c).strip() for c in glb.sticky_ranges if str(c).strip()]
        if svc:
            sections.append(Section(
                config="mwan3", name=SVC_RULE_NAME, stype="rule",
                options={
                    "dest_ip": ",".join(svc),
                    "family": "ipv4",
                    "sticky": "1",
                    "timeout": str(glb.sticky_timeout),
                    "use_policy": POLICY_NAME,
                },
            ))

        sections.append(Section(
            config="mwan3", name=RULE_NAME, stype="rule",
            options={
                "dest_ip": "0.0.0.0/0",
                "family": "ipv4",
                "use_policy": POLICY_NAME,
            },
        ))

    return sections


def parse_section_names(text: str, config: str) -> List[str]:
    """Extract section names from `uci show <config>` output.

    Only section-declaration lines (`<config>.<name>=<type>`) count, so option
    lines are ignored. Used to discover what's currently on the router.
    """
    names: List[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if "=" not in line:
            continue
        lhs = line.split("=", 1)[0]
        parts = lhs.split(".")
        if len(parts) == 2 and parts[0] == config:
            names.append(parts[1])
    return names


def render(sec: Section) -> List[str]:
    """Render one Section into idempotent `uci` shell commands."""
    cmds = [f"uci set {sec.config}.{sec.name}={sec.stype}"]
    for opt, val in sec.options.items():
        cmds.append(f"uci set {sec.config}.{sec.name}.{opt}='{val}'")
    for opt, vals in sec.lists.items():
        cmds.append(f"uci -q delete {sec.config}.{sec.name}.{opt}")
        for v in vals:
            cmds.append(f"uci add_list {sec.config}.{sec.name}.{opt}='{v}'")
    return cmds


def _managed_names(specs: List[DialSpec]) -> Dict[str, set]:
    """Names this system owns, by config — used to spot stale sections."""
    net = set()
    mw = set()
    for spec in specs:
        net.add(spec.device)
        net.add(dev_section_name(spec.slot))
        mw.add(spec.device)
        mw.add(member_name(spec))
    return {"network": net, "mwan3": mw}


def _is_managed_name(name: str) -> bool:
    return name.startswith(("dial", "md_dev", "m_dial", "md_"))


def reconcile(
    specs: List[DialSpec], glb: Globals, existing: Dict[str, List[str]],
    prune: bool = False,
) -> List[str]:
    """Command list to make the router match `specs` — additive by default.

    `existing` maps config name -> list of current section names on the router.

    Additive (prune=False, the default): only (re)create the desired sections;
    NEVER delete. This is the steady-state path — lowering the dial count must
    not tear down a running dial (the campus AC blocks the churned MAC). Removal
    is always a deliberate, single-dial `decommission_sections()` call instead.

    Pruning (prune=True): also delete managed sections (dial*/md_*/m_dial*) not
    in `specs`. Reserved for an explicit full teardown/migration, never the
    count-driven reconcile.
    """
    desired = desired_sections(specs, glb)
    desired_by_config: Dict[str, set] = {"network": set(), "mwan3": set()}
    for s in desired:
        desired_by_config.setdefault(s.config, set()).add(s.name)

    cmds: List[str] = []

    # 1. (optional) delete stale managed sections — only on explicit prune
    if prune:
        for config in ("network", "mwan3"):
            for name in existing.get(config, []):
                if not _is_managed_name(name):
                    continue
                if name not in desired_by_config.get(config, set()):
                    cmds.append(f"uci -q delete {config}.{name}")

    # 1b. mwan3 rules are order-sensitive (first match wins) and — unlike dial
    # devices — NOT campus-AC-sensitive, so always delete the managed rule
    # sections and let the render below re-append them in desired order. This
    # guarantees md_default lands LAST (after the md_intra*/md_svc sticky rules);
    # a pre-existing md_default would otherwise keep its earlier file position
    # and shadow the sticky rules. Dials/members/policy stay additive.
    if specs:
        for name in existing.get("mwan3", []):
            if is_managed_rule(name):
                cmds.append(f"uci -q delete mwan3.{name}")

    # 2. (re)create desired sections (idempotent `uci set`)
    for s in desired:
        cmds.extend(render(s))

    # 3. commit touched configs
    for config in ("network", "mwan3"):
        cmds.append(f"uci commit {config}")

    return cmds


def decommission_sections(slot: int) -> List[str]:
    """UCI commands to remove exactly ONE dial's sections (explicit delete).

    The sole deletion path under the additive model: lowering the count never
    deletes, so removing a dial is always a deliberate single-dial action. Drops
    the member from the balancing policy first, then the mwan3 + network
    sections, then commits both configs.
    """
    dev = f"dial{slot}"
    member = f"m_{dev}"
    return [
        f"uci -q del_list mwan3.{POLICY_NAME}.use_member='{member}'",
        f"uci -q delete mwan3.{member}",
        f"uci -q delete mwan3.{dev}",
        f"uci -q delete network.{dev}",
        f"uci -q delete network.{dev_section_name(slot)}",
        "uci commit network",
        "uci commit mwan3",
    ]
