"""TDD: UCI config model + dial planner for the multi-dial management system.

The daemon reads `/etc/config/multidial` (parsed from `uci show multidial`
output on the router) and turns enabled accounts into a deterministic set of
DialSpecs — one per virtual WAN it must stand up. Pure logic, no I/O, so it is
fully unit-testable off-router.
"""
import pytest

from multidial.config import (
    Account,
    Globals,
    DialSpec,
    MultidialConfig,
    ConfigError,
    rotated_macaddr,
)


# --- sample `uci show multidial` output ---------------------------------
UCI_SHOW = """\
multidial.globals=globals
multidial.globals.enabled='1'
multidial.globals.phy_ifname='eth1'
multidial.globals.gateway='192.0.2.1'
multidial.globals.interval='20'
multidial.acct_a=account
multidial.acct_a.userid='demo_user_a'
multidial.acct_a.password='s3cr3t'
multidial.acct_a.dials='2'
multidial.acct_a.enabled='1'
multidial.acct_b=account
multidial.acct_b.userid='demo_user_b'
multidial.acct_b.password='pw2'
multidial.acct_b.dials='1'
multidial.acct_b.enabled='1'
"""


class TestParseGlobals:
    def test_parses_globals_fields(self):
        cfg = MultidialConfig.from_uci_show(UCI_SHOW)
        assert cfg.globals.enabled is True
        assert cfg.globals.phy_ifname == "eth1"
        assert cfg.globals.gateway == "192.0.2.1"
        assert cfg.globals.interval == 20

    def test_sqm_defaults_on_and_parses_off(self):
        # SQM is provisioned for every dial by default (absent => True)...
        assert MultidialConfig.from_uci_show(UCI_SHOW).globals.sqm is True
        # ...but an explicit sqm='0' disables it
        off = UCI_SHOW + "multidial.globals.sqm='0'\n"
        assert MultidialConfig.from_uci_show(off).globals.sqm is False

    def test_track_ips_default_to_empty(self):
        assert not MultidialConfig.from_uci_show(UCI_SHOW).globals.track_ips

    def test_track_ips_parsed_from_config(self):
        cfg = MultidialConfig.from_uci_show(
            "multidial.globals=globals\n"
            "multidial.globals.track_ips='192.0.2.53, 198.51.100.53'\n"
        )
        assert cfg.globals.track_ips == ["192.0.2.53", "198.51.100.53"]

    def test_dns_defaults_use_peer_without_site_overrides(self):
        g = MultidialConfig.from_uci_show(UCI_SHOW).globals
        assert g.dns_use_peer and g.dns_strict_order
        assert g.dns_servers == [] and g.dns_rebind_domains == []

    def test_no_site_ranges_by_default(self):
        g = MultidialConfig.from_uci_show(UCI_SHOW).globals
        assert g.intranet_ranges == [] and g.sticky_ranges == []
        assert g.sticky_timeout == 600

    def test_intranet_sticky_parsed_from_config(self):
        cfg = MultidialConfig.from_uci_show(
            "multidial.globals=globals\n"
            "multidial.globals.intranet_sticky='0'\n"
            "multidial.globals.intranet_ranges='10.0.0.0/8 192.168.0.0/16'\n"
            "multidial.globals.sticky_timeout='300'\n"
        )
        assert cfg.globals.intranet_sticky is False
        assert cfg.globals.intranet_ranges == ["10.0.0.0/8", "192.168.0.0/16"]
        assert cfg.globals.sticky_timeout == 300

    def test_explicit_probe_and_gateway(self):
        g = MultidialConfig.from_uci_show(UCI_SHOW + "multidial.globals.probe_url='http://probe.example.test/check'\n").globals
        assert g.gateway == "192.0.2.1"
        assert g.probe_url == "http://probe.example.test/check"

    def test_service_sticky_ranges_parsed_and_blank_disables(self):
        cfg = MultidialConfig.from_uci_show(
            "multidial.globals=globals\n"
            "multidial.globals.sticky_ranges='1.2.3.0/24, 5.6.0.0/16'\n"
        )
        assert cfg.globals.sticky_ranges == ["1.2.3.0/24", "5.6.0.0/16"]
        off = MultidialConfig.from_uci_show(
            "multidial.globals=globals\nmultidial.globals.sticky_ranges=''\n"
        )
        assert off.globals.sticky_ranges == []

    def test_dns_servers_parsed_from_config(self):
        cfg = MultidialConfig.from_uci_show(
            "multidial.globals=globals\n"
            "multidial.globals.dns_servers='10.0.0.53, 192.0.2.53'\n"
            "multidial.globals.dns_rebind_domains='example.edu.cn, intranet.test'\n"
            "multidial.globals.dns_use_peer='0'\n"
            "multidial.globals.dns_strict_order='0'\n"
        )
        assert cfg.globals.dns_servers == ["10.0.0.53", "192.0.2.53"]
        assert cfg.globals.dns_rebind_domains == ["example.edu.cn", "intranet.test"]
        assert cfg.globals.dns_use_peer is False
        assert cfg.globals.dns_strict_order is False

    def test_hostname_template_default_and_override(self):
        cfg = MultidialConfig.from_uci_show(UCI_SHOW)
        assert cfg.globals.dhcp_hostname == "multidial-{suffix}"

        custom = MultidialConfig.from_uci_show(
            "multidial.globals=globals\n"
            "multidial.globals.dhcp_hostname='LAPTOP-{suffix}'\n"
        )
        assert custom.globals.dhcp_hostname == "LAPTOP-{suffix}"

    def test_globals_defaults_when_absent(self):
        cfg = MultidialConfig.from_uci_show(
            "multidial.acct_a=account\nmultidial.acct_a.userid='x'\n"
            "multidial.acct_a.password='y'\nmultidial.acct_a.dials='1'\n"
        )
        # sane built-in defaults so a minimal config still works
        assert cfg.globals.phy_ifname == "wan"
        assert cfg.globals.interval == 20
        assert cfg.globals.probe_url == ""
        assert cfg.globals.mac_prefix.count(":") == 4

    def test_globals_disabled(self):
        cfg = MultidialConfig.from_uci_show(
            "multidial.globals=globals\nmultidial.globals.enabled='0'\n"
        )
        assert cfg.globals.enabled is False


