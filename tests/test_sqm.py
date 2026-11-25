"""TDD: per-dial SQM (CAKE + cake-autorate) provisioning + live status parsing.

Pure module — command builders and the `tc`/`ps`/HNAT parser are all functions,
so this needs no router. Mirrors test_uci_sync.py in style.
"""
import pytest

from multidial import sqm
from multidial.config import Globals, MultidialConfig, Account


def _spec(slot, device=None):
    cfg = MultidialConfig(globals=Globals(),
                          accounts=[Account(name="a", userid="u", password="p",
                                            dials=slot + 1)])
    return cfg.make_spec(cfg.accounts[0], index=slot, slot=slot)


class TestIdleRecoveryWindow:
    """Pure policy checks; live validation is in tests/live/, not a fake runner."""

    def test_default_window_covers_min_to_base_recovery_with_margin(self):
        # At the upstream integer-truncated +1%/s recovery gain, 2880 ->
        # 50000 needs 289 whole adjustment steps. Keep the documented 420 s
        # floor so a short quiet
        # period cannot freeze a dial at the congested floor.
        assert sqm.recovery_steps_to_base(2880, 50000) == 289
        assert sqm.required_sustained_idle_sleep_s(2880, 50000) == 420

    def test_recovery_window_scales_when_bounds_change(self):
        window = sqm.required_sustained_idle_sleep_s(1000, 200000)
        assert window > 420
        # Expanded bounds must not turn the UCI float back into an integer
        # literal (e.g. `653`), which cake-autorate rejects at runtime.
        assert isinstance(window, float)
        assert str(window).endswith(".0")

    def test_integer_recovery_rejects_a_rate_that_cannot_advance(self):
        # E.g. 1 * 1010 // 1000 is still 1; never spin forever on a future
        # small-rate configuration.
        with pytest.raises(ValueError, match="makes no progress"):
            sqm.recovery_steps_to_base(1, 2)


