"""TDD: the HTTP status/control API. Routing is a pure function over a daemon,
so it's testable without binding a socket. The LuCI dashboard reaches these
endpoints through an rpcd ucode proxy (same-origin, reusing LuCI auth)."""
import json

import pytest

from multidial.api import route


class FakeDaemon:
    def __init__(self):
        self.reloaded = False
        self.reauthed = False
        self.calls = []  # records (op, *args) for control-action assertions

    def snapshot(self):
        return {
            "enabled": True,
            "aggregate": {"total": 2, "online": 2},
            "dials": [{"device": "dial0", "account_userid": "demo_user_a", "online": True,
                       "stopped": False, "mark": ""}],
            "accounts": [{"name": "acct_a", "userid": "demo_user_a", "dials": 2}],
        }

    def force_reload(self):
        self.reloaded = True
        return True

    def force_reauth(self):
        self.reauthed = True
        return self.snapshot()

    # control surface
    def set_power(self, enabled):
        self.calls.append(("power", enabled))
        return self.snapshot()

    def stop_dial(self, device):
        if device == "nope":
            raise ValueError("unknown dial: nope")
        self.calls.append(("stop", device))
        return self.snapshot()

    def start_dial(self, device):
        if device == "nope":
            raise ValueError("unknown dial: nope")
        self.calls.append(("start", device))
        return self.snapshot()

    def mark_dial(self, device, mark):
        if device == "nope":
            raise ValueError("unknown dial: nope")
        self.calls.append(("mark", device, mark))
        return self.snapshot()

    def rotate_mac(self, device):
        if device == "nope":
            raise ValueError("unknown dial: nope")
        self.calls.append(("rotate", device))
        return self.snapshot()

    def decommission_dial(self, device):
        if device == "nope":
            raise ValueError("invalid dial: nope")
        self.calls.append(("decommission", device))
        return self.snapshot()

    def add_or_update_account(self, name, userid, password, dials, enabled=True):
        if not name or " " in name:
            raise ValueError("invalid account name")
        self.calls.append(("account", name, userid, dials, enabled))
        return self.snapshot()

    def delete_account(self, name):
        self.calls.append(("delete", name))
        return self.snapshot()

    def log_lines(self, limit=200):
        self.calls.append(("log", limit))
        return ["[t] line one", "[t] line two"]

    def history_events(self, limit=50):
        self.calls.append(("history", limit))
        return [{"device": "dial0", "kind": "down", "ts": 1.0, "detail": "captive"}]

    def traffic(self):
        self.calls.append(("traffic",))
        return {"ts": 5.0, "dials": {"dial0": {"down_bytes": 10, "up_bytes": 20}}}

    def sticky_status(self):
        self.calls.append(("sticky",))
        return {"timeout": 600, "pins": [], "health": {}, "domains_supported": False}

    def set_sticky(self, data):
        if "sticky_timeout" in data and data["sticky_timeout"] == 7:
            raise ValueError("sticky_timeout must be one of 300, 600, 1800, 3600")
        self.calls.append(("set_sticky", data))
        return self.sticky_status()

    def reauth_dial(self, device):
        if device == "nope":
            raise ValueError("unknown dial: nope")
        self.calls.append(("reauth_dial", device))
        return self.snapshot()

    def sqm_status(self):
        self.calls.append(("sqm",))
        return {
            "enabled": True,
            "hnat_offload": False,
            "dials": [{"device": "dial0", "cake": True, "autorate": True,
                       "dl_kbit": 90000, "ul_kbit": 40000}],
            "aggregate": {"total": 1, "active": 1},
        }


