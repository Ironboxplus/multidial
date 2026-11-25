"""TDD: the orchestration daemon — reconcile UCI + per-dial keepalive auth.

Everything the daemon touches (shell commands via a Runner, HTTP via a
transport, the clock) is injected, so one tick is fully testable off-router.
"""
import json
import pathlib
import re

import pytest

from multidial import sqm, sticky
from multidial.api import route
from multidial.config import MultidialConfig

TEST_STICKY_RANGES = ["198.51.100.0/24", "203.0.113.0/24"]
from multidial.daemon import Daemon
from multidial.dialer import NetStatus

MULTIDIAL_SHOW = """\
multidial.globals=globals
multidial.globals.enabled='1'
multidial.globals.phy_ifname='eth1'
multidial.globals.track_ips='192.0.2.11 192.0.2.12 192.0.2.13'
multidial.globals.probe_url='http://probe.example.test/check'
multidial.globals.intranet_ranges='10.0.0.0/8 172.16.0.0/12'
multidial.globals.sticky_ranges='198.51.100.0/24 203.0.113.0/24'
multidial.globals.dns_servers='192.0.2.53 198.51.100.53'
multidial.globals.dns_rebind_domains='site.example.test example.test'
multidial.globals.gateway='192.0.2.1'
multidial.globals.interval='20'
multidial.acct_a=account
multidial.acct_a.userid='demo_user_a'
multidial.acct_a.password='s3cr3t'
multidial.acct_a.dials='2'
"""

NETWORK_SHOW = "network.lan=interface\nnetwork.wan=interface\n"
MWAN3_SHOW = "mwan3.globals=globals\n"
# The managed mwan3 rules exactly as desired_sections() renders them for the
# default globals — a steady-state router carries this, so the content-aware
# restart gate sees "unchanged" and does NOT bounce mwan3 on a daemon restart.
MWAN3_RULES_SHOW = (
    "mwan3.md_intra0=rule\n"
    "mwan3.md_intra0.dest_ip='10.0.0.0/8'\n"
    "mwan3.md_intra0.family='ipv4'\n"
    "mwan3.md_intra0.sticky='1'\n"
    "mwan3.md_intra0.timeout='600'\n"
    "mwan3.md_intra0.use_policy='md_balanced'\n"
    "mwan3.md_intra1=rule\n"
    "mwan3.md_intra1.dest_ip='172.16.0.0/12'\n"
    "mwan3.md_intra1.family='ipv4'\n"
    "mwan3.md_intra1.sticky='1'\n"
    "mwan3.md_intra1.timeout='600'\n"
    "mwan3.md_intra1.use_policy='md_balanced'\n"
    "mwan3.md_svc=rule\n"
    f"mwan3.md_svc.dest_ip='{','.join(TEST_STICKY_RANGES)}'\n"
    "mwan3.md_svc.family='ipv4'\n"
    "mwan3.md_svc.sticky='1'\n"
    "mwan3.md_svc.timeout='600'\n"
    "mwan3.md_svc.use_policy='md_balanced'\n"
    "mwan3.md_default=rule\n"
    "mwan3.md_default.dest_ip='0.0.0.0/0'\n"
    "mwan3.md_default.family='ipv4'\n"
    "mwan3.md_default.use_policy='md_balanced'\n"
)


class TestMwanTrackReloadPolicy:
    """Pure command/config checks; activation is validated on the real router."""

    def test_parses_live_tracking_options_per_dial(self):
        live = (
            "mwan3.dial0=interface\n"
            "mwan3.dial0.timeout='2'\n"
            "mwan3.dial0.track_ip='192.0.2.11' '198.51.100.53'\n"
            "mwan3.dial1.track_ip='192.0.2.11' '192.0.2.13'\n"
        )
        parsed = Daemon._managed_tracking(live)
        assert parsed["dial0"]["timeout"] == "2"
        assert parsed["dial0"]["track_ip"] == ["192.0.2.11", "198.51.100.53"]
        assert parsed["dial1"]["track_ip"] == ["192.0.2.11", "192.0.2.13"]

    @staticmethod
    def _live_mwan3(timeout):
        """`uci show mwan3` for a steady-state router whose dial0/dial1 trackers
        match desired_sections() except for the given `timeout`."""
        cfg = MultidialConfig.from_uci_show(MULTIDIAL_SHOW)
        specs = [cfg.make_spec(cfg.accounts[0], index=i, slot=i) for i in (0, 1)]
        lines = []
        for dev, want in Daemon._desired_tracking(cfg, specs).items():
            lines.append(f"mwan3.{dev}=interface")
            for key, val in want.items():
                if key == "track_ip":
                    val = " ".join(f"'{ip}'" for ip in val)
                elif key == "timeout":
                    val = f"'{timeout}'"
                else:
                    val = f"'{val}'"
                lines.append(f"mwan3.{dev}.{key}={val}")
        return MWAN3_RULES_SHOW + "\n".join(lines) + "\n"

    def _tick_with_live(self, timeout):
        r = FakeRunner(network="network.dial0=interface\nnetwork.dial1=interface\n"
                               "network.md_dev0=device\nnetwork.md_dev1=device\n",
                       mwan3=self._live_mwan3(timeout))
        d = _daemon(runner=r)
        d.tick()
        return r

    def test_changed_tracker_timeout_respawns_trackers_without_mwan3_restart(self):
        # A live tracker still on the old timeout must be respawned: mwan3track
        # reads timeout/interval/down/up only once, at process start.
        r = self._tick_with_live(timeout="2")
        for dev in ("dial0", "dial1"):
            assert Daemon._mwan3_tracker_reload_cmd(dev) in r.commands
        assert "/etc/init.d/mwan3 restart" not in r.commands
        assert "/etc/init.d/mwan3 reload" not in r.commands

    def test_matching_tracker_settings_do_not_respawn(self):
        r = self._tick_with_live(timeout="1")
        assert not any("instance\":\"track_" in c for c in r.commands)
        assert "/etc/init.d/mwan3 restart" not in r.commands

    def test_tracker_reload_is_scoped_to_procd_instance(self):
        assert Daemon._mwan3_tracker_reload_cmd("dial5") == (
            "ubus call service signal "
            "'{\"name\":\"mwan3\",\"instance\":\"track_dial5\",\"signal\":15}'"
        )

CAPTIVE_BODY = (
    "<script>top.self.location.href='http://192.0.2.80/eportal/index.jsp?"
    "wlanuserip=192.0.2.20&wlanacname=ac01';</script>"
)
SUCCESS_JSON = '{"result":"success","message":"ok","userIndex":"x"}'


class FakeRunner:
    """Programmable `run(cmd) -> (rc, stdout)`; records every command."""

    def __init__(self, multidial=MULTIDIAL_SHOW, network=NETWORK_SHOW, mwan3=MWAN3_SHOW):
        self.responses = {
            "uci show multidial": (0, multidial),
            "uci show network": (0, network),
            "uci show mwan3": (0, mwan3),
        }
        self.commands = []

    def run(self, cmd):
        self.commands.append(cmd)
        if cmd in self.responses:
            return self.responses[cmd]
        # Model `uci -q delete multidial.<name>` so a later `uci show multidial`
        # reflects it — delete_account force_reloads, and without this the static
        # canned config would "resurrect" the just-deleted account.
        m = re.match(r"uci -q delete multidial\.(\w+)$", cmd)
        if m:
            name = m.group(1)
            cur = self.responses["uci show multidial"][1]
            kept = "\n".join(ln for ln in cur.splitlines()
                             if not ln.startswith(f"multidial.{name}"))
            self.responses["uci show multidial"] = (0, kept)
        return (0, "")


class FakeTransport:
    def __init__(self, get_seq=None, post=(200, SUCCESS_JSON)):
        self.get_seq = list(get_seq or [(204, "")])
        self.post_resp = post
        self.calls = []

    def get(self, url, device, timeout=6, fwmark=0):
        self.calls.append(("GET", url, device))
        return self.get_seq.pop(0) if self.get_seq else (204, "")

    def post(self, url, fields, device, timeout=10, fwmark=0):
        self.calls.append(("POST", url, device))
        return self.post_resp


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        self.t += 1
        return self.t