# --- command builders --------------------------------------------------------
class TestSectionBuilders:
    def test_sqm_section_binds_interface_and_cake(self):
        cmds = sqm.sqm_section_cmds(_spec(0), Globals())
        joined = "\n".join(cmds)
        assert "uci set sqm.dial0=queue" in cmds
        assert "uci set sqm.dial0.interface='dial0'" in joined
        assert "uci set sqm.dial0.qdisc='cake'" in joined
        assert "uci set sqm.dial0.enabled='1'" in joined
        # ethernet/overhead for pure-ethernet macvlan dials (no PPPoE)
        assert "uci set sqm.dial0.linklayer='ethernet'" in joined
        assert f"uci set sqm.dial0.overhead='{sqm.SQM_OVERHEAD}'" in joined

    def test_sqm_section_isolates_hosts_behind_nat(self):
        joined = "\n".join(sqm.sqm_section_cmds(_spec(2), Globals()))
        # upload on the dial: hosts are sources; download on the IFB: destinations
        assert "uci set sqm.dial2.eqdisc_opts='nat dual-srchost'" in joined
        assert "uci set sqm.dial2.iqdisc_opts='nat dual-dsthost'" in joined
        # LuCI drops the opts on save unless both advanced flags are set
        assert "uci set sqm.dial2.qdisc_advanced='1'" in joined
        assert "uci set sqm.dial2.qdisc_really_really_advanced='1'" in joined

    def test_cakeautorate_section_binds_ifb_and_pings_own_dial(self):
        cmds = sqm.cakeautorate_section_cmds(_spec(1), Globals())
        joined = "\n".join(cmds)
        assert "uci set cake-autorate.dial1=cake_autorate" in cmds
        # download shaping lives on the ingress IFB, upload on the dial
        assert "uci set cake-autorate.dial1.dl_if='ifb4dial1'" in joined
        assert "uci set cake-autorate.dial1.ul_if='dial1'" in joined
        # each instance pings reflectors bound to its OWN dial (multi-homed req.)
        assert "uci set cake-autorate.dial1.ping_extra_args='-I dial1'" in joined
        # bounds carried through
        assert f"max_dl_shaper_rate_kbps='{sqm.DEFAULT_MAX_KBPS}'" in joined
        assert f"min_dl_shaper_rate_kbps='{sqm.DEFAULT_MIN_KBPS}'" in joined
        # Keep upstream idle sleep enabled, but never inherit its unsafe 60 s
        # default while the 1%/s recovery still needs several minutes.
        assert "enable_sleep_function='1'" in joined
        assert (
            f"sustained_idle_sleep_thr_s='{sqm.SUSTAINED_IDLE_SLEEP_THR_S}'"
            in joined
        )
        # This UCI option is schema-validated as a float by cake-autorate;
        # `420` crash-loops its procd child while the numeric-equivalent
        # literal `420.0` is accepted.
        assert "uci set cake-autorate.dial1.sustained_idle_sleep_thr_s='420.0'" in cmds
        # the LuCI status view has no data without SUMMARY lines (default 0)
        assert "uci set cake-autorate.dial1.output_summary_stats='1'" in cmds

    def test_provision_commits_both_configs(self):
        cmds = sqm.provision_cmds([_spec(0), _spec(1)], Globals())
        assert "uci commit sqm" in cmds
        assert "uci commit cake-autorate" in cmds
        # both dials present
        assert any("sqm.dial0=queue" in c for c in cmds)
        assert any("sqm.dial1=queue" in c for c in cmds)

    def test_provision_empty_specs_emits_nothing(self):
        assert sqm.provision_cmds([], Globals()) == []

    def test_apply_restarts_sqm_and_cakeautorate_cleanly(self):
        cmds = sqm.apply_cmds()
        joined = "\n".join(cmds)
        assert "/etc/init.d/sqm restart" in joined
        assert "/etc/init.d/cake-autorate stop 2>/dev/null || true" in cmds
        assert "/etc/init.d/cake-autorate start" in cmds
        assert "/etc/init.d/cake-autorate restart" not in cmds
        # OpenWrt procd can leave bash/fping children behind when the launcher is
        # restarted repeatedly; the restart path must reap those stale loops.
        assert any("[c]ake-autorate" in c and "kill" in c for c in cmds)
        assert any("[l]auncher" in c and "kill" in c for c in cmds)
        assert any("[f]ping" in c and "kill" in c for c in cmds)
        assert cmds.index("/etc/init.d/cake-autorate stop 2>/dev/null || true") < cmds.index(
            "/etc/init.d/cake-autorate start"
        )
        # HNAT is a persistent system setting (turboacc) now — the daemon must
        # NOT toggle offload; it only reports it via parse_status.
        assert not any("hook_toggle" in c for c in cmds)

    def test_restart_waits_for_old_instances_before_start(self):
        # Root cause of "one dial loses its cake-autorate loop on every restart":
        # the launcher backgrounds fresh per-dial instances immediately, and a
        # new instance whose predecessor's run dir still exists (with a live main
        # pid) self-terminates ("run_path already exists and an instance appears
        # to be running — Exiting"). The restart must WAIT for every stopped
        # instance to fully die, then sweep stale run dirs, BEFORE start — so the
        # fresh instances begin from a clean slate and all come up.
        cmds = sqm.cakeautorate_restart_cmds(enable=False)
        joined = "\n".join(cmds)
        settle = [i for i, c in enumerate(cmds)
                  if "[c]ake-autorate" in c and "while" in c]
        assert settle, "restart must wait for old instances to exit before start"
        start_i = cmds.index("/etc/init.d/cake-autorate start")
        last_kill_i = max(i for i, c in enumerate(cmds) if "kill" in c)
        # ordering: kill(s) -> settle-wait -> start
        assert last_kill_i < settle[0] < start_i
        # and it sweeps leftover per-dial run dirs so start is from a clean slate
        assert "/var/run/cake-autorate" in joined

    def test_apply_settles_before_starting_cakeautorate(self):
        # the same settle guard must be on the provision/apply path, not just the
        # standalone restart — apply_cmds() is what runs after (re)provisioning.
        cmds = sqm.apply_cmds()
        settle = [i for i, c in enumerate(cmds)
                  if "[c]ake-autorate" in c and "while" in c]
        assert settle
        assert settle[0] < cmds.index("/etc/init.d/cake-autorate start")

    def test_deprovision_restarts_cakeautorate_cleanly(self):
        cmds = sqm.deprovision_apply_cmds()
        assert "/etc/init.d/sqm restart" in cmds
        assert "/etc/init.d/cake-autorate restart" not in cmds
        assert "/etc/init.d/cake-autorate stop 2>/dev/null || true" in cmds
        assert "/etc/init.d/cake-autorate start" in cmds

    def test_deprovision_removes_one_dials_sections(self):
        cmds = sqm.deprovision_cmds("dial2")
        assert "uci -q delete sqm.dial2" in cmds
        assert "uci -q delete cake-autorate.dial2" in cmds
        assert "uci commit sqm" in cmds and "uci commit cake-autorate" in cmds


