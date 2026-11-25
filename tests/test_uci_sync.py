"""TDD: turn DialSpecs into desired UCI network + mwan3 state.

netifd builds the macvlan WANs from `network`, mwan3 load-balances them — so
every dial is visible/editable in LuCI. Sections are deterministic and named
(md_*/dialN) so a shrunk config reconciles by deleting stale sections.
"""
import re

import pytest

from multidial.config import MultidialConfig, Globals
from multidial.uci_sync import (
    Section,
    desired_sections,
    dhcp_hostname_for_spec,
    render,
    reconcile,
    decommission_sections,
    parse_section_names,
)

UCI_SHOW = """\
multidial.globals=globals
multidial.globals.phy_ifname='eth1'
multidial.globals.track_ips='192.0.2.11 192.0.2.12 192.0.2.13'
multidial.globals.probe_url='http://probe.example.test/check'
multidial.globals.intranet_ranges='10.0.0.0/8 172.16.0.0/12'
multidial.globals.sticky_ranges='198.51.100.0/24 203.0.113.0/24'
multidial.globals.dns_servers='192.0.2.53 198.51.100.53'
multidial.globals.dns_rebind_domains='site.example.test example.test'
multidial.globals.gateway='192.0.2.1'
multidial.acct_a=account
multidial.acct_a.userid='demo_user_a'
multidial.acct_a.password='s3cr3t'
multidial.acct_a.dials='2'
multidial.acct_b=account
multidial.acct_b.userid='demo_user_b'
multidial.acct_b.password='pw2'
multidial.acct_b.dials='1'
"""


def _specs():
    cfg = MultidialConfig.from_uci_show(UCI_SHOW)
    return cfg, cfg.plan_dials()


def _by_config(sections, config):
    return [s for s in sections if s.config == config]


class TestNetworkSections:
    def test_one_macvlan_device_per_dial(self):
        cfg, specs = _specs()
        secs = desired_sections(specs, cfg.globals)
        devs = [s for s in _by_config(secs, "network") if s.stype == "device"]
        assert len(devs) == 3
        for d in devs:
            assert d.options["type"] == "macvlan"
            assert d.options["ifname"] == "eth1"
            assert d.options["name"].startswith("dial")

    def test_device_macs_match_specs_and_unique(self):
        cfg, specs = _specs()
        secs = desired_sections(specs, cfg.globals)
        devs = [s for s in _by_config(secs, "network") if s.stype == "device"]
        macs = {d.options["macaddr"] for d in devs}
        assert macs == {s.macaddr for s in specs}
        assert len(macs) == 3

    def test_interface_per_dial_is_dhcp_on_its_device(self):
        cfg, specs = _specs()
        secs = desired_sections(specs, cfg.globals)
        ifaces = [s for s in _by_config(secs, "network") if s.stype == "interface"]
        assert len(ifaces) == 3
        for spec in specs:
            match = [i for i in ifaces if i.name == spec.device]
            assert len(match) == 1
            assert match[0].options["proto"] == "dhcp"
            assert match[0].options["device"] == spec.device
            assert match[0].options["peerdns"] == "1"
            assert re.fullmatch(r"MULTIDIAL-[A-Z0-9]{7}", match[0].options["hostname"])

    def test_dhcp_hostname_suffix_is_per_dial_and_stable(self):
        cfg, specs = _specs()
        names1 = [dhcp_hostname_for_spec(s, cfg.globals) for s in specs]
        names2 = [dhcp_hostname_for_spec(s, cfg.globals) for s in specs]

        assert names1 == names2
        assert len(set(names1)) == len(specs)
        assert all(name.startswith("MULTIDIAL-") for name in names1)
        assert all(len(name) == len("MULTIDIAL-") + 7 for name in names1)

    def test_dhcp_hostname_template_can_change_prefix(self):
        cfg, specs = _specs()
        cfg.globals.dhcp_hostname = "LAPTOP-{suffix}"
        assert dhcp_hostname_for_spec(specs[0], cfg.globals).startswith("LAPTOP-")


