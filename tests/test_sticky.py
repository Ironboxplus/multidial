"""TDD: pure parsing/validation behind /api/sticky and /api/traffic.

The fixtures under tests/fixtures/sticky are anonymized router-shaped examples (see its README):
the broken state mwan3 leaves behind (sticky chains without restore rules) and
the same chain right after `mwan3_set_user_rules` rebuilt it.
"""
import pathlib

import pytest

from multidial import sticky
from multidial.config import Globals

FIX = pathlib.Path(__file__).parent / "fixtures" / "sticky"


def _fx(name):
    return (FIX / name).read_text()


def _fixed_mangle():
    """The broken dump with md_svc's chain replaced by the rebuilt one."""
    kept = [ln for ln in _fx("mangle_broken.txt").splitlines()
            if not ln.startswith("-A mwan3_rule_md_svc ")]
    return "\n".join(kept) + "\n" + _fx("chain_md_svc_rebuilt.txt")


ORDER = ["dial0", "dial1", "dial2", "dial3", "dial6"]


class TestMarkMapping:
    def test_interface_order_follows_uci_declaration_order(self):
        assert sticky.parse_iface_order(_fx("uci_mwan3.txt")) == ORDER

    @pytest.mark.parametrize("mark,dev", [
        ("0x00000100", "dial0"), ("0x200", "dial1"), ("0x00000400", "dial3"),
        ("0x500", "dial6"),
    ])
    def test_mark_to_device(self, mark, dev):
        assert sticky.mark_to_device(int(mark, 16), ORDER) == dev

    @pytest.mark.parametrize("mark", [0x0, 0x600, 0x10000])
    def test_unmapped_mark_is_none(self, mark):
        assert sticky.mark_to_device(mark, ORDER) is None

    def test_sticky_rules_with_their_policy(self):
        assert sticky.parse_sticky_rules(_fx("uci_mwan3.txt")) == [
            ("md_intra0", "md_balanced"),
            ("md_intra1", "md_balanced"),
            ("md_svc", "md_balanced"),
        ]


class TestPins:
    def test_pins_from_fixture_ipset(self):
        pins = sticky.parse_pins("md_svc", _fx("ipset_md_svc.txt"), ORDER)
        assert pins == [
            {"rule": "md_svc", "client": "192.0.2.139", "device": "dial3", "ttl": 257},
            {"rule": "md_svc", "client": "192.0.2.178", "device": "dial0", "ttl": 251},
        ]

    def test_large_ipset_parses_every_member(self):
        pins = sticky.parse_pins("md_intra0", _fx("ipset_md_intra0.txt"), ORDER)
        assert len(pins) == 75
        assert all(p["device"] in ORDER for p in pins)

    def test_empty_or_failed_ipset_has_no_pins(self):
        assert sticky.parse_pins("md_svc", "", ORDER) == []
        assert sticky.parse_pins("md_svc", "ipset v7: The set with the given name does not exist",
                                 ORDER) == []


class TestHealth:
    def test_policy_members_are_the_commented_marks(self):
        members = sticky.policy_members(_fx("mangle_broken.txt"), "md_balanced")
        assert members == ORDER

    def test_broken_chain_has_no_restore_rules(self):
        assert sticky.restore_members(_fx("mangle_broken.txt"), "md_svc", ORDER) == []

    def test_rebuilt_chain_restores_every_online_member(self):
        assert sticky.restore_members(_fixed_mangle(), "md_svc", ORDER) == ORDER

    def test_health_false_when_restore_rules_missing(self):
        h = sticky.rule_health(_fx("mangle_broken.txt"), "md_svc", "md_balanced", ORDER)
        assert h == {"online": ORDER, "restore": [], "ok": False}

    def test_health_true_after_rebuild(self):
        h = sticky.rule_health(_fixed_mangle(), "md_svc", "md_balanced", ORDER)
        assert h == {"online": ORDER, "restore": ORDER, "ok": True}

    def test_health_false_when_nothing_online(self):
        assert sticky.rule_health("", "md_svc", "md_balanced", ORDER)["ok"] is False

    def test_health_false_when_restore_set_differs(self):
        # drop the dial6 restore pair entirely
        lines = [ln for ln in _fixed_mangle().splitlines()
                 if not (ln.startswith("-A mwan3_rule_md_svc") and "0x500/0x3f00" in ln)]
        h = sticky.rule_health("\n".join(lines) + "\n", "md_svc", "md_balanced", ORDER)
        assert h["restore"] == ["dial0", "dial1", "dial2", "dial3"]
        assert h["ok"] is False


class TestDomainsSupported:
    def test_no_ipset_means_unsupported(self):
        assert sticky.domains_supported(_fx("dnsmasq_version.txt")) is False

    def test_ipset_means_supported(self):
        text = "Compile time options: IPv6 GNU-getopt DHCP ipset nftset auth\n"
        assert sticky.domains_supported(text) is True

    def test_garbage_means_unsupported(self):
        assert sticky.domains_supported("") is False


