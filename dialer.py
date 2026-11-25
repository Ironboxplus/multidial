"""Per-dial Ruijie ePortal authentication, bound to the dial's DEVICE.

Egress is pinned with SO_BINDTODEVICE(<device>) — never a source IP. The campus
DHCP lease drifts when auth expires; the macvlan device name and MAC do not, so
device binding is the only stable handle. Each dial authenticates with its own
account's credentials, so multiple campus accounts coexist on one physical link.

The HTTP transport is injected, so the auth state machine is unit-testable
without root or a live network.
"""
from __future__ import annotations

import json
import re
import socket
import http.client
import urllib.parse
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple

from .config import DialSpec, Globals

SO_BINDTODEVICE = 25  # Linux socket option number
SO_MARK = 36          # Linux socket option number (needs CAP_NET_ADMIN)

# mwan3 marks each member's egress with (member_index)*0x100 and installs an
# `ip rule fwmark <mark>/0x3f00 lookup <table>`. Members are generated in slot
# order (m_dial0=0x100->table1, m_dial1=0x200->table2, ...), so a dial's mark is
# (slot+1)*0x100. The probe/login sockets MUST carry this mark: otherwise, when
# mwan3 has marked the dial OFFLINE, the unmarked locally-generated packet hits
# mwan3's fallback rule and is blackholed (EHOSTUNREACH) — so the daemon can
# never reach the portal to re-authenticate an offline dial, a self-sustaining
# deadlock. Stamping the mark routes the packet via the dial's own table
# regardless of mwan3's online/offline verdict.
MWAN3_MARK_STRIDE = 0x100


class NetStatus(str, Enum):
    ONLINE = "online"
    CAPTIVE = "captive"
    NET_DOWN = "net_down"
    UNKNOWN = "unknown"


class AuthOutcome(str, Enum):
    SUCCESS = "success"
    ALREADY_ONLINE = "already_online"
    FAIL_CRED = "fail_cred"
    FAIL_PORTAL = "fail_portal"
    FAIL_NETWORK = "fail_network"
    FAIL_UNKNOWN = "fail_unknown"


_OK_OUTCOMES = (AuthOutcome.SUCCESS, AuthOutcome.ALREADY_ONLINE)


@dataclass
class Probe:
    status: NetStatus
    portal_base: str = ""
    query_string: str = ""
    portal_url: str = ""
    http_code: int = 0
    detail: str = ""


@dataclass
class DialResult:
    device: str
    account_userid: str
    online: bool
    authed: bool = False
    outcome: Optional[AuthOutcome] = None
    detail: str = ""


# --- pure helpers (no I/O) ----------------------------------------------
_CAPTIVE_INDICATORS = ("eportal", "wlanuserip", "wlanacname", "nasip", "interface.do")


def is_captive_body(body: str) -> bool:
    low = (body or "").lower()
    return sum(1 for i in _CAPTIVE_INDICATORS if i in low) >= 2


def parse_captive(body: str, probe_url: str, code: int) -> Probe:
    m = re.search(r"location\.href=['\"]([^'\"]+eportal[^'\"]*)['\"]", body)
    if not m:
        m = re.search(r"(https?://[^\s'\"<>]+eportal/index\.jsp\?[^\s'\"<>]+)", body)
    if not m:
        m = re.search(r"(https?://[^\s'\"<>]+eportal[^\s'\"<>]*)", body)
    if m:
        full = m.group(1)
        base = re.match(r"(https?://[^/]+)", full)
        qs = re.search(r"index\.jsp\?(.+)", full)
        return Probe(
            status=NetStatus.CAPTIVE,
            portal_url=full,
            portal_base=base.group(1) if base else "",
            query_string=qs.group(1) if qs else "",
            http_code=code,
            detail=f"captive via {probe_url}",
        )
    return Probe(status=NetStatus.CAPTIVE, http_code=code, detail="captive, unparsed redirect")


def json_val(text: str, key: str) -> str:
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and key in obj:
            return str(obj[key])
    except (ValueError, TypeError):
        pass
    m = re.search(rf'"{re.escape(key)}"\s*:\s*"([^"]*)"', text or "")
    return m.group(1) if m else ""