def _daemon(runner=None, transport=None, clock=None):
    return Daemon(
        runner=runner or FakeRunner(),
        transport=transport or FakeTransport(),
        clock=clock or FakeClock(),
        sleep=lambda _s: None,
    )


# --- login-backoff fixtures (禁止频繁登陆) -----------------------------------
SINGLE_DIAL_SHOW = """\
multidial.globals=globals
multidial.globals.enabled='1'
multidial.globals.phy_ifname='eth1'
multidial.globals.track_ips='192.0.2.11 192.0.2.12 192.0.2.13'
multidial.globals.probe_url='http://probe.example.test/check'
multidial.globals.intranet_ranges='10.0.0.0/8 172.16.0.0/12'
multidial.globals.sticky_ranges='198.51.100.0/24 203.0.113.0/24'
multidial.globals.dns_servers='192.0.2.53 198.51.100.53'
multidial.globals.dns_rebind_domains='site.example.test example.test'
multidial.globals.gateway='192.0.2.1'
multidial.globals.interval='20'
multidial.acct_a=account
multidial.acct_a.userid='demo_user_a'
multidial.acct_a.password='s3cr3t'
multidial.acct_a.dials='1'
"""

FAIL_JSON = '{"result":"fail","message":"密码错误"}'  # 密码错误 -> FAIL_CRED


class StepClock:
    """Clock the test drives explicitly (FakeClock auto-increments, unusable here)."""
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class _CaptiveFailTransport:
    """Every GET looks captive; every login POST fails. Counts POSTs."""
    def __init__(self):
        self.posts = 0

    def get(self, url, device, timeout=6, fwmark=0):
        return (200, CAPTIVE_BODY)

    def post(self, url, fields, device, timeout=10, fwmark=0):
        self.posts += 1
        return (200, FAIL_JSON)


class _SeqPostTransport:
    """Captive GETs; login POST returns the next item of a programmed sequence."""
    def __init__(self, posts):
        self._posts = list(posts)
        self.posts = 0

    def get(self, url, device, timeout=6, fwmark=0):
        return (200, CAPTIVE_BODY)

    def post(self, url, fields, device, timeout=10, fwmark=0):
        self.posts += 1
        return self._posts.pop(0) if self._posts else (200, SUCCESS_JSON)


class TestLoginBackoff:
    def _d(self, transport, clock):
        return _daemon(runner=FakeRunner(multidial=SINGLE_DIAL_SHOW),
                       transport=transport, clock=clock)

    def test_failed_auth_backs_off_does_not_hammer(self):
        clk = StepClock()
        tr = _CaptiveFailTransport()
        d = self._d(tr, clk)
        d.tick()                  # provision + 1 auth attempt (fails) -> backoff 30s
        assert tr.posts == 1
        clk.advance(5)
        d.tick()                  # inside the 30s window -> MUST NOT re-auth
        assert tr.posts == 1
        clk.advance(30)
        d.tick()                  # window elapsed -> exactly one retry
        assert tr.posts == 2

    def test_successful_auth_clears_backoff(self):
        clk = StepClock()
        tr = _SeqPostTransport([(200, FAIL_JSON), (200, SUCCESS_JSON)])
        d = self._d(tr, clk)
        d.tick()                  # fail -> backoff recorded
        assert "dial0" in d._auth_backoff
        clk.advance(40)           # past the window
        d.tick()                  # success -> backoff cleared
        assert d._auth_backoff == {}

    def test_backoff_grows_exponentially_then_caps(self):
        clk = StepClock()
        tr = _CaptiveFailTransport()
        d = self._d(tr, clk)
        delays = []
        for _ in range(8):
            d.tick()
            nxt = d._auth_backoff["dial0"]["next_ts"]
            delays.append(nxt - clk.t)
            clk.advance(nxt - clk.t)   # jump exactly to the next allowed instant
        assert delays[0] == 30.0
        assert delays[1] == 60.0
        assert max(delays) == 600.0    # capped at 10 min


class TestReconcile:
    def test_first_tick_reconciles_and_reloads(self):
        r = FakeRunner()
        d = _daemon(runner=r)
        reconciled = d.tick()
        assert reconciled is True
        # macvlan devices + mwan3 members created for both dials
        joined = "\n".join(r.commands)
        assert "uci set network.dial0=interface" in joined
        assert "uci set network.dial1=interface" in joined
        assert "uci set mwan3.m_dial0=member" in joined
        # netifd + mwan3 reloaded
        assert "ubus call network reload" in r.commands
        assert "/etc/init.d/mwan3 restart" in r.commands

    def test_first_tick_reconciles_dnsmasq_defaults(self):
        r = FakeRunner()
        d = _daemon(runner=r)
        d.tick()
        joined = "\n".join(r.commands)
        campus = "uci add_list dhcp.@dnsmasq[0].server='192.0.2.53'"
        public = "uci add_list dhcp.@dnsmasq[0].server='198.51.100.53'"
        assert "uci set dhcp.@dnsmasq[0].noresolv='0'" in r.commands
        assert "uci set dhcp.@dnsmasq[0].strictorder='1'" in r.commands
        assert campus in r.commands
        assert public in r.commands
        assert "uci add_list dhcp.@dnsmasq[0].rebind_domain='/site.example.test/'" in r.commands
        assert joined.index(campus) < joined.index(public)
        assert "/etc/init.d/dnsmasq restart" in r.commands

    def test_unchanged_config_skips_reconcile(self):
        r = FakeRunner()
        d = _daemon(runner=r)
        d.tick()
        n_after_first = len(r.commands)
        d.tick()  # same config text -> no new reconcile
        new_cmds = r.commands[n_after_first:]
        assert not any(c.startswith("uci set network.dial") for c in new_cmds)
        assert "ubus call network reload" not in new_cmds

    def test_zero_dials_does_not_touch_network(self):
        # globals only, no accounts -> no dials -> must NOT reload/restart anything
        r = FakeRunner(multidial="multidial.globals=globals\n"
                                  "multidial.globals.enabled='1'\n"
                                  "multidial.globals.phy_ifname='eth1'\n")
        d = _daemon(runner=r)
        reconciled = d.tick()
        assert reconciled is True
        assert "ubus call network reload" not in r.commands
        assert "/etc/init.d/mwan3 restart" not in r.commands
        assert not any(c.startswith("uci set network.dial") for c in r.commands)

    def test_restart_with_existing_sections_skips_network_reload(self):
        # daemon restart, config unchanged: all managed sections already exist,
        # so NO network reload / mwan3 restart (must not drop dial sessions).
        net = ("network.lan=interface\nnetwork.wan=interface\n"
               "network.md_dev0=device\nnetwork.dial0=interface\n"
               "network.md_dev1=device\nnetwork.dial1=interface\n")
        mw = ("mwan3.globals=globals\n"
              "mwan3.dial0=interface\nmwan3.m_dial0=member\n"
              "mwan3.dial1=interface\nmwan3.m_dial1=member\n"
              "mwan3.md_balanced=policy\n" + MWAN3_RULES_SHOW)
        r = FakeRunner(network=net, mwan3=mw)
        d = _daemon(runner=r)
        reconciled = d.tick()
        assert reconciled is True
        assert "ubus call network reload" not in r.commands
        assert "/etc/init.d/mwan3 restart" not in r.commands

    def test_changing_sticky_policy_restarts_mwan3(self):
        # finding: a CONTENT change (not just a new rule name) must restart mwan3 so
        # the live kernel stops enforcing stale rules. Here sticky is disabled while
        # the router still carries the md_intra* rules — reconcile deletes them and
        # mwan3 must restart to drop the pinning, without bouncing dials.
        net = ("network.lan=interface\nnetwork.wan=interface\n"
               "network.md_dev0=device\nnetwork.dial0=interface\n"
               "network.md_dev1=device\nnetwork.dial1=interface\n")
        mw = ("mwan3.globals=globals\n"
              "mwan3.dial0=interface\nmwan3.m_dial0=member\n"
              "mwan3.dial1=interface\nmwan3.m_dial1=member\n"
              "mwan3.md_balanced=policy\n" + MWAN3_RULES_SHOW)
        r = FakeRunner(multidial=MULTIDIAL_SHOW + "multidial.globals.intranet_sticky='0'\n",
                       network=net, mwan3=mw)
        d = _daemon(runner=r)
        d.tick()
        assert "uci -q delete mwan3.md_intra0" in r.commands
        assert "/etc/init.d/mwan3 restart" in r.commands
        assert "ubus call network reload" not in r.commands  # no dial bounce

    def test_new_sticky_rules_restart_mwan3_without_touching_dials(self):
        # Upgrade path: dials already exist (additive, no new device), but the
        # intranet sticky rules are new. They must be created AND activated with a
        # single mwan3 restart — without an ubus network reload / ifup that would
        # bounce the running dial sessions.
        net = ("network.lan=interface\nnetwork.wan=interface\n"
               "network.md_dev0=device\nnetwork.dial0=interface\n"
               "network.md_dev1=device\nnetwork.dial1=interface\n")
        mw = ("mwan3.globals=globals\n"
              "mwan3.dial0=interface\nmwan3.m_dial0=member\n"
              "mwan3.dial1=interface\nmwan3.m_dial1=member\n"
              "mwan3.md_balanced=policy\nmwan3.md_default=rule\n")  # no md_intra*
        r = FakeRunner(network=net, mwan3=mw)
        d = _daemon(runner=r)
        d.tick()
        assert "uci set mwan3.md_intra0=rule" in r.commands
        assert "/etc/init.d/mwan3 restart" in r.commands
        assert "ubus call network reload" not in r.commands  # no dial bounce

    def test_new_service_sticky_rule_restarts_mwan3_once_without_touching_dials(self):
        # Upgrade path of the live router: intranet rules already present, md_svc
        # (Aliyun login ranges) is new. One mwan3 restart activates it; the
        # running dials are not reloaded.
        net = ("network.lan=interface\nnetwork.wan=interface\n"
               "network.md_dev0=device\nnetwork.dial0=interface\n"
               "network.md_dev1=device\nnetwork.dial1=interface\n")
        without_svc = re.sub(r"mwan3\.md_svc[^\n]*\n", "", MWAN3_RULES_SHOW)
        mw = ("mwan3.globals=globals\n"
              "mwan3.dial0=interface\nmwan3.m_dial0=member\n"
              "mwan3.dial1=interface\nmwan3.m_dial1=member\n"
              "mwan3.md_balanced=policy\n" + without_svc)
        r = FakeRunner(network=net, mwan3=mw)
        d = _daemon(runner=r)
        d.tick()
        assert "uci set mwan3.md_svc=rule" in r.commands
        assert r.commands.count("/etc/init.d/mwan3 restart") == 1
        assert "ubus call network reload" not in r.commands

    def test_orphan_section_not_in_registry_is_left_alone(self):
        # An unmanaged dial2 network section (never registered by us) is NOT
        # touched: scale-down only removes dials WE provisioned (the registry is
        # truth), so a stray/manual section is never auto-deleted.
        r = FakeRunner(network="network.dial0=interface\nnetwork.dial1=interface\n"
                               "network.dial2=interface\nnetwork.md_dev2=device\n"
                               "network.lan=interface\n")
        d = _daemon(runner=r)
        d.tick()  # registers dial0/dial1 for acct_a (count=2); dial2 is an orphan
        joined = "\n".join(r.commands)
        assert "uci -q delete network.dial2" not in joined
        assert "uci -q delete network.md_dev2" not in joined
        # adopting existing dial0/dial1 needs no network reload either
        assert "ubus call network reload" not in r.commands