class TestTraffic:
    def test_parse_sysfs_bytes(self):
        assert sticky.parse_traffic(_fx("sysfs_bytes.txt")) == {
            "dial0": {"down_bytes": 465477183912, "up_bytes": 673882185173},
            "dial3": {"down_bytes": 260389697928, "up_bytes": 367295686310},
            "dial1": {"down_bytes": 280620995461, "up_bytes": 367420472929},
            "dial2": {"down_bytes": 389433709256, "up_bytes": 384833628628},
            "dial6": {"down_bytes": 308833357309, "up_bytes": 397873519450},
        }

    def test_missing_counters_are_omitted(self):
        got = sticky.parse_traffic("dial0 - 5\ndial1 - -\ndial2 7 8\njunk\n")
        assert got == {"dial0": {"up_bytes": 5},
                       "dial2": {"down_bytes": 7, "up_bytes": 8}}

    def test_command_reads_both_sysfs_counters_per_dial(self):
        cmd = sticky.traffic_cmd(["dial0", "dial3"])
        assert "/sys/class/net/ifb4dial3/statistics/tx_bytes" in cmd
        assert "/sys/class/net/dial3/statistics/tx_bytes" in cmd


class TestBuildStatus:
    def _status(self, mangle):
        return sticky.build_status(
            Globals(intranet_ranges=["10.0.0.0/8", "172.16.0.0/12"], sticky_ranges=["10.90.0.0/16"]), _fx("uci_mwan3.txt"), mangle,
            {"md_intra0": _fx("ipset_md_intra0.txt"),
             "md_intra1": _fx("ipset_md_intra1.txt"),
             "md_svc": _fx("ipset_md_svc.txt")},
            _fx("dnsmasq_version.txt"))

    def test_status_shape_on_broken_router(self):
        st = self._status(_fx("mangle_broken.txt"))
        assert st["timeout"] == 600
        assert st["intranet"] == {"enabled": True,
                                  "ranges": ["10.0.0.0/8", "172.16.0.0/12"]}
        assert st["service"]["domains"] == []
        assert "10.90.0.0/16" in st["service"]["ranges"]
        assert st["domains_supported"] is False
        assert {"rule": "md_svc", "client": "192.0.2.139", "device": "dial3",
                "ttl": 257} in st["pins"]
        assert set(st["health"]) == {"md_intra0", "md_intra1", "md_svc"}
        assert all(h["ok"] is False for h in st["health"].values())

    def test_status_ok_for_the_rebuilt_rule(self):
        st = self._status(_fixed_mangle())
        assert st["health"]["md_svc"] == {"online": ORDER, "restore": ORDER, "ok": True}
        assert st["health"]["md_intra0"]["ok"] is False


class TestValidateUpdate:
    def test_normalises_ranges_and_dedupes(self):
        got = sticky.validate_update(
            {"sticky_ranges": ["10.90.0.0/16", "1.2.3.4", "10.90.0.0/16"]}, False)
        assert got == {"sticky_ranges": ["10.90.0.0/16", "1.2.3.4/32"]}

    def test_empty_list_is_allowed(self):
        assert sticky.validate_update({"intranet_ranges": []}, False) == {"intranet_ranges": []}

    @pytest.mark.parametrize("bad", [
        "10.0.0.0/33", "10.0.0.1/8", "300.1.1.1/8", "abc", "2001:db8::/32", "",
    ])
    def test_rejects_bad_cidr(self, bad):
        with pytest.raises(ValueError):
            sticky.validate_update({"intranet_ranges": [bad]}, False)

    def test_ranges_must_be_a_list_of_strings(self):
        with pytest.raises(ValueError):
            sticky.validate_update({"sticky_ranges": "10.0.0.0/8"}, False)
        with pytest.raises(ValueError):
            sticky.validate_update({"sticky_ranges": [8]}, False)

    @pytest.mark.parametrize("t", [300, 600, 1800, 3600, "1800"])
    def test_timeout_choices(self, t):
        assert sticky.validate_update({"sticky_timeout": t}, False) == {
            "sticky_timeout": int(t)}

    @pytest.mark.parametrize("t", [0, 60, 601, -1, "abc", True, None, 600.5])
    def test_rejects_other_timeouts(self, t):
        with pytest.raises(ValueError):
            sticky.validate_update({"sticky_timeout": t}, False)

    def test_domains_rejected_when_unsupported(self):
        with pytest.raises(ValueError, match="暂不支持域名"):
            sticky.validate_update({"sticky_domains": ["login.example.com"]}, False)

    def test_domains_accepted_and_lowercased_when_supported(self):
        assert sticky.validate_update(
            {"sticky_domains": ["Login.Example.COM"]}, True) == {
            "sticky_domains": ["login.example.com"]}

    def test_empty_domains_ok_when_unsupported(self):
        assert sticky.validate_update({"sticky_domains": []}, False) == {"sticky_domains": []}

    @pytest.mark.parametrize("bad", ["localhost", "a b.com", "x.c", "1.2.3.4", "a.com/x"])
    def test_rejects_bad_domain(self, bad):
        with pytest.raises(ValueError):
            sticky.validate_update({"sticky_domains": [bad]}, True)

    def test_nothing_to_update_is_an_error(self):
        with pytest.raises(ValueError):
            sticky.validate_update({}, False)
        with pytest.raises(ValueError):
            sticky.validate_update({"bogus": 1}, False)
