"""Per-dial SQM (CAKE + cake-autorate) provisioning and live status.

Every managed dial gets its own CAKE shaper (via sqm-scripts) plus a
cake-autorate instance that *floats* the shaper rate by measured latency — so
bufferbloat stays controlled on each campus uplink independently, and a
congested dial backs off without dragging the others down.

Like `uci_sync`, everything here is a pure function: the command builders return
shell command lists, and the status reader is split into a runner-fed raw fetch
(in the daemon) and a pure parser here, so the whole module is unit-tested
off-router.

CRITICAL: CAKE only sees packets on the *software* forwarding path. MediaTek
HNAT (hardware offload) binds heavy flows into the PPE and forwards them past
the qdisc, so SQM provisioning forces HNAT off (`hook_toggle=0`). The status
reader surfaces `hnat_offload` so the UI can warn when shaping is being
bypassed. See the dial-sqm-cake-autorate project note.

On a Qualcomm NSS router (AX9000) the same per-dial CAKE runs on the CPU, and
the two NSS paths are what can bypass or fight it:
  - ECM offloads a flow into NSS only through a macvlan in *private* mode;
    netifd's default (vepa, what multidial creates) is never offloaded, so the
    dial traffic stays on the CPU path where CAKE sees it.
  - NSS qdiscs (nss-zk.qos) cannot shape a dial at all — nss_qdisc and
    nssmirred refuse a netdev unknown to the NSS firmware, and a macvlan is
    one — but on the physical WAN below the dials they would shape every dial
    as one aggregate, under the per-dial CAKE.
`offload_warnings` reports either condition; nothing here changes them.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from .config import DialSpec, Globals

# --- default shaper bounds (kbit/s), per dial ---------------------------------
# A campus dial is policed to ~100 Mbit; cake-autorate floats the real rate
# between MIN (the congested floor) and MAX (the idle-latency ceiling), starting
# at BASE. MIN = 3.6Mbit * 0.8 keeps a fully-congested dial usable.
DEFAULT_MAX_KBPS = 100000
DEFAULT_BASE_KBPS = 50000
DEFAULT_MIN_KBPS = 2880
# Below this throughput cake-autorate idles (stops pinging) to save CPU.
CONNECTION_ACTIVE_THR_KBPS = 2000
# cake-autorate's upstream recovery is approximately +1% per adjustment
# second, using integer truncation on every step. Starting at the current 2880
# kbit/s floor, returning to 50000 kbit/s takes 289 seconds. Its
# upstream 60-second idle sleep therefore freezes a lightly used dial far
# below BASE.  Preserve sleep/CPU savings, but only after sufficient time to
# recover and a deliberately conservative observation margin.
RECOVERY_GAIN_PER_SECOND = 0.01
IDLE_RECOVERY_SAFETY_S = 120
# cake-autorate's UCI validator declares this option as a float.  Keep the
# literal float through command generation: its procd launcher rejects `420`
# but accepts `420.0` (even though they represent the same number).
MIN_SUSTAINED_IDLE_SLEEP_S = 420.0
# Min seconds between liveness heals (a cake-autorate bounce to respawn a dead
# per-dial loop). A dial whose loop keeps dying — e.g. its iface can't come up,
# so cake-autorate's verify_ifs_up keeps failing — must not bounce the healthy
# siblings' shaping every tick; the cooldown caps the churn.
SQM_HEAL_COOLDOWN = 300.0
# pure-ethernet macvlan dials (no PPPoE): standard ethernet overhead.
SQM_OVERHEAD = 38
# Per-host fairness behind the NAT. A dial's egress sees every LAN host as the
# dial's own address and its ingress IFB sees the packets before de-NAT, so
# CAKE's default `triple-isolate nonat` isolates nothing between hosts. `nat`
# makes CAKE look the real addresses up in conntrack. sqm-scripts' run.sh hands
# these to piece_of_cake.qos as-is; the qdisc_advanced flags only keep LuCI from
# dropping the hidden options when someone saves the SQM page.
SQM_EGRESS_OPTS = "nat dual-srchost"
SQM_INGRESS_OPTS = "nat dual-dsthost"

HNAT_TOGGLE = "/sys/kernel/debug/hnat/hook_toggle"

_CAKE_AUTORATE_KILL_CMD = (
    "for pid in $(ps w | awk '/[c]ake-autorate\\.sh .*config\\./ {print $1}'); "
    'do kill "$pid" 2>/dev/null || true; done'
)
_CAKE_AUTORATE_LAUNCHER_KILL_CMD = (
    "for pid in $(ps w | awk '/[l]auncher\\.sh/ {print $1}'); "
    'do kill "$pid" 2>/dev/null || true; done'
)
_CAKE_AUTORATE_FPING_KILL_CMD = (
    "for pid in $(ps w | awk '/[f]ping -I dial/ {print $1}'); "
    'do kill "$pid" 2>/dev/null || true; done'
)
_CAKE_AUTORATE_RUN_DIR = "/var/run/cake-autorate"
# Between the stop/kill and the fresh start, WAIT for every stopped instance to
# fully exit (and finish its own `rm -r ${run_path}` on the way out), then sweep
# any leftover per-dial run dirs. Without this, the launcher backgrounds fresh
# per-dial instances immediately and a new instance whose predecessor's run dir
# still exists with a live main pid self-terminates ("run_path already exists and
# an instance appears to be running — Exiting"). That race silently drops exactly
# one dial's cake-autorate loop on every restart (its CAKE shaper then freezes at
# base rate with no latency floating). Bounded to ~8s so a wedged process can't
# hang the restart; the sweep guarantees the fresh start begins from a clean slate.
_CAKE_AUTORATE_SETTLE_CMD = (
    "i=0; while [ $i -lt 8 ] && ps w 2>/dev/null "
    "| grep -a '[c]ake-autorate\\.sh .*config\\.' >/dev/null 2>&1; "
    "do sleep 1; i=$((i+1)); done; "
    f"rm -rf {_CAKE_AUTORATE_RUN_DIR}/* 2>/dev/null || true"
)


def ifb_name(device: str) -> str:
    """sqm-scripts names the ingress IFB for an interface `ifb4<iface>`."""
    return f"ifb4{device}"


def _sq(value) -> str:
    """Single-quote a value for safe `sh -c` interpolation."""
    return "'" + str(value).replace("'", "'\\''") + "'"


def recovery_steps_to_base(
    min_kbps: int,
    base_kbps: int,
    gain_per_second: float = RECOVERY_GAIN_PER_SECOND,
) -> int:
    """Whole upstream integer-truncated +gain steps from min -> base.

    This is intentionally a pure policy calculation.  The actual deployment
    and runtime evidence is collected by ``tests/live/cake_autorate_idle_check.sh``
    against a router; this function keeps the emitted UCI setting correct when
    per-dial bounds change in code.
    """
    if min_kbps <= 0 or base_kbps <= 0:
        raise ValueError("shaper rates must be positive")
    if not 0 < gain_per_second:
        raise ValueError("gain_per_second must be positive")
    if base_kbps <= min_kbps:
        return 0
    # cake-autorate.sh applies the default recovery exactly as
    # `(rate * 1010) / 1000` in shell integer arithmetic. Repeating a
    # continuous logarithm undercounts because every individual increase is
    # truncated. Keep the parameter for policy flexibility, but quantize it to
    # the same per-mille integer arithmetic as the upstream implementation.
    multiplier_per_mille = round((1 + gain_per_second) * 1000)
    if multiplier_per_mille <= 1000:
        raise ValueError("gain_per_second cannot advance the integer shaper rate")
    rate = min_kbps
    steps = 0
    while rate < base_kbps:
        next_rate = (rate * multiplier_per_mille) // 1000
        if next_rate <= rate:
            raise ValueError("integer shaper recovery makes no progress")
        rate = next_rate
        steps += 1
    return steps


def required_sustained_idle_sleep_s(min_kbps: int, base_kbps: int) -> float:
    """Safe cake-autorate idle-sleep threshold for these shaper bounds."""
    # `max` would otherwise return an int whenever the calculated recovery
    # window exceeds the 420.0 floor.  cake-autorate validates this UCI option
    # as a float, so preserve a float literal for *all* future shaper bounds.
    return float(max(
        MIN_SUSTAINED_IDLE_SLEEP_S,
        recovery_steps_to_base(min_kbps, base_kbps) + IDLE_RECOVERY_SAFETY_S,
    ))


SUSTAINED_IDLE_SLEEP_THR_S = required_sustained_idle_sleep_s(
    DEFAULT_MIN_KBPS, DEFAULT_BASE_KBPS
)


# --- command builders ---------------------------------------------------------
def sqm_section_cmds(spec: DialSpec, glb: Globals) -> List[str]:
    """sqm-scripts `config queue` for one dial — the CAKE plumbing.

    download/upload are just the CEILINGS (= per-dial campus cap); cake-autorate
    floats the live rate within its own min/base/max below this.
    """
    d = spec.device
    return [
        f"uci set sqm.{d}=queue",
        f"uci set sqm.{d}.interface={_sq(d)}",
        f"uci set sqm.{d}.qdisc='cake'",
        f"uci set sqm.{d}.script='piece_of_cake.qos'",
        f"uci set sqm.{d}.qdisc_advanced='1'",
        f"uci set sqm.{d}.qdisc_really_really_advanced='1'",
        f"uci set sqm.{d}.eqdisc_opts={_sq(SQM_EGRESS_OPTS)}",
        f"uci set sqm.{d}.iqdisc_opts={_sq(SQM_INGRESS_OPTS)}",
        f"uci set sqm.{d}.linklayer='ethernet'",
        f"uci set sqm.{d}.overhead={_sq(SQM_OVERHEAD)}",
        f"uci set sqm.{d}.download={_sq(DEFAULT_MAX_KBPS)}",
        f"uci set sqm.{d}.upload={_sq(DEFAULT_MAX_KBPS)}",
        f"uci set sqm.{d}.enabled='1'",
    ]


def cakeautorate_section_cmds(spec: DialSpec, glb: Globals) -> List[str]:
    """cake-autorate UCI section for one dial — the latency closed loop.

    Each instance pings reflectors bound to *its own* dial (`-I dialN`,
    mandatory in a multi-homed setup) and adjusts the CAKE rate on that dial's
    egress + ingress-IFB.
    """
    d = spec.device
    return [
        f"uci set cake-autorate.{d}=cake_autorate",
        f"uci set cake-autorate.{d}.enabled='1'",
        f"uci set cake-autorate.{d}.dl_if={_sq(ifb_name(d))}",
        f"uci set cake-autorate.{d}.ul_if={_sq(d)}",
        f"uci set cake-autorate.{d}.adjust_dl_shaper_rate='1'",
        f"uci set cake-autorate.{d}.adjust_ul_shaper_rate='1'",
        f"uci set cake-autorate.{d}.min_dl_shaper_rate_kbps={_sq(DEFAULT_MIN_KBPS)}",
        f"uci set cake-autorate.{d}.base_dl_shaper_rate_kbps={_sq(DEFAULT_BASE_KBPS)}",
        f"uci set cake-autorate.{d}.max_dl_shaper_rate_kbps={_sq(DEFAULT_MAX_KBPS)}",
        f"uci set cake-autorate.{d}.min_ul_shaper_rate_kbps={_sq(DEFAULT_MIN_KBPS)}",
        f"uci set cake-autorate.{d}.base_ul_shaper_rate_kbps={_sq(DEFAULT_BASE_KBPS)}",
        f"uci set cake-autorate.{d}.max_ul_shaper_rate_kbps={_sq(DEFAULT_MAX_KBPS)}",
        f"uci set cake-autorate.{d}.connection_active_thr_kbps={_sq(CONNECTION_ACTIVE_THR_KBPS)}",
        # Do not inherit cake-autorate's 60 s default: that expires long before
        # a 1%/s min -> base recovery completes and freezes an idle dial at a
        # few Mbit/s.  Keep sleep enabled, with an explicit safe threshold.
        f"uci set cake-autorate.{d}.enable_sleep_function='1'",
        f"uci set cake-autorate.{d}.sustained_idle_sleep_thr_s="
        f"{_sq(SUSTAINED_IDLE_SLEEP_THR_S)}",
        # bind this instance's reflector pings to its own dial (fping -I)
        f"uci set cake-autorate.{d}.ping_extra_args={_sq('-I ' + d)}",
        # SUMMARY log lines (off by default) are what the LuCI status view
        # (luci-app-cake-autorate-external) reads its rates and charts from.
        f"uci set cake-autorate.{d}.output_summary_stats='1'",
    ]


def provision_cmds(specs: List[DialSpec], glb: Globals) -> List[str]:
    """UCI commands to (idempotently) configure SQM + cake-autorate for `specs`.

    `uci set` is idempotent, so re-running for an already-provisioned dial is a
    harmless no-op — the daemon gates on a per-dial `sqm_provisioned` flag to
    avoid needless service restarts, not for correctness.
    """
    cmds: List[str] = []
    for spec in specs:
        cmds.extend(sqm_section_cmds(spec, glb))
        cmds.extend(cakeautorate_section_cmds(spec, glb))
    if specs:
        cmds.append("uci commit sqm")
        cmds.append("uci commit cake-autorate")
    return cmds


def apply_cmds() -> List[str]:
    """Bring provisioned SQM live: (re)start sqm so each queue gets its CAKE
    qdisc, then (re)start cake-autorate so it manages the new sections.

    HNAT/offload is deliberately NOT touched here. It is kept off persistently
    as a system setting (turboacc `fastpath_mh_eth_hnat=0`), so the daemon never
    toggles it — `parse_status` only *reports* HNAT state (`hnat_offload`) so the
    UI can warn if it is ever re-enabled out from under SQM.
    """
    return [
        "/etc/init.d/sqm enable 2>/dev/null",
        "/etc/init.d/sqm restart",
        *cakeautorate_restart_cmds(enable=True),
    ]


def deprovision_cmds(device: str) -> List[str]:
    """UCI commands to remove ONE dial's SQM + cake-autorate sections."""
    return [
        f"uci -q delete sqm.{device}",
        f"uci -q delete cake-autorate.{device}",
        "uci commit sqm",
        "uci commit cake-autorate",
    ]