class TestRoute:
    def test_status_returns_snapshot_json(self):
        d = FakeDaemon()
        status, ctype, body = route(d, "GET", "/api/status")
        assert status == 200
        assert "json" in ctype
        payload = json.loads(body)
        assert payload["aggregate"]["online"] == 2
        assert payload["dials"][0]["device"] == "dial0"

    def test_status_never_leaks_username(self):
        d = FakeDaemon()
        _, _, body = route(d, "GET", "/api/status")
        assert "userName" not in body

    def test_reload_triggers_force_reload(self):
        d = FakeDaemon()
        status, _, _ = route(d, "POST", "/api/reload")
        assert status == 200
        assert d.reloaded is True

    def test_reauth_triggers_force_reauth(self):
        d = FakeDaemon()
        status, _, body = route(d, "POST", "/api/reauth")
        assert status == 200
        assert d.reauthed is True
        assert json.loads(body)["aggregate"]["total"] == 2

    def test_root_serves_html(self):
        d = FakeDaemon()
        status, ctype, body = route(d, "GET", "/")
        assert status == 200
        assert "text/html" in ctype
        assert "<" in body

    def test_sqm_returns_status(self):
        d = FakeDaemon()
        status, ctype, body = route(d, "GET", "/api/sqm")
        assert status == 200
        assert "json" in ctype
        payload = json.loads(body)
        assert payload["aggregate"]["active"] == 1
        assert payload["dials"][0]["device"] == "dial0"
        assert payload["hnat_offload"] is False

    def test_sqm_rejects_non_get(self):
        d = FakeDaemon()
        status, _, _ = route(d, "POST", "/api/sqm")
        assert status == 405

    def test_unknown_path_404(self):
        d = FakeDaemon()
        status, _, body = route(d, "GET", "/nope")
        assert status == 404

    def test_wrong_method_405(self):
        d = FakeDaemon()
        status, _, _ = route(d, "GET", "/api/reauth")  # reauth is POST-only
        assert status == 405

    def test_query_string_stripped_from_path(self):
        d = FakeDaemon()
        status, _, _ = route(d, "GET", "/api/status?t=123")
        assert status == 200


class TestPower:
    def test_power_requires_enabled_field(self):
        d = FakeDaemon()
        status, _, _ = route(d, "POST", "/api/power", "{}")
        assert status == 400

    def test_power_toggles(self):
        d = FakeDaemon()
        status, _, _ = route(d, "POST", "/api/power", json.dumps({"enabled": False}))
        assert status == 200
        assert ("power", False) in d.calls


class TestDialActions:
    def test_stop_dial(self):
        d = FakeDaemon()
        status, _, _ = route(d, "POST", "/api/dial/dial1/stop")
        assert status == 200
        assert ("stop", "dial1") in d.calls

    def test_start_dial(self):
        d = FakeDaemon()
        status, _, _ = route(d, "POST", "/api/dial/dial1/start")
        assert status == 200
        assert ("start", "dial1") in d.calls

    def test_mark_dial(self):
        d = FakeDaemon()
        body = json.dumps({"mark": "blocked"})
        status, _, _ = route(d, "POST", "/api/dial/dial0/mark", body)
        assert status == 200
        assert ("mark", "dial0", "blocked") in d.calls

    def test_rotate_dial(self):
        d = FakeDaemon()
        status, _, _ = route(d, "POST", "/api/dial/dial1/rotate")
        assert status == 200
        assert ("rotate", "dial1") in d.calls

    @pytest.mark.parametrize("action", ["stop", "start", "mark"])
    def test_unknown_dial_action_400(self, action):
        d = FakeDaemon()
        status, _, body = route(d, "POST", f"/api/dial/nope/{action}", "{}")
        assert status == 400
        assert "unknown dial" in json.loads(body)["error"]

    def test_rotate_unknown_dial_400(self):
        d = FakeDaemon()
        status, _, _ = route(d, "POST", "/api/dial/nope/rotate")
        assert status == 400

    def test_decommission_dial(self):
        d = FakeDaemon()
        status, _, _ = route(d, "DELETE", "/api/dial/dial1")
        assert status == 200
        assert ("decommission", "dial1") in d.calls

    def test_decommission_invalid_dial_400(self):
        d = FakeDaemon()
        status, _, _ = route(d, "DELETE", "/api/dial/nope")
        assert status == 400

    def test_decommission_requires_delete_method(self):
        d = FakeDaemon()
        status, _, _ = route(d, "POST", "/api/dial/dial1")  # no action => DELETE-only
        assert status == 405

    def test_unknown_dial_action_404(self):
        d = FakeDaemon()
        status, _, _ = route(d, "POST", "/api/dial/dial0/frobnicate")
        assert status == 404

    def test_dial_action_get_not_allowed(self):
        d = FakeDaemon()
        status, _, _ = route(d, "GET", "/api/dial/dial0/stop")
        assert status == 405