# --- rate conversion ---------------------------------------------------------
class TestRateConversion:
    def test_units(self):
        assert sqm._to_kbit("50Mbit") == 50000
        assert sqm._to_kbit("92177Kbit") == 92177
        assert sqm._to_kbit("1Gbit") == 1000000
        assert sqm._to_kbit("unlimited") is None
        assert sqm._to_kbit("8000bit") == 8  # raw bits -> kbit


# --- tc -s qdisc show parsing ------------------------------------------------
_TC = """\
qdisc noqueue 0: dev lo root refcnt 2
qdisc cake 800d: dev dial0 root refcnt 2 bandwidth 40Mbit besteffort triple-isolate
 Sent 455941721 bytes 2295745 pkt (dropped 8, overlimits 946112 requeues 0)
 backlog 0b 0p requeues 0
qdisc cake 8011: dev ifb4dial0 root refcnt 2 bandwidth 92177Kbit besteffort
 Sent 140177822 bytes 1947232 pkt (dropped 12, overlimits 438931 requeues 0)
 backlog 1514b 3p requeues 0
qdisc fq_codel 0: dev eth0 root refcnt 2 limit 10240p
 Sent 1 bytes 1 pkt (dropped 0, overlimits 0 requeues 0)
qdisc cake 8020: dev dial1 root refcnt 2 bandwidth unlimited besteffort
 Sent 0 bytes 0 pkt (dropped 0, overlimits 0 requeues 0)
 backlog 0b 0p requeues 0
"""


class TestTcParsing:
    def test_extracts_rate_drops_backlog(self):
        r = sqm.parse_tc_rates(_TC)
        assert r["dial0"]["bandwidth_kbit"] == 40000
        assert r["dial0"]["dropped"] == 8
        assert r["dial0"]["backlog_pkt"] == 0
        assert r["ifb4dial0"]["bandwidth_kbit"] == 92177
        assert r["ifb4dial0"]["dropped"] == 12
        assert r["ifb4dial0"]["backlog_pkt"] == 3

    def test_unlimited_rate_is_none(self):
        r = sqm.parse_tc_rates(_TC)
        assert r["dial1"]["bandwidth_kbit"] is None

    def test_non_cake_qdisc_ignored(self):
        r = sqm.parse_tc_rates(_TC)
        assert "eth0" not in r  # fq_codel, not cake
        assert "lo" not in r

    def test_empty_input(self):
        assert sqm.parse_tc_rates("") == {}


# --- cake-autorate process / HNAT --------------------------------------------
_PS = """\
 8864 root  bash /root/cake-autorate/launcher.sh
 9493 root  bash /root/cake-autorate/cake-autorate.sh /root/cake-autorate/config.dial0.sh
 9494 root  bash /root/cake-autorate/cake-autorate.sh /root/cake-autorate/config.dial1.sh
"""