class TestProvisioningLifecycle:
    def test_count_increase_provisions_only_new_dial_via_ifup(self):
        # registry already has dial0/dial1 (existing sections); raising the count
        # to 3 creates ONLY dial2 and ifups just it — never touches dial0/dial1
        net = ("network.lan=interface\nnetwork.dial0=interface\n"
               "network.md_dev0=device\nnetwork.dial1=interface\n"
               "network.md_dev1=device\n")
        r = FakeRunner(
            multidial=MULTIDIAL_SHOW.replace("dials='2'", "dials='3'"),
            network=net)
        d = _daemon(runner=r)
        d.tick()
        assert "ifup dial2" in r.commands
        assert "ifup dial0" not in r.commands and "ifup dial1" not in r.commands
        assert "ubus call network reload" in r.commands  # register new section

    def test_count_decrease_decommissions_excess(self):
        # provision 2, then lower count to 1: the daemon actively tears down the
        # highest-index dial (dial1), leaving dial0. This is the operator-driven
        # scale-down: "改拨号数减少" must take effect, not sit as a no-op.
        r = FakeRunner()
        d = _daemon(runner=r)
        d.tick()  # provisions + registers dial0, dial1
        r2 = FakeRunner(multidial=MULTIDIAL_SHOW.replace("dials='2'", "dials='1'"),
                        network="network.dial0=interface\nnetwork.dial1=interface\n"
                                "network.md_dev0=device\nnetwork.md_dev1=device\n")
        d.runner = r2
        d._last_config_text = None  # force re-read of the lowered count
        d.tick()
        joined = "\n".join(r2.commands)
        # dial1 (highest index) is torn down; dial0 survives
        assert "ifdown dial1" in r2.commands
        assert "uci -q delete network.dial1" in joined
        assert "ifdown dial0" not in r2.commands
        assert d.control.is_provisioned("dial1") is False
        assert {x["device"] for x in d.snapshot()["dials"]} == {"dial0"}

    def test_scale_down_removes_only_the_excess_highest_index(self):
        # acct_a with 3 dials, lowered to 2: ONLY the index-2 dial (dial2) goes;
        # dial0/dial1 keep running, and the balancer is rebuilt exactly once.
        r = FakeRunner(multidial=MULTIDIAL_SHOW.replace("dials='2'", "dials='3'"))
        d = _daemon(runner=r)
        d.tick()  # dial0/1/2 registered
        assert {"dial0", "dial1", "dial2"} <= {x["device"] for x in d.snapshot()["dials"]}
        # r2 carries the rules the first tick created, so the content-aware gate
        # sees them unchanged: the ONLY restart is the teardown's batched one.
        r2 = FakeRunner(multidial=MULTIDIAL_SHOW.replace("dials='2'", "dials='2'"),
                        network="network.dial0=interface\nnetwork.dial1=interface\n"
                                "network.dial2=interface\n",
                        mwan3=MWAN3_SHOW + MWAN3_RULES_SHOW)
        d.runner = r2
        d._last_config_text = None
        d.tick()
        joined = "\n".join(r2.commands)
        assert "ifdown dial2" in r2.commands
        assert "ifdown dial1" not in r2.commands and "ifdown dial0" not in r2.commands
        assert {x["device"] for x in d.snapshot()["dials"]} == {"dial0", "dial1"}
        assert joined.count("/etc/init.d/mwan3 restart") == 1  # batched teardown

    def test_scale_down_prefers_stopped_dial_over_healthy(self):
        # acct_a has dial0 (operator-stopped, e.g. MAC-blocked) + dial1 (healthy).
        # Lowering to 1 must tear down the STOPPED dial0, never strand the account
        # on a dead dial by removing the working higher-index dial1.
        r = FakeRunner()
        d = _daemon(runner=r)
        d.tick()  # provisions dial0, dial1
        d.control.set_stopped("dial0", True)
        r2 = FakeRunner(multidial=MULTIDIAL_SHOW.replace("dials='2'", "dials='1'"),
                        network="network.dial0=interface\nnetwork.dial1=interface\n"
                                "network.md_dev0=device\nnetwork.md_dev1=device\n")
        d.runner = r2
        d._last_config_text = None
        d.tick()
        assert "ifdown dial0" in r2.commands
        assert "ifdown dial1" not in r2.commands
        assert {x["device"] for x in d.snapshot()["dials"]} == {"dial1"}

    def test_decommission_removes_one_dial_and_syncs_count(self):
        r = FakeRunner()
        d = _daemon(runner=r)
        d.tick()
        assert "dial1" in {x["device"] for x in d.snapshot()["dials"]}
        d.decommission_dial("dial1")
        joined = "\n".join(r.commands)
        assert "ifdown dial1" in r.commands
        assert "uci -q delete network.dial1" in joined
        assert "uci -q delete network.md_dev1" in joined
        # account count synced down to the remaining registered dial
        assert "uci set multidial.acct_a.dials='1'" in joined
        # gone from the registry + snapshot
        assert d.control.is_provisioned("dial1") is False
        assert "dial1" not in {x["device"] for x in d.snapshot()["dials"]}

    def test_decommission_then_reconcile_does_not_recreate(self):
        r = FakeRunner()
        d = _daemon(runner=r)
        d.tick()
        d.decommission_dial("dial1")
        # next tick with the synced (lower) count must not re-provision dial1
        r2 = FakeRunner(multidial=MULTIDIAL_SHOW.replace("dials='2'", "dials='1'"),
                        network="network.dial0=interface\nnetwork.md_dev0=device\n")
        d.runner = r2
        d._last_config_text = None
        d.tick()
        assert "ifup dial1" not in r2.commands

    def test_delete_account_decommissions_its_dials_not_just_the_section(self):
        # Regression: deleting an account must fully tear down EVERY dial it owns.
        # The reconcile is additive (never prunes) and _registered_specs skips a
        # deleted account's dials, so a section-only delete leaks the dial: netdev
        # up with a live DHCP lease + lingering network/mwan3/sqm/cake-autorate
        # sections, ip rules and a running cake-autorate loop.
        r = FakeRunner()
        d = _daemon(runner=r)
        d.tick()  # provisions dial0 + dial1 for acct_a (dials='2')
        assert {"dial0", "dial1"} <= {x["device"] for x in d.snapshot()["dials"]}
        r.commands.clear()
        d.delete_account("acct_a")
        joined = "\n".join(r.commands)
        for dev in ("dial0", "dial1"):
            assert f"ifdown {dev}" in r.commands
            assert f"uci -q delete network.{dev}" in joined
            assert f"uci -q delete mwan3.{dev}" in joined
            assert f"uci -q delete cake-autorate.{dev}" in joined
            assert f"ip link del {dev}" in r.commands  # macvlan netdev destroyed
            assert d.control.is_provisioned(dev) is False
        assert "uci -q delete multidial.acct_a" in joined
        # both dials torn down, but the balancer is rebuilt ONCE, not per-dial
        assert joined.count("/etc/init.d/mwan3 restart") == 1

    def test_rotate_mac_rewrites_only_that_dial(self):
        r = FakeRunner()
        d = _daemon(runner=r)
        d.tick()
        r.commands.clear()  # scope assertions to the rotate, not initial provision
        d.rotate_mac("dial1")
        joined = "\n".join(r.commands)
        # rotation 1 of slot 1 -> :11 becomes :21
        assert "uci set network.md_dev1.macaddr='02:00:00:00:00:21'" in joined
        assert "ifdown dial1" in r.commands and "ifup dial1" in r.commands
        # never rewrites a sibling's MAC
        assert "md_dev0.macaddr" not in joined
        assert d.control.get_rotation("dial1") == 1

    def test_rotate_mac_persists_across_reconcile(self):
        r = FakeRunner()
        d = _daemon(runner=r)
        d.tick()
        d.rotate_mac("dial1")
        # a subsequent reconcile must re-emit the ROTATED mac, not the base :11
        r.commands.clear()
        d._last_config_text = None
        d.tick()
        joined = "\n".join(r.commands)
        assert "macaddr='02:00:00:00:00:21'" in joined
        assert "macaddr='02:00:00:00:00:11'" not in joined