class TestParseAccounts:
    def test_parses_all_accounts(self):
        cfg = MultidialConfig.from_uci_show(UCI_SHOW)
        assert len(cfg.accounts) == 2
        a = cfg.accounts[0]
        assert a.userid == "demo_user_a"
        assert a.password == "s3cr3t"
        assert a.dials == 2
        assert a.enabled is True
        assert a.name == "acct_a"

    def test_account_enabled_defaults_true(self):
        cfg = MultidialConfig.from_uci_show(
            "multidial.acct_a=account\nmultidial.acct_a.userid='u'\n"
            "multidial.acct_a.password='p'\nmultidial.acct_a.dials='1'\n"
        )
        assert cfg.accounts[0].enabled is True

    def test_account_preserves_uci_order(self):
        cfg = MultidialConfig.from_uci_show(UCI_SHOW)
        assert [a.name for a in cfg.accounts] == ["acct_a", "acct_b"]

    def test_password_with_special_chars(self):
        cfg = MultidialConfig.from_uci_show(
            "multidial.acct_a=account\nmultidial.acct_a.userid='u'\n"
            "multidial.acct_a.password='p@ss:w0rd!'\nmultidial.acct_a.dials='1'\n"
        )
        assert cfg.accounts[0].password == "p@ss:w0rd!"


class TestValidation:
    def test_rejects_negative_dials(self):
        with pytest.raises(ConfigError):
            MultidialConfig.from_uci_show(
                "multidial.acct_a=account\nmultidial.acct_a.userid='u'\n"
                "multidial.acct_a.password='p'\nmultidial.acct_a.dials='-1'\n"
            )

    def test_rejects_account_missing_userid(self):
        with pytest.raises(ConfigError):
            MultidialConfig.from_uci_show(
                "multidial.acct_a=account\nmultidial.acct_a.password='p'\n"
                "multidial.acct_a.dials='1'\n"
            )

    def test_non_numeric_dials_raises(self):
        with pytest.raises(ConfigError):
            MultidialConfig.from_uci_show(
                "multidial.acct_a=account\nmultidial.acct_a.userid='u'\n"
                "multidial.acct_a.password='p'\nmultidial.acct_a.dials='two'\n"
            )