def _decode_message(msg: str) -> str:
    if msg and not msg.isascii():
        try:
            return msg.encode("latin1").decode("utf-8")
        except Exception:
            try:
                return msg.encode("latin1").decode("gbk")
            except Exception:
                return msg
    return msg


def classify_auth_result(result_str: str, message: str) -> AuthOutcome:
    if result_str == "success":
        return AuthOutcome.SUCCESS
    message = _decode_message(message or "")
    if any(k in message for k in ("已在线", "already online", "已经在线")):
        return AuthOutcome.ALREADY_ONLINE
    if any(k in message for k in ("不存在", "not exist", "用户名", "密码", "password", "Password")):
        return AuthOutcome.FAIL_CRED
    if any(k in message for k in ("欠费", "余额", "quota", "balance")):
        return AuthOutcome.FAIL_CRED
    if any(k in message for k in ("禁用", "disabled", "locked", "锁定")):
        return AuthOutcome.FAIL_CRED
    return AuthOutcome.FAIL_UNKNOWN


# --- real transport (device-bound) --------------------------------------
class _DeviceBoundConnection(http.client.HTTPConnection):
    def __init__(self, host, device=None, fwmark=0, **kw):
        super().__init__(host, **kw)
        self._device = device
        self._fwmark = fwmark

    def connect(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if self._device:
            s.setsockopt(socket.SOL_SOCKET, SO_BINDTODEVICE, self._device.encode() + b"\0")
        if self._fwmark:
            # Route via the dial's own mwan3 table even when mwan3 thinks the
            # dial is OFFLINE — without this the packet is blackholed and the
            # dial can never re-authenticate (see MWAN3_MARK_STRIDE note).
            s.setsockopt(socket.SOL_SOCKET, SO_MARK, self._fwmark)
        if self.timeout is not None:
            s.settimeout(self.timeout)
        s.connect((self.host, self.port))
        self.sock = s


class DeviceBoundTransport:
    """HTTP GET/POST pinned to a device via SO_BINDTODEVICE (needs CAP_NET_RAW)."""

    def _request(self, method, url, device, timeout, body=None, headers=None, fwmark=0):
        u = urllib.parse.urlsplit(url)
        port = u.port or (443 if u.scheme == "https" else 80)
        path = u.path + (("?" + u.query) if u.query else "")
        conn = _DeviceBoundConnection(
            u.hostname, device=device, fwmark=fwmark, port=port, timeout=timeout
        )
        try:
            conn.request(method, path or "/", body=body, headers=headers or {})
            resp = conn.getresponse()
            data = resp.read().decode("utf-8", errors="replace")
            return resp.status, data
        finally:
            conn.close()

    def _error_detail(self, exc: Exception) -> str:
        cls = exc.__class__.__name__
        msg = str(exc).strip()
        if isinstance(exc, socket.gaierror):
            return f"dns error: {msg or cls}"
        if isinstance(exc, socket.timeout):
            return "timeout"
        if isinstance(exc, TimeoutError):
            return "timeout"
        if isinstance(exc, PermissionError):
            return f"permission error: {msg or cls}"
        if isinstance(exc, OSError):
            return f"os error {getattr(exc, 'errno', '')}: {msg or cls}".strip()
        return f"{cls}: {msg}" if msg else cls

    def get(self, url, device, timeout=6, fwmark=0):
        try:
            return self._request("GET", url, device, timeout, fwmark=fwmark)
        except Exception as exc:
            return (0, "", self._error_detail(exc))

    def post(self, url, fields, device, timeout=10, fwmark=0):
        body = urllib.parse.urlencode(fields).encode("utf-8")
        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "User-Agent": "MultidialAuth/1.0",
        }
        try:
            return self._request("POST", url, device, timeout, body=body, headers=headers, fwmark=fwmark)
        except Exception as exc:
            return (0, "", self._error_detail(exc))