class TestSqmLifecycle:
    def _prime_sqm_healthy(self, runner):
        for dev in ("dial0", "dial1"):
            runner.responses[f"uci -q show sqm.{dev}"] = (
                0, f"sqm.{dev}=queue\nsqm.{dev}.interface='{dev}'\n")
            runner.responses[f"uci -q show cake-autorate.{dev}"] = (
                0, f"cake-autorate.{dev}=cake_autorate\n")

    def test_reconcile_provisions_sqm_per_dial(self):
        r = FakeRunner()
        d = _daemon(runner=r)
        d.tick()
        joined = "\n".join(r.commands)
        # each dial gets its own CAKE queue + cake-autorate instance
        assert "uci set sqm.dial0=queue" in joined
        assert "uci set sqm.dial1=queue" in joined
        assert "uci set cake-autorate.dial0=cake_autorate" in joined
        assert "uci set cake-autorate.dial1.ping_extra_args='-I dial1'" in joined
        # services brought up (HNAT is a persistent system setting, not toggled here)
        assert "/etc/init.d/cake-autorate restart" not in r.commands
        assert "/etc/init.d/cake-autorate stop 2>/dev/null || true" in r.commands
        assert "/etc/init.d/cake-autorate start" in r.commands
        assert any("[c]ake-autorate" in c for c in r.commands)
        assert not any("hook_toggle" in c for c in r.commands)
        # registry remembers it so we don't re-provision next time
        assert d.control.get("dial0").sqm_provisioned is True
        assert d.control.get("dial1").sqm_provisioned is True

    def test_sqm_provisioned_once_not_reapplied(self):
        r = FakeRunner()
        d = _daemon(runner=r)
        d.tick()
        r.commands.clear()
        d._last_config_text = None  # force another reconcile pass
        d.tick()
        new = "\n".join(r.commands)
        # already provisioned -> no SQM uci, no service churn
        assert "uci set sqm.dial0=queue" not in new
        assert "/etc/init.d/cake-autorate start" not in r.commands

    def test_tick_reprovisions_sqm_when_sqm_section_was_deleted(self):
        r = FakeRunner()
        d = _daemon(runner=r)
        d.tick()
        self._prime_sqm_healthy(r)
        r.responses["uci -q show sqm.dial0"] = (1, "")
        r.commands.clear()

        d.tick()  # same multidial config: this must still self-heal SQM drift

        joined = "\n".join(r.commands)
        assert "uci -q show sqm.dial0" in r.commands
        assert "uci set sqm.dial0=queue" in joined
        assert "uci set cake-autorate.dial0=cake_autorate" in joined
        assert "uci set sqm.dial1=queue" not in joined
        assert "ubus call network reload" not in r.commands
        assert "/etc/init.d/mwan3 restart" not in r.commands
        assert "/etc/init.d/cake-autorate restart" not in r.commands
        assert "/etc/init.d/cake-autorate stop 2>/dev/null || true" in r.commands
        assert "/etc/init.d/cake-autorate start" in r.commands
        assert d.control.get("dial0").sqm_provisioned is True

    def test_tick_reprovisions_sqm_when_cake_autorate_section_was_deleted(self):
        r = FakeRunner()
        d = _daemon(runner=r)
        d.tick()
        self._prime_sqm_healthy(r)
        r.responses["uci -q show cake-autorate.dial1"] = (1, "")
        r.commands.clear()

        d.tick()

        joined = "\n".join(r.commands)
        assert "uci -q show cake-autorate.dial1" in r.commands
        assert "uci set sqm.dial1=queue" in joined
        assert "uci set cake-autorate.dial1=cake_autorate" in joined
        assert "uci set sqm.dial0=queue" not in joined
        assert "/etc/init.d/cake-autorate restart" not in r.commands
        assert "/etc/init.d/cake-autorate stop 2>/dev/null || true" in r.commands
        assert "/etc/init.d/cake-autorate start" in r.commands

    def test_force_reload_restarts_sqm_services_even_when_sections_are_present(self):
        r = FakeRunner()
        d = _daemon(runner=r)
        d.tick()
        self._prime_sqm_healthy(r)
        r.commands.clear()

        d.force_reload()

        joined = "\n".join(r.commands)
        assert "uci set sqm.dial0=queue" not in joined
        assert "uci set cake-autorate.dial0=cake_autorate" not in joined
        assert "/etc/init.d/sqm restart" in r.commands
        assert "/etc/init.d/cake-autorate restart" not in r.commands
        assert "/etc/init.d/cake-autorate stop 2>/dev/null || true" in r.commands
        assert "/etc/init.d/cake-autorate start" in r.commands

    def test_sqm_disabled_globally_skips_provision(self):
        r = FakeRunner(multidial=MULTIDIAL_SHOW.replace(
            "multidial.globals.enabled='1'",
            "multidial.globals.enabled='1'\nmultidial.globals.sqm='0'"))
        d = _daemon(runner=r)
        d.tick()
        assert not any("uci set sqm.dial" in c for c in r.commands)
        assert "/etc/init.d/cake-autorate start" not in r.commands
        assert d.control.get("dial0").sqm_provisioned is False

    def test_decommission_deprovisions_sqm(self):
        r = FakeRunner()
        d = _daemon(runner=r)
        d.tick()
        r.commands.clear()
        d.decommission_dial("dial1")
        joined = "\n".join(r.commands)
        assert "uci -q delete sqm.dial1" in joined
        assert "uci -q delete cake-autorate.dial1" in joined
        assert "ip link del ifb4dial1 2>/dev/null || true" in r.commands

    def test_tick_heals_dead_cake_autorate_loop(self):
        # A provisioned dial's cake-autorate loop can silently die at a restart
        # (its instance lost the verify_ifs_up iface race and self-terminated);
        # the launcher never respawns a dead loop, so the dial's CAKE shaper
        # freezes at its base rate with no latency floating. With NO UCI drift
        # (sections all present), the daemon must still detect the missing loop
        # from `ps` — seen because the SIBLING's loop is running but this one is
        # not — and bounce cake-autorate to respawn it.
        r = FakeRunner()
        d = _daemon(runner=r)
        d.tick()                    # provisions dial0 + dial1
        self._prime_sqm_healthy(r)  # UCI sections present -> no drift
        # dial0's loop runs; dial1's is dead (absent from ps)
        r.responses[sqm.STATUS_PS_CMD] = (
            0, " 1 root bash /root/cake-autorate/cake-autorate.sh /x/config.dial0.sh\n")
        r.commands.clear()
        d.tick()
        joined = "\n".join(r.commands)
        # no UCI re-provision (the sections are fine)...
        assert "uci set sqm.dial1=queue" not in joined
        assert "uci set cake-autorate.dial1=cake_autorate" not in joined
        # ...but cake-autorate is bounced so the dead loop respawns
        assert "/etc/init.d/cake-autorate stop 2>/dev/null || true" in r.commands
        assert "/etc/init.d/cake-autorate start" in r.commands

    def test_tick_does_not_heal_when_all_loops_running(self):
        # Steady state: every provisioned dial's loop is visible in `ps` -> no
        # heal, no service churn.
        r = FakeRunner()
        d = _daemon(runner=r)
        d.tick()
        self._prime_sqm_healthy(r)
        r.responses[sqm.STATUS_PS_CMD] = (
            0, " 1 root .../config.dial0.sh\n 2 root .../config.dial1.sh\n")
        r.commands.clear()
        d.tick()
        assert "/etc/init.d/cake-autorate start" not in r.commands

    def test_tick_does_not_heal_when_ps_empty(self):
        # An empty `ps` means "no loop visible yet" (cold start) or "ps
        # unavailable" — NOT a confirmed partial failure. Healing here would
        # thrash a cold start, so the liveness heal fires ONLY when some sibling
        # loop is visibly running.
        r = FakeRunner()
        d = _daemon(runner=r)
        d.tick()
        self._prime_sqm_healthy(r)
        r.responses[sqm.STATUS_PS_CMD] = (0, "")
        r.commands.clear()
        d.tick()
        assert "/etc/init.d/cake-autorate start" not in r.commands

    def test_sqm_heal_is_throttled(self):
        # If a dial genuinely can't come up, verify_ifs_up keeps failing; the
        # heal must not bounce the healthy siblings every tick. A cooldown gates
        # repeated restarts.
        r = FakeRunner()
        clk = StepClock()
        d = _daemon(runner=r, clock=clk)
        d.tick()
        self._prime_sqm_healthy(r)
        r.responses[sqm.STATUS_PS_CMD] = (
            0, " 1 root .../config.dial0.sh\n")  # dial1 loop missing, persistently
        d.tick()                       # first detection -> one heal
        r.commands.clear()
        clk.advance(5)
        d.tick()                       # inside cooldown -> MUST NOT bounce again
        assert "/etc/init.d/cake-autorate start" not in r.commands
        clk.advance(sqm.SQM_HEAL_COOLDOWN)
        d.tick()                       # cooldown elapsed -> heal again
        assert "/etc/init.d/cake-autorate start" in r.commands

    def test_sqm_status_folds_live_tc_and_ps(self):
        tc = ("qdisc cake 1: dev dial0 root bandwidth 40Mbit besteffort\n"
              " Sent 1 bytes 1 pkt (dropped 2, overlimits 0 requeues 0)\n"
              " backlog 0b 0p requeues 0\n"
              "qdisc cake 2: dev ifb4dial0 root bandwidth 90Mbit besteffort\n"
              " Sent 1 bytes 1 pkt (dropped 0, overlimits 0 requeues 0)\n"
              " backlog 0b 0p requeues 0\n")
        ps = " 1 root bash /root/cake-autorate/cake-autorate.sh /x/config.dial0.sh\n"
        r = FakeRunner()
        r.responses[sqm.STATUS_TC_CMD] = (0, tc)
        r.responses[sqm.STATUS_PS_CMD] = (0, ps)
        r.responses[sqm.STATUS_HNAT_CMD] = (0, "disabled")
        d = _daemon(runner=r)
        d.tick()
        st = d.sqm_status()
        assert st["enabled"] is True
        assert st["hnat_offload"] is False
        d0 = next(x for x in st["dials"] if x["device"] == "dial0")
        assert d0["ul_kbit"] == 40000 and d0["dl_kbit"] == 90000
        assert d0["autorate"] is True and d0["cake"] is True

    def test_sqm_status_warns_when_hnat_on(self):
        r = FakeRunner()
        r.responses[sqm.STATUS_HNAT_CMD] = (0, "enabled")
        d = _daemon(runner=r)
        d.tick()
        assert d.sqm_status()["hnat_offload"] is True

    def test_sqm_status_reports_nss_conditions_against_the_configured_wan(self):
        r = FakeRunner()
        r.responses[sqm.STATUS_TC_CMD] = (
            0, "qdisc nsstbl 1: dev eth1 root refcnt 2 accel_mode 0\n")
        r.responses[sqm.STATUS_OFFLOAD_CMD] = (
            0, "ecm=1\n16: dial0@eth1: <UP> mtu 1500 \\    macvlan mode private bcqueuelen 1000\n")
        d = _daemon(runner=r)
        d.tick()
        st = d.sqm_status()
        d0 = next(x for x in st["dials"] if x["device"] == "dial0")
        assert d0["macvlan_mode"] == "private"
        assert len(st["offload_warnings"]) == 2

    def test_sqm_not_provisioned_when_master_switch_off(self):
        # master switch OFF: must NOT disable HNAT or start shapers (dormant)
        r = FakeRunner(multidial=MULTIDIAL_SHOW.replace(
            "multidial.globals.enabled='1'", "multidial.globals.enabled='0'"))
        d = _daemon(runner=r)
        d.tick()
        assert not any("uci set sqm.dial" in c for c in r.commands)
        assert d.control.get("dial0").sqm_provisioned is False

    def test_daemon_never_toggles_hnat(self):
        # HNAT is a persistent system setting (turboacc); the daemon only READS
        # it (sqm_status), never WRITES hook_toggle — not on provision, reconcile,
        # or status. (The status read `cat ... hook_toggle` is allowed.)
        r = FakeRunner()
        d = _daemon(runner=r)
        d.tick()
        d.sqm_status()
        assert not any(f"> {sqm.HNAT_TOGGLE}" in c for c in r.commands)


