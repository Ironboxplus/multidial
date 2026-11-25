"""dnsmasq provisioning for campus DNS plus public fallback.

Campus/internal HTTPS names may only resolve through the DNS servers handed out
by the Ethernet DHCP lease. The multi-dial setup keeps static public resolvers
as fallback, but must prefer campus DNS so split-horizon names do not fail with
public NXDOMAIN answers.
"""
from __future__ import annotations

from typing import List

from .config import Globals


DNSMASQ = "dhcp.@dnsmasq[0]"
RESOLV_AUTO = "/tmp/resolv.conf.d/resolv.conf.auto"


def _sq(value) -> str:
    return "'" + str(value).replace("'", "'\\''") + "'"


def reconcile_cmds(glb: Globals) -> List[str]:
    """Commands that make dnsmasq use peer DNS plus ordered static fallback."""
    cmds: List[str] = []

    cmds.append(f"uci set {DNSMASQ}.noresolv={_sq('0' if glb.dns_use_peer else '1')}")
    if glb.dns_use_peer:
        cmds.append(f"uci set {DNSMASQ}.resolvfile={_sq(RESOLV_AUTO)}")

    if glb.dns_strict_order:
        cmds.append(f"uci set {DNSMASQ}.strictorder='1'")
    else:
        cmds.append(f"uci -q delete {DNSMASQ}.strictorder")

    cmds.append(f"uci -q delete {DNSMASQ}.server")
    for server in glb.dns_servers:
        cmds.append(f"uci add_list {DNSMASQ}.server={_sq(server)}")

    cmds.append(f"uci -q delete {DNSMASQ}.rebind_domain")
    for domain in glb.dns_rebind_domains:
        domain = str(domain).strip().strip("/")
        if domain:
            cmds.append(f"uci add_list {DNSMASQ}.rebind_domain={_sq('/' + domain + '/')}")

    cmds.append("uci commit dhcp")
    cmds.append("/etc/init.d/dnsmasq restart")
    return cmds
