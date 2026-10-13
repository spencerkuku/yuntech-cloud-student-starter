#!/usr/bin/env python3
"""Publish the W6 32-message gateway batch."""
import argparse
import json
import re
import ssl
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as mqtt

ROOT = Path(__file__).resolve().parents[1]
IOT = ROOT / ".local" / "iot"
CODE_RE = re.compile(r"^[A-Za-z0-9-]{1,40}$")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", required=True)
    parser.add_argument("--run", required=True)
    args = parser.parse_args()
    if not CODE_RE.fullmatch(args.code) or not CODE_RE.fullmatch(args.run):
        raise SystemExit("code and run must match ^[A-Za-z0-9-]{1,40}$")
    prefix = f"{args.code}-{args.run}"
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"gw-{args.code}", protocol=mqtt.MQTTv5,
    )
    client.tls_set(
        ca_certs=str(IOT / "AmazonRootCA1.pem"),
        certfile=str(IOT / "gw.cert.pem"), keyfile=str(IOT / "gw.private.key"),
        cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS_CLIENT,
    )
    connected = threading.Event()
    acknowledgements = {}

    def on_connect(_client, _userdata, _flags, reason_code, _properties):
        if reason_code == 0:
            connected.set()

    def on_publish(_client, _userdata, mid, reason_code, _properties):
        acknowledgements[mid] = reason_code

    client.on_connect = on_connect
    client.on_publish = on_publish
    endpoint = (IOT / "endpoint").read_text(encoding="utf-8").strip()
    client.connect(endpoint, 8883, 60)
    client.loop_start()
    if not connected.wait(10):
        raise TimeoutError("MQTT connection timed out")
    events = []
    for number in range(1, 4):
        for device in range(1, 11):
            events.append((f"{prefix}-d{device:02d}-{number:02d}", f"{args.code}-d{device:02d}"))
    payloads = []
    for event_id, device_id in events:
        payloads.append({
            "event_id": event_id, "device_id": device_id,
            "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "type": "status",
        })
    payloads.append(dict(payloads[0]))
    payloads.append({
        "event_id": f"{prefix}-d99-01", "device_id": f"{args.code}-d99",
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "type": "status",
    })
    ok = 0
    for payload in payloads:
        info = client.publish(f"cxe/{args.code}/gw/events", json.dumps(payload), qos=1)
        deadline = time.monotonic() + 10
        while info.mid not in acknowledgements and time.monotonic() < deadline:
            time.sleep(0.01)
        if acknowledgements.get(info.mid) == 0:
            ok += 1
        else:
            raise RuntimeError(f"PUBACK failed for {payload['event_id']}")
    client.disconnect()
    client.loop_stop()
    print(f"puback_ok={ok}/32")
    path = ROOT / ".local" / "w06-last-run.json"
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_text(json.dumps({"prefix": prefix, "puback_ok": ok}) + "\n", encoding="utf-8")
    path.chmod(0o600)


if __name__ == "__main__":
    main()