class TestKeepalive:
    def test_marks_all_dials_online(self):
        d = _daemon(transport=FakeTransport(get_seq=[(204, ""), (204, "")]))
        d.tick()
        snap = d.snapshot()
        assert snap["aggregate"]["total"] == 2
        assert snap["aggregate"]["online"] == 2
        assert all(dl["online"] for dl in snap["dials"])

    def test_authenticates_captive_dial(self):
        # dial0: captive then 204 (after auth); dial1: already 204
        t = FakeTransport(get_seq=[(200, CAPTIVE_BODY), (204, ""), (204, "")])
        d = _daemon(transport=t)
        d.tick()
        snap = d.snapshot()
        assert snap["aggregate"]["online"] == 2
        d0 = [x for x in snap["dials"] if x["device"] == "dial0"][0]
        assert d0["authed"] is True
        assert any(c[0] == "POST" for c in t.calls)

    def test_disabled_globals_skips_keepalive(self):
        r = FakeRunner(multidial=MULTIDIAL_SHOW.replace(
            "multidial.globals.enabled='1'", "multidial.globals.enabled='0'"))
        t = FakeTransport()
        d = _daemon(runner=r, transport=t)
        d.tick()
        # no auth/probe traffic when globally disabled
        assert t.calls == []
        assert d.snapshot()["enabled"] is False


