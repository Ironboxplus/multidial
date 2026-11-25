"""TDD: per-dial ePortal auth, bound to the dial's DEVICE (never a source IP).

The real transport uses SO_BINDTODEVICE so egress is pinned to the macvlan
device name — stable across DHCP renewals / auth expiry. Tests inject a fake
transport, so the auth state machine is verified without root or a network.
"""
import pytest
import socket

from multidial.config import Globals, DialSpec
from multidial.dialer import (
    Dialer,
    DeviceBoundTransport,
    Probe,
    AuthOutcome,
    NetStatus,
    is_captive_body,
    parse_captive,
    classify_auth_result,
    json_val,
)


CAPTIVE_BODY = (
    "<html><script>top.self.location.href="
    "'http://192.0.2.80/eportal/index.jsp?"
    "wlanuserip=192.0.2.20&wlanacname=ac01&nasip=192.0.2.53';"
    "</script></html>"
)

SUCCESS_JSON = '{"result":"success","message":"认证成功","userIndex":"abc123"}'
ALREADY_JSON = '{"result":"fail","message":"用户已在线","userIndex":""}'
WRONGPW_JSON = '{"result":"fail","message":"密码错误","userIndex":""}'


def _spec(slot=0, userid="demo_user_a", pw="s3cr3t"):
    return DialSpec(
        account_userid=userid, account_name="acct_a", password=pw,
        index=0, slot=slot, device=f"dial{slot}",
        macaddr=f"02:00:00:00:00:{16 + slot:02x}",
        table=100 + slot, pref=1000 + slot,
    )


class FakeTransport:
    """Records (method, url, device) calls; returns programmed responses."""

    def __init__(self):
        self.calls = []
        self.get_responses = []   # list of (code, body)
        self.post_response = (200, SUCCESS_JSON)

    def get(self, url, device, timeout=6, fwmark=0):
        self.calls.append(("GET", url, device, fwmark))
        if self.get_responses:
            return self.get_responses.pop(0)
        return (204, "")

    def post(self, url, fields, device, timeout=10, fwmark=0):
        self.calls.append(("POST", url, device, fields, fwmark))
        return self.post_response


# --- pure helpers -------------------------------------------------------
class TestPureHelpers:
    def test_is_captive_body_detects_eportal(self):
        assert is_captive_body(CAPTIVE_BODY) is True

    def test_is_captive_body_rejects_genuine(self):
        assert is_captive_body("<html><body>hello world</body></html>") is False

    def test_parse_captive_extracts_base_and_qs(self):
        p = parse_captive(CAPTIVE_BODY, "http://probe", 200)
        assert p.status == NetStatus.CAPTIVE
        assert p.portal_base == "http://192.0.2.80"
        assert "wlanuserip=192.0.2.20" in p.query_string
        assert "nasip=192.0.2.53" in p.query_string

    def test_json_val(self):
        assert json_val(SUCCESS_JSON, "result") == "success"
        assert json_val(SUCCESS_JSON, "userIndex") == "abc123"

    def test_classify_success(self):
        assert classify_auth_result("success", "ok") == AuthOutcome.SUCCESS

    def test_classify_already_online(self):
        assert classify_auth_result("fail", "用户已在线") == AuthOutcome.ALREADY_ONLINE

    def test_classify_wrong_password(self):
        assert classify_auth_result("fail", "密码错误") == AuthOutcome.FAIL_CRED

    def test_classify_unknown(self):
        assert classify_auth_result("fail", "weird error") == AuthOutcome.FAIL_UNKNOWN


# --- probe --------------------------------------------------------------
class TestProbe:
    def test_probe_204_is_online(self):
        t = FakeTransport()
        t.get_responses = [(204, "")]
        d = Dialer(_spec(), Globals(), transport=t)
        p = d.probe()
        assert p.status == NetStatus.ONLINE

    def test_probe_binds_to_device(self):
        t = FakeTransport()
        t.get_responses = [(204, "")]
        d = Dialer(_spec(slot=2), Globals(), transport=t)
        d.probe()
        # the GET must be bound to THIS dial's device name, not an IP
        assert t.calls[0][0] == "GET"
        assert t.calls[0][2] == "dial2"

    def test_probe_stamps_dial_mwan3_fwmark(self):
        # Each dial's probe must carry its mwan3 fwmark (slot+1)*0x100 so it
        # routes via the dial's own table even when mwan3 has marked the dial
        # offline; without this the daemon can never re-auth an offline dial
        # (the unmarked packet is blackholed -> EHOSTUNREACH -> deadlock).
        for slot, mark in [(0, 0x100), (1, 0x200), (2, 0x300), (3, 0x400)]:
            t = FakeTransport()
            t.get_responses = [(204, "")]
            Dialer(_spec(slot=slot), Globals(), transport=t).probe()
            assert t.calls[0][3] == mark   # (GET, url, device, fwmark)

    def test_probe_captive_returns_portal(self):
        t = FakeTransport()
        t.get_responses = [(200, CAPTIVE_BODY)]
        d = Dialer(_spec(), Globals(), transport=t)
        p = d.probe()
        assert p.status == NetStatus.CAPTIVE
        assert p.portal_base == "http://192.0.2.80"

    def test_probe_no_response_includes_transport_error(self):
        t = FakeTransport()
        t.get_responses = [(0, "", "os error 101: Network unreachable")]
        d = Dialer(_spec(), Globals(), transport=t)
        p = d.probe()
        assert p.status == NetStatus.NET_DOWN
        assert p.detail == "no response: os error 101: Network unreachable"


