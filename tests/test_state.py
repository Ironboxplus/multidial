"""TDD: runtime state — per-dial control (persisted), history ring, rotating log.

Persistence matters: an operator who stops a blocked dial must NOT have the
daemon auto-resume hammering it after a restart, so ControlStore round-trips
through a JSON file. History/log are bounded rings.
"""
import json

from multidial.state import ControlStore, History, LogBuffer


class FixedClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class TestControlStore:
    def test_default_dial_is_not_stopped(self):
        cs = ControlStore(path=None)
        assert cs.is_stopped("dial0") is False
        assert cs.get("dial0").mark == ""

    def test_stop_then_query(self):
        cs = ControlStore(path=None)
        cs.set_stopped("dial1", True)
        assert cs.is_stopped("dial1") is True
        cs.set_stopped("dial1", False)
        assert cs.is_stopped("dial1") is False

    def test_mark_records_note_and_timestamp(self):
        cs = ControlStore(path=None, clock=FixedClock(1234.0))
        cs.set_mark("dial0", "MAC blocked / cooling")
        c = cs.get("dial0")
        assert c.mark == "MAC blocked / cooling"
        assert c.mark_ts == 1234.0

    def test_persists_across_reload(self, tmp_path):
        p = str(tmp_path / "control.json")
        cs = ControlStore(path=p)
        cs.set_stopped("dial1", True)
        cs.set_mark("dial1", "blocked")
        # a fresh store reading the same file must see the stop survive a restart
        cs2 = ControlStore(path=p)
        assert cs2.is_stopped("dial1") is True
        assert cs2.get("dial1").mark == "blocked"

    def test_file_is_valid_json(self, tmp_path):
        p = str(tmp_path / "control.json")
        cs = ControlStore(path=p)
        cs.set_stopped("dial0", True)
        data = json.loads((tmp_path / "control.json").read_text())
        assert data["dial0"]["stopped"] is True

    def test_get_returns_copy_not_live_ref(self):
        cs = ControlStore(path=None)
        cs.set_mark("dial0", "x")
        c = cs.get("dial0")
        c.mark = "mutated"
        assert cs.get("dial0").mark == "x"

    def test_prune_drops_unknown_devices(self):
        cs = ControlStore(path=None)
        cs.set_stopped("dial0", True)
        cs.set_stopped("dial9", True)
        cs.prune(["dial0"])
        assert "dial0" in cs.all()
        assert "dial9" not in cs.all()

    def test_missing_file_loads_empty(self, tmp_path):
        cs = ControlStore(path=str(tmp_path / "nope.json"))
        assert cs.all() == {}

    def test_corrupt_file_loads_empty(self, tmp_path):
        p = tmp_path / "control.json"
        p.write_text("{not json")
        cs = ControlStore(path=str(p))
        assert cs.all() == {}