# --- the dialer ---------------------------------------------------------
class Dialer:
    def __init__(self, spec: DialSpec, glb: Globals, transport=None):
        self.spec = spec
        self.glb = glb
        self.transport = transport or DeviceBoundTransport()
        self._last_auth_error_detail = ""
        # This dial's mwan3 egress fwmark — stamped on every probe/login socket
        # so they route via the dial's own table even while mwan3 deems the dial
        # offline (see MWAN3_MARK_STRIDE: without it, re-auth deadlocks).
        self.fwmark = (spec.slot + 1) * MWAN3_MARK_STRIDE

    def _http_result(self, result) -> Tuple[int, str, str]:
        if not isinstance(result, tuple):
            return 0, "", f"bad transport result: {type(result).__name__}"
        if len(result) == 2:
            code, body = result
            return int(code or 0), body or "", ""
        if len(result) >= 3:
            code, body, detail = result[:3]
            return int(code or 0), body or "", str(detail or "")
        return 0, "", "empty transport result"

    def _no_response_detail(self, transport_detail: str) -> str:
        return f"no response: {transport_detail}" if transport_detail else "no response"

    def probe(self) -> Probe:
        code, body, transport_detail = self._http_result(self.transport.get(
            self.glb.probe_url, device=self.spec.device, timeout=6, fwmark=self.fwmark
        ))
        if code == 204:
            return Probe(status=NetStatus.ONLINE, http_code=code, detail="HTTP 204")
        if code in (200, 301, 302) and is_captive_body(body):
            return parse_captive(body, self.glb.probe_url, code)
        if code == 200:
            return Probe(status=NetStatus.ONLINE, http_code=code, detail="HTTP 200 genuine")
        if code == 0:
            return Probe(
                status=NetStatus.NET_DOWN,
                http_code=0,
                detail=self._no_response_detail(transport_detail),
            )
        return Probe(status=NetStatus.UNKNOWN, http_code=code, detail=f"HTTP {code}")

    def authenticate(self, probe: Probe) -> AuthOutcome:
        self._last_auth_error_detail = ""
        portal_base = probe.portal_base or self.glb.portal_base
        if not portal_base or not probe.query_string:
            return AuthOutcome.FAIL_PORTAL
        login_url = f"{portal_base}/eportal/InterFace.do?method=login"
        fields = {
            "userId": self.spec.account_userid,
            "password": self.spec.password,
            "service": "",
            "queryString": probe.query_string,
            "operatorPwd": "",
            "operatorUserId": "",
            "validcode": "",
            "passwordEncrypt": "false",
        }
        code, raw, transport_detail = self._http_result(self.transport.post(
            login_url, fields, device=self.spec.device, timeout=10, fwmark=self.fwmark
        ))
        if code == 0:
            self._last_auth_error_detail = self._no_response_detail(transport_detail)
            return AuthOutcome.FAIL_NETWORK
        return classify_auth_result(json_val(raw, "result"), json_val(raw, "message"))

    def ensure_online(self) -> DialResult:
        """Probe; if captive, authenticate with this dial's account; re-probe."""
        probe = self.probe()
        if probe.status == NetStatus.ONLINE:
            return DialResult(
                device=self.spec.device, account_userid=self.spec.account_userid,
                online=True, authed=False, detail=probe.detail,
            )
        if probe.status != NetStatus.CAPTIVE:
            return DialResult(
                device=self.spec.device, account_userid=self.spec.account_userid,
                online=False, authed=False, detail=probe.detail,
            )
        outcome = self.authenticate(probe)
        if outcome in _OK_OUTCOMES:
            reprobe = self.probe()
            online = reprobe.status == NetStatus.ONLINE or outcome == AuthOutcome.ALREADY_ONLINE
            return DialResult(
                device=self.spec.device, account_userid=self.spec.account_userid,
                online=online, authed=True, outcome=outcome,
                detail=f"auth {outcome.value}",
            )
        return DialResult(
            device=self.spec.device, account_userid=self.spec.account_userid,
            online=False, authed=True, outcome=outcome,
            detail=(
                f"auth {outcome.value}: {self._last_auth_error_detail}"
                if outcome == AuthOutcome.FAIL_NETWORK and self._last_auth_error_detail
                else f"auth {outcome.value}"
            ),
        )
