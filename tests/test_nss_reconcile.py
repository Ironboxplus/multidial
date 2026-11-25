import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from multidial.nss_reconcile import acknowledge, propose, rollback, transition
from multidial.tests.test_nss import DATA


def state():
    return {"version": 1, "revision": 0, "applied": None, "pending": None}


def complete(current, data):
    pending = propose(current, data)
    for index in range(len(pending["steps"])):
        acknowledge(current, pending["id"], index, True)


def test_first_activation_waits_for_verification_and_allow_receipt():
    current = state()
    pending = propose(current, DATA)
    assert [step["operation"] for step in pending["steps"]] == [
        "block_learning", "wait_offloads_drained", "prepare_tree",
        "verify_both_directions", "allow_learning"]
    for index in range(4):
        acknowledge(current, pending["id"], index, True)
        assert current["applied"] is None
    acknowledge(current, pending["id"], 4, True)
    assert current["pending"] is None
    assert propose(current, DATA) is None


@pytest.mark.parametrize("field,value", [("address", "192.0.2.9"),
                                        ("generation", 1), ("ifindex", 42)])
def test_renewal_or_device_replacement_only_blocks_changed_line(field, value):
    current = state()
    complete(current, DATA)
    updated = copy.deepcopy(DATA)
    updated["uplinks"][0][field] = value
    steps = propose(current, updated)["steps"]
    assert steps[0]["devices"] == ["dial0"]
    assert steps[2]["operation"] == "update_owned_lines"
    assert steps[-1]["devices"] == ["dial0"]


def test_failed_step_retains_previous_state_and_rejects_stale_or_unordered_receipt():
    current = state()
    complete(current, DATA)
    baseline = copy.deepcopy(current["applied"])
    updated = copy.deepcopy(DATA)
    updated["uplinks"].pop()
    pending = propose(current, updated)
    assert pending["steps"][0]["devices"] == ["dial1"]
    acknowledge(current, pending["id"], 0, False)
    assert current["applied"] == baseline and pending["next"] == 0
    for token, index in [("old", 0), (pending["id"], 1), (pending["id"], -1)]:
        with pytest.raises(ValueError):
            acknowledge(current, token, index, True)
    with pytest.raises(ValueError):
        propose(current, DATA)


def test_stop_never_reenables_a_removed_line():
    current = state()
    complete(current, DATA)
    pending = propose(current, None)
    assert [item["operation"] for item in pending["steps"]] == [
        "block_learning", "wait_offloads_drained", "remove_owned_tree"]
    for index in range(3):
        acknowledge(current, pending["id"], index, True)
    assert current["applied"] is None and current["pending"] is None


def test_shared_budget_reallocation_includes_sibling_classes():
    current = state()
    base = copy.deepcopy(DATA)
    base["physical_download_kbit"] = 40000
    complete(current, base)
    updated = copy.deepcopy(base)
    updated["uplinks"][0]["download_kbit"] = 10000
    pending = propose(current, updated)
    assert pending["steps"][0]["devices"] == ["dial0", "dial1"]


def test_stale_lease_is_rejected_and_rollback_invalidates_old_receipts():
    current = state()
    base = copy.deepcopy(DATA)
    base["uplinks"][0]["generation"] = 2
    complete(current, base)
    with pytest.raises(ValueError, match="stale"):
        propose(current, DATA)
    changed = copy.deepcopy(base)
    changed["uplinks"][0]["generation"] = 3
    failed = propose(current, changed)
    acknowledge(current, failed["id"], 0, True)
    restoring = rollback(current)
    assert restoring["rollback_of"] == failed["id"]
    assert restoring["steps"][0]["devices"] == ["dial0", "dial1"]
    with pytest.raises(ValueError, match="stale"):
        acknowledge(current, failed["id"], 1, True)
    for index in range(len(restoring["steps"])):
        acknowledge(current, restoring["id"], index, True)
    assert propose(current, base) is None


@pytest.mark.skipif(sys.platform != "linux", reason="POSIX process lock")
def test_real_cli_restart_and_concurrent_proposals_share_one_transaction(tmp_path):
    root = Path(__file__).resolve().parents[2]
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps(DATA))
    stored = tmp_path / "state.json"
    command = [sys.executable, "-m", "multidial.nss_reconcile", "--state", str(stored)]
    processes = [subprocess.Popen([*command, "--profile", str(profile)], cwd=root,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for _ in range(6)]
    documents = []
    for process in processes:
        output, error = process.communicate(timeout=10)
        assert process.returncode == 0, error
        documents.append(json.loads(output))
    tokens = {item["pending"]["id"] for item in documents}
    assert len(tokens) == 1
    token = tokens.pop()
    result = subprocess.run([*command, "--receipt", token, "0", "ok"], cwd=root,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(stored.read_text())["pending"]["next"] == 1
    # A fresh CLI process must resume at step 1 rather than replaying block.
    resumed = subprocess.run([*command, "--profile", str(profile)], cwd=root,
                             capture_output=True, text=True, timeout=10)
    assert resumed.returncode == 0, resumed.stderr
    assert json.loads(resumed.stdout)["pending"]["next"] == 1
    duplicate = subprocess.run([*command, "--receipt", token, "0", "ok"], cwd=root,
                               capture_output=True, text=True, timeout=10)
    assert duplicate.returncode != 0
    assert json.loads(stored.read_text())["pending"]["next"] == 1


@pytest.mark.skipif(sys.platform != "linux", reason="POSIX process lock")
def test_corrupt_state_is_preserved_and_never_silently_reset(tmp_path):
    stored = tmp_path / "state.json"
    stored.write_text('{"version":1,"pending":')
    original = stored.read_bytes()
    result = subprocess.run([sys.executable, "-m", "multidial.nss_reconcile",
                             "--state", str(stored), "--stop"],
                            cwd=Path(__file__).resolve().parents[2], capture_output=True,
                            text=True, timeout=5)
    assert result.returncode != 0
    assert stored.read_bytes() == original
    assert result.stdout == ""