def deprovision_apply_cmds() -> List[str]:
    """Reload the shapers after a dial's sections were removed (drop its qdisc)."""
    return [
        "/etc/init.d/sqm restart",
        *cakeautorate_restart_cmds(enable=False),
    ]


def cakeautorate_restart_cmds(enable: bool = False) -> List[str]:
    """Cleanly restart cake-autorate, including stale child loops.

    The OpenWrt service starts a bash launcher that backgrounds one
    `cake-autorate.sh .../config.<dial>.sh` child per dial, and each child may
    show up as several bash worker processes in `ps`. This restart path is
    deliberately scoped to a full service restart: stop procd, reap known
    cake-autorate/launcher/fping process shapes, WAIT for them to fully exit and
    sweep stale run dirs (see `_CAKE_AUTORATE_SETTLE_CMD` — without it, one dial's
    fresh instance races its predecessor's still-present run dir and self-exits),
    then start one fresh launcher.
    """
    cmds: List[str] = []
    if enable:
        cmds.append("/etc/init.d/cake-autorate enable 2>/dev/null")
    cmds.extend([
        "/etc/init.d/cake-autorate stop 2>/dev/null || true",
        _CAKE_AUTORATE_KILL_CMD,
        _CAKE_AUTORATE_LAUNCHER_KILL_CMD,
        _CAKE_AUTORATE_FPING_KILL_CMD,
        _CAKE_AUTORATE_SETTLE_CMD,
        "/etc/init.d/cake-autorate start",
    ])
    return cmds


