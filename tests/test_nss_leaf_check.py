import pytest

from multidial.tools.nss_leaf_check import counters, check_interval


def fixture(a, b):
    return f"""qdisc nsshtb 1: root refcnt 2
 Sent 9999 bytes 999 pkt (dropped 0, overlimits 0 requeues 0)
qdisc nssfq_codel 100: parent 1:100 target 5ms
 Sent {a} bytes {a // 100} pkt (dropped 0, overlimits 0 requeues 0)
qdisc nssfq_codel 101: parent 1:101 target 5ms
 Sent {b} bytes {b // 100} pkt (dropped 0, overlimits 0 requeues 0)
"""


def test_separate_leaf_progress():
    before = counters(fixture(1000, 2000))
    after = counters(fixture(4000, 2000))
    assert check_interval(before, after, {0}, [0, 1]) == {
        0: {"bytes": 3000, "packets": 30}, 1: {"bytes": 0, "packets": 0}
    }


@pytest.mark.parametrize("after,active", [
    (fixture(1000, 2000), {0}),
    (fixture(1000, 4000), {0}),
    (fixture(4000, 4000), {0}),
    (fixture(100, 2000), {0}),
    ("", {0}),
    (fixture(4000, 2000), {2}),
])
def test_reject_wrong_queue_missing_or_reset(after, active):
    with pytest.raises(ValueError):
        check_interval(counters(fixture(1000, 2000)), counters(after), active, [0, 1])
