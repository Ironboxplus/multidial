"""TDD: dnsmasq defaults for campus split-DNS plus public fallback."""

from multidial import dns
from multidial.config import Globals


def test_dnsmasq_uses_peer_dns_and_ordered_static_fallbacks():
    cmds = dns.reconcile_cmds(Globals(dns_servers=["192.0.2.53", "198.51.100.53"], dns_rebind_domains=["site.example.test"]))
    joined = "\n".join(cmds)

    assert "uci set dhcp.@dnsmasq[0].noresolv='0'" in cmds
    assert "uci set dhcp.@dnsmasq[0].resolvfile='/tmp/resolv.conf.d/resolv.conf.auto'" in cmds
    assert "uci set dhcp.@dnsmasq[0].strictorder='1'" in cmds
    assert "uci -q delete dhcp.@dnsmasq[0].server" in cmds
    assert "uci -q delete dhcp.@dnsmasq[0].rebind_domain" in cmds

    campus = "uci add_list dhcp.@dnsmasq[0].server='192.0.2.53'"
    public = "uci add_list dhcp.@dnsmasq[0].server='198.51.100.53'"
    rebind = "uci add_list dhcp.@dnsmasq[0].rebind_domain='/site.example.test/'"
    assert campus in cmds
    assert public in cmds
    assert rebind in cmds
    assert joined.index(campus) < joined.index(public)
    assert cmds[-2:] == ["uci commit dhcp", "/etc/init.d/dnsmasq restart"]


def test_no_static_resolver_or_domain_by_default():
    cmds = dns.reconcile_cmds(Globals())
    assert not any(cmd.startswith("uci add_list") for cmd in cmds)


def test_dnsmasq_can_disable_peer_dns_and_strict_order():
    glb = Globals(
        dns_servers=["10.0.0.53"],
        dns_rebind_domains=["example.edu.cn"],
        dns_use_peer=False,
        dns_strict_order=False,
    )
    cmds = dns.reconcile_cmds(glb)

    assert "uci set dhcp.@dnsmasq[0].noresolv='1'" in cmds
    assert "uci -q delete dhcp.@dnsmasq[0].strictorder" in cmds
    assert "uci add_list dhcp.@dnsmasq[0].server='10.0.0.53'" in cmds
    assert "uci add_list dhcp.@dnsmasq[0].rebind_domain='/example.edu.cn/'" in cmds
