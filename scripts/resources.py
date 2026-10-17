"""Read deployment targets from supported local resource ledger formats."""
import json
from pathlib import Path
import re


class ResourceLedgerError(ValueError):
    pass


def _validate_target(instance_id, key_name, state):
    if not isinstance(instance_id, str) or not re.fullmatch(r"i-[0-9a-f]+", instance_id):
        raise ResourceLedgerError("resource ledger has no valid EC2 instance ID")
    if not isinstance(key_name, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", key_name):
        raise ResourceLedgerError("resource ledger has no valid SSH key name")
    if state not in {"running", "stopped"}:
        raise ResourceLedgerError("resource ledger instance state is missing or unsupported")
    return {"instance_id": instance_id, "key_name": key_name, "state": state}


def deployment_target(resources):
    """Resolve the latest live W3 round, or the legacy flat target."""
    if not isinstance(resources, dict):
        raise ResourceLedgerError("resource ledger must be a JSON object")

    rounds = resources.get("rounds")
    if rounds is not None:
        if not isinstance(rounds, list):
            raise ResourceLedgerError("resource ledger rounds must be a list")
        for round_data in reversed(rounds):
            if not isinstance(round_data, dict):
                continue
            instance = round_data.get("instance") or {}
            if not isinstance(instance, dict) or instance.get("state") == "terminated":
                continue
            key_pair = round_data.get("key_pair") or {}
            if not isinstance(key_pair, dict):
                key_pair = {}
            return _validate_target(
                instance.get("id"),
                key_pair.get("name") or round_data.get("key_name"),
                instance.get("state"),
            )
        raise ResourceLedgerError("resource ledger has no live rounds")

    instance_id = resources.get("instance_id")
    key_name = resources.get("key_name")
    if instance_id is not None or key_name is not None:
        return _validate_target(instance_id, key_name, resources.get("state", "running"))
    raise ResourceLedgerError("resource ledger has no live rounds or legacy instance target")


def load_deployment_target(path):
    try:
        resources = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise ResourceLedgerError("cannot read valid resource ledger JSON") from None
    return deployment_target(resources)