class TestAutorateAndHnat:
    def test_running_devices(self):
        assert sqm.parse_autorate_pids(_PS) == {"dial0", "dial1"}

    def test_repeated_ps_workers_do_not_change_running_set(self):
        ps = _PS + (
            " 9495 root bash /root/cake-autorate/cake-autorate.sh "
            "/root/cake-autorate/config.dial1.sh\n"
        )
        assert sqm.parse_autorate_pids(ps) == {"dial0", "dial1"}

    def test_no_processes(self):
        assert sqm.parse_autorate_pids("") == set()

    def test_hnat_states(self):
        assert sqm.hnat_is_on("enabled") is True
        assert sqm.hnat_is_on("1") is True
        assert sqm.hnat_is_on("disabled") is False
        assert sqm.hnat_is_on("0") is False
        assert sqm.hnat_is_on("") is False  # no debugfs => offload absent


# --- full status assembly ----------------------------------------------------
class TestParseStatus:
    def test_folds_dl_from_ifb_ul_from_dial(self):
        st = sqm.parse_status(["dial0", "dial1"], _TC, _PS, "disabled", enabled=True)
        assert st["enabled"] is True
        assert st["hnat_offload"] is False
        d0 = next(d for d in st["dials"] if d["device"] == "dial0")
        assert d0["ul_kbit"] == 40000      # egress on dial0
        assert d0["dl_kbit"] == 92177      # ingress on ifb4dial0
        assert d0["cake"] is True
        assert d0["autorate"] is True
        assert d0["dl_backlog_pkt"] == 3

    def test_repeated_autorate_ps_lines_are_not_reported_as_duplicates(self):
        ps = _PS + (
            " 9495 root bash /root/cake-autorate/cake-autorate.sh "
            "/root/cake-autorate/config.dial1.sh\n"
        )
        st = sqm.parse_status(["dial0", "dial1"], _TC, ps, "0")
        d1 = next(d for d in st["dials"] if d["device"] == "dial1")
        assert d1["autorate"] is True
        assert "autorate_instances" not in d1
        assert "autorate_duplicate" not in d1
        assert "duplicates" not in st["aggregate"]

    def test_aggregate_counts_cake_and_autorate(self):
        st = sqm.parse_status(["dial0", "dial1"], _TC, _PS, "0")
        assert st["aggregate"]["total"] == 2
        # both have cake; both have autorate
        assert st["aggregate"]["active"] == 2

    def test_dial_without_cake_or_autorate(self):
        st = sqm.parse_status(["dial9"], _TC, _PS, "0")
        d9 = st["dials"][0]
        assert d9["cake"] is False
        assert d9["autorate"] is False
        assert d9["ul_kbit"] is None and d9["dl_kbit"] is None

    def test_hnat_on_flags_offload_bypass(self):
        st = sqm.parse_status(["dial0"], _TC, _PS, "enabled")
        assert st["hnat_offload"] is True


# --- NSS router (AX9000): ECM / NSS qdisc conditions that bypass per-dial CAKE ---
_OFFLOAD = (
    "ecm=1\n"
    "16: dial0@wan: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc cake state UP "
    "mode DEFAULT group default qlen 1000\    link/ether 02:00:00:00:00:10 brd "
    "ff:ff:ff:ff:ff:ff promiscuity 0 \    macvlan mode vepa bcqueuelen 1000 "
    "usedbcqueuelen 1000 addrgenmode eui64\n"
    "17: dial1@wan: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc cake state UP "
    "mode DEFAULT group default qlen 1000\    link/ether 02:00:00:00:00:11 brd "
    "ff:ff:ff:ff:ff:ff promiscuity 0 \    macvlan mode private bcqueuelen 1000\n"
)
_NSS_ON_WAN = (
    "qdisc nsstbl 1: dev wan root refcnt 2 buffer/maxrate/mtu 3028b/100Mbit/1518b accel_mode 0\n"
    "qdisc nssprio 10: dev wan parent 1: refcnt 2 bands 3 accel_mode 0\n"
    "qdisc nsstbl 1: dev ifb@wan root refcnt 2 buffer/maxrate/mtu 3028b/100Mbit/1518b\n"
)