class TestMwan3Sections:
    def test_interface_and_member_per_dial(self):
        cfg, specs = _specs()
        secs = desired_sections(specs, cfg.globals)
        mw_ifaces = [s for s in _by_config(secs, "mwan3") if s.stype == "interface"]
        members = [s for s in _by_config(secs, "mwan3") if s.stype == "member"]
        assert len(mw_ifaces) == 3
        assert len(members) == 3
        for spec in specs:
            assert any(m.options.get("interface") == spec.device for m in members)

    def test_track_uses_public_icmp_not_gateway(self):
        # Public-IP ping, NOT the campus gateway: a captive/blocked uplink still
        # answers gateway pings, so only a public-ICMP track sees the block.
        cfg, specs = _specs()
        secs = desired_sections(specs, cfg.globals)
        mw_ifaces = [s for s in _by_config(secs, "mwan3") if s.stype == "interface"]
        for mi in mw_ifaces:
            assert mi.options["track_method"] == "ping"
            assert mi.lists["track_ip"] == [
                "192.0.2.11",
                "192.0.2.12",
                "192.0.2.13",
            ]
            assert "192.0.2.1" not in mi.lists["track_ip"]  # never the gateway

    def test_dead_dial_detection_budget(self):
        # mwan3track drops a dial from the policy after `down` failed rounds; a
        # failed round pings every target, each waiting the full timeout. That
        # model matched the N60 (timeout=2: 22 s measured, ~21 s predicted), so
        # keep the worst case at ~12 s: more targets or a longer timeout must
        # be a deliberate trade, not a silent regression.
        cfg, specs = _specs()
        secs = desired_sections(specs, cfg.globals)
        for mi in [s for s in _by_config(secs, "mwan3") if s.stype == "interface"]:
            o = mi.options
            assert o["reliability"] == "1"   # any one target up => online
            budget = int(o["down"]) * (
                len(mi.lists["track_ip"]) * int(o["timeout"]) + int(o["interval"]))
            assert budget <= 12

    def test_balanced_policy_lists_all_members_equal_weight(self):
        cfg, specs = _specs()
        secs = desired_sections(specs, cfg.globals)
        policies = [s for s in _by_config(secs, "mwan3") if s.stype == "policy"]
        assert len(policies) == 1
        members = [s for s in _by_config(secs, "mwan3") if s.stype == "member"]
        assert len(policies[0].lists["use_member"]) == len(members)
        weights = {m.options["weight"] for m in members}
        assert weights == {"1"}

    def test_default_rule_points_at_policy(self):
        cfg, specs = _specs()
        secs = desired_sections(specs, cfg.globals)
        rules = [s for s in _by_config(secs, "mwan3") if s.stype == "rule"]
        policies = [s for s in _by_config(secs, "mwan3") if s.stype == "policy"]
        default = [r for r in rules if r.name == "md_default"]
        assert len(default) == 1
        assert default[0].options["use_policy"] == policies[0].name
        assert default[0].options["dest_ip"] == "0.0.0.0/0"
        # the catch-all must stay pure-balanced (link aggregation for the internet)
        assert "sticky" not in default[0].options

    def test_intranet_dest_is_sticky_and_precedes_default(self):
        # Campus auth systems resolve to intranet 10.x / 172.x (yjsgl.example.test ->
        # 192.0.2.75). mwan3 balances per-flow, so consecutive requests egress via
        # different dials => different source IP => "不同请求 IP 变 -> 认证失败". Pin
        # intranet dests sticky (per-source -> one dial => stable identity), while
        # everything else falls through to md_default and keeps aggregating.
        cfg, specs = _specs()
        secs = desired_sections(specs, cfg.globals)
        rules = [s for s in _by_config(secs, "mwan3") if s.stype == "rule"]
        names = [r.name for r in rules]
        # one sticky rule per configured intranet range, all BEFORE the catch-all
        intra = [r for r in rules if r.name.startswith("md_intra")]
        assert [r.options["dest_ip"] for r in intra] == ["10.0.0.0/8", "172.16.0.0/12"]
        for r in intra:
            assert r.options["sticky"] == "1"
            assert r.options["timeout"] == "600"          # mwan3 sticky-timeout key
            assert r.options["use_policy"] == "md_balanced"
            assert r.options["family"] == "ipv4"
        assert max(names.index(r.name) for r in intra) < names.index("md_default")

    def test_intranet_sticky_can_be_disabled(self):
        cfg, specs = _specs()
        cfg.globals.intranet_sticky = False
        secs = desired_sections(specs, cfg.globals)
        rules = [s for s in _by_config(secs, "mwan3") if s.stype == "rule"]
        assert [r.name for r in rules] == ["md_svc", "md_default"]

    def test_service_ranges_share_one_sticky_rule_between_intranet_and_default(self):
        # Aliyun login: passport (10.90.x) and account (10.94.247.x) must see
        # the SAME source IP, so every service range lives in ONE rule => one
        # mwan3 sticky set; per-range rules would pin each range independently.
        cfg, specs = _specs()
        secs = desired_sections(specs, cfg.globals)
        rules = [s for s in _by_config(secs, "mwan3") if s.stype == "rule"]
        names = [r.name for r in rules]
        assert names == ["md_intra0", "md_intra1", "md_svc", "md_default"]
        svc = rules[names.index("md_svc")]
        dests = svc.options["dest_ip"].split(",")
        assert "198.51.100.0/24" in dests and "203.0.113.0/24" in dests
        assert svc.options["sticky"] == "1"
        assert svc.options["timeout"] == "600"
        assert svc.options["use_policy"] == "md_balanced"
        assert svc.options["family"] == "ipv4"
        # mwan3 drops rules whose name exceeds 15 chars
        assert len(svc.name) <= 15

    def test_service_sticky_can_be_disabled(self):
        cfg, specs = _specs()
        cfg.globals.sticky_ranges = []
        secs = desired_sections(specs, cfg.globals)
        rules = [s for s in _by_config(secs, "mwan3") if s.stype == "rule"]
        assert "md_svc" not in [r.name for r in rules]