class TestPlanDials:
    def test_total_dial_count(self):
        cfg = MultidialConfig.from_uci_show(UCI_SHOW)
        specs = cfg.plan_dials()
        # 2 (acct_a) + 1 (acct_b) = 3
        assert len(specs) == 3

    def test_dial_specs_unique_resources(self):
        cfg = MultidialConfig.from_uci_show(UCI_SHOW)
        specs = cfg.plan_dials()
        assert len({s.device for s in specs}) == 3
        assert len({s.macaddr for s in specs}) == 3
        assert len({s.table for s in specs}) == 3
        assert len({s.pref for s in specs}) == 3

    def test_dial_carries_account_creds(self):
        cfg = MultidialConfig.from_uci_show(UCI_SHOW)
        specs = cfg.plan_dials()
        a_specs = [s for s in specs if s.account_userid == "demo_user_a"]
        assert len(a_specs) == 2
        assert all(s.password == "s3cr3t" for s in a_specs)

    def test_skips_disabled_account(self):
        text = UCI_SHOW + "multidial.acct_b.enabled='0'\n"
        cfg = MultidialConfig.from_uci_show(text)
        specs = cfg.plan_dials()
        assert len(specs) == 2
        assert all(s.account_userid == "demo_user_a" for s in specs)

    def test_zero_dials_account_produces_nothing(self):
        text = (
            "multidial.acct_a=account\nmultidial.acct_a.userid='u'\n"
            "multidial.acct_a.password='p'\nmultidial.acct_a.dials='0'\n"
        )
        cfg = MultidialConfig.from_uci_show(text)
        assert cfg.plan_dials() == []

    def test_slot_assignment_is_deterministic(self):
        cfg = MultidialConfig.from_uci_show(UCI_SHOW)
        s1 = cfg.plan_dials()
        s2 = cfg.plan_dials()
        assert [s.device for s in s1] == [s.device for s in s2]
        # global slot increments across accounts
        assert [s.slot for s in s1] == [0, 1, 2]

    def test_device_name_within_linux_ifname_limit(self):
        cfg = MultidialConfig.from_uci_show(UCI_SHOW)
        for s in cfg.plan_dials():
            assert len(s.device) <= 15

    def test_mac_derives_from_prefix_and_slot(self):
        cfg = MultidialConfig.from_uci_show(UCI_SHOW)
        specs = cfg.plan_dials()
        prefix = cfg.globals.mac_prefix
        for s in specs:
            assert s.macaddr.startswith(prefix)
            # last octet is a valid hex byte
            last = s.macaddr.split(":")[-1]
            assert 0 <= int(last, 16) <= 0xFF

    def test_table_and_pref_offset_by_slot(self):
        cfg = MultidialConfig.from_uci_show(UCI_SHOW)
        specs = cfg.plan_dials()
        for s in specs:
            assert s.table == cfg.globals.table_base + s.slot
            assert s.pref == cfg.globals.pref_base + s.slot

    def test_disabled_globals_still_plans_but_flagged(self):
        # plan_dials reflects desired dials; the daemon decides whether to act
        text = UCI_SHOW.replace(
            "multidial.globals.enabled='1'", "multidial.globals.enabled='0'"
        )
        cfg = MultidialConfig.from_uci_show(text)
        assert cfg.globals.enabled is False
        # planning is independent of the global kill-switch
        assert len(cfg.plan_dials()) == 3