class TestDeviceBoundTransportErrors:
    def test_get_returns_dns_error_detail(self):
        class BoomTransport(DeviceBoundTransport):
            def _request(self, *args, **kwargs):
                raise socket.gaierror(-2, "Name or service not known")

        code, body, detail = BoomTransport().get(
            "http://probe.example.test/check",
            device="dial0",
            fwmark=0x100,
        )
        assert code == 0
        assert body == ""
        assert detail.startswith("dns error:")


# --- authenticate -------------------------------------------------------
class TestAuthenticate:
    def test_auth_posts_account_credentials_and_binds_device(self):
        t = FakeTransport()
        t.post_response = (200, SUCCESS_JSON)
        spec = _spec(slot=1, userid="demo_user_a", pw="s3cr3t")
        d = Dialer(spec, Globals(), transport=t)
        probe = parse_captive(CAPTIVE_BODY, "http://probe", 200)
        outcome = d.authenticate(probe)
        assert outcome == AuthOutcome.SUCCESS
        method, url, device, fields, fwmark = t.calls[0]
        assert method == "POST"
        assert device == "dial1"                       # device-bound, not IP
        assert fwmark == 0x200                          # dial1 -> mwan3 mark (slot+1)*0x100
        assert "InterFace.do?method=login" in url
        assert fields["userId"] == "demo_user_a"          # THIS account's creds
        assert fields["password"] == "s3cr3t"
        assert fields["passwordEncrypt"] == "false"    # plaintext, no SCRAM
        assert fields["queryString"] == probe.query_string

    def test_auth_already_online_is_ok(self):
        t = FakeTransport()
        t.post_response = (200, ALREADY_JSON)
        d = Dialer(_spec(), Globals(), transport=t)
        probe = parse_captive(CAPTIVE_BODY, "http://probe", 200)
        assert d.authenticate(probe) == AuthOutcome.ALREADY_ONLINE

    def test_auth_wrong_password(self):
        t = FakeTransport()
        t.post_response = (200, WRONGPW_JSON)
        d = Dialer(_spec(), Globals(), transport=t)
        probe = parse_captive(CAPTIVE_BODY, "http://probe", 200)
        assert d.authenticate(probe) == AuthOutcome.FAIL_CRED

    def test_auth_without_portal_fails_portal(self):
        t = FakeTransport()
        d = Dialer(_spec(), Globals(), transport=t)
        bad = Probe(status=NetStatus.CAPTIVE, portal_base="", query_string="")
        assert d.authenticate(bad) == AuthOutcome.FAIL_PORTAL
        # must not attempt a POST with no portal
        assert t.calls == []

    def test_auth_network_failure_keeps_transport_error_for_detail(self):
        t = FakeTransport()
        t.post_response = (0, "", "timeout")
        d = Dialer(_spec(), Globals(), transport=t)
        probe = parse_captive(CAPTIVE_BODY, "http://probe", 200)
        assert d.authenticate(probe) == AuthOutcome.FAIL_NETWORK
        assert d._last_auth_error_detail == "no response: timeout"


# --- ensure_online (full state machine) ---------------------------------
class TestEnsureOnline:
    def test_already_online_no_auth(self):
        t = FakeTransport()
        t.get_responses = [(204, "")]
        d = Dialer(_spec(), Globals(), transport=t)
        res = d.ensure_online()
        assert res.online is True
        assert res.authed is False
        assert all(c[0] != "POST" for c in t.calls)

    def test_captive_then_auth_then_online(self):
        t = FakeTransport()
        # 1st probe: captive; auth POST: success; re-probe: 204
        t.get_responses = [(200, CAPTIVE_BODY), (204, "")]
        t.post_response = (200, SUCCESS_JSON)
        d = Dialer(_spec(), Globals(), transport=t)
        res = d.ensure_online()
        assert res.online is True
        assert res.authed is True
        assert res.outcome == AuthOutcome.SUCCESS
        # exactly one POST, bound to the device
        posts = [c for c in t.calls if c[0] == "POST"]
        assert len(posts) == 1
        assert posts[0][2] == "dial0"

    def test_captive_then_auth_fail_stays_offline(self):
        t = FakeTransport()
        t.get_responses = [(200, CAPTIVE_BODY)]
        t.post_response = (200, WRONGPW_JSON)
        d = Dialer(_spec(), Globals(), transport=t)
        res = d.ensure_online()
        assert res.online is False
        assert res.outcome == AuthOutcome.FAIL_CRED

    def test_captive_then_auth_network_fail_reports_reason(self):
        t = FakeTransport()
        t.get_responses = [(200, CAPTIVE_BODY)]
        t.post_response = (0, "", "timeout")
        d = Dialer(_spec(), Globals(), transport=t)
        res = d.ensure_online()
        assert res.online is False
        assert res.outcome == AuthOutcome.FAIL_NETWORK
        assert res.detail == "auth fail_network: no response: timeout"
