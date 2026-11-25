import copy

import pytest

from multidial.nss import Profile, plan, rate_change, upload_rules


DATA = {
    "physical": "eth-test", "ifb": "ifb-test",
    "default_download_kbit": 1000, "default_upload_kbit": 1000,
    "uplinks": [
        {"slot": 0, "device": "dial0", "address": "192.0.2.2", "download_kbit": 30000, "upload_kbit": 10000},
        {"slot": 1, "device": "dial1", "address": "192.0.2.3", "download_kbit": 45000, "upload_kbit": 15000},
    ],
}


def test_complete_two_direction_plan():
    result = plan(Profile.parse(DATA))
    assert result["setup"][0] == ["ip", "link", "add", "name", "ifb-test", "type", "ifb"]
    assert "nssmirred redirect dev ifb-test fromdev eth-test" in result["bind_tc"][1]
    for slot, address in [(0, "192.0.2.2"), (1, "192.0.2.3")]:
        leaf = f"{0x100 + slot:x}"
        assert any(f"parent 1:{leaf} handle {leaf}:" in cmd for cmd in result["upload_tc"])
        assert any(f"dst {address}/32 flowid {leaf}:" in cmd for cmd in result["download_tc"])
    assert not any("filter" in cmd for cmd in result["upload_tc"])


def test_upload_tags_preserve_mwan3_marks_and_priority_minor():
    rules = upload_rules(Profile.parse(DATA))
    assert "mark set" not in rules
    assert "priority -140" in rules
    assert 'oifname "dial0" counter meta priority set (meta priority & 0x0000ffff) | 0x01000000' in rules
    assert 'oifname "dial1" counter meta priority set (meta priority & 0x0000ffff) | 0x01010000' in rules


def test_autorate_measures_each_line_and_updates_only_its_own_leaf():
    fragments = plan(Profile.parse(DATA))["autorate_fragments"]
    for slot in (0, 1):
        fragment = fragments[f"dial{slot}"]
        assert f"/sys/class/net/dial{slot}/statistics/rx_bytes" in fragment
        assert f"/sys/class/net/dial{slot}/statistics/tx_bytes" in fragment
        assert "/sys/class/net/ifb-test/" not in fragment
        assert f"ping_extra_args='-I dial{slot}'" in fragment
        assert f"md_nss_leaf='{0x100 + slot:x}'" in fragment
        assert "md_nss_ul_device='eth-test'" in fragment


def test_rate_change_touches_only_selected_classes():
    commands = rate_change(Profile.parse(DATA), 1, 40000, 12000)
    assert len(commands) == 2
    assert all("class change" in cmd and "1:101 " in cmd for cmd in commands)
    assert "rate 40000kbit" in commands[0] and "dev ifb-test" in commands[0]
    assert "rate 12000kbit" in commands[1] and "dev eth-test" in commands[1]
    with pytest.raises(ValueError):
        rate_change(Profile.parse(DATA), 3, 1, 1)


@pytest.mark.parametrize("path,value", [
    (("physical",), "eth0; reboot"),
    (("ifb",), "eth-test"),
    (("uplinks", 1, "slot"), 0),
    (("uplinks", 1, "slot"), True),
    (("uplinks", 1, "device"), "dial0"),
    (("uplinks", 1, "device"), "eth-test"),
    (("uplinks", 1, "address"), "192.0.2.2"),
    (("uplinks", 1, "address"), "127.0.0.1"),
    (("uplinks", 1, "upload_kbit"), 1.5),
    (("uplinks", 1, "download_kbit"), 0),
    (("uplinks",), []),
])
def test_bad_profile_emits_no_plan(path, value):
    data = copy.deepcopy(DATA)
    parent = data
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value
    with pytest.raises(ValueError):
        Profile.parse(data)