class TestSnapshot:
    def test_snapshot_exposes_accounts_and_only_userid(self):
        d = _daemon()
        d.tick()
        snap = d.snapshot()
        assert snap["accounts"][0]["userid"] == "demo_user_a"
        assert snap["accounts"][0]["dials"] == 2
        # never leak a userName field
        assert "userName" not in snap["accounts"][0]
        for dl in snap["dials"]:
            assert "userName" not in dl
            assert dl["account_userid"] == "demo_user_a"

    def test_snapshot_counts_reauths(self):
        t = FakeTransport(get_seq=[(200, CAPTIVE_BODY), (204, ""), (204, "")])
        d = _daemon(transport=t)
        d.tick()
        assert d.snapshot()["reauth_count"] >= 1

    def test_snapshot_exposes_routing_policy_for_dashboard(self):
        d = _daemon()
        d.tick()
        pol = d.snapshot()["policy"]
        assert pol["intranet_sticky"] is True
        assert pol["intranet_ranges"] == ["10.0.0.0/8", "172.16.0.0/12"]
        assert pol["sticky_ranges"] == list(TEST_STICKY_RANGES)
        assert pol["sticky_timeout"] == 600
        assert "example.test" in pol["dns_rebind_domains"]


class TestStopDial:
    def test_stop_excludes_from_online_and_routes_around(self):
        r = FakeRunner()
        d = _daemon(runner=r)
        d.tick()
        assert d.snapshot()["aggregate"]["online"] == 2
        d.stop_dial("dial1")
        snap = d.snapshot()
        # online keepalive count drops by one
        assert snap["aggregate"]["online"] == 1
        dial1 = [x for x in snap["dials"] if x["device"] == "dial1"][0]
        assert dial1["stopped"] is True
        assert dial1["online"] is False
        # mwan3 told to route around it
        assert "mwan3 interface dial1 down" in r.commands

    def test_stopped_dial_is_not_probed(self):
        t = FakeTransport()
        d = _daemon(transport=t)
        d.tick()
        d.control.set_stopped("dial1", True)
        t.calls.clear()
        d.tick()
        probed = {c[2] for c in t.calls}
        assert "dial1" not in probed
        assert "dial0" in probed

    def test_start_dial_restores_routing(self):
        r = FakeRunner()
        d = _daemon(runner=r)
        d.tick()
        d.stop_dial("dial1")
        d.start_dial("dial1")
        assert d.control.is_stopped("dial1") is False
        assert "mwan3 interface dial1 up" in r.commands

    def test_stop_reasserts_after_mwan3_restart(self):
        # mwan3 restart re-enables interfaces; keepalive must re-down a stopped dial
        r = FakeRunner()
        d = _daemon(runner=r)
        d.tick()
        d.stop_dial("dial1")
        d._enforced_stopped.clear()  # simulate state lost by a restart
        r.commands.clear()
        d.tick()
        assert "mwan3 interface dial1 down" in r.commands


class TestDialNameGuard:
    # The device name comes straight from the URL path and is spliced into
    # `sh -c` commands, so only registered dialN names may reach the runner.
    @pytest.mark.parametrize("op", ["stop_dial", "start_dial", "mark_dial"])
    @pytest.mark.parametrize("device", ["dial9", "x;reboot", "dial0;reboot", ""])
    def test_actions_reject_unregistered_or_malformed(self, op, device):
        r = FakeRunner()
        d = _daemon(runner=r)
        d.tick()
        r.commands.clear()
        args = (device, "note") if op == "mark_dial" else (device,)
        with pytest.raises(ValueError):
            getattr(d, op)(*args)
        assert r.commands == []
        # no junk control row persisted for the rejected name
        assert device not in d.control._controls

    def test_decommission_rejects_name_that_parses_to_a_real_slot(self):
        # "ab;x1"[4:] == "1": without the name check this would tear down the
        # real dial1 and run `ifdown ab;x1` through the shell.
        r = FakeRunner()
        d = _daemon(runner=r)
        d.tick()
        r.commands.clear()
        with pytest.raises(ValueError):
            d.decommission_dial("ab;x1")
        assert r.commands == []
        assert "dial1" in d.control.registered()


class TestPruneStaleDials:
    def test_shrinking_dials_prunes_ghost_from_snapshot(self):
        # config has 2 dials; a dial2 left over from a previous larger config
        # must NOT linger in the snapshot (web UI would keep showing it)
        d = _daemon()
        d.tick()
        with d._lock:
            d._dials["dial2"] = {"device": "dial2", "slot": 2, "online": True,
                                 "stopped": False, "account_userid": "x", "mark": ""}
        d._prev_online["dial2"] = True
        d.tick()  # plan is still 2 dials -> dial2 pruned
        devices = {x["device"] for x in d.snapshot()["dials"]}
        assert "dial2" not in devices
        assert devices == {"dial0", "dial1"}


class TestMark:
    def test_mark_sets_note_without_stopping(self):
        d = _daemon()
        d.tick()
        d.mark_dial("dial0", "MAC blocked / cooling")
        dial0 = [x for x in d.snapshot()["dials"] if x["device"] == "dial0"][0]
        assert dial0["mark"] == "MAC blocked / cooling"
        assert dial0["stopped"] is False


class TestHistory:
    def test_records_disconnect_transition(self):
        # tick1: both online (baseline); tick2: dial0 goes net-down -> 'down' event
        t = FakeTransport(get_seq=[(204, ""), (204, ""), (0, ""), (204, "")])
        d = _daemon(transport=t)
        d.tick()
        d.tick()
        downs = [e for e in d.history_events()
                 if e["kind"] == "down" and e["device"] == "dial0"]
        assert len(downs) == 1

    def test_no_event_on_first_sight(self):
        d = _daemon()
        d.tick()
        assert d.history_events() == []


