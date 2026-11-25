"""Composition root: wire the real subprocess Runner + device-bound transport
into the Daemon, run its loop in a background thread, and serve the HTTP API.

  python3 -m multidial            # on the router (needs root for SO_BINDTODEVICE)

Env: MULTIDIAL_HOST (default 127.0.0.1), MULTIDIAL_PORT (default 23334),
     MULTIDIAL_CONTROL (default /etc/multidial/control.json — persisted stop/mark),
     MULTIDIAL_LOG (default /var/log/multidial.log — tmpfs, size-rotated).
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading

from .daemon import Daemon
from .dialer import DeviceBoundTransport
from .state import ControlStore, History, LogBuffer
from . import api


class SubprocessRunner:
    """Runs a shell command, returns (returncode, stdout)."""

    def run(self, cmd: str):
        try:
            p = subprocess.run(
                ["sh", "-c", cmd],
                capture_output=True, text=True, timeout=40,
            )
            return p.returncode, p.stdout
        except Exception as e:  # timeout / OSError -> treat as failure
            return 1, f"{e}"


def _log(*a):
    print("[multidial]", *a, flush=True)


def main() -> int:
    host = os.environ.get("MULTIDIAL_HOST", "127.0.0.1")
    port = int(os.environ.get("MULTIDIAL_PORT", "23334"))
    control_path = os.environ.get("MULTIDIAL_CONTROL", "/etc/multidial/control.json")
    log_path = os.environ.get("MULTIDIAL_LOG", "/var/log/multidial.log")

    daemon = Daemon(
        runner=SubprocessRunner(),
        transport=DeviceBoundTransport(),
        logger=_log,
        control=ControlStore(path=control_path),
        history=History(capacity=200),
        log_buffer=LogBuffer(path=log_path, capacity=500, max_bytes=256 * 1024, backups=1),
    )
    threading.Thread(target=daemon.run, name="multidial-daemon", daemon=True).start()
    _log(f"API on http://{host}:{port}")
    try:
        api.serve(daemon, host, port)
    except KeyboardInterrupt:
        daemon.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