class TestRegistry:
    def test_register_marks_provisioned_with_binding(self):
        cs = ControlStore(path=None)
        cs.register("dial0", account="campus", slot=0, index=0)
        assert cs.is_provisioned("dial0") is True
        c = cs.get("dial0")
        assert c.account == "campus"
        assert c.slot == 0

    def test_register_is_idempotent_and_preserves_stop_mark_rotation(self):
        cs = ControlStore(path=None)
        cs.register("dial1", account="campus", slot=1)
        cs.set_stopped("dial1", True)
        cs.set_mark("dial1", "cooling")
        cs.bump_rotation("dial1")
        cs.register("dial1", account="campus", slot=1)  # re-provision
        c = cs.get("dial1")
        assert c.stopped is True and c.mark == "cooling" and c.mac_rotation == 1

    def test_deregister_forgets_dial(self):
        cs = ControlStore(path=None)
        cs.register("dial2", account="campus", slot=2)
        cs.deregister("dial2")
        assert cs.is_provisioned("dial2") is False
        assert "dial2" not in cs.all()

    def test_registered_returns_only_provisioned(self):
        cs = ControlStore(path=None)
        cs.register("dial0", account="campus", slot=0)
        cs.set_stopped("dial5", True)  # a stop row, never provisioned
        reg = cs.registered()
        assert set(reg) == {"dial0"}

    def test_used_slots_reflects_provisioned(self):
        cs = ControlStore(path=None)
        cs.register("dial0", account="c", slot=0)
        cs.register("dial2", account="c", slot=2)
        assert cs.used_slots() == {0, 2}

    def test_prune_never_drops_provisioned(self):
        # lowering the count must NOT delete a running dial — prune leaves
        # provisioned rows even when they're absent from valid_devices
        cs = ControlStore(path=None)
        cs.register("dial2", account="campus", slot=2)
        cs.prune(["dial0", "dial1"])  # count dropped to 2; dial2 not in plan
        assert cs.is_provisioned("dial2") is True

    def test_bump_rotation_increments_and_persists(self, tmp_path):
        p = str(tmp_path / "control.json")
        cs = ControlStore(path=p)
        cs.register("dial1", account="campus", slot=1)
        assert cs.bump_rotation("dial1") == 1
        assert cs.bump_rotation("dial1") == 2
        assert ControlStore(path=p).get_rotation("dial1") == 2

    def test_registry_round_trips_through_file(self, tmp_path):
        p = str(tmp_path / "control.json")
        cs = ControlStore(path=p)
        cs.register("dial0", account="campus", slot=0, index=0)
        cs2 = ControlStore(path=p)
        c = cs2.get("dial0")
        assert cs2.is_provisioned("dial0") is True
        assert c.account == "campus" and c.slot == 0


class TestHistory:
    def test_records_and_returns_newest_first(self):
        h = History()
        h.record("dial0", "u1", "down", 1.0, "captive")
        h.record("dial0", "u1", "up", 2.0, "204")
        out = h.recent()
        assert [e["kind"] for e in out] == ["up", "down"]
        assert out[0]["ts"] == 2.0
        assert out[0]["detail"] == "204"

    def test_is_bounded(self):
        h = History(capacity=3)
        for i in range(10):
            h.record("dial0", "u1", "down", float(i))
        out = h.recent(limit=0)
        assert len(out) == 3
        assert out[0]["ts"] == 9.0  # newest kept

    def test_limit_caps_result(self):
        h = History()
        for i in range(20):
            h.record("dial0", "u1", "down", float(i))
        assert len(h.recent(limit=5)) == 5


class TestLogBuffer:
    def test_write_then_lines(self):
        lb = LogBuffer(path=None)
        lb.write("hello")
        lb.write("world")
        out = lb.lines()
        assert any("hello" in x for x in out)
        assert out[-1].endswith("world")

    def test_ring_is_bounded(self):
        lb = LogBuffer(path=None, capacity=3)
        for i in range(10):
            lb.write(f"line{i}")
        out = lb.lines(limit=0)
        assert len(out) == 3
        assert "line9" in out[-1]

    def test_lines_limit(self):
        lb = LogBuffer(path=None, capacity=100)
        for i in range(50):
            lb.write(f"l{i}")
        assert len(lb.lines(limit=10)) == 10

    def test_writes_to_file(self, tmp_path):
        p = str(tmp_path / "multidial.log")
        lb = LogBuffer(path=p)
        lb.write("persisted line")
        assert "persisted line" in (tmp_path / "multidial.log").read_text()

    def test_file_rotation(self, tmp_path):
        p = tmp_path / "multidial.log"
        lb = LogBuffer(path=str(p), max_bytes=200, backups=1)
        for i in range(100):
            lb.write("x" * 50)  # quickly exceed 200 bytes
        assert (tmp_path / "multidial.log.1").exists()
        # current log stays bounded near max_bytes (rotation happened)
        assert p.stat().st_size < 200 * 3