class TestAccountCRUD:
    def test_add_account_writes_uci(self):
        r = FakeRunner()
        d = _daemon(runner=r)
        d.tick()
        d.add_or_update_account("acct_b", "30001", "pw", 1, True)
        joined = "\n".join(r.commands)
        assert "uci set multidial.acct_b=account" in joined
        assert "uci set multidial.acct_b.userid='30001'" in joined
        assert "uci set multidial.acct_b.dials='1'" in joined
        assert "uci commit multidial" in joined

    def test_add_rejects_bad_name(self):
        d = _daemon()
        with pytest.raises(ValueError):
            d.add_or_update_account("bad name!", "30001", "pw", 1)

    def test_add_rejects_empty_userid(self):
        d = _daemon()
        with pytest.raises(ValueError):
            d.add_or_update_account("acct_z", "", "pw", 1)

    def test_update_without_password_keeps_secret(self):
        r = FakeRunner()
        d = _daemon(runner=r)
        d.add_or_update_account("acct_a", "demo_user_a", "", 3, True)
        assert "multidial.acct_a.password" not in "\n".join(r.commands)

    def test_password_never_logged(self):
        logs = []
        d = Daemon(runner=FakeRunner(), transport=FakeTransport(), clock=FakeClock(),
                   sleep=lambda _s: None, logger=lambda m: logs.append(m))
        d.add_or_update_account("acct_x", "999", "TOPSECRET", 1, True)
        assert all("TOPSECRET" not in m for m in logs)

    def test_password_single_quote_escaped(self):
        r = FakeRunner()
        d = _daemon(runner=r)
        d.add_or_update_account("acct_q", "111", "a'b", 1)
        cmd = [c for c in r.commands if "acct_q.password" in c][0]
        assert cmd == "uci set multidial.acct_q.password='a'\\''b'"

    def test_delete_account(self):
        r = FakeRunner()
        d = _daemon(runner=r)
        d.delete_account("acct_a")
        assert "uci -q delete multidial.acct_a" in r.commands


class TestPower:
    def test_power_off_disables_and_stops_mwan3(self):
        r = FakeRunner()
        d = _daemon(runner=r)
        d.tick()
        d.set_power(False)
        joined = "\n".join(r.commands)
        assert "uci set multidial.globals.enabled='0'" in joined
        assert "/etc/init.d/mwan3 stop" in joined

    def test_power_on_enables_and_starts_mwan3(self):
        r = FakeRunner()
        d = _daemon(runner=r)
        d.set_power(True)
        joined = "\n".join(r.commands)
        assert "uci set multidial.globals.enabled='1'" in joined
        assert "/etc/init.d/mwan3 start" in joined


class TestAsyncApply:
    """Account edits must not run the multi-second reconcile inline: that blocked
    the HTTP response until the browser gave up (BrokenPipe) and the UI showed a
    bogus failure — the "改账号没反应/没用" symptom."""

    def test_account_update_defers_reconcile_off_request_thread(self):
        r = FakeRunner()
        d = _daemon(runner=r)
        d.tick()
        r.commands.clear()
        d.add_or_update_account("acct_a", "demo_user_a", "", 3, True)  # raise to 3
        cmds = "\n".join(r.commands)
        # UCI persisted synchronously...
        assert "uci set multidial.acct_a.dials='3'" in cmds
        # ...but the heavy apply (reload/ifup/mwan3) is deferred to the daemon loop
        assert "ubus call network reload" not in r.commands
        assert not any(c.startswith("ifup dial") for c in r.commands)
        assert d._apply_event.is_set()
        assert d._last_config_text is None  # next tick re-reconciles

    def test_deferred_apply_applies_on_next_tick(self):
        r = FakeRunner()
        d = _daemon(runner=r)
        d.tick()
        d.add_or_update_account("acct_a", "demo_user_a", "", 3, True)
        r2 = FakeRunner(multidial=MULTIDIAL_SHOW.replace("dials='2'", "dials='3'"),
                        network="network.dial0=interface\nnetwork.dial1=interface\n"
                                "network.md_dev0=device\nnetwork.md_dev1=device\n")
        d.runner = r2
        d.tick()  # the daemon loop's next tick applies the pending change
        assert "ifup dial2" in r2.commands

    def test_request_apply_wakes_loop_and_invalidates_cache(self):
        d = _daemon()
        d.tick()
        d._apply_event.clear()
        d.request_apply()
        assert d._apply_event.is_set()
        assert d._last_config_text is None
        assert d._force_sqm_apply is True

    def test_power_toggle_defers_reconcile(self):
        r = FakeRunner()
        d = _daemon(runner=r)
        d.tick()
        r.commands.clear()
        d.set_power(False)
        # the master-switch UCI + mwan3 stop run inline (fast, deterministic)...
        assert "uci set multidial.globals.enabled='0'" in "\n".join(r.commands)
        assert "/etc/init.d/mwan3 stop" in r.commands
        # ...the reconcile is deferred
        assert d._apply_event.is_set()


class TestPerDialStats:
    """Per-dial reauth/down counters + attributable logging: the global 47 could
    not tell you WHICH account keeps dropping, and the log recorded too little."""

    def test_per_dial_reauth_counter_and_attributable_log(self):
        from multidial.state import LogBuffer
        lb = LogBuffer(path=None)
        t = FakeTransport(get_seq=[(200, CAPTIVE_BODY), (204, ""), (204, "")])
        d = Daemon(runner=FakeRunner(), transport=t, clock=FakeClock(),
                   sleep=lambda _s: None, log_buffer=lb)
        d.tick()
        snap = d.snapshot()
        d0 = [x for x in snap["dials"] if x["device"] == "dial0"][0]
        d1 = [x for x in snap["dials"] if x["device"] == "dial1"][0]
        assert d0["reauth"] == 1
        assert d1["reauth"] == 0
        # the reauth is logged WITH the account userid -> the drop is attributable
        assert any("dial0" in ln and "demo_user_a" in ln for ln in d.log_lines())

    def test_per_dial_down_counter(self):
        t = FakeTransport(get_seq=[(204, ""), (204, ""), (0, ""), (204, "")])
        d = _daemon(transport=t)
        d.tick()  # baseline online
        d.tick()  # dial0 net_down -> down transition
        d0 = [x for x in d.snapshot()["dials"] if x["device"] == "dial0"][0]
        assert d0["down"] == 1

    def test_account_rolls_up_per_dial_stats(self):
        t = FakeTransport(get_seq=[(200, CAPTIVE_BODY), (204, ""), (204, "")])
        d = _daemon(transport=t)
        d.tick()
        acct = d.snapshot()["accounts"][0]
        assert acct["reauth"] >= 1

    def test_stats_pruned_when_dial_decommissioned(self):
        t = FakeTransport(get_seq=[(200, CAPTIVE_BODY), (204, ""), (204, "")])
        d = _daemon(transport=t)
        d.tick()
        assert d._dial_stats.get("dial0", {}).get("reauth", 0) >= 1
        d.decommission_dial("dial0")
        assert "dial0" not in d._dial_stats


class TestLogWiring:
    def test_logs_reach_buffer(self):
        from multidial.state import LogBuffer
        lb = LogBuffer(path=None)
        d = Daemon(runner=FakeRunner(), transport=FakeTransport(), clock=FakeClock(),
                   sleep=lambda _s: None, log_buffer=lb)
        d.log("hello world")
        assert any("hello world" in x for x in d.log_lines())


_STICKY_FIX = pathlib.Path(__file__).parent / "fixtures" / "sticky"


def _sfx(name):
    return (_STICKY_FIX / name).read_text()


def _sticky_daemon(dnsmasq=None):
    """Daemon after one tick, with the live-router reads answered by the real fixtures."""
    r = FakeRunner()
    d = _daemon(runner=r)
    d.tick()
    r.commands.clear()
    r.responses["uci show mwan3"] = (0, _sfx("uci_mwan3.txt"))
    r.responses[sticky.MANGLE_CMD] = (0, _sfx("mangle_broken.txt"))
    for rule in ("md_intra0", "md_intra1", "md_svc"):
        r.responses[sticky.ipset_cmd(rule)] = (0, _sfx(f"ipset_{rule}.txt"))
    r.responses[sticky.DNSMASQ_CMD] = (0, dnsmasq or _sfx("dnsmasq_version.txt"))
    return d, r


def _writes(runner):
    """The config-mutating commands a runner saw (the status reads are noise here)."""
    return [c for c in runner.commands
            if c.startswith(("uci set ", "uci add_list ", "uci -q delete ", "uci commit "))]