class TestRender:
    def test_render_emits_uci_set_commands(self):
        sec = Section(
            config="network", name="dial0", stype="interface",
            options={"proto": "dhcp", "device": "dial0"},
            lists={},
        )
        cmds = render(sec)
        assert "uci set network.dial0=interface" in cmds
        assert "uci set network.dial0.proto='dhcp'" in cmds
        assert "uci set network.dial0.device='dial0'" in cmds

    def test_render_list_uses_add_list(self):
        sec = Section(
            config="mwan3", name="md_balanced", stype="policy",
            options={}, lists={"use_member": ["m_dial0", "m_dial1"]},
        )
        cmds = render(sec)
        assert "uci -q delete mwan3.md_balanced.use_member" in cmds
        assert "uci add_list mwan3.md_balanced.use_member='m_dial0'" in cmds
        assert "uci add_list mwan3.md_balanced.use_member='m_dial1'" in cmds


class TestParseSectionNames:
    def test_extracts_only_section_declarations(self):
        text = (
            "network.dial0=interface\n"
            "network.dial0.proto='dhcp'\n"
            "network.md_dev0=device\n"
            "network.lan=interface\n"
            "network.lan.ipaddr='192.0.2.1'\n"
        )
        names = parse_section_names(text, "network")
        assert names == ["dial0", "md_dev0", "lan"]

    def test_ignores_other_configs_and_blank(self):
        assert parse_section_names("", "network") == []
        assert parse_section_names("mwan3.x=interface\n", "network") == []


