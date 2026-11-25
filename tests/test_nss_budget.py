import copy
import re

import pytest

from multidial.nss import Profile, plan, rate_change
from multidial.tests.test_nss import DATA


def test_shared_cap_bounds_committed_rates_and_keeps_individual_ceilings():
    data = copy.deepcopy(DATA)
    data.update(physical_download_kbit=40000, physical_upload_kbit=14000)
    profile = Profile.parse(data)
    result = plan(profile)
    for section, cap, default in [("upload_tc", 14000, 1000), ("download_tc", 40000, 1000)]:
        lines = [cmd for cmd in result[section] if cmd.startswith("class add")]
        assert f"rate {cap}kbit" in lines[0]
        rates = [int(re.search(r"\brate (\d+)kbit", cmd)[1]) for cmd in lines[1:]]
        assert sum(rates) <= cap and rates[0] == default
        assert all(rate > 0 for rate in rates)
    assert "crate 30000kbit" in result["download_tc"][4]
    assert "crate 10000kbit" in result["upload_tc"][4]
    changed = rate_change(profile, 0, 28000, 9000)
    assert "crate 28000kbit" in changed[0]
    assert re.search(r"\brate (\d+)kbit", changed[0])[1] != "28000"


@pytest.mark.parametrize("value", [0, True, 1001, 1.5, 34359739])
def test_invalid_or_insufficient_cap_rejected(value):
    data = copy.deepcopy(DATA)
    data["physical_download_kbit"] = value
    with pytest.raises(ValueError):
        Profile.parse(data)


def test_egress_and_igs_use_disjoint_tags_and_generated_limits():
    result = plan(Profile.parse(DATA))
    for line in Profile.parse(DATA).uplinks:
        assert line.leaf != line.download_leaf
        fragment = result["autorate_fragments"][line.device]
        assert f"md_nss_dl_leaf='{line.download_leaf}'" in fragment
        assert f"md_nss_ul_leaf='{line.leaf}'" in fragment
        assert f"md_nss_dl_ceiling_kbit='{line.download_kbit}'" in fragment
        assert f"md_nss_ul_ceiling_kbit='{line.upload_kbit}'" in fragment
