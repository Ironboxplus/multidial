from pathlib import Path
import subprocess
import sys

import pytest

from multidial.tools.nss_ingress_plan import ingress_plan

LINES = [(0, "192.0.2.2", 30000), (10, "192.0.2.12", 50000)]


def test_separate_leaf_tags_and_default():
    plan = ingress_plan("ifb@test", LINES, 1000)
    assert plan == ingress_plan("ifb@test", reversed(LINES), 1000)
    assert sum("set_default" in cmd for cmd in plan) == 1
    for minor, addr, pref in [("100", "192.0.2.2", 100), ("10a", "192.0.2.12", 110)]:
        assert any(f"parent 1:{minor} handle {minor}: nssfq_codel" in cmd for cmd in plan)
        assert f"filter add dev ifb@test parent 1: protocol ip pref {pref} u32 match ip dst {addr}/32 flowid {minor}:" in plan


def test_rate_update_only_names_existing_class():
    assert ingress_plan("ifb0", LINES, 1000, 10) == [
        "class change dev ifb0 parent 1:1 classid 1:10a nsshtb rate 50000kbit burst 31250 crate 50000kbit cburst 31250"
    ]
    with pytest.raises(ValueError):
        ingress_plan("ifb0", LINES, 1000, 3)


@pytest.mark.parametrize("line", [
    (0, "192.0.2.4", 1), (1, "192.0.2.2", 1), (-1, "192.0.2.4", 1),
    (255, "192.0.2.4", 1), (1, "::1", 1), (1, "0.0.0.0", 1),
    (1, "192.0.2.4", 0), (1, "192.0.2.4", 34359739),
])
def test_reject_invalid_or_ambiguous_mapping(line):
    with pytest.raises(ValueError):
        ingress_plan("ifb0", [*LINES, line], 1000)


def test_cli_never_emits_partial_batch():
    tool = Path(__file__).resolve().parents[1] / "tools" / "nss_ingress_plan.py"
    result = subprocess.run([sys.executable, str(tool), "ifb0", "--default-kbit", "1",
                             "--line", "0", "192.0.2.2", "1", "--line", "0", "192.0.2.3", "2"],
                            capture_output=True)
    assert result.returncode and not result.stdout
