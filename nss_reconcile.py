"""Offline, persistent NSS transition plans.

The CLI never executes network commands. A stored transition remains pending
until its ordered steps have explicit receipts. A receipt is an execution
record, not hardware qualification. This tool cannot authorize production use.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import tempfile

from .nss import Profile, plan


def identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def transition(previous, desired):
    """Compute changes without restarting an unaffected line's shaper.

    Full physical-tree changes involve all lines. Any replacement or removal
    first stops new learning and then waits for old offloads to drain.
    """
    old = Profile.parse(previous) if previous else None
    new = Profile.parse(desired) if desired else None
    old_lines = {line.device: asdict(line) for line in old.uplinks} if old else {}
    new_lines = {line.device: asdict(line) for line in new.uplinks} if new else {}
    structural_keys = ("physical", "ifb", "default_download_kbit", "default_upload_kbit",
                       "physical_download_kbit", "physical_upload_kbit", "offload")
    structural = bool(old and new and any(getattr(old, key) != getattr(new, key) for key in structural_keys))
    # Rates and addresses can change without replacing the physical tree.
    changed = sorted(device for device in old_lines.keys() | new_lines.keys()
                     if structural or old_lines.get(device) != new_lines.get(device))
    for device in old_lines.keys() & new_lines.keys():
        if new_lines[device]["generation"] < old_lines[device]["generation"]:
            raise ValueError("stale lease generation")
    if not changed:
        return []
    steps = [{"operation": "block_learning", "devices": changed},
             {"operation": "wait_offloads_drained", "devices": changed}]
    if new is None:
        steps.append({"operation": "remove_owned_tree", "devices": changed})
        return steps
    if old is None or structural:
        if old:
            steps.append({"operation": "remove_owned_tree", "devices": changed})
        steps.append({"operation": "prepare_tree", "plan": plan(new)})
    else:
        steps.append({"operation": "update_owned_lines", "before": old_lines,
                      "after": new_lines, "devices": changed,
                      "desired_plan": plan(new)})
    steps.append({"operation": "verify_both_directions", "devices": changed,
                  "checks": ["policy_route_and_nat_source", "upload_leaf", "igs_download_leaf"]})
    online = [device for device in changed if device in new_lines]
    if online:
        steps.append({"operation": "allow_learning", "devices": online})
    return steps


@contextmanager
def locked_state(path):
    # OpenWrt/Linux tooling. A separate stable lock inode survives atomic
    # replacement of the JSON state and serializes independent CLI processes.
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(path.read_text()) if path.exists() else {
            "version": 1, "epoch": os.urandom(16).hex(), "revision": 0,
            "applied": None, "pending": None}
        if state.get("version") != 1:
            raise ValueError("unsupported state version")
        state.setdefault("epoch", os.urandom(16).hex())
        yield state
        fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as output:
                json.dump(state, output, sort_keys=True)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def propose(state, desired):
    if desired:
        # Normalize defaults so an old profile and its newer explicit defaults
        # describe the same desired state.
        desired = json.loads(json.dumps(asdict(Profile.parse(desired))))
    pending = state["pending"]
    if pending:
        if pending["desired"] != desired:
            raise ValueError("finish the pending transition before replacing its desired state")
        return pending
    steps = transition(state["applied"], desired)
    if not steps:
        return None
    state["revision"] += 1
    state.setdefault("epoch", os.urandom(16).hex())
    pending = {"id": identity([state["epoch"], state["revision"], desired]), "desired": desired,
               "steps": steps, "next": 0, "receipts": []}
    state["pending"] = pending
    return pending


def acknowledge(state, token, index, success):
    pending = state["pending"]
    if not pending or pending["id"] != token:
        raise ValueError("stale transition receipt")
    if type(index) is not int or index != pending["next"]:
        raise ValueError("receipt must name the next ordered step")
    if not success:
        # Retry this exact step. Previously recorded progress is preserved,
        # and the applied profile remains the last completely finished one.
        return pending
    pending["receipts"].append(index)
    pending["next"] += 1
    if pending["next"] == len(pending["steps"]):
        state["applied"] = pending["desired"]
        state["pending"] = None
    return state["pending"]


def rollback(state):
    """Replace a failed transaction with an ordered restoration plan.

    Partial application is unknown. Block the union of involved devices and
    drain before rebuilding the previous tree. Old receipts lose authority.
    """
    pending = state["pending"]
    if not pending:
        raise ValueError("no pending transition to roll back")
    profiles = [Profile.parse(value) for value in (state["applied"], pending["desired"]) if value]
    devices = sorted({line.device for profile in profiles for line in profile.uplinks})
    steps = [{"operation": "block_learning", "devices": devices},
             {"operation": "wait_offloads_drained", "devices": devices},
             {"operation": "remove_owned_tree", "devices": devices}]
    if state["applied"]:
        old = Profile.parse(state["applied"])
        online = [line.device for line in old.uplinks]
        steps += [{"operation": "prepare_tree", "plan": plan(old)},
                  {"operation": "verify_both_directions", "devices": online},
                  {"operation": "allow_learning", "devices": online}]
    state["revision"] += 1
    state["pending"] = {"id": identity([state["epoch"], state["revision"], "rollback", pending["id"]]),
                        "desired": state["applied"], "steps": steps, "next": 0,
                        "receipts": [], "rollback_of": pending["id"]}
    return state["pending"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    command = parser.add_mutually_exclusive_group(required=True)
    command.add_argument("--profile", type=Path)
    command.add_argument("--stop", action="store_true")
    command.add_argument("--rollback", action="store_true")
    command.add_argument("--receipt", nargs=3, metavar=("TRANSACTION", "STEP", "ok|failed"))
    args = parser.parse_args()
    try:
        with locked_state(args.state) as state:
            if args.rollback:
                rollback(state)
            elif args.receipt:
                token, index, status = args.receipt
                if status not in ("ok", "failed"):
                    raise ValueError("receipt result must be ok or failed")
                acknowledge(state, token, int(index), status == "ok")
            else:
                desired = json.loads(args.profile.read_text()) if args.profile else None
                propose(state, desired)
            result = json.dumps(state, indent=2)
    except (ValueError, TypeError, KeyError, OSError) as exc:
        parser.error(str(exc))
    print(result)


if __name__ == "__main__":
    main()