# --- live status parsing ------------------------------------------------------
# A CAKE qdisc header line, e.g.
#   qdisc cake 800d: dev dial0 root refcnt 2 bandwidth 50Mbit besteffort ...
# or, once cake-autorate has floated it,
#   qdisc cake 8011: dev ifb4dial0 root refcnt 2 bandwidth 92177Kbit ...
_QHDR_RE = re.compile(r"^qdisc cake \S+ dev (\S+).*?\bbandwidth (\S+)")
_DROPPED_RE = re.compile(r"\(dropped (\d+),")
_BACKLOG_RE = re.compile(r"^\s+backlog \S+ (\d+)p")
_RATE_RE = re.compile(r"^([\d.]+)([KMG]?)bit$")
_AUTORATE_RE = re.compile(r"config\.(\w+)\.sh")

_RATE_UNIT = {"": 0.001, "K": 1.0, "M": 1000.0, "G": 1000000.0}


def _to_kbit(token: str) -> Optional[int]:
    """'50Mbit' -> 50000, '92177Kbit' -> 92177, 'unlimited' -> None."""
    m = _RATE_RE.match(token.strip())
    if not m:
        return None
    return int(float(m.group(1)) * _RATE_UNIT[m.group(2)])


def parse_tc_rates(text: str) -> Dict[str, dict]:
    """Parse `tc -s qdisc show` into device -> {bandwidth_kbit, dropped, backlog_pkt}.

    Only CAKE qdiscs are tracked; a following non-CAKE qdisc block ends the
    current device's stat window.
    """
    out: Dict[str, dict] = {}
    cur: Optional[dict] = None
    for line in (text or "").splitlines():
        hdr = _QHDR_RE.match(line)
        if hdr:
            cur = {
                "bandwidth_kbit": _to_kbit(hdr.group(2)),
                "dropped": None,
                "backlog_pkt": None,
            }
            out[hdr.group(1)] = cur
            continue
        if line.startswith("qdisc"):
            cur = None  # a different (non-cake) qdisc — stop attaching stats
            continue
        if cur is None:
            continue
        d = _DROPPED_RE.search(line)
        if d and cur["dropped"] is None:
            cur["dropped"] = int(d.group(1))
        b = _BACKLOG_RE.match(line)
        if b and cur["backlog_pkt"] is None:
            cur["backlog_pkt"] = int(b.group(1))
    return out


