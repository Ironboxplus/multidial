"""Runtime state that lives *beside* the UCI config: per-dial control
(stop/mark), connectivity history, and a rotating log buffer.

Why separate from `config.py`: accounts/dials are declarative config the user
edits; this module is operational state the daemon mutates at runtime in
response to events (a dial's MAC gets blocked, a line flaps, the user clicks
"stop"). `ControlStore` persists to JSON so a daemon restart does NOT silently
resume hammering a dial the operator deliberately stopped. History and logs
live on tmpfs (`/var`, RAM on OpenWRT) so they never wear flash.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Callable, Deque, Dict, List, Optional


# --- per-dial control ---------------------------------------------------
@dataclass
class DialControl:
    stopped: bool = False   # True => keepalive skips it AND mwan3 routes around it
    mark: str = ""          # freeform operator note, e.g. "MAC blocked / cooling"
    mark_ts: float = 0.0
    # --- registry fields: a dial is "provisioned" once created, and stays so
    # until explicitly decommissioned. The registry — NOT the account `dials`
    # count — is the source of truth for which dials exist, so lowering the
    # count never auto-deletes a running dial. account/slot/index let the daemon
    # rebuild a dial's spec even when it's no longer in the count-driven plan.
    provisioned: bool = False
    account: str = ""       # owning account section name
    slot: int = -1          # stable global slot (drives device/MAC/table/pref)
    index: int = 0          # per-account dial index (display only)
    mac_rotation: int = 0   # how many times this dial's MAC has been rotated
    sqm_provisioned: bool = False  # SQM (CAKE + cake-autorate) applied to this dial


class ControlStore:
    """Per-dial stop/mark flags, persisted to a JSON file.

    Persistence is the point: if a dial's MAC is blocked and the operator stops
    it, a daemon restart must not auto-resume the auth storm. Keyed by device
    name (dial0/dial1/...), which is stable for a given account ordering.
    """

    def __init__(self, path: Optional[str] = None, clock: Callable[[], float] = time.time):
        self.path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._controls: Dict[str, DialControl] = {}
        self.load()

    def load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return
        with self._lock:
            self._controls = {
                dev: DialControl(
                    stopped=bool(v.get("stopped", False)),
                    mark=str(v.get("mark", "")),
                    mark_ts=float(v.get("mark_ts", 0.0)),
                    provisioned=bool(v.get("provisioned", False)),
                    account=str(v.get("account", "")),
                    slot=int(v.get("slot", -1)),
                    index=int(v.get("index", 0)),
                    mac_rotation=int(v.get("mac_rotation", 0)),
                    sqm_provisioned=bool(v.get("sqm_provisioned", False)),
                )
                for dev, v in (raw or {}).items()
                if isinstance(v, dict)
            }

    def save(self) -> None:
        if not self.path:
            return
        with self._lock:
            data = {dev: asdict(c) for dev, c in self._controls.items()}
        try:
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def get(self, device: str) -> DialControl:
        with self._lock:
            c = self._controls.get(device)
            return DialControl(**asdict(c)) if c else DialControl()

    def is_stopped(self, device: str) -> bool:
        with self._lock:
            c = self._controls.get(device)
            return bool(c and c.stopped)

    def set_stopped(self, device: str, stopped: bool) -> None:
        with self._lock:
            c = self._controls.get(device)
            if c is None:
                c = DialControl()
                self._controls[device] = c
            c.stopped = stopped
        self.save()

    def set_mark(self, device: str, mark: str) -> None:
        with self._lock:
            c = self._controls.get(device)
            if c is None:
                c = DialControl()
                self._controls[device] = c
            c.mark = mark
            c.mark_ts = self._clock()
        self.save()

    def all(self) -> Dict[str, DialControl]:
        with self._lock:
            return {dev: DialControl(**asdict(c)) for dev, c in self._controls.items()}

    # --- registry: which dials exist (source of truth, not the count) -------
    def _ensure(self, device: str) -> "DialControl":
        c = self._controls.get(device)
        if c is None:
            c = DialControl()
            self._controls[device] = c
        return c

    def register(self, device: str, account: str, slot: int, index: int = 0) -> None:
        """Mark a dial provisioned and record its account binding + slot.

        Idempotent: re-registering an existing dial refreshes its binding but
        preserves stop/mark/rotation (operational state survives reconciles).
        """
        with self._lock:
            c = self._ensure(device)
            c.provisioned = True
            c.account = account
            c.slot = slot
            c.index = index
        self.save()

    def deregister(self, device: str) -> None:
        """Forget a dial entirely (explicit decommission, the only delete path)."""
        with self._lock:
            existed = device in self._controls
            self._controls.pop(device, None)
        if existed:
            self.save()

    def is_provisioned(self, device: str) -> bool:
        with self._lock:
            c = self._controls.get(device)
            return bool(c and c.provisioned)

    def registered(self) -> Dict[str, DialControl]:
        """All provisioned dials, device -> control (copies)."""
        with self._lock:
            return {dev: DialControl(**asdict(c))
                    for dev, c in self._controls.items() if c.provisioned}

    def used_slots(self) -> set:
        with self._lock:
            return {c.slot for c in self._controls.values()
                    if c.provisioned and c.slot >= 0}

    def get_rotation(self, device: str) -> int:
        with self._lock:
            c = self._controls.get(device)
            return int(c.mac_rotation) if c else 0

    def bump_rotation(self, device: str) -> int:
        """Increment a dial's MAC rotation counter and return the new value."""
        with self._lock:
            c = self._ensure(device)
            c.mac_rotation = int(c.mac_rotation) + 1
            new = c.mac_rotation
        self.save()
        return new

    def set_sqm_provisioned(self, device: str, value: bool = True) -> None:
        """Record whether SQM (CAKE + cake-autorate) has been applied to a dial.

        Mirrors `provisioned`: once true, the daemon skips re-running the SQM
        UCI + service restart for that dial, so reconciles stay cheap. Cleared
        implicitly by decommission (the row is dropped) or set False to force a
        re-provision (e.g. after the SQM config schema changes)."""
        with self._lock:
            c = self._ensure(device)
            c.sqm_provisioned = bool(value)
        self.save()

    def prune(self, valid_devices) -> None:
        """Drop NON-provisioned control rows for devices not in `valid_devices`.

        Provisioned dials are the registry's source of truth and are never
        pruned here — only an explicit decommission (deregister) removes them.
        This still reaps stray stop/mark rows left by a never-provisioned device.
        """
        valid = set(valid_devices)
        with self._lock:
            stale = [d for d, c in self._controls.items()
                     if not c.provisioned and d not in valid]
            for d in stale:
                del self._controls[d]
        if stale:
            self.save()


