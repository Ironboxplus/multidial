from pathlib import Path
import shutil
import subprocess

import pytest

BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(not BASH, reason="Bash required for the autorate adapter")
ROOT = Path(__file__).resolve().parents[1]


def run(script):
    return subprocess.run([BASH, "-c", "source tools/nss-class-shaper.sh\n" + script],
                          cwd=ROOT, capture_output=True, text=True)


def test_class_update_names_one_class_and_both_rate_fields():
    result = run('tc() { printf "%s\\n" "$*"; }; md_nss_set_rate eth-test 101 12000')
    assert result.returncode == 0
    assert result.stdout.strip() == (
        "class change dev eth-test parent 1:1 classid 1:101 nsshtb "
        "rate 12000kbit burst 7500 crate 12000kbit cburst 7500"
    )


@pytest.mark.parametrize("args", [
    "eth-test 1ff 12000", "eth-test 100 0", "eth-test 100 34359739",
    "eth-test 100 1.2", "'eth;reboot' 100 1",
])
def test_invalid_adapter_input_never_calls_tc(args):
    result = run('tc() { echo unexpected; }; md_nss_set_rate ' + args)
    assert result.returncode != 0 and not result.stdout


@pytest.mark.parametrize("failure", [False, True])
def test_failed_tc_keeps_last_applied_rate_for_retry(failure):
    script = '''
declare -A shaper_rate_kbps=([dl]=30000 [ul]=10000)
declare -A last_shaper_rate_kbps=([dl]=20000 [ul]=10000)
declare -A adjust_shaper_rate=([dl]=1 [ul]=1)
declare -A compensated_owd_delta_thr_us compensated_avg_owd_delta_thr_us
dl_max_wire_packet_size_bits=12000
ul_max_wire_packet_size_bits=12000
dl_owd_delta_thr_us=10000
ul_owd_delta_thr_us=10000
dl_avg_owd_delta_thr_us=20000
ul_avg_owd_delta_thr_us=20000
md_nss_dl_device=ifb-test
md_nss_ul_device=eth-test
md_nss_leaf=100
md_nss_dl_ceiling_kbit=30000
md_nss_ul_ceiling_kbit=10000
log_msg() { :; }
tc() { return TC_RC; }
set_shaper_rate dl
rc=$?
printf '%s %s %s' "$rc" "${last_shaper_rate_kbps[dl]}" "${last_shaper_rate_kbps[ul]}"
'''
    result = run(script.replace("TC_RC", "1" if failure else "0"))
    assert result.returncode == 0
    assert result.stdout == ("1 20000 10000" if failure else "0 30000 10000")