class TestAccountEndpoints:
    def test_create_account(self):
        d = FakeDaemon()
        body = json.dumps({"name": "acct_b", "userid": "30001",
                           "password": "pw", "dials": 1, "enabled": True})
        status, _, _ = route(d, "POST", "/api/account", body)
        assert status == 200
        assert any(c[0] == "account" and c[1] == "acct_b" for c in d.calls)

    def test_create_account_bad_name_400(self):
        d = FakeDaemon()
        body = json.dumps({"name": "bad name", "userid": "30001", "dials": 1})
        status, _, _ = route(d, "POST", "/api/account", body)
        assert status == 400

    def test_create_account_invalid_json_400(self):
        d = FakeDaemon()
        status, _, _ = route(d, "POST", "/api/account", "{not json")
        assert status == 400

    def test_account_response_never_leaks_password(self):
        d = FakeDaemon()
        body = json.dumps({"name": "acct_b", "userid": "30001",
                           "password": "TOPSECRET", "dials": 1})
        _, _, resp = route(d, "POST", "/api/account", body)
        assert "TOPSECRET" not in resp

    def test_delete_account(self):
        d = FakeDaemon()
        status, _, _ = route(d, "DELETE", "/api/account/acct_a")
        assert status == 200
        assert ("delete", "acct_a") in d.calls


class TestLogAndHistory:
    def test_log_returns_lines(self):
        d = FakeDaemon()
        status, _, body = route(d, "GET", "/api/log?limit=50")
        assert status == 200
        assert "line one" in body
        assert ("log", 50) in d.calls

    def test_history_returns_events(self):
        d = FakeDaemon()
        status, _, body = route(d, "GET", "/api/history")
        assert status == 200
        assert json.loads(body)["events"][0]["device"] == "dial0"


class TestBrokenPipeResilience:
    """A mutating call commits server-side before replying; if the browser gave
    up (slow reconcile) the socket is gone and the write raises BrokenPipe. That
    must be swallowed, not dumped as a traceback over a completed operation."""

    def test_safe_reply_swallows_broken_pipe(self):
        from multidial.api import _Handler

        class _BrokenWfile:
            def write(self, _data):
                raise BrokenPipeError(32, "Broken pipe")

        class _Stub:
            def __init__(self):
                self.wfile = _BrokenWfile()
                self.responded = None

            def send_response(self, s):
                self.responded = s

            def send_header(self, *a):
                pass

            def end_headers(self):
                pass

        stub = _Stub()
        # must not raise even though the client socket is gone
        _Handler._safe_reply(stub, 200, "application/json", b"{}")
        assert stub.responded == 200


class TestTrafficAndSticky:
    def test_traffic_returns_counters(self):
        d = FakeDaemon()
        status, _, body = route(d, "GET", "/api/traffic")
        assert status == 200
        assert json.loads(body)["dials"]["dial0"]["up_bytes"] == 20
        assert ("traffic",) in d.calls

    def test_traffic_rejects_non_get(self):
        assert route(FakeDaemon(), "POST", "/api/traffic")[0] == 405

    def test_sticky_get(self):
        d = FakeDaemon()
        status, _, body = route(d, "GET", "/api/sticky")
        assert status == 200
        assert json.loads(body)["timeout"] == 600

    def test_sticky_post_passes_body_and_returns_status(self):
        d = FakeDaemon()
        status, _, body = route(d, "POST", "/api/sticky",
                                json.dumps({"sticky_timeout": 1800}))
        assert status == 200
        assert ("set_sticky", {"sticky_timeout": 1800}) in d.calls
        assert "pins" in json.loads(body)

    def test_sticky_post_validation_error_is_400(self):
        status, _, body = route(FakeDaemon(), "POST", "/api/sticky",
                                json.dumps({"sticky_timeout": 7}))
        assert status == 400
        assert "sticky_timeout" in json.loads(body)["error"]

    def test_sticky_post_invalid_json_400(self):
        assert route(FakeDaemon(), "POST", "/api/sticky", "{")[0] == 400

    def test_sticky_rejects_other_methods(self):
        assert route(FakeDaemon(), "DELETE", "/api/sticky")[0] == 405

    def test_single_dial_reauth(self):
        d = FakeDaemon()
        status, _, body = route(d, "POST", "/api/dial/dial0/reauth")
        assert status == 200
        assert ("reauth_dial", "dial0") in d.calls
        assert "dials" in json.loads(body)

    def test_single_dial_reauth_unknown_device_400(self):
        status, _, body = route(FakeDaemon(), "POST", "/api/dial/nope/reauth")
        assert status == 400
        assert "unknown dial" in json.loads(body)["error"]

    def test_single_dial_reauth_requires_post(self):
        assert route(FakeDaemon(), "GET", "/api/dial/dial0/reauth")[0] == 405
