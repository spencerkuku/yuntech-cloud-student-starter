#!/usr/bin/env python3
"""Simulate a gateway that publishes a batch of device events to the IoT broker."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import re
import time

try:
    import paho.mqtt.client as mqtt
except Exception as exc:  # pragma: no cover
    raise SystemExit(f"paho-mqtt is required: {exc}")

ROOT = Path(__file__).resolve().parents[1]
IOT_DIR = ROOT / ".local" / "iot"
STATE_PATH = ROOT / ".local" / "w06-last-run.json"


def build_event(prefix, device_id, index):
    event = {
        "event_id": f"{prefix}-{device_id}-{index:02d}",
        "device_id": device_id,
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "+00:00"),
        "type": "status",
    }
    return event


def load_endpoint():
    return (IOT_DIR / "endpoint").read_text(encoding="utf-8").strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code", required=True)
    parser.add_argument("--run", required=True)
    args = parser.parse_args()

    if not re.fullmatch(r"^[A-Za-z0-9-]{1,40}$", args.code):
        raise SystemExit("code must match ^[A-Za-z0-9-]{1,40}$")
    prefix = f"{args.code}-{args.run}"
    if not re.fullmatch(r"^[A-Za-z0-9-]{1,40}$", prefix):
        raise SystemExit("run prefix must match ^[A-Za-z0-9-]{1,40}$")

    endpoint = load_endpoint()
    client = mqtt.Client(client_id=f"gw-{args.code}", callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    client.tls_set(ca_certs=str(IOT_DIR / "AmazonRootCA1.pem"), certfile=str(IOT_DIR / "gw.cert.pem"), keyfile=str(IOT_DIR / "gw.private.key"))
    client.tls_insecure_set(False)
    client.connect(endpoint, 8883, 60)
    client.loop_start()

    count = 0
    for device_index in range(1, 11):
        device_id = f"{args.code}-d{device_index:02d}"
        for event_index in range(1, 4):
            event = build_event(prefix, device_id, event_index)
            client.publish(f"cxe/{args.code}/gw/events", json.dumps(event), qos=1)
            count += 1
            time.sleep(0.1)
    # duplicate the first event and send an extra invalid-device event
    dup = build_event(prefix, f"{args.code}-d01", 1)
    client.publish(f"cxe/{args.code}/gw/events", json.dumps(dup), qos=1)
    count += 1
    invalid = build_event(prefix, f"{args.code}-d99", 1)
    client.publish(f"cxe/{args.code}/gw/events", json.dumps(invalid), qos=1)
    count += 1

    time.sleep(1)
    client.loop_stop(force=True)
    client.disconnect()

    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps({"prefix": prefix, "puback_ok": count, "published": count}, indent=2), encoding="utf-8")
    print(f"puback_ok={count}/32")


if __name__ == "__main__":
    main()