class TestTrafficApi:
    def test_reads_sysfs_counters_of_registered_dials(self):
        r = FakeRunner()
        d = _daemon(runner=r)
        d.tick()
        r.responses[sticky.traffic_cmd(["dial0", "dial1"])] = (
            0, "dial0 11 22\ndial1 33 44\nstray 1 2\n")
        out = d.traffic()
        assert isinstance(out["ts"], float)
        assert out["dials"] == {
            "dial0": {"down_bytes": 11, "up_bytes": 22},
            "dial1": {"down_bytes": 33, "up_bytes": 44},
        }

    def test_route_serves_it(self):
        r = FakeRunner()
        d = _daemon(runner=r)
        d.tick()
        r.responses[sticky.traffic_cmd(["dial0", "dial1"])] = (0, "dial0 1 2\n")
        status, _, body = route(d, "GET", "/api/traffic")
        assert status == 200
        assert json.loads(body)["dials"] == {"dial0": {"down_bytes": 1, "up_bytes": 2}}


class TestSqmLimitsApi:
    def test_sqm_status_includes_cake_autorate_limits(self):
        r = FakeRunner()
        d = _daemon(runner=r)
        d.tick()
        r.responses["uci -q show cake-autorate"] = (
            0, "cake-autorate.dial0=cake_autorate\n"
               "cake-autorate.dial0.max_dl_shaper_rate_kbps='95000'\n")
        lim = d.sqm_status()["limits"]
        assert set(lim) == {"dial0", "dial1"}
        assert lim["dial0"]["dl_max"] == 95000
        assert lim["dial0"]["ul_max"] == sqm.DEFAULT_MAX_KBPS
        assert lim["dial1"]["dl_min"] == sqm.DEFAULT_MIN_KBPS


class TestStickyApi:
    def test_get_reports_pins_health_and_capability(self):
        d, _ = _sticky_daemon()
        status, _, body = route(d, "GET", "/api/sticky")
        assert status == 200
        st = json.loads(body)
        assert st["timeout"] == 600
        assert st["intranet"]["ranges"] == ["10.0.0.0/8", "172.16.0.0/12"]
        assert st["service"]["ranges"] == list(TEST_STICKY_RANGES)
        assert st["domains_supported"] is False
        assert {"rule": "md_svc", "client": "192.0.2.139", "device": "dial3",
                "ttl": 257} in st["pins"]
        assert st["health"]["md_svc"]["ok"] is False
        assert st["health"]["md_svc"]["online"] == ["dial0", "dial1", "dial2", "dial3", "dial6"]

    def test_get_tolerates_missing_mwan3(self):
        r = FakeRunner()
        d = _daemon(runner=r)
        d.tick()
        st = d.sticky_status()
        assert st["pins"] == [] and st["health"] == {}

    def test_post_valid_writes_uci_then_defers_reconcile(self):
        d, r = _sticky_daemon()
        status, _, body = route(d, "POST", "/api/sticky", json.dumps({
            "sticky_ranges": ["10.90.0.0/16", "1.2.3.4"],
            "intranet_ranges": ["10.0.0.0/8"],
            "sticky_timeout": 1800,
        }))
        assert status == 200
        assert _writes(r) == [
            "uci set multidial.globals.intranet_ranges='10.0.0.0/8'",
            "uci set multidial.globals.sticky_ranges='10.90.0.0/16 1.2.3.4/32'",
            "uci set multidial.globals.sticky_timeout='1800'",
            "uci commit multidial",
        ]
        # reconcile (=> one mwan3 restart on rule change) is left to the daemon loop
        assert "/etc/init.d/mwan3 restart" not in r.commands
        assert d._apply_event.is_set() and d._last_config_text is None
        # a routing edit must not restart every dial's shaper (sqm + cake-autorate)
        assert d._force_sqm_apply is False
        st = json.loads(body)
        assert st["timeout"] == 1800
        assert st["service"]["ranges"] == ["10.90.0.0/16", "1.2.3.4/32"]
        assert st["intranet"]["ranges"] == ["10.0.0.0/8"]

    def test_post_partial_update_touches_only_given_keys(self):
        d, r = _sticky_daemon()
        route(d, "POST", "/api/sticky", json.dumps({"sticky_timeout": 300}))
        assert _writes(r) == ["uci set multidial.globals.sticky_timeout='300'",
                              "uci commit multidial"]

    def test_post_empty_range_list_clears_it(self):
        d, r = _sticky_daemon()
        status, _, body = route(d, "POST", "/api/sticky", json.dumps({"sticky_ranges": []}))
        assert status == 200
        assert "uci set multidial.globals.sticky_ranges=''" in r.commands
        assert json.loads(body)["service"]["ranges"] == []

    @pytest.mark.parametrize("payload", [
        {"intranet_ranges": ["10.0.0.0/33"]},
        {"sticky_ranges": ["not-a-cidr"]},
        {"sticky_timeout": 42},
        {"sticky_timeout": "soon"},
        {},
    ])
    def test_post_invalid_is_400_and_writes_nothing(self, payload):
        d, r = _sticky_daemon()
        status, _, body = route(d, "POST", "/api/sticky", json.dumps(payload))
        assert status == 400
        assert "error" in json.loads(body)
        assert _writes(r) == []
        assert not d._apply_event.is_set()

    def test_post_domains_rejected_when_dnsmasq_has_no_ipset(self):
        d, r = _sticky_daemon()
        status, _, body = route(d, "POST", "/api/sticky",
                                json.dumps({"sticky_domains": ["login.example.com"]}))
        assert status == 400
        assert json.loads(body)["error"] == "dnsmasq 未编译 ipset，暂不支持域名"
        assert _writes(r) == []

    def test_post_domains_written_as_uci_list_when_supported(self):
        d, r = _sticky_daemon(dnsmasq="Compile time options: IPv6 DHCP ipset nftset\n")
        status, _, body = route(d, "POST", "/api/sticky",
                                json.dumps({"sticky_domains": ["A.example.com", "b.example.org"]}))
        assert status == 200
        assert _writes(r) == [
            "uci -q delete multidial.globals.sticky_domains",
            "uci add_list multidial.globals.sticky_domains='a.example.com'",
            "uci add_list multidial.globals.sticky_domains='b.example.org'",
            "uci commit multidial",
        ]
        assert json.loads(body)["service"]["domains"] == ["a.example.com", "b.example.org"]
        assert json.loads(body)["domains_supported"] is True


class TestSingleDialReauth:
    def test_reauths_only_the_requested_dial(self):
        t = FakeTransport(get_seq=[(200, CAPTIVE_BODY), (204, "")])
        d = _daemon(transport=t)
        d.tick()
        t.calls.clear()
        t.get_seq = [(200, CAPTIVE_BODY), (204, "")]
        status, _, body = route(d, "POST", "/api/dial/dial1/reauth")
        assert status == 200
        assert {c[2] for c in t.calls} == {"dial1"}
        assert any(c[0] == "POST" for c in t.calls)
        snap = json.loads(body)
        d1 = next(x for x in snap["dials"] if x["device"] == "dial1")
        assert d1["authed"] is True and d1["online"] is True

    @pytest.mark.parametrize("device", ["dial9", "x;reboot", "dial0;reboot"])
    def test_unknown_dial_is_400_and_runs_nothing(self, device):
        r = FakeRunner()
        t = FakeTransport()
        d = _daemon(runner=r, transport=t)
        d.tick()
        r.commands.clear()
        t.calls.clear()
        status, _, body = route(d, "POST", f"/api/dial/{device}/reauth")
        assert status == 400
        assert "unknown dial" in json.loads(body)["error"]
        assert r.commands == [] and t.calls == []

    def test_stopped_dial_stays_unprobed(self):
        t = FakeTransport()
        d = _daemon(transport=t)
        d.tick()
        d.control.set_stopped("dial1", True)
        t.calls.clear()
        d.reauth_dial("dial1")
        assert t.calls == []