def parse_autorate_pids(text: str) -> set:
    """Devices with live cake-autorate activity, from `ps | grep cake-autorate`.

    A single `cake-autorate.sh .../config.<section>.sh` instance can spawn
    multiple bash workers that appear as repeated `ps` lines with the same
    config. Do not infer duplicate top-level instances from line counts here;
    the set only answers whether that dial has autorate running.
    """
    return {m.group(1) for m in _AUTORATE_RE.finditer(text or "")}


def hnat_is_on(text: str) -> bool:
    """`/sys/kernel/debug/hnat/hook_toggle` -> True when HNAT bypasses CAKE.

    The file reads 'enabled'/'disabled' (newer) or '1'/'0' (older); an empty
    read means no HNAT debugfs (offload absent) -> treat as off.
    """
    return (text or "").strip().lower() in ("1", "enabled", "enable", "on", "true")


_ECM_RE = re.compile(r"^ecm=1$", re.M)
# `ip -o -d link show type macvlan`, one device per line, e.g.
#   16: dial0@eth1: <BROADCAST,...> mtu 1500 ... \    macvlan mode vepa bcqueuelen ...
_MACVLAN_RE = re.compile(r"^\d+:\s+([^@:\s]+)(?:@\S+)?:.*?\bmacvlan mode (\S+)", re.M)
# Any NSS qdisc header from `tc qdisc show`, e.g. `qdisc nsstbl 1: dev wan root ...`.
_NSS_QDISC_RE = re.compile(r"^qdisc nss\w+ \S+ dev (\S+)", re.M)