# --- connectivity history ----------------------------------------------
@dataclass
class Event:
    device: str
    account_userid: str
    kind: str          # "down" (online->offline) | "up" (offline->online)
    ts: float
    detail: str = ""


class History:
    """Bounded ring of online<->offline transition events (newest-first read)."""

    def __init__(self, capacity: int = 200):
        self._events: Deque[Event] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def record(self, device: str, account_userid: str, kind: str,
               ts: float, detail: str = "") -> None:
        with self._lock:
            self._events.append(Event(device, account_userid, kind, ts, detail))

    def recent(self, limit: int = 50) -> List[dict]:
        with self._lock:
            items = list(self._events)
        if limit:
            items = items[-limit:]
        return [asdict(e) for e in reversed(items)]  # newest first


# --- rotating log buffer -------------------------------------------------
class LogBuffer:
    """In-memory ring of recent log lines + a size-rotated file sink.

    The memory ring backs the web log panel (fast, no disk read). The file sink
    rotates by size so logs survive a panel reload without growing unbounded.
    On OpenWRT `/var` is tmpfs, so file writes hit RAM, not flash.
    """

    def __init__(self, path: Optional[str] = None, capacity: int = 500,
                 max_bytes: int = 256 * 1024, backups: int = 1,
                 clock: Callable[[], float] = time.time):
        self.path = path
        self.max_bytes = max_bytes
        self.backups = backups
        self._clock = clock
        self._lines: Deque[str] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def write(self, msg: str) -> None:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._clock()))
        line = f"[{ts}] {msg}"
        with self._lock:
            self._lines.append(line)
        self._to_file(line)

    def _to_file(self, line: str) -> None:
        if not self.path:
            return
        try:
            self._rotate_if_needed()
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def _rotate_if_needed(self) -> None:
        try:
            if not (os.path.exists(self.path)
                    and os.path.getsize(self.path) >= self.max_bytes):
                return
            for i in range(self.backups, 0, -1):
                src = self.path if i == 1 else f"{self.path}.{i - 1}"
                dst = f"{self.path}.{i}"
                if os.path.exists(src):
                    os.replace(src, dst)
        except OSError:
            pass

    def lines(self, limit: int = 200) -> List[str]:
        with self._lock:
            items = list(self._lines)
        return items[-limit:] if limit else items