class TestReconcile:
    def test_additive_default_never_deletes_stale_dials(self):
        # NEW MODEL: a lower count must NOT auto-delete a still-provisioned dial.
        # dial3 exists on the router but isn't in `specs`; additive reconcile
        # leaves it alone (removal is an explicit decommission, never here).
        cfg, specs = _specs()
        existing = {
            "network": ["dial0", "dial1", "dial2", "dial3", "md_dev0", "md_dev1",
                        "md_dev2", "md_dev3", "lan", "wan"],
            "mwan3": ["dial0", "dial1", "dial2", "dial3", "m_dial0", "m_dial1",
                      "m_dial2", "m_dial3"],
        }
        cmds = reconcile(specs, cfg.globals, existing)  # prune defaults False
        joined = "\n".join(cmds)
        # no SECTION-level delete of the stale dial (option-level list resets
        # like `delete ...track_ip` are fine and expected)
        assert "uci -q delete network.dial3" not in joined
        assert "uci -q delete network.md_dev3" not in joined
        assert "uci -q delete mwan3.dial3" not in joined
        assert "uci -q delete mwan3.m_dial3" not in joined
        # still (re)creates the desired dials
        assert "uci set network.dial0=interface" in joined

    def test_prune_true_deletes_stale_but_not_unrelated(self):
        # explicit teardown/migration path: prune=True removes managed sections
        # not in `specs`, but never lan/wan
        cfg, specs = _specs()
        existing = {
            "network": ["dial0", "dial1", "dial2", "dial3", "md_dev3", "lan", "wan"],
            "mwan3": ["dial3", "m_dial3"],
        }
        cmds = reconcile(specs, cfg.globals, existing, prune=True)
        joined = "\n".join(cmds)
        assert "uci -q delete network.dial3" in joined
        assert "uci -q delete network.md_dev3" in joined
        assert "uci -q delete mwan3.m_dial3" in joined
        assert "delete network.lan" not in joined
        assert "delete network.wan" not in joined

    def test_decommission_sections_removes_one_dial(self):
        cmds = decommission_sections(2)
        joined = "\n".join(cmds)
        # the member is pulled from the balancing policy first, then both sides
        assert "uci -q del_list mwan3.md_balanced.use_member='m_dial2'" in joined
        assert "uci -q delete mwan3.m_dial2" in joined
        assert "uci -q delete mwan3.dial2" in joined
        assert "uci -q delete network.dial2" in joined
        assert "uci -q delete network.md_dev2" in joined
        assert "uci commit network" in cmds
        assert "uci commit mwan3" in cmds
        # never touches a sibling
        assert "dial1" not in joined and "dial3" not in joined

    def test_keeps_desired_dials(self):
        cfg, specs = _specs()
        existing = {"network": ["dial0", "md_dev0"], "mwan3": ["dial0", "m_dial0"]}
        cmds = reconcile(specs, cfg.globals, existing)
        joined = "\n".join(cmds)
        assert "delete network.dial0\n" not in joined + "\n"
        assert "delete mwan3.m_dial0\n" not in joined + "\n"

    def test_reconcile_includes_commit(self):
        cfg, specs = _specs()
        cmds = reconcile(specs, cfg.globals, {"network": [], "mwan3": []})
        assert any(c == "uci commit network" for c in cmds)
        assert any(c == "uci commit mwan3" for c in cmds)

    def test_reconcile_recreates_rules_with_default_last(self):
        # mwan3 evaluates rules in file order (first match wins). A pre-existing
        # md_default would keep its earlier position and shadow the sticky rules,
        # so reconcile deletes the managed rule sections and re-appends them with
        # md_default LAST. Rules are not campus-AC-sensitive, so delete+recreate
        # is safe (unlike dial devices, which stay additive).
        cfg, specs = _specs()
        existing = {
            "network": ["dial0", "dial1", "dial2", "md_dev0", "md_dev1", "md_dev2"],
            "mwan3": ["dial0", "m_dial0", "md_balanced", "md_default", "md_intra0",
                      "md_svc"],
        }
        cmds = reconcile(specs, cfg.globals, existing)
        joined = "\n".join(cmds)
        assert "uci -q delete mwan3.md_default" in cmds
        assert "uci -q delete mwan3.md_intra0" in cmds
        assert "uci -q delete mwan3.md_svc" in cmds
        assert joined.index("uci set mwan3.md_svc=rule") < \
            joined.index("uci set mwan3.md_default=rule")
        # recreated with every intranet sticky rule ahead of the catch-all
        assert joined.index("uci set mwan3.md_intra0=rule") < \
            joined.index("uci set mwan3.md_default=rule")
        assert joined.index("uci set mwan3.md_intra1=rule") < \
            joined.index("uci set mwan3.md_default=rule")
        # dial sections are still additive — never section-deleted (exact command
        # membership: the track_ip list-reset "delete mwan3.dial0.track_ip" is fine)
        assert "uci -q delete mwan3.dial0" not in cmds
        assert "uci -q delete mwan3.m_dial0" not in cmds