def parse_offload(text: str) -> tuple:
    """`STATUS_OFFLOAD_CMD` output -> (ECM loaded?, {macvlan device: mode})."""
    text = text or ""
    return bool(_ECM_RE.search(text)), dict(_MACVLAN_RE.findall(text))


def nss_shaped_devices(tc_text: str) -> set:
    """Devices carrying an NSS qdisc (nss-zk.qos and friends)."""
    return set(_NSS_QDISC_RE.findall(tc_text or ""))


def offload_warnings(devices: List[str], tc_text: str, offload_text: str,
                     phy_ifname: str) -> List[str]:
    """Conditions under which per-dial CAKE is bypassed or double-shaped.

    Only reported, never fixed: both are deliberate system settings that the
    operator owns (see the module docstring).
    """
    ecm, modes = parse_offload(offload_text)
    warnings = []
    private = [d for d in devices if modes.get(d) == "private"]
    if ecm and private:
        warnings.append(
            f"{', '.join(private)} 是 private 模式的 macvlan：ECM 会把它的流量交给 NSS 转发，"
            "CAKE 被绕过。拨号口应保持默认的 vepa 模式。")
    if phy_ifname:
        shaped = nss_shaped_devices(tc_text) & {phy_ifname, f"ifb@{phy_ifname}"}
        if shaped:
            warnings.append(
                f"物理 WAN 口 {phy_ifname} 上挂着 NSS SQM（{', '.join(sorted(shaped))}）："
                "所有拨号会在逐路 CAKE 之外再被它合并整形一次。多拨时应关闭该口的 SQM。")
    return warnings