class TestOffloadWarnings:
    def test_parse_offload_reads_ecm_and_macvlan_modes(self):
        ecm, modes = sqm.parse_offload(_OFFLOAD)
        assert ecm is True
        assert modes == {"dial0": "vepa", "dial1": "private"}

    def test_no_ecm_no_macvlan(self):
        assert sqm.parse_offload("") == (False, {})

    def test_private_macvlan_under_ecm_is_flagged(self):
        w = sqm.offload_warnings(["dial0", "dial1"], _TC, _OFFLOAD, "wan")
        assert len(w) == 1 and "dial1" in w[0] and "dial0" not in w[0]

    def test_private_macvlan_without_ecm_is_fine(self):
        # MediaTek (op): no ECM module; HNAT is reported separately.
        text = _OFFLOAD.replace("ecm=1\n", "")
        assert sqm.offload_warnings(["dial0", "dial1"], _TC, text, "eth1") == []

    def test_unmanaged_private_macvlan_is_not_ours_to_report(self):
        assert sqm.offload_warnings(["dial0"], _TC, _OFFLOAD, "wan") == []

    def test_nss_sqm_on_the_physical_wan_is_flagged(self):
        w = sqm.offload_warnings(["dial0"], _TC + _NSS_ON_WAN, "", "wan")
        assert len(w) == 1 and "ifb@wan" in w[0] and "wan," in w[0]

    def test_nss_qdisc_elsewhere_is_ignored(self):
        tc = _NSS_ON_WAN.replace("dev wan", "dev lan1").replace("ifb@wan", "ifb@lan1")
        assert sqm.offload_warnings(["dial0"], tc, "", "wan") == []

    def test_status_carries_macvlan_mode_and_warnings(self):
        st = sqm.parse_status(["dial0", "dial1"], _TC, _PS, "", offload_text=_OFFLOAD,
                              phy_ifname="wan")
        assert [d["macvlan_mode"] for d in st["dials"]] == ["vepa", "private"]
        assert len(st["offload_warnings"]) == 1
        assert st["hnat_offload"] is False

    def test_status_defaults_keep_the_mediatek_payload(self):
        st = sqm.parse_status(["dial0"], _TC, _PS, "disabled")
        assert st["offload_warnings"] == []
        assert st["dials"][0]["macvlan_mode"] is None


class TestShaperLimits:
    """`/api/sqm` limits: the cake-autorate floor/base/ceiling the UI draws bars against."""

    UCI = (
        "cake-autorate.dial0=cake_autorate\n"
        "cake-autorate.dial0.min_dl_shaper_rate_kbps='3000'\n"
        "cake-autorate.dial0.base_dl_shaper_rate_kbps='40000'\n"
        "cake-autorate.dial0.max_dl_shaper_rate_kbps='95000'\n"
        "cake-autorate.dial0.min_ul_shaper_rate_kbps='2000'\n"
        "cake-autorate.dial0.base_ul_shaper_rate_kbps='30000'\n"
        "cake-autorate.dial0.max_ul_shaper_rate_kbps='60000'\n"
        "cake-autorate.dial1=cake_autorate\n"
        "cake-autorate.dial1.max_dl_shaper_rate_kbps='oops'\n"
    )

    def test_reads_configured_values(self):
        lim = sqm.parse_limits(["dial0"], self.UCI)
        assert lim["dial0"] == {
            "dl_min": 3000, "dl_base": 40000, "dl_max": 95000,
            "ul_min": 2000, "ul_base": 30000, "ul_max": 60000,
        }

    def test_missing_or_invalid_values_fall_back_to_defaults(self):
        lim = sqm.parse_limits(["dial1", "dial2"], self.UCI)
        want = {
            "dl_min": sqm.DEFAULT_MIN_KBPS, "dl_base": sqm.DEFAULT_BASE_KBPS,
            "dl_max": sqm.DEFAULT_MAX_KBPS, "ul_min": sqm.DEFAULT_MIN_KBPS,
            "ul_base": sqm.DEFAULT_BASE_KBPS, "ul_max": sqm.DEFAULT_MAX_KBPS,
        }
        assert lim["dial1"] == want
        assert lim["dial2"] == want

    def test_only_requested_devices_are_reported(self):
        assert set(sqm.parse_limits(["dial0"], self.UCI)) == {"dial0"}
