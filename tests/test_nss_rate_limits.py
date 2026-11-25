"""Real Bash processes at the adapter's tc process boundary.

The recording executable injects command failures without loading a kernel NSS
module. Actual kernel packet tests live in test_nss_network.py.
"""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

BASH = shutil.which("bash")
ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not BASH, reason="Bash required")


def invoke(tmp_path, rate, ceiling="30000", tc_status=0):
    executable = tmp_path / "tc"
    executable.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$TC_RECORD"\nexit "$TC_STATUS"\n'
    )
    executable.chmod(0o755)
    record = tmp_path / "calls"
    env = dict(os.environ, PATH=f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
               TC_RECORD=str(record), TC_STATUS=str(tc_status))
    script = f"""
source tools/nss-class-shaper.sh
declare -A shaper_rate_kbps=([dl]={rate} [ul]=10000)
declare -A last_shaper_rate_kbps=([dl]=20000 [ul]=10000)
declare -A adjust_shaper_rate=([dl]=1 [ul]=0)
declare -A compensated_owd_delta_thr_us compensated_avg_owd_delta_thr_us
dl_max_wire_packet_size_bits=12000; ul_max_wire_packet_size_bits=12000
dl_owd_delta_thr_us=10000; ul_owd_delta_thr_us=10000
dl_avg_owd_delta_thr_us=20000; ul_avg_owd_delta_thr_us=20000
md_nss_dl_device=ifb-test; md_nss_ul_device=eth-test; md_nss_leaf=100
md_nss_dl_ceiling_kbit='{ceiling}'; md_nss_ul_ceiling_kbit=10000
log_msg() {{ printf '%s\\n' "$*" >&2; }}
set_shaper_rate dl
status=$?
printf '%s %s' "$status" "${{last_shaper_rate_kbps[dl]}}"
"""
    result = subprocess.run([BASH, "-c", script], cwd=ROOT, env=env,
                            text=True, capture_output=True, timeout=5)
    return result, record.read_text() if record.exists() else ""


@pytest.mark.parametrize("rate,ceiling", [(30001, "30000"), (25000, ""),
                                          (25000, "0"), (25000, "03")])
def test_rejected_update_never_executes_tc(tmp_path, rate, ceiling):
    result, calls = invoke(tmp_path, rate, ceiling)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "1 20000"
    assert calls == ""


@pytest.mark.parametrize("tc_status", [0, 7])
def test_ceiling_update_and_process_failure(tmp_path, tc_status):
    result, calls = invoke(tmp_path, 30000, tc_status=tc_status)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ("0 30000" if tc_status == 0 else "1 20000")
    assert "rate 30000kbit" in calls and "crate 30000kbit" in calls
