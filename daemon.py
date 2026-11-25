"""Orchestration daemon: reconcile UCI to the desired dials, then keep each
dial authenticated.

One `tick()` = (1) read `/etc/config/multidial`, (2) if it changed, regenerate
the UCI network+mwan3 sections and reload netifd/mwan3, (3) ensure per-dial SQM
sections still exist, (4) run a device-bound ePortal keepalive for every planned
dial. The shell Runner, the HTTP transport and the clock are all injected, so
the whole loop is unit-tested off-router.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Callable, Dict, List, Optional

from . import dns, sqm, sticky
from .config import ConfigError, MultidialConfig, Globals, rotated_macaddr
from .dialer import Dialer, AuthOutcome, NetStatus, DialResult
from .state import ControlStore, History
from .uci_sync import (
    reconcile, decommission_sections, parse_section_names, dev_section_name,
    desired_sections, is_managed_rule,
)

_OK = ("success", "already_online")

# Login backoff (禁止频繁登陆): after a FAILED auth on a captive dial, wait before
# retrying so the daemon never hammers the AC into an anti-abuse black-hole (the
# failure mode that hard-blocked a dial this session). A successful auth — or a
# dial that comes back ONLINE on its own — clears the backoff immediately.
_AUTH_BACKOFF_BASE = 30.0   # first retry waits this long after a failure
_AUTH_BACKOFF_CAP = 600.0   # exponential (×2 each fail), capped at 10 minutes

_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
_USERID_RE = re.compile(r"^[A-Za-z0-9_.@-]+$")
_DEVICE_RE = re.compile(r"^dial[0-9]+$")

# Per-dial health counters (source-of-truth shape; copied per dial via _stat()).
_ZERO_STAT = {"reauth": 0, "down": 0, "last_reauth_ts": 0.0, "last_down_ts": 0.0}


def _sq(value) -> str:
    """Single-quote a value for safe `sh -c` interpolation (injection-proof)."""
    return "'" + str(value).replace("'", "'\\''") + "'"


def _truthy(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() not in ("0", "false", "no", "off", "")


class Daemon:
    def __init__(
        self,
        runner,
        transport=None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        logger: Optional[Callable[..., None]] = None,
        control: Optional[ControlStore] = None,
        history: Optional[History] = None,
        log_buffer=None,
    ):
        self.runner = runner
        self.transport = transport
        self.clock = clock
        self._sleep = sleep  # vestigial: run() now idles on _apply_event.wait()
        self._logger = logger or (lambda *a, **k: None)
        self.control = control if control is not None else ControlStore(path=None)
        self.history = history if history is not None else History()
        self.log_buffer = log_buffer

        self._lock = threading.Lock()
        self._stop = threading.Event()
        # Event-driven apply: a config-mutating API call sets this so the run loop
        # wakes and reconciles immediately, instead of the loop busy-polling. The
        # loop otherwise blocks on it for `interval` seconds between keepalives.
        self._apply_event = threading.Event()
        self._cfg: Optional[MultidialConfig] = None
        self._last_config_text: Optional[str] = None
        self._dials: Dict[str, dict] = {}
        self._prev_online: Dict[str, bool] = {}
        self._enforced_stopped: set = set()
        self._reconcile_count = 0
        self._reauth_count = 0
        self._last_tick = 0.0
        self._auth_backoff: Dict[str, dict] = {}  # dev -> {fails, next_ts}
        # Per-dial health counters so the operator can see WHICH account keeps
        # dropping (the global reauth_count can't attribute a drop to an account).
        self._dial_stats: Dict[str, dict] = {}  # dev -> {reauth, down, last_reauth_ts, last_down_ts}
        self._force_sqm_apply = False
        self._last_sqm_heal_ts = 0.0  # throttles cake-autorate liveness heals

    def log(self, *parts) -> None:
        """Fan log lines out to the injected logger AND the rotating buffer."""
        msg = " ".join(str(p) for p in parts)
        try:
            self._logger(msg)
        except Exception:
            pass
        if self.log_buffer is not None:
            self.log_buffer.write(msg)

    # --- inputs ---------------------------------------------------------
    def load_config(self):
        rc, out = self.runner.run("uci show multidial")
        text = out if rc == 0 else ""
        return MultidialConfig.from_uci_show(text), text

    def existing_sections(self) -> Dict[str, List[str]]:
        res: Dict[str, List[str]] = {}
        for config in ("network", "mwan3"):
            rc, out = self.runner.run(f"uci show {config}")
            res[config] = parse_section_names(out, config) if rc == 0 else []
        return res

    # --- reconcile (additive: provision-once, never auto-delete) --------
    @staticmethod
    def _slot_of(device: str) -> int:
        try:
            return int(device[len("dial"):])
        except (ValueError, TypeError):
            return -1

    @staticmethod
    def _next_free_slot(used: set) -> int:
        s = 0
        while s in used:
            s += 1
        return s

    def _registered_specs(self, cfg: MultidialConfig) -> List:
        """Specs for every currently-provisioned dial (the registry is truth).

        Independent of the account `dials` count: a dial registered when the
        count was higher stays here until explicitly decommissioned, so lowering
        the count never drops a running dial. Dials whose account was deleted are
        skipped (orphaned, but never auto-removed). Rotated MACs are honored.
        """
        accounts = {a.name: a for a in cfg.accounts}
        specs = []
        for dev, c in self.control.registered().items():
            acct = accounts.get(c.account)
            if acct is None:
                continue
            specs.append(cfg.make_spec(acct, index=c.index, slot=c.slot,
                                       rotation=c.mac_rotation))
        specs.sort(key=lambda s: s.slot)
        return specs

    def _provision_new(self, cfg: MultidialConfig, existing_net: List[str]) -> List[str]:
        """Register dials where an enabled account's count grew; return the
        devices that are genuinely NEW (need create + ifup), as opposed to
        existing router sections we simply adopt into the registry.

        Bulk-at-start falls out naturally: an empty registry with count=N
        provisions slots 0..N-1 in one pass. A later +1 provisions just the next
        free slot. Existing sections (a restart, or a manual setup) are adopted
        without a reload.
        """
        existing_devs = {n for n in existing_net if n.startswith("dial")}
        by_account: Dict[str, int] = {}
        for c in self.control.registered().values():
            by_account[c.account] = by_account.get(c.account, 0) + 1
        used = set(self.control.used_slots())
        added: List[str] = []
        for acct in cfg.accounts:
            if not acct.enabled:
                continue
            have = by_account.get(acct.name, 0)
            for i in range(have, acct.dials):
                slot = self._next_free_slot(used)
                used.add(slot)
                dev = f"dial{slot}"
                self.control.register(dev, account=acct.name, slot=slot, index=i)
                if dev not in existing_devs:
                    added.append(dev)  # genuinely new -> create + ifup + reload
        return added

    def _deprovision_excess(self, cfg: MultidialConfig) -> List[str]:
        """Tear down dials beyond an enabled account's configured `dials` count.

        The operator-driven scale-down: lowering an account's count actively
        removes its excess dials (highest per-account index first, so the
        earliest/most-stable dials stay), batched into ONE mwan3/SQM re-apply.
        Only dials WE registered are candidates — an unmanaged/orphan section is
        never auto-deleted. Deleted or disabled accounts are handled elsewhere
        (delete_account / keepalive), not here.
        """
        accounts = {a.name: a for a in cfg.accounts}
        by_account: Dict[str, List] = {}
        for dev, c in self.control.registered().items():
            by_account.setdefault(c.account, []).append((c.stopped, c.index, dev))
        removed: List[str] = []
        for name, dials in by_account.items():
            acct = accounts.get(name)
            if acct is None or not acct.enabled:
                continue
            excess = len(dials) - max(0, acct.dials)
            if excess <= 0:
                continue
            # Remove excess worst-first: an operator-STOPPED dial (flagged dead /
            # MAC-blocked) goes before any healthy one, then highest per-account
            # index — so lowering the count never strands the account on a stopped
            # dial while a working sibling gets torn down.
            ranked = sorted(dials, reverse=True)  # (stopped, index) descending
            victims = [dev for _stopped, _index, dev in ranked[:excess]]
            removed.extend(self._teardown_many(victims))
        if removed:
            self._apply_dial_teardown()  # ONE restart for the whole scale-down
        return removed

    def reconcile_if_changed(self, cfg: MultidialConfig, raw_text: str) -> bool:
        if raw_text == self._last_config_text:
            return False
        existing = self.existing_sections()
        # Snapshot the LIVE mwan3 rule content BEFORE any write, so we can tell
        # whether reconcile actually changes the rule set/content and thus needs a
        # mwan3 restart to take effect (name-only diffing missed content edits like
        # a changed sticky timeout, a shrunk range list, or sticky being disabled).
        _, mwan3_text = self.runner.run("uci show mwan3")
        added = self._provision_new(cfg, existing.get("network", []))
        self._deprovision_excess(cfg)
        specs = self._registered_specs(cfg)
        rules_changed = bool(specs) and (
            self._desired_rule_kv(cfg) != self._managed_rule_kv(mwan3_text)
        )
        # Compare EVERY tracker setting (timeout/interval/down/... and track_ip),
        # not just the target list: mwan3track reads all of them once at start,
        # so any edit needs the per-dial tracker respawn below to take effect.
        live_tracking = self._managed_tracking(mwan3_text)
        tracking_changed = any(
            dev in live_tracking and any(
                live_tracking[dev].get(key) != value for key, value in want.items()
            )
            for dev, want in self._desired_tracking(cfg, specs).items()
        )
        # additive: (re)create desired sections, NEVER delete (a lower count is a
        # no-op on the router; removal is an explicit decommission_dial()).
        for cmd in reconcile(specs, cfg.globals, existing):
            self.runner.run(cmd)
        if added:
            # Register the brand-new section(s) with netifd via the INCREMENTAL
            # reload (`ubus call network reload`) instead of the heavy
            # `/etc/init.d/network reload`. The init-script reload re-runs
            # board/switch setup and can bounce siblings + the EasyTier overlay;
            # the ubus reload only applies the config delta — it learns the new
            # sections and starts ONLY the changed dial, leaving running dials and
            # tun0 untouched (verified live: tun0 IP + dial0 204 + overlay peer all
            # survived a reload). Then `ifup` just the new dial: its iface-up
            # hotplugs (11-sqm, 15-mwan3) wire SQM + per-iface mwan3 tracking for
            # that one port. mwan3 is still restarted so the load-balancer picks up
            # the new member (balancer rules are rebuilt across the full member set).
            self.runner.run("ubus call network reload")
            for dev in sorted(added):
                self.runner.run(f"ifup {dev}")
            self.runner.run("/etc/init.d/mwan3 restart")
            self._enforced_stopped.clear()
        elif rules_changed:
            # No new dial (so no session-bouncing network reload/ifup), but the
            # mwan3 rule set/content changed — first rollout of the intranet sticky
            # rules, a range/timeout edit, or sticky being disabled. Activate with a
            # lone mwan3 restart AFTER the UCI writes (a scale-down teardown restart,
            # if any, ran against the pre-write config), so the new routing takes
            # effect without disturbing running dials.
            self.runner.run("/etc/init.d/mwan3 restart")
            self.log("mwan3 rules updated (sticky/intranet) — restarted")
        elif tracking_changed:
            # mwan3track reads track_ip only once when its process starts.  Do
            # not run `/etc/init.d/mwan3 reload`: this build implements reload
            # as a full stop/start that tears down routing tables for ~2 s.
            # Signal only the six procd tracker instances; procd respawns each
            # against the freshly committed UCI while policies/routes and the
            # dial interfaces remain continuously live.
            for spec in specs:
                self.runner.run(self._mwan3_tracker_reload_cmd(spec.device))
            self.log("mwan3 tracking settings updated — trackers respawning")
        for cmd in dns.reconcile_cmds(cfg.globals):
            self.runner.run(cmd)
        self._last_config_text = raw_text
        self._reconcile_count += 1
        self.log(f"reconciled: {len(specs)} dial(s), added={len(added)}")
        return True

    @staticmethod
    def _managed_tracking(mwan3_text: str) -> Dict[str, Dict[str, object]]:
        """Per-section option values from real ``uci show mwan3``.

        Scalars are unquoted strings; ``track_ip`` (the only list a managed
        interface carries) becomes a list of its quoted tokens, since UCI
        renders lists as adjacent quoted values
        (``mwan3.dial0.track_ip='192.0.2.11' '192.0.2.13'``). Sections that
        aren't interfaces are harmless extras: the caller only looks up the
        dial names it manages.
        """
        result: Dict[str, Dict[str, object]] = {}
        pattern = re.compile(r"^mwan3\.([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)=(.*)$")
        for raw in (mwan3_text or "").splitlines():
            match = pattern.match(raw.strip())
            if not match:
                continue
            sec, opt, val = match.groups()
            tokens = re.findall(r"'([^']*)'", val)
            result.setdefault(sec, {})[opt] = (
                tokens if opt == "track_ip" else (tokens[0] if tokens else val)
            )
        return result

    @staticmethod
    def _desired_tracking(cfg: MultidialConfig, specs: List) -> Dict[str, Dict[str, object]]:
        """Desired mwan3 interface settings per dial, shaped like `_managed_tracking`."""
        result: Dict[str, Dict[str, object]] = {}
        for s in desired_sections(specs, cfg.globals):
            if s.config != "mwan3" or s.stype != "interface":
                continue
            want: Dict[str, object] = dict(s.options)
            want["track_ip"] = list(s.lists.get("track_ip", []))
            result[s.name] = want
        return result

    @staticmethod
    def _mwan3_tracker_reload_cmd(device: str) -> str:
        """Ask procd to respawn one tracker without touching mwan3 routes."""
        return (
            "ubus call service signal "
            f"'{{\"name\":\"mwan3\",\"instance\":\"track_{device}\","
            "\"signal\":15}'"
        )

    @staticmethod
    def _managed_rule_kv(mwan3_text: str) -> Dict[str, str]:
        """Live content of the managed mwan3 rules (md_default/md_intra*/md_svc) parsed
        from `uci show mwan3` into a {key: unquoted_value} map for content diffing.
        """
        kv: Dict[str, str] = {}
        for raw in (mwan3_text or "").splitlines():
            line = raw.strip()
            if "=" not in line:
                continue
            lhs, rhs = line.split("=", 1)
            parts = lhs.split(".")
            if len(parts) < 2 or parts[0] != "mwan3":
                continue
            sec = parts[1]
            if not is_managed_rule(sec):
                continue
            val = rhs.strip()
            if len(val) >= 2 and val[0] == "'" and val[-1] == "'":
                val = val[1:-1]
            kv[lhs] = val
        return kv

    def _desired_rule_kv(self, cfg: MultidialConfig) -> Dict[str, str]:
        """Desired managed-rule content in the same shape as `_managed_rule_kv`, so
        a plain dict compare reports whether the rule SET or ANY rule's content
        (dest_ip / sticky / timeout / use_policy / order-independent) changed —
        the trigger for a one-off mwan3 restart without a dial-bouncing reload.
        """
        kv: Dict[str, str] = {}
        specs = self._registered_specs(cfg)
        for s in desired_sections(specs, cfg.globals):
            if s.config != "mwan3" or s.stype != "rule":
                continue
            kv[f"mwan3.{s.name}"] = s.stype
            for opt, v in s.options.items():
                kv[f"mwan3.{s.name}.{opt}"] = str(v)
        return kv

    def _uci_section_exists(self, package: str, section: str) -> bool:
        rc, _ = self.runner.run(f"uci -q show {package}.{section}")
        return rc == 0

    def _sqm_needs_provision(self, spec) -> bool:
        ctl = self.control.get(spec.device)
        if not ctl.sqm_provisioned:
            return True

        missing = []
        for package in ("sqm", "cake-autorate"):
            if not self._uci_section_exists(package, spec.device):
                missing.append(f"{package}.{spec.device}")

        if missing:
            self.log(f"sqm drift detected on {spec.device}: missing {', '.join(missing)}")
            return True
        return False

    def _provision_sqm(self, specs: List, glb: Globals) -> None:
        """Give every registered dial its own CAKE shaper + cake-autorate loop.

        Default-on (`globals.sqm`). Idempotent and cheap: never-provisioned dials
        are applied immediately, while already-provisioned dials only run two UCI
        existence checks (`sqm.<dial>` and `cake-autorate.<dial>`). The services
        restart only when a dial is new or drifted — so steady-state does not
        churn SQM/cake-autorate, but manual config deletion is repaired.

        Gated on the master switch too: when multidial is powered off
        (`globals.enabled=0`) we must NOT start shapers — the system is meant to
        be dormant (mwan3 stopped, native single-WAN fallback).

        HNAT/offload is a persistent system setting (turboacc
        `fastpath_mh_eth_hnat=0`), NOT managed here; `sqm_status` only reports it
        so the UI warns if it's ever re-enabled.
        """
        force_apply = self._force_sqm_apply
        self._force_sqm_apply = False
        if not glb.enabled or not glb.sqm:
            return
        pending = [s for s in specs if self._sqm_needs_provision(s)]
        if not pending:
            if force_apply and specs:
                for cmd in sqm.apply_cmds():
                    self.runner.run(cmd)
                self.log(f"sqm services reapplied: {len(specs)} dial(s)")
            elif specs:
                self._heal_dead_autorate(specs)
            return
        for cmd in sqm.provision_cmds(pending, glb):
            self.runner.run(cmd)
        for cmd in sqm.apply_cmds():
            self.runner.run(cmd)
        for s in pending:
            self.control.set_sqm_provisioned(s.device, True)
        self.log(f"sqm provisioned: {len(pending)} dial(s)")

    def _heal_dead_autorate(self, specs: List) -> None:
        """Respawn a provisioned dial's cake-autorate loop that silently died.

        UCI-drift detection (`_sqm_needs_provision`) can't catch this: the
        `sqm.<dial>` / `cake-autorate.<dial>` SECTIONS are all present, but the
        running loop for one dial is gone. At the last restart that dial's
        instance lost cake-autorate's `verify_ifs_up` race — its shaper iface or
        ingress IFB (`ifb4<dial>`) wasn't up yet, so the instance self-terminated
        — and the launcher never respawns a dead loop. That dial's CAKE shaper is
        then frozen at its base rate with cake-autorate no longer floating it by
        latency (the "SQM not applied" symptom), while its siblings keep adapting.

        Detect it from `ps` (same read the `/api/sqm` status uses), but ONLY when
        at least one sibling loop is visibly running — an all-empty read means
        "not started yet / ps unavailable", which the normal provision + apply
        path owns, so a cold start never thrash-restarts. Bounce cake-autorate to
        bring the missing loop back, throttled by SQM_HEAL_COOLDOWN so a dial that
        genuinely can't come up can't bounce the healthy siblings every tick.
        """
        _, ps_out = self.runner.run(sqm.STATUS_PS_CMD)
        live = sqm.parse_autorate_pids(ps_out)
        if not live:
            return  # nothing running to compare against -> not a partial failure
        missing = [s.device for s in specs if s.device not in live]
        if not missing:
            return
        now = self.clock()
        if now - self._last_sqm_heal_ts < sqm.SQM_HEAL_COOLDOWN:
            return
        self._last_sqm_heal_ts = now
        for cmd in sqm.cakeautorate_restart_cmds(enable=True):
            self.runner.run(cmd)
        self.log(f"sqm heal: cake-autorate loop missing for {', '.join(missing)} "
                 "— cake-autorate restarted")

    # --- keepalive ------------------------------------------------------
    def _set_dial(self, spec, online, authed, outcome, detail, stopped, mark) -> None:
        with self._lock:
            self._dials[spec.device] = {
                "device": spec.device,
                "slot": spec.slot,
                "account_userid": spec.account_userid,
                "account_name": spec.account_name,
                "macaddr": spec.macaddr,
                "online": online,
                "authed": authed,
                "outcome": outcome,
                "detail": detail,
                "stopped": stopped,
                "mark": mark,
                "ts": self.clock(),
            }

    def _stat(self, dev: str) -> dict:
        """Per-dial health counters (created on first touch).

        The setdefault (an outer-dict structural change) is serialized against
        snapshot()/_prune_state()/_teardown_dial() via the lock. The returned
        inner dict is mutated by the caller off-lock — those are value bumps that
        never resize the outer dict, so they can't race the prune rebuild.
        """
        with self._lock:
            return self._dial_stats.setdefault(dev, dict(_ZERO_STAT))

    def _note_transition(self, spec, online_now: bool, detail: str) -> None:
        """Record an online<->offline flip in history (first sight = baseline)."""
        dev = spec.device
        prev = self._prev_online.get(dev)
        if prev is not None and prev != online_now:
            kind = "up" if online_now else "down"
            self.history.record(dev, spec.account_userid, kind, self.clock(), detail)
            if not online_now:
                st = self._stat(dev)
                st["down"] += 1
                st["last_down_ts"] = self.clock()
            # Attributable log line (dial + account userid) so a drop points at
            # the account, not just the dial — fixes "看不出哪个号掉 / log 太少".
            self.log(f"{dev} ({spec.account_userid}) {kind} ({detail})")
        self._prev_online[dev] = online_now

    def _enforce_admin_state(self, dev: str, stopped: bool) -> None:
        """Keep mwan3 routing in sync with the stop flag, idempotently.

        A stopped dial must be taken out of mwan3 (its track pings the gateway,
        which still answers while captive/blocked — so mwan3 alone would keep
        routing through a dead dial). `_enforced_stopped` makes this a no-op once
        applied, and is cleared on mwan3 restart so we re-assert.
        """
        if stopped and dev not in self._enforced_stopped:
            self.runner.run(f"mwan3 interface {dev} down")
            self._enforced_stopped.add(dev)
        elif not stopped and dev in self._enforced_stopped:
            self.runner.run(f"mwan3 interface {dev} up")
            self._enforced_stopped.discard(dev)

    def _prune_state(self, planned: set) -> None:
        """Drop in-memory state for dials no longer in the plan.

        Without this, shrinking the dial count leaves a ghost dial in the
        snapshot (the daemon kept its last-seen entry in `_dials`), so the web UI
        keeps showing e.g. dial2 after the count goes 3 -> 2.
        """
        with self._lock:
            for dev in [d for d in self._dials if d not in planned]:
                del self._dials[dev]
            # _dial_stats is read on the HTTP thread (snapshot) and popped by
            # _teardown_dial (also HTTP thread) — rebuild it UNDER the lock so a
            # concurrent pop can't trip "dict changed size during iteration".
            self._dial_stats = {d: v for d, v in self._dial_stats.items() if d in planned}
        self._prev_online = {d: v for d, v in self._prev_online.items() if d in planned}
        self._enforced_stopped &= planned
        self._auth_backoff = {d: v for d, v in self._auth_backoff.items() if d in planned}

    # --- login backoff (禁止频繁登陆) -----------------------------------
    def _auth_allowed(self, dev: str, now: float) -> bool:
        bo = self._auth_backoff.get(dev)
        return bo is None or now >= bo["next_ts"]

    def _auth_wait(self, dev: str, now: float) -> int:
        bo = self._auth_backoff.get(dev)
        return max(0, int(bo["next_ts"] - now)) if bo else 0

    def _backoff_after_fail(self, dev: str, now: float) -> float:
        fails = self._auth_backoff.get(dev, {}).get("fails", 0) + 1
        delay = min(_AUTH_BACKOFF_BASE * (2 ** (fails - 1)), _AUTH_BACKOFF_CAP)
        self._auth_backoff[dev] = {"fails": fails, "next_ts": now + delay}
        return delay

    def _backoff_clear(self, dev: str) -> None:
        self._auth_backoff.pop(dev, None)

    def _keep_one(self, spec, glb) -> DialResult:
        """ensure_online() with a throttle: a captive dial whose auth keeps
        failing is retried on an exponential backoff instead of every cycle, so
        the daemon can never hammer the AC into an anti-abuse block. A black-holed
        dial (probe NET_DOWN) is never logged into at all.
        """
        dev = spec.device
        dlr = Dialer(spec, glb, transport=self.transport)
        probe = dlr.probe()
        now = self.clock()
        if probe.status == NetStatus.ONLINE:
            self._backoff_clear(dev)
            return DialResult(device=dev, account_userid=spec.account_userid,
                              online=True, authed=False, detail=probe.detail)
        if probe.status != NetStatus.CAPTIVE:
            # net_down / unknown: nothing to log in to (no auth, no hammer)
            return DialResult(device=dev, account_userid=spec.account_userid,
                              online=False, authed=False, detail=probe.detail)
        if not self._auth_allowed(dev, now):
            return DialResult(device=dev, account_userid=spec.account_userid,
                              online=False, authed=False,
                              detail=f"captive (backoff {self._auth_wait(dev, now)}s)")
        outcome = dlr.authenticate(probe)
        if outcome in (AuthOutcome.SUCCESS, AuthOutcome.ALREADY_ONLINE):
            self._backoff_clear(dev)
            reprobe = dlr.probe()
            online = (reprobe.status == NetStatus.ONLINE
                      or outcome == AuthOutcome.ALREADY_ONLINE)
            return DialResult(device=dev, account_userid=spec.account_userid,
                              online=online, authed=True, outcome=outcome,
                              detail=f"auth {outcome.value}")
        delay = self._backoff_after_fail(dev, now)
        return DialResult(device=dev, account_userid=spec.account_userid,
                          online=False, authed=True, outcome=outcome,
                          detail=f"auth {outcome.value}; backoff {int(delay)}s")

    def keepalive(self, cfg: MultidialConfig, only: Optional[str] = None) -> None:
        specs = self._registered_specs(cfg)
        planned = {s.device for s in specs}
        self.control.prune(list(planned))
        self._prune_state(planned)
        if not cfg.globals.enabled:
            return
        for spec in specs:
            dev = spec.device
            if only is not None and dev != only:
                continue
            ctl = self.control.get(dev)
            self._enforce_admin_state(dev, ctl.stopped)

            if ctl.stopped:
                # deliberately offline: no probe, no auth, excluded from online
                self._set_dial(spec, online=False, authed=False, outcome=None,
                               detail="stopped", stopped=True, mark=ctl.mark)
                self._prev_online[dev] = False
                continue

            try:
                result = self._keep_one(spec, cfg.globals)
            except Exception as e:  # never let one dial kill the loop
                self.log(f"dial {dev} error: {e}")
                continue
            if result.authed and result.outcome and result.outcome.value in _OK:
                self._reauth_count += 1
                st = self._stat(dev)
                st["reauth"] += 1
                st["last_reauth_ts"] = self.clock()
                self.log(f"{dev} ({spec.account_userid}) reauth ok "
                         f"[{result.outcome.value}] #{st['reauth']}")
            self._note_transition(spec, result.online, result.detail)
            self._set_dial(spec, online=result.online, authed=result.authed,
                           outcome=result.outcome.value if result.outcome else None,
                           detail=result.detail, stopped=False, mark=ctl.mark)

    # --- loop -----------------------------------------------------------
    def tick(self) -> bool:
        cfg, raw = self.load_config()
        if cfg.globals.enabled and any(a.enabled and a.dials for a in cfg.accounts):
            if not cfg.globals.track_ips or not cfg.globals.probe_url.strip():
                raise ConfigError("configure track_ips and probe_url before enabling active uplinks")
        self._cfg = cfg
        reconciled = self.reconcile_if_changed(cfg, raw)
        self._provision_sqm(self._registered_specs(cfg), cfg.globals)
        self.keepalive(cfg)
        self._last_tick = self.clock()
        return reconciled

    def run(self) -> None:
        self.log("multidial daemon start")
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:
                self.log(f"tick error: {e}")
            interval = self._cfg.globals.interval if self._cfg else 20
            # Event-driven idle: block until the next keepalive is due OR an API
            # call requests an apply (config change) — no busy-polling, and a
            # config edit takes effect at once instead of waiting out `interval`.
            self._apply_event.wait(interval)
            self._apply_event.clear()

    def request_apply(self) -> None:
        """Ask the run loop to reconcile ASAP, off the caller's thread.

        Config-mutating API calls use this so the HTTP handler returns in
        milliseconds instead of blocking on the multi-second reconcile+keepalive
        (which timed the browser out mid-response -> BrokenPipe, and made a
        successful edit look like it failed). The daemon thread does the work on
        wake; the caller just persisted UCI, so the next status poll shows it.
        """
        self._invalidate_cache()
        self._apply_event.set()

    def request_reconcile(self) -> None:
        """request_apply for edits that only touch mwan3 rules: re-reconcile
        without the forced SQM re-apply (which restarts every dial's shaper)."""
        self._last_config_text = None
        self._apply_event.set()

    def _invalidate_cache(self) -> None:
        """Drop the config cache + force an SQM re-apply on the next tick."""
        self._last_config_text = None
        self._force_sqm_apply = True

    def stop(self) -> None:
        self._stop.set()
        self._apply_event.set()  # wake the idle loop so it exits promptly

    # --- status ---------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            dials = [dict(d) for d in self._dials.values()]
            stats = dict(self._dial_stats)  # snapshot the outer dict under lock
        # Fold each dial's health counters in, and roll them up per account so the
        # UI can rank accounts by drops (answers "哪个号在掉" at a glance).
        rollup: Dict[str, dict] = {}
        for d in dials:
            d.update(stats.get(d["device"], _ZERO_STAT))
            agg = rollup.setdefault(d.get("account_name", ""), {"reauth": 0, "down": 0})
            agg["reauth"] += d["reauth"]
            agg["down"] += d["down"]
        cfg = self._cfg
        accounts = []
        if cfg:
            for a in cfg.accounts:
                agg = rollup.get(a.name, {"reauth": 0, "down": 0})
                accounts.append({
                    "name": a.name,
                    "userid": a.userid,   # only userId is ever exposed, never userName
                    "dials": a.dials,
                    "enabled": a.enabled,
                    "reauth": agg["reauth"],
                    "down": agg["down"],
                })
        online = sum(1 for d in dials if d.get("online") and not d.get("stopped"))
        g = cfg.globals if cfg else None
        return {
            "enabled": cfg.globals.enabled if cfg else False,
            "interval": cfg.globals.interval if cfg else 20,
            "phy_ifname": cfg.globals.phy_ifname if cfg else "",
            "accounts": accounts,
            "dials": sorted(dials, key=lambda d: d["slot"]),
            "aggregate": {"total": len(dials), "online": online},
            # Routing policy — surfaced so the dashboard can explain WHY campus
            # sites stay on one dial while the internet aggregates.
            "policy": {
                "intranet_sticky": g.intranet_sticky if g else False,
                "intranet_ranges": list(g.intranet_ranges) if g else [],
                "sticky_ranges": list(g.sticky_ranges) if g else [],
                "sticky_timeout": g.sticky_timeout if g else 0,
                "dns_rebind_domains": list(g.dns_rebind_domains) if g else [],
            },
            "reconcile_count": self._reconcile_count,
            "reauth_count": self._reauth_count,
            "last_tick": self._last_tick,
        }

    # --- control (used by API) -----------------------------------------
    def force_reload(self) -> bool:
        """Drop the config cache so the next tick re-reconciles (synchronous)."""
        self._invalidate_cache()
        return self.tick()

    def force_reauth(self) -> dict:
        """Re-run keepalive immediately (e.g. user clicked re-auth)."""
        if self._cfg:
            self.keepalive(self._cfg)
        return self.snapshot()

    def reauth_dial(self, device: str) -> dict:
        """Run the keepalive (probe, then login if captive) for ONE dial.

        Same path as force_reauth, so the login backoff and the stopped flag
        still apply: a click can't hammer the AC for a dial it is blocking."""
        self._require_dial(device)
        if self._cfg:
            self.keepalive(self._cfg, only=device)
        return self.snapshot()

    # --- per-dial stop / mark ------------------------------------------
    def _userid_for(self, device: str) -> str:
        with self._lock:
            d = self._dials.get(device)
        return d["account_userid"] if d else ""

    def _require_dial(self, device: str) -> None:
        """Reject anything but a registered dial. The name is spliced into
        `sh -c` commands, and an unknown one would persist a junk control row."""
        if not _DEVICE_RE.match(device or "") or not self.control.get(device).provisioned:
            raise ValueError(f"unknown dial: {device}")

    def stop_dial(self, device: str) -> dict:
        """Stop keeping a dial alive AND take it out of mwan3 routing.

        The online count drops by one: a stopped dial is forced offline and
        excluded from the aggregate. Persisted, so a restart won't resume it.
        """
        self._require_dial(device)
        self.control.set_stopped(device, True)
        self.runner.run(f"mwan3 interface {device} down")
        self._enforced_stopped.add(device)
        with self._lock:
            if device in self._dials:
                self._dials[device]["online"] = False
                self._dials[device]["stopped"] = True
                self._dials[device]["detail"] = "stopped"
        self._prev_online[device] = False
        self.history.record(device, self._userid_for(device), "stopped",
                            self.clock(), self.control.get(device).mark)
        self.log(f"{device} stopped by operator")
        return self.snapshot()

    def start_dial(self, device: str) -> dict:
        """Resume keepalive + put the dial back into mwan3 routing."""
        self._require_dial(device)
        self.control.set_stopped(device, False)
        self.runner.run(f"mwan3 interface {device} up")
        self._enforced_stopped.discard(device)
        with self._lock:
            if device in self._dials:
                self._dials[device]["stopped"] = False
                self._dials[device]["detail"] = "(re)starting"
        self.history.record(device, self._userid_for(device), "started",
                            self.clock(), "")
        self.log(f"{device} started by operator")
        return self.snapshot()

    def mark_dial(self, device: str, mark: str) -> dict:
        """Attach a freeform operator note to a dial (does not stop it)."""
        self._require_dial(device)
        mark = str(mark or "")
        self.control.set_mark(device, mark)
        with self._lock:
            if device in self._dials:
                self._dials[device]["mark"] = mark
        self.history.record(device, self._userid_for(device), "marked",
                            self.clock(), mark)
        self.log(f"{device} marked: {mark}")
        return self.snapshot()

    # --- per-dial decommission (the ONLY delete path) ------------------
    def _sync_account_count(self, account: str) -> None:
        """Write an account's `dials` to its actual registered count so a later
        reconcile won't re-provision a slot we just removed."""
        if not account or not _NAME_RE.match(account):
            return
        n = sum(1 for c in self.control.registered().values()
                if c.account == account)
        self.runner.run(f"uci set multidial.{account}.dials={_sq(str(n))}")
        self.runner.run("uci commit multidial")

    def _teardown_dial(self, device: str) -> None:
        """Remove ONE dial's resources — everything EXCEPT the global re-apply
        (`_apply_dial_teardown`), which the caller batches so removing an N-dial
        account costs ONE mwan3/cake-autorate restart, not N.

        Deleting the UCI sections is not enough on its own: netifd only destroys
        the running macvlan device on a reload, so we apply the delta (`ubus call
        network reload`, the same lightweight path the add side uses) and hard-
        remove the links as a belt-and-suspenders — otherwise the macvlan can
        linger UP holding a DHCP lease, and sqm-scripts can leave the per-dial
        ingress IFB (`ifb4<dial>`) behind after its UCI queue was deleted.
        """
        # Name check first: `_slot_of` only parses device[4:], so "ab;x5" would
        # resolve to slot 5 — tearing down a real sibling and splicing the
        # raw name into the shell commands below.
        if not _DEVICE_RE.match(device or ""):
            raise ValueError(f"invalid dial: {device}")
        c = self.control.get(device)
        slot = c.slot if c.slot >= 0 else self._slot_of(device)
        if slot < 0:
            raise ValueError(f"invalid dial: {device}")
        self.runner.run(f"ifdown {device}")
        for cmd in decommission_sections(slot):
            self.runner.run(cmd)
        for cmd in sqm.deprovision_cmds(device):
            self.runner.run(cmd)
        self.runner.run("ubus call network reload")
        self.runner.run(f"ip link del {device}")
        self.runner.run(f"ip link del {sqm.ifb_name(device)} 2>/dev/null || true")
        self.control.deregister(device)
        self._sync_account_count(c.account)
        with self._lock:
            self._dials.pop(device, None)
            self._dial_stats.pop(device, None)  # under lock: snapshot/prune read it
        self._prev_online.pop(device, None)
        self.history.record(device, c.account, "removed", self.clock(), "")
        self.log(f"{device} decommissioned by operator")
        # account count changed -> let the next tick re-read cleanly
        self._last_config_text = None

    def _teardown_many(self, devices) -> List[str]:
        """Tear down each device (skipping any whose slot can't be resolved),
        WITHOUT the global re-apply. The caller batches `_apply_dial_teardown()`
        once when the returned list is non-empty, so removing N dials costs ONE
        mwan3/SQM restart, not N. Shared by scale-down and account deletion."""
        removed = []
        for dev in devices:
            try:
                self._teardown_dial(dev)
                removed.append(dev)
            except ValueError:
                pass
        return removed

    def _apply_dial_teardown(self) -> None:
        """One-shot re-apply after one or more `_teardown_dial` calls: re-attach
        SQM shapers and rebuild the mwan3 balancer over the surviving members."""
        for cmd in sqm.deprovision_apply_cmds():
            self.runner.run(cmd)
        self.runner.run("/etc/init.d/mwan3 restart")
        self._enforced_stopped.clear()

    def decommission_dial(self, device: str) -> dict:
        """Explicitly remove ONE dial: ifdown, delete its UCI sections, forget it.

        This is the sole single-dial deletion path — lowering the count never
        reaches here. The owning account's `dials` is synced down so the slot
        isn't recreated.
        """
        self._teardown_dial(device)
        self._apply_dial_teardown()
        return self.snapshot()

    # --- per-dial MAC rotation (recover a blocked dial) ----------------
    def rotate_mac(self, device: str) -> dict:
        """Rotate one dial to a fresh MAC the campus AC has never blocked.

        Bumps the persisted rotation counter, rewrites just this dial's macvlan
        MAC, and re-ups only this dial (ifdown -> reload picks up the new MAC ->
        ifup). Siblings are untouched. Persisted, so a reconcile won't revert it.
        """
        c = self.control.get(device)
        if not c.provisioned or c.slot < 0:
            raise ValueError(f"unknown dial: {device}")
        slot = c.slot
        rotation = self.control.bump_rotation(device)
        glb = self._cfg.globals if self._cfg else Globals()
        mac = rotated_macaddr(glb, slot, rotation)
        self.runner.run(f"ifdown {device}")
        self.runner.run(
            f"uci set network.{dev_section_name(slot)}.macaddr={_sq(mac)}")
        self.runner.run("uci commit network")
        self.runner.run("/etc/init.d/network reload")
        self.runner.run(f"ifup {device}")
        with self._lock:
            if device in self._dials:
                self._dials[device]["macaddr"] = mac
                self._dials[device]["detail"] = "MAC rotated"
        self.history.record(device, self._userid_for(device), "rotated",
                            self.clock(), mac)
        self.log(f"{device} MAC rotated -> slot{slot}#rot{rotation}")
        return self.snapshot()

    # --- account CRUD (writes /etc/config/multidial) -------------------
    def add_or_update_account(self, name: str, userid: str, password,
                              dials, enabled=True) -> dict:
        """Create or update a campus account section, then re-reconcile.

        An empty/omitted password leaves the stored one untouched (so the UI can
        edit dials/enabled without re-sending the secret). The password is never
        logged — only userid/dials/enabled are.
        """
        if not _NAME_RE.match(name or "") or name == "globals":
            raise ValueError(f"invalid account name: {name!r}")
        userid = (userid or "").strip()
        if not _USERID_RE.match(userid):
            raise ValueError("invalid or empty userid")
        try:
            dials = int(dials)
        except (TypeError, ValueError):
            raise ValueError("dials must be an integer")
        if dials < 0:
            raise ValueError("dials must be >= 0")
        en = "1" if _truthy(enabled) else "0"
        cmds = [
            f"uci set multidial.{name}=account",
            f"uci set multidial.{name}.userid={_sq(userid)}",
            f"uci set multidial.{name}.dials={_sq(str(dials))}",
            f"uci set multidial.{name}.enabled={_sq(en)}",
        ]
        if password:
            cmds.append(f"uci set multidial.{name}.password={_sq(password)}")
        cmds.append("uci commit multidial")
        for c in cmds:
            self.runner.run(c)
        self.log(f"account saved: {name} userid={userid} dials={dials} enabled={en}")
        self.request_apply()   # reconcile (incl. scale-down) on the daemon thread
        return self.snapshot()

    def delete_account(self, name: str) -> dict:
        if not _NAME_RE.match(name or "") or name == "globals":
            raise ValueError(f"invalid account name: {name!r}")
        # Tear down every dial this account owns FIRST. The reconcile is additive
        # and never prunes, and _registered_specs skips a deleted account's dials
        # — so without an explicit decommission here, deleting an account leaks
        # the dial wholesale: the macvlan netdev stays up holding a DHCP lease,
        # and its network/mwan3/sqm/cake-autorate sections + ip rules + the live
        # cake-autorate loop all linger with nothing left to clean them.
        removed = self._teardown_many(sorted(
            (d for d, c in self.control.registered().items() if c.account == name),
            key=self._slot_of,
        ))
        if removed:
            self._apply_dial_teardown()  # ONE restart for the whole account
        self.runner.run(f"uci -q delete multidial.{name}")
        self.runner.run("uci commit multidial")
        self.log(f"account deleted: {name}")
        self.request_apply()
        return self.snapshot()

    # --- master switch --------------------------------------------------
    def set_power(self, enabled) -> dict:
        """One-click master switch: toggle our keepalive AND mwan3.

        Off => keepalive stops and mwan3 is stopped, so LAN falls back to the
        native single-WAN default route. On => both come back.
        """
        on = _truthy(enabled)
        self.runner.run(f"uci set multidial.globals.enabled={_sq('1' if on else '0')}")
        self.runner.run("uci commit multidial")
        if on:
            self.runner.run("/etc/init.d/mwan3 enable")
            self.runner.run("/etc/init.d/mwan3 start")
        else:
            self.runner.run("/etc/init.d/mwan3 stop")
        self.log(f"power set: {'on' if on else 'off'}")
        self.request_apply()
        return self.snapshot()

    # --- SQM live status (used by API) ---------------------------------
    def sqm_status(self) -> dict:
        """Live per-dial CAKE shaper rates + cake-autorate health.

        Read-only: shells `tc`/`ps`/HNAT-toggle/ECM+macvlan reads through the injected Runner and
        folds them into the `/api/sqm` payload. Rates float as cake-autorate
        adapts, so the UI polls this faster than the keepalive interval.
        """
        cfg = self._cfg
        devices = [s.device for s in self._registered_specs(cfg)] if cfg else []
        enabled = bool(cfg.globals.sqm) if cfg else False
        _, tc_out = self.runner.run(sqm.STATUS_TC_CMD)
        _, ps_out = self.runner.run(sqm.STATUS_PS_CMD)
        _, hnat_out = self.runner.run(sqm.STATUS_HNAT_CMD)
        _, offload_out = self.runner.run(sqm.STATUS_OFFLOAD_CMD)
        phy = cfg.globals.phy_ifname if cfg else ""
        status = sqm.parse_status(devices, tc_out, ps_out, hnat_out, enabled=enabled,
                                  offload_text=offload_out, phy_ifname=phy)
        _, ca_out = self.runner.run(sqm.STATUS_LIMITS_CMD)
        status["limits"] = sqm.parse_limits(devices, ca_out)
        return status

    def traffic(self) -> dict:
        """Cumulative per-dial byte counters (the UI differentiates them)."""
        cfg = self._cfg
        devices = [s.device for s in self._registered_specs(cfg)] if cfg else []
        _, out = self.runner.run(sticky.traffic_cmd(devices))
        return {"ts": float(self.clock()),
                "dials": {d: v for d, v in sticky.parse_traffic(out).items()
                          if d in devices}}

    # --- sticky (used by API) -------------------------------------------
    def _globals(self) -> Globals:
        if self._cfg is None:
            self._cfg = self.load_config()[0]
        return self._cfg.globals

    def _domains_supported(self) -> bool:
        _, out = self.runner.run(sticky.DNSMASQ_CMD)
        return sticky.domains_supported(out)

    def sticky_status(self) -> dict:
        """Sticky config + who is pinned where + whether mwan3 can actually
        restore pins for every online dial (see sticky.rule_health)."""
        _, mwan3_text = self.runner.run("uci show mwan3")
        _, mangle = self.runner.run(sticky.MANGLE_CMD)
        ipsets = {rule: self.runner.run(sticky.ipset_cmd(rule))[1]
                  for rule, _policy in sticky.parse_sticky_rules(mwan3_text)}
        _, dnsmasq = self.runner.run(sticky.DNSMASQ_CMD)
        return sticky.build_status(self._globals(), mwan3_text, mangle, ipsets, dnsmasq)

    def set_sticky(self, data: dict) -> dict:
        """Partial update of the sticky settings (ValueError => HTTP 400).

        Persists to `multidial.globals`; the reconcile that follows rewrites the
        mwan3 rules and restarts mwan3 once if they changed. `sticky_domains` is
        only stored for now."""
        upd = sticky.validate_update(data, self._domains_supported())
        glb = self._globals()
        for key, val in upd.items():
            if key == "sticky_domains":
                self.runner.run("uci -q delete multidial.globals.sticky_domains")
                for dom in val:
                    self.runner.run(
                        f"uci add_list multidial.globals.sticky_domains={_sq(dom)}")
            elif key == "sticky_timeout":
                self.runner.run(f"uci set multidial.globals.{key}={_sq(val)}")
            else:
                self.runner.run(f"uci set multidial.globals.{key}={_sq(' '.join(val))}")
            setattr(glb, key, val)
        self.runner.run("uci commit multidial")
        self.log("sticky settings saved: " + ", ".join(upd))
        self.request_reconcile()
        return self.sticky_status()

    # --- read-only accessors (used by API) -----------------------------
    def log_lines(self, limit: int = 200) -> List[str]:
        return self.log_buffer.lines(limit) if self.log_buffer is not None else []

    def history_events(self, limit: int = 50) -> List[dict]:
        return self.history.recent(limit)