def parse_status(
    devices: List[str],
    tc_text: str,
    ps_text: str,
    hnat_text: str,
    enabled: bool = True,
    offload_text: str = "",
    phy_ifname: str = "",
) -> dict:
    """Assemble the `/api/sqm` payload from raw `tc`/`ps`/HNAT/offload reads.

    For each managed dial: upload shaping lives on `dialN`, download shaping on
    its ingress IFB `ifb4dialN`. `cake` = a CAKE qdisc exists on either side;
    `autorate` = the cake-autorate loop for that dial is running.
    """
    rates = parse_tc_rates(tc_text)
    autorate_devs = parse_autorate_pids(ps_text)
    _, modes = parse_offload(offload_text)
    dials = []
    for dev in devices:
        ul = rates.get(dev)
        dl = rates.get(ifb_name(dev))
        dials.append({
            "device": dev,
            "macvlan_mode": modes.get(dev),
            "cake": bool(ul) or bool(dl),
            "autorate": dev in autorate_devs,
            "ul_kbit": ul.get("bandwidth_kbit") if ul else None,
            "dl_kbit": dl.get("bandwidth_kbit") if dl else None,
            "ul_dropped": ul.get("dropped") if ul else None,
            "dl_dropped": dl.get("dropped") if dl else None,
            "ul_backlog_pkt": ul.get("backlog_pkt") if ul else None,
            "dl_backlog_pkt": dl.get("backlog_pkt") if dl else None,
        })
    active = sum(1 for d in dials if d["cake"] and d["autorate"])
    return {
        "enabled": enabled,
        "hnat_offload": hnat_is_on(hnat_text),
        "offload_warnings": offload_warnings(devices, tc_text, offload_text, phy_ifname),
        "dials": dials,
        "aggregate": {
            "total": len(dials),
            "active": active,
        },
    }


_LIMIT_OPTS = {
    "dl_min": ("min_dl_shaper_rate_kbps", DEFAULT_MIN_KBPS),
    "dl_base": ("base_dl_shaper_rate_kbps", DEFAULT_BASE_KBPS),
    "dl_max": ("max_dl_shaper_rate_kbps", DEFAULT_MAX_KBPS),
    "ul_min": ("min_ul_shaper_rate_kbps", DEFAULT_MIN_KBPS),
    "ul_base": ("base_ul_shaper_rate_kbps", DEFAULT_BASE_KBPS),
    "ul_max": ("max_ul_shaper_rate_kbps", DEFAULT_MAX_KBPS),
}


def parse_limits(devices: List[str], uci_text: str) -> Dict[str, dict]:
    """Per-dial cake-autorate floor/base/ceiling (kbit/s) from `uci show
    cake-autorate`; a missing or non-numeric option falls back to the defaults
    provisioning writes."""
    found: Dict[str, Dict[str, int]] = {}
    for raw in (uci_text or "").splitlines():
        lhs, sep, rhs = raw.strip().partition("=")
        parts = lhs.split(".")
        if not sep or len(parts) != 3 or parts[0] != "cake-autorate":
            continue
        val = rhs.strip().strip("'")
        if val.isdigit():
            found.setdefault(parts[1], {})[parts[2]] = int(val)
    return {
        dev: {key: found.get(dev, {}).get(opt, default)
              for key, (opt, default) in _LIMIT_OPTS.items()}
        for dev in devices
    }


# shell snippets the daemon feeds to its injected Runner for live status.
STATUS_TC_CMD = "tc -s qdisc show"
STATUS_PS_CMD = "ps w 2>/dev/null | grep -a cake-autorate.sh | grep -av grep"
STATUS_LIMITS_CMD = "uci -q show cake-autorate"
STATUS_HNAT_CMD = f"cat {HNAT_TOGGLE} 2>/dev/null"
STATUS_OFFLOAD_CMD = (
    "[ -d /sys/module/ecm ] && echo ecm=1; "
    "ip -o -d link show type macvlan 2>/dev/null"
)