class TestMacRotation:
    def test_rotation_zero_matches_plan_dials_mac(self):
        # rotation 0 must reproduce the original deterministic MAC (no drift for
        # existing dials)
        cfg = MultidialConfig.from_uci_show(UCI_SHOW)
        g = cfg.globals
        for s in cfg.plan_dials():
            assert rotated_macaddr(g, s.slot, 0) == s.macaddr

    def test_rotation_shifts_last_byte_by_stride(self):
        g = Globals()  # base 0x10, prefix 02:00:00:00:00
        assert rotated_macaddr(g, 1, 0) == "02:00:00:00:00:11"
        assert rotated_macaddr(g, 1, 1) == "02:00:00:00:00:21"
        assert rotated_macaddr(g, 1, 2) == "02:00:00:00:00:31"

    def test_rotation_collision_free_against_other_slots(self):
        g = Globals()
        seen = set()
        for slot in range(8):
            for rot in range(4):
                mac = rotated_macaddr(g, slot, rot)
                assert mac not in seen, f"collision at slot={slot} rot={rot}"
                seen.add(mac)

    def test_rotation_overflow_raises(self):
        g = Globals()
        with pytest.raises(ConfigError):
            rotated_macaddr(g, 0, 99)  # 0x10 + 99*0x10 well past 0xFE

    def test_make_spec_applies_rotation_without_changing_slot(self):
        cfg = MultidialConfig.from_uci_show(UCI_SHOW)
        acct = cfg.accounts[0]
        base = cfg.make_spec(acct, index=0, slot=1, rotation=0)
        rotated = cfg.make_spec(acct, index=0, slot=1, rotation=1)
        assert base.device == rotated.device == "dial1"
        assert base.table == rotated.table  # slot-derived resources unchanged
        assert base.macaddr != rotated.macaddr  # only the MAC moves


class TestRoundTripToUci:
    """The LuCI app edits UCI; the daemon must also be able to emit a fresh
    config (e.g. for defaults / first-run). Emit -> parse round-trips."""

    def test_emit_uci_round_trips(self):
        cfg = MultidialConfig(
            globals=Globals(interval=15),
            accounts=[Account(name="acct_a", userid="u1", password="p1", dials=2)],
        )
        text = cfg.to_uci_show()
        reparsed = MultidialConfig.from_uci_show(text)
        assert reparsed.globals.interval == 15
        assert reparsed.globals.dns_servers == cfg.globals.dns_servers
        assert reparsed.globals.dns_rebind_domains == cfg.globals.dns_rebind_domains
        assert reparsed.globals.intranet_sticky == cfg.globals.intranet_sticky
        assert reparsed.globals.intranet_ranges == cfg.globals.intranet_ranges
        assert reparsed.globals.sticky_ranges == cfg.globals.sticky_ranges
        assert reparsed.globals.sticky_timeout == cfg.globals.sticky_timeout
        assert reparsed.globals.dhcp_hostname == cfg.globals.dhcp_hostname
        assert reparsed.accounts[0].userid == "u1"
        assert reparsed.accounts[0].dials == 2
        assert len(reparsed.plan_dials()) == 2


class TestStickyDomains:
    def test_default_is_empty(self):
        assert Globals().sticky_domains == []
        assert MultidialConfig.from_uci_show(UCI_SHOW).globals.sticky_domains == []

    def test_parses_uci_list_rendering(self):
        # `uci show` renders a list as adjacent quoted values
        text = UCI_SHOW + "multidial.globals.sticky_domains='a.example.com' 'b.example.org'\n"
        g = MultidialConfig.from_uci_show(text).globals
        assert g.sticky_domains == ["a.example.com", "b.example.org"]

    def test_parses_single_and_blank(self):
        one = UCI_SHOW + "multidial.globals.sticky_domains='a.example.com'\n"
        assert MultidialConfig.from_uci_show(one).globals.sticky_domains == ["a.example.com"]
        blank = UCI_SHOW + "multidial.globals.sticky_domains=''\n"
        assert MultidialConfig.from_uci_show(blank).globals.sticky_domains == []

    def test_round_trips_through_to_uci_show(self):
        cfg = MultidialConfig.from_uci_show(UCI_SHOW)
        cfg.globals.sticky_domains = ["a.example.com", "b.example.org"]
        back = MultidialConfig.from_uci_show(cfg.to_uci_show())
        assert back.globals.sticky_domains == ["a.example.com", "b.example.org"]
