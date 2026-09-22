#!/usr/bin/env python3
"""deploy/down.py — reclaim one recorded round by ID (verify tags first), or --stop it.

Only touches IDs listed in .local/resources.json. Never searches by name to mass delete.
--stop: stop the instance only, keep SG and key pair (W3 keep-to-W4 flow).
Delete mode: terminate instance (auto-releases ENI, root EBS, public IPv4), then delete
the SG and the imported key pair. Read-backs every item and prints the result with UTC.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from lab import run_aws, context, LabError  # noqa: E402

COURSE = "yuntech-115-1"
WEEK = "w03"
CONFIG_PATH = ROOT / ".local" / "config"
RESOURCES_PATH = ROOT / ".local" / "resources.json"


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_config():
    cfg = {}
    if CONFIG_PATH.is_file():
        for line in CONFIG_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            cfg[key.strip()] = value.strip()
    return cfg


def load_resources():
    if not RESOURCES_PATH.is_file():
        sys.exit(f"STOP: {RESOURCES_PATH} does not exist; nothing recorded to reclaim.")
    return json.loads(RESOURCES_PATH.read_text(encoding="utf-8"))


def save_resources(data):
    fd, name = tempfile.mkstemp(prefix="resources-", dir=RESOURCES_PATH.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        os.replace(name, RESOURCES_PATH)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def confirm(prompt, token):
    print(prompt)
    if not sys.stdin.isatty():
        sys.exit("STOP: no interactive terminal. Run this script yourself, do not wire it to an agent.")
    answer = input(f"Type exactly {token!r} to proceed: ").strip()
    if answer != token:
        sys.exit("Cancelled; nothing was changed.")


def verify_identity_gate():
    result = subprocess.run(["bash", "scripts/verify-aws.sh"], cwd=ROOT,
                            capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit("STOP: identity gate failed. See scripts/verify-aws.sh output in your terminal.")
    print(result.stdout.strip())


def describe_instance(region, iid):
    try:
        rows = run_aws(["ec2", "describe-instances", "--instance-ids", iid], region)["Reservations"]
        return rows[0]["Instances"][0] if rows else None
    except LabError as exc:
        if "NotFound" in str(exc):
            return None
        raise


def wait_for_state(region, iid, wanted, timeout=300, interval=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = describe_instance(region, iid)
        state = detail["State"]["Name"] if detail else None
        if wanted == "terminated" and state is None:
            print(f"OBS ok instance absent (NotFound) @ {utc_now()}")
            return
        if state == wanted:
            print(f"OBS ok instance state == {wanted}: {utc_now()}")
            return
        time.sleep(interval)
    sys.exit(f"STOP: timeout waiting for instance {iid} to reach {wanted}.")


def resource_exists(region, args, missing_hint):
    """Return True if the resource still exists; raises only on non-NotFound errors."""
    try:
        run_aws(args, region)
        return True
    except LabError as exc:
        if "NotFound" in str(exc) or missing_hint in str(exc):
            print(f"   read-back: not found ({missing_hint}) @ {utc_now()}")
            return False
        raise


def readback_absent(region, args, label, missing_hint, attempts=4, interval=10):
    """Retry a read-back; ENI/volume release can lag termination by a few seconds."""
    for attempt in range(1, attempts + 1):
        if not resource_exists(region, args, missing_hint):
            return
        if attempt < attempts:
            print(f"   read-back: {label} still listed, retry {attempt}/{attempts - 1} ...")
            time.sleep(interval)
    sys.exit(f"STOP: {label} still exists after {attempts} read-backs; inspect before manual cleanup.")


def require_tags(tags, label):
    if tags.get("course") != COURSE or tags.get("week") != WEEK:
        sys.exit(f"STOP: {label} tags do not match course={COURSE}/week={WEEK}; "
                 f"refusing to delete. Inspect ownership before changing the script.")
    print(f"   tag check passed ({label}): course={COURSE}, week={WEEK}, "
          f"group={tags.get('group')}, owner={tags.get('owner')}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--round", type=int, default=None,
                        help="1-based round number from .local/resources.json (default: latest)")
    parser.add_argument("--stop", action="store_true",
                        help="stop the instance only; keep SG and key pair")
    args = parser.parse_args()

    cfg = load_config()
    ctx = context()
    region = ctx["region"]
    resources = load_resources()
    rounds = resources.get("rounds", [])
    if not rounds:
        sys.exit("STOP: no recorded rounds in .local/resources.json.")
    index = (args.round - 1) if args.round else (len(rounds) - 1)
    if not 0 <= index < len(rounds):
        sys.exit("STOP: --round out of range.")
    rnd = rounds[index]
    iid = (rnd.get("instance") or {}).get("id")
    sg = rnd.get("security_group") or {}
    kp = rnd.get("key_pair") or {}
    eni = rnd.get("eni") or {}
    volume = rnd.get("root_volume") or {}
    print(f"\n===== Round {index + 1}: created {rnd.get('created_utc')}, commit {rnd.get('commit')} =====")

    if not iid:
        print("NOTE: no instance ID recorded for this round (partial run); "
              "only SG/key pair will be handled.")
        detail = None
        state = "absent"
    else:
        detail = describe_instance(region, iid)
        state = detail["State"]["Name"] if detail else "absent"
        print(f"Instance {iid} current state: {state}")

    if args.stop:
        if not iid:
            sys.exit("STOP: --stop needs an instance ID; none recorded in this round.")
        if state in ("stopped", "absent"):
            print(f"Instance already {state}; nothing to do.")
            return 0
        if state != "running":
            sys.exit(f"STOP: instance state is {state}, cannot stop.")
        require_tags({t["Key"]: t["Value"] for t in detail.get("Tags", [])}, "instance")
        confirm(f"\nWill STOP instance {iid} only. SG {sg.get('id')} and key pair "
                f"{kp.get('name')} stay for W4.", "STOP")
        verify_identity_gate()
        run_aws(["ec2", "stop-instances", "--instance-ids", iid], region)
        wait_for_state(region, iid, "stopped")
        rnd["instance"]["state"] = "stopped"
        rnd["stopped_utc"] = utc_now()
        save_resources(resources)
        print(f"Stop complete: {iid} state=stopped @ {rnd['stopped_utc']} "
              "(public IPv4 will change on next start; SG source /32 must be re-checked).")
        return 0

    # --- delete mode ---
    print(f"\nWill DELETE (round {index + 1}):")
    print(f"  1. instance {iid or '(none recorded)'}  "
          f"(auto-releases ENI {eni.get('id')}, root volume "
          f"{volume.get('id')}, public IPv4)")
    print(f"  2. security group {sg.get('id')}")
    print(f"  3. imported key pair {kp.get('name')} ({kp.get('id')})")
    if detail and state != "absent":
        require_tags({t["Key"]: t["Value"] for t in detail.get("Tags", [])}, "instance")

    if sg.get("id"):
        sg_info = run_aws(["ec2", "describe-security-groups", "--group-ids", sg["id"]], region)
        require_tags({t["Key"]: t["Value"] for t in sg_info["SecurityGroups"][0].get("Tags", [])},
                     "security group")

    expected_key = f"w03-{cfg.get('OWNER', '?')}-key"
    if kp.get("name") and cfg.get("OWNER") and kp["name"] != expected_key:
        sys.exit(f"STOP: key pair name {kp['name']} != expected {expected_key}; "
                 "ownership check failed, refusing to delete.")
    if kp.get("name"):
        kp_info = run_aws(["ec2", "describe-key-pairs", "--key-names", kp["name"]], region)
        print(f"   key pair exists on AWS: {kp_info['KeyPairs'][0]['KeyName']} "
              f"({kp_info['KeyPairs'][0].get('KeyPairId')})")

    confirm("\nProceed with deletion?", "DELETE")
    verify_identity_gate()

    if iid and state != "absent":
        run_aws(["ec2", "terminate-instances", "--instance-ids", iid], region)
        try:
            wait_for_state(region, iid, "terminated")
        except LabError:
            pass  # instance may already be gone
    elif iid:
        print(f"   instance already absent @ {utc_now()}")

    print(f"\n===== Read-back (all must be absent) @ {utc_now()} =====")
    # Terminated instances may stay visible to the API briefly; absent == NotFound,
    # and state "terminated" is reported honestly as no-longer-billed instead of faked.
    if iid:
        still = describe_instance(region, iid)
        if still is None:
            print(f"   read-back: instance not found (InvalidInstanceID.NotFound) @ {utc_now()}")
        elif still["State"]["Name"] == "terminated":
            print(f"   read-back: instance state=terminated (API may list it briefly; "
                  f"billing stopped, no longer a usable host) @ {utc_now()}")
        else:
            sys.exit(f"STOP: instance still in state {still['State']['Name']} after termination attempt.")
    else:
        print("   read-back: instance not created in this round (skipped)")
    if eni.get("id"):
        readback_absent(region, ["ec2", "describe-network-interfaces",
                                 "--network-interface-ids", eni["id"]],
                        f"ENI {eni['id']}", "InvalidNetworkInterfaceID.NotFound")
    if volume.get("id"):
        readback_absent(region, ["ec2", "describe-volumes", "--volume-ids", volume["id"]],
                        f"volume {volume['id']}", "InvalidVolume.NotFound")

    # Delete the SG (ENI is gone by now) and the imported key pair,
    # then read back each one AFTER deletion so all five are confirmed absent.
    if sg.get("id"):
        run_aws(["ec2", "delete-security-group", "--group-id", sg["id"]], region)
        print(f"OBS deleted security group {sg['id']}: {utc_now()}")
        resource_exists(region, ["ec2", "describe-security-groups", "--group-ids", sg["id"]],
                        "InvalidGroup.NotFound")
    if kp.get("name"):
        run_aws(["ec2", "delete-key-pair", "--key-name", kp["name"]], region)
        print(f"OBS deleted key pair {kp['name']}: {utc_now()}")
        resource_exists(region, ["ec2", "describe-key-pairs", "--key-names", kp["name"]],
                        "InvalidKeyPair.NotFound")

    if iid:
        rnd["instance"]["state"] = "terminated"
    rnd["reclaimed_utc"] = utc_now()
    rnd["reclaimed"] = True
    save_resources(resources)
    print("\nRound reclaimed. Recorded in .local/resources.json.")


if __name__ == "__main__":
    try:
        main()
    except LabError as exc:
        print("STOP: " + str(exc), file=sys.stderr)
        sys.exit(1)