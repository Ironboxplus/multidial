"""UCI config model + dial planner for the multi-dial management system.

Reads `/etc/config/multidial` (as emitted by `uci show multidial`) and turns
enabled campus accounts into a deterministic list of `DialSpec`s — one per
virtual WAN the daemon must stand up.

Binding policy (hard requirement): a dial is identified by its **device name**
and **fixed MAC**, never by a source IP. DHCP-assigned IPs drift when Ruijie
auth expires; device/MAC do not. So `DialSpec` carries no IP — egress binding is
done downstream with SO_BINDTODEVICE + `ip rule oif <dev>`.
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import List, Optional


class ConfigError(ValueError):
    """Raised when the UCI config is malformed or semantically invalid."""


# --- defaults -----------------------------------------------------------
# Site-specific DNS, routing and probe settings come from UCI.
DEFAULT_PHY_IFNAME = "wan"
DEFAULT_GATEWAY = ""
DEFAULT_TRACK_IPS = ()
DEFAULT_DNS_SERVERS = ()
DEFAULT_DNS_REBIND_DOMAINS = ()
DEFAULT_INTRANET_RANGES = ()
DEFAULT_STICKY_RANGES = ()
DEFAULT_STICKY_TIMEOUT = 600
DEFAULT_DHCP_HOSTNAME = "multidial-{suffix}"
DEFAULT_PROBE_URL = ""
DEFAULT_INTERVAL = 20
DEFAULT_MAC_PREFIX = "02:00:00:00:00"
DEFAULT_TABLE_BASE = 100
DEFAULT_PREF_BASE = 1000
DEFAULT_MAC_SLOT_BASE = 16
MAX_MAC_SLOT = 0xFE
# A rotated MAC jumps the last byte by this stride per rotation, so a blocked
# dial recovers under a fresh identity the campus AC has never seen. Stride 0x10
# keeps slot s rotation r collision-free against any other (slot, rotation) for
# slots < 0x10 (we never run that many concurrent dials).
MAC_ROT_STRIDE = 0x10


def rotated_macaddr(glb: "Globals", slot: int, rotation: int = 0) -> str:
    """MAC for a global slot, optionally rotated to a fresh identity.

    rotation=0 reproduces the original deterministic MAC (prefix:base+slot), so
    existing dials are untouched; rotation>0 shifts the last byte by
    rotation*MAC_ROT_STRIDE to dodge a MAC the AC has blocked.
    """
    mac_byte = glb.mac_slot_base + slot + rotation * MAC_ROT_STRIDE
    if mac_byte > MAX_MAC_SLOT:
        raise ConfigError(
            f"MAC pool exhausted: slot {slot} rotation {rotation} exceeds "
            f"{MAX_MAC_SLOT:#x} (base {glb.mac_slot_base:#x})"
        )
    return f"{glb.mac_prefix}:{mac_byte:02x}"


@dataclass
class Globals:
    enabled: bool = True
    sqm: bool = True   # per-dial CAKE + cake-autorate, provisioned by default
    phy_ifname: str = DEFAULT_PHY_IFNAME
    gateway: str = DEFAULT_GATEWAY
    track_ips: List[str] = field(default_factory=lambda: list(DEFAULT_TRACK_IPS))
    dns_servers: List[str] = field(default_factory=lambda: list(DEFAULT_DNS_SERVERS))
    dns_rebind_domains: List[str] = field(
        default_factory=lambda: list(DEFAULT_DNS_REBIND_DOMAINS)
    )
    dns_use_peer: bool = True
    dns_strict_order: bool = True
    intranet_sticky: bool = True
    intranet_ranges: List[str] = field(
        default_factory=lambda: list(DEFAULT_INTRANET_RANGES)
    )
    sticky_ranges: List[str] = field(
        default_factory=lambda: list(DEFAULT_STICKY_RANGES)
    )
    sticky_timeout: int = DEFAULT_STICKY_TIMEOUT
    # Stored and reported, not enforced yet: matching domains needs a dnsmasq
    # built with ipset (see sticky.domains_supported).
    sticky_domains: List[str] = field(default_factory=list)
    dhcp_hostname: str = DEFAULT_DHCP_HOSTNAME
    probe_url: str = DEFAULT_PROBE_URL
    portal_base: str = ""
    interval: int = DEFAULT_INTERVAL
    mac_prefix: str = DEFAULT_MAC_PREFIX
    table_base: int = DEFAULT_TABLE_BASE
    pref_base: int = DEFAULT_PREF_BASE
    mac_slot_base: int = DEFAULT_MAC_SLOT_BASE


@dataclass
class Account:
    userid: str
    password: str
    dials: int
    enabled: bool = True
    name: str = ""  # UCI section name / label


@dataclass(frozen=True)
class DialSpec:
    """One virtual WAN to stand up. Identified by device + MAC, never by IP."""

    account_userid: str
    account_name: str
    password: str
    index: int   # per-account dial index (0..dials-1)
    slot: int    # global slot (0..N-1), drives all resource allocation
    device: str  # macvlan netdev name, e.g. "dial0" (<=15 chars)
    macaddr: str # fixed MAC derived from prefix + slot
    table: int   # policy-routing table id
    pref: int    # `ip rule` preference


# --- value coercion -----------------------------------------------------
def _to_bool(val: str, default: bool = True) -> bool:
    if val is None or val == "":
        return default
    return val.strip() not in ("0", "false", "no", "off")


def _to_int(val: str, field_name: str) -> int:
    try:
        return int(str(val).strip())
    except (TypeError, ValueError):
        raise ConfigError(f"{field_name!r} must be an integer, got {val!r}")


def _parse_track_ips(val, default) -> List[str]:
    """Split a space/comma-separated track-IP string; fall back to default."""
    if not val or not str(val).strip():
        return list(default)
    parts = [p.strip() for p in re.split(r"[,\s]+", str(val).strip()) if p.strip()]
    return parts or list(default)


def _parse_dns_servers(val) -> List[str]:
    """Split DNS server list; absent uses defaults, explicit blank disables."""
    if val is None:
        return list(DEFAULT_DNS_SERVERS)
    if not str(val).strip():
        return []
    return [p.strip() for p in re.split(r"[,\s]+", str(val).strip()) if p.strip()]


def _parse_dns_rebind_domains(val) -> List[str]:
    """Split DNS rebind whitelist; absent uses campus defaults."""
    if val is None:
        return list(DEFAULT_DNS_REBIND_DOMAINS)
    if not str(val).strip():
        return []
    return [p.strip().strip("/") for p in re.split(r"[,\s]+", str(val).strip()) if p.strip()]


def _parse_cidr_list(val, default) -> List[str]:
    """Split a CIDR list; absent uses `default`, explicit blank disables."""
    if val is None:
        return list(default)
    if not str(val).strip():
        return []
    return [p.strip() for p in re.split(r"[,\s]+", str(val).strip()) if p.strip()]


def normalize_cidr(val: str) -> str:
    """Strict IPv4 network ("10.0.0.0/8"; a bare address becomes /32).

    Unlike `_parse_cidr_list` (which trusts what is already in UCI) this is for
    user input: host bits set, bad octets, IPv6 and blanks raise ValueError."""
    net = ipaddress.ip_network(str(val).strip(), strict=True)
    if net.version != 4:
        raise ValueError(f"not an IPv4 network: {val!r}")
    return str(net)


def _parse_domain_list(val) -> List[str]:
    """Split a domain list. `uci show` renders a list as adjacent quoted values,
    which _unquote leaves as `a' 'b`, so quotes count as separators too."""
    if val is None:
        return []
    return [p for p in re.split(r"[,\s']+", str(val)) if p]


def _parse_intranet_ranges(val) -> List[str]:
    """Split intranet CIDR list; absent uses campus defaults, blank disables."""
    return _parse_cidr_list(val, DEFAULT_INTRANET_RANGES)


def _unquote(val: str) -> str:
    v = val.strip()
    if len(v) >= 2 and v[0] == "'" and v[-1] == "'":
        v = v[1:-1]
    # uci escapes embedded single quotes as '\''
    return v.replace("'\\''", "'")


class MultidialConfig:
    def __init__(self, globals: Globals, accounts: List[Account]):
        self.globals = globals
        self.accounts = accounts

    # --- parsing --------------------------------------------------------
    @classmethod
    def from_uci_show(cls, text: str) -> "MultidialConfig":
        """Parse `uci show multidial` output into a config model."""
        sections: dict = {}        # name -> {"type": str, "opts": {opt: val}}
        order: List[str] = []      # section names in first-seen order

        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            lhs, rhs = line.split("=", 1)
            parts = lhs.split(".")
            if len(parts) < 2 or parts[0] != "multidial":
                continue
            sec = parts[1]
            if sec not in sections:
                sections[sec] = {"type": "", "opts": {}}
                order.append(sec)
            if len(parts) == 2:
                # section declaration: multidial.<sec>=<type>
                sections[sec]["type"] = _unquote(rhs)
            elif len(parts) == 3:
                sections[sec]["opts"][parts[2]] = _unquote(rhs)

        # globals
        g = Globals()
        for sec in order:
            if sections[sec]["type"] == "globals" or sec == "globals":
                opts = sections[sec]["opts"]
                g = Globals(
                    enabled=_to_bool(opts.get("enabled"), True),
                    sqm=_to_bool(opts.get("sqm"), True),
                    phy_ifname=opts.get("phy_ifname", DEFAULT_PHY_IFNAME),
                    gateway=opts.get("gateway", DEFAULT_GATEWAY),
                    track_ips=_parse_track_ips(opts.get("track_ips"), DEFAULT_TRACK_IPS),
                    dns_servers=_parse_dns_servers(opts.get("dns_servers")),
                    dns_rebind_domains=_parse_dns_rebind_domains(
                        opts.get("dns_rebind_domains")
                    ),
                    dns_use_peer=_to_bool(opts.get("dns_use_peer"), True),
                    dns_strict_order=_to_bool(opts.get("dns_strict_order"), True),
                    intranet_sticky=_to_bool(opts.get("intranet_sticky"), True),
                    intranet_ranges=_parse_intranet_ranges(opts.get("intranet_ranges")),
                    sticky_ranges=_parse_cidr_list(
                        opts.get("sticky_ranges"), DEFAULT_STICKY_RANGES
                    ),
                    sticky_timeout=_to_int(
                        opts.get("sticky_timeout", DEFAULT_STICKY_TIMEOUT),
                        "sticky_timeout",
                    ),
                    sticky_domains=_parse_domain_list(opts.get("sticky_domains")),
                    dhcp_hostname=opts.get("dhcp_hostname", DEFAULT_DHCP_HOSTNAME),
                    probe_url=opts.get("probe_url", DEFAULT_PROBE_URL),
                    portal_base=opts.get("portal_base", ""),
                    interval=_to_int(opts.get("interval", DEFAULT_INTERVAL), "interval"),
                    mac_prefix=opts.get("mac_prefix", DEFAULT_MAC_PREFIX),
                    table_base=_to_int(opts.get("table_base", DEFAULT_TABLE_BASE), "table_base"),
                    pref_base=_to_int(opts.get("pref_base", DEFAULT_PREF_BASE), "pref_base"),
                    mac_slot_base=_to_int(
                        opts.get("mac_slot_base", DEFAULT_MAC_SLOT_BASE), "mac_slot_base"
                    ),
                )
                break

        # accounts
        accounts: List[Account] = []
        for sec in order:
            if sections[sec]["type"] != "account":
                continue
            opts = sections[sec]["opts"]
            userid = opts.get("userid", "").strip()
            if not userid:
                raise ConfigError(f"account {sec!r} is missing 'userid'")
            dials = _to_int(opts.get("dials", "0"), f"{sec}.dials")
            if dials < 0:
                raise ConfigError(f"account {sec!r} has negative dials: {dials}")
            accounts.append(
                Account(
                    userid=userid,
                    password=opts.get("password", ""),
                    dials=dials,
                    enabled=_to_bool(opts.get("enabled"), True),
                    name=sec,
                )
            )

        return cls(globals=g, accounts=accounts)

    # --- planning -------------------------------------------------------
    def plan_dials(self) -> List[DialSpec]:
        """Expand enabled accounts into a deterministic list of DialSpecs.

        Slot assignment is global and sequential, so device names, MACs,
        routing tables and rule prefs never collide across accounts. Planning
        is independent of the global enabled kill-switch (the daemon decides
        whether to actually act on the plan).
        """
        specs: List[DialSpec] = []
        slot = 0
        for acct in self.accounts:
            if not acct.enabled or acct.dials <= 0:
                continue
            for i in range(acct.dials):
                specs.append(self.make_spec(acct, index=i, slot=slot))
                slot += 1
        return specs

    def make_spec(self, acct: Account, index: int, slot: int,
                  rotation: int = 0) -> DialSpec:
        """Build one DialSpec for an account at a fixed global slot.

        `slot` is the stable per-dial identity (drives device/MAC/table/pref) —
        the registry assigns it once and never renumbers, so a dial keeps its
        identity even when the count shrinks or a sibling is decommissioned.
        `rotation` re-MACs a blocked dial without changing its slot.
        """
        return DialSpec(
            account_userid=acct.userid,
            account_name=acct.name,
            password=acct.password,
            index=index,
            slot=slot,
            device=f"dial{slot}",
            macaddr=rotated_macaddr(self.globals, slot, rotation),
            table=self.globals.table_base + slot,
            pref=self.globals.pref_base + slot,
        )

    # --- emission -------------------------------------------------------
    def to_uci_show(self) -> str:
        """Emit a `uci show multidial`-style representation (round-trips)."""
        lines: List[str] = []
        g = self.globals
        lines.append("multidial.globals=globals")
        lines.append(f"multidial.globals.enabled='{1 if g.enabled else 0}'")
        lines.append(f"multidial.globals.sqm='{1 if g.sqm else 0}'")
        lines.append(f"multidial.globals.phy_ifname='{g.phy_ifname}'")
        lines.append(f"multidial.globals.gateway='{g.gateway}'")
        lines.append(f"multidial.globals.track_ips='{' '.join(g.track_ips)}'")
        lines.append(f"multidial.globals.dns_servers='{' '.join(g.dns_servers)}'")
        lines.append(
            f"multidial.globals.dns_rebind_domains='{' '.join(g.dns_rebind_domains)}'"
        )
        lines.append(f"multidial.globals.dns_use_peer='{1 if g.dns_use_peer else 0}'")
        lines.append(f"multidial.globals.dns_strict_order='{1 if g.dns_strict_order else 0}'")
        lines.append(f"multidial.globals.intranet_sticky='{1 if g.intranet_sticky else 0}'")
        lines.append(
            f"multidial.globals.intranet_ranges='{' '.join(g.intranet_ranges)}'"
        )
        lines.append(f"multidial.globals.sticky_ranges='{' '.join(g.sticky_ranges)}'")
        lines.append(f"multidial.globals.sticky_timeout='{g.sticky_timeout}'")
        lines.append(f"multidial.globals.sticky_domains='{' '.join(g.sticky_domains)}'")
        lines.append(f"multidial.globals.dhcp_hostname='{g.dhcp_hostname}'")
        lines.append(f"multidial.globals.probe_url='{g.probe_url}'")
        if g.portal_base:
            lines.append(f"multidial.globals.portal_base='{g.portal_base}'")
        lines.append(f"multidial.globals.interval='{g.interval}'")
        lines.append(f"multidial.globals.mac_prefix='{g.mac_prefix}'")
        for idx, acct in enumerate(self.accounts):
            sec = acct.name or f"acct{idx}"
            lines.append(f"multidial.{sec}=account")
            lines.append(f"multidial.{sec}.userid='{acct.userid}'")
            lines.append(f"multidial.{sec}.password='{acct.password}'")
            lines.append(f"multidial.{sec}.dials='{acct.dials}'")
            lines.append(f"multidial.{sec}.enabled='{1 if acct.enabled else 0}'")
        return "\n".join(lines) + "\n"
