#!/usr/bin/env python3
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import ssl
import threading

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
        parser.error("code and run must match ^[A-Za-z0-9-]{1,40}$")
    endpoint = (IOT / "endpoint").read_text(encoding="utf-8").strip()
    topic = f"cxe/{args.code}/gw/events"
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"gw-{args.code}",
                         protocol=mqtt.MQTTv5)
    client.tls_set(ca_certs=str(IOT / "AmazonRootCA1.pem"),
                   certfile=str(IOT / "gw.cert.pem"), keyfile=str(IOT / "gw.private.key"),
                   cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS_CLIENT)
    connected = threading.Event()
    published = threading.Event()
    ack_count = 0
    last_mid = None

    def on_connect(client, userdata, flags, reason_code, properties):
        connected.set()

    def on_publish(client, userdata, mid, reason_code, properties):
        nonlocal ack_count
        if mid == last_mid:
            ack_count += 1
            published.set()

    client.on_connect = on_connect
    client.on_publish = on_publish
    client.connect(endpoint, 8883, keepalive=60)
    client.loop_start()
    try:
        connected.wait(10)
        prefix = f"{args.code}-{args.run}"
        events = []
        for number in range(1, 11):
            device = f"{args.code}-d{number:02d}"
            for sequence in range(1, 4):
                events.append(_event(f"{prefix}-d{number:02d}-{sequence:02d}", device))
        events.append(events[0].copy())
        events.append(_event(f"{prefix}-d99-01", f"{args.code}-d99"))
        for event in events:
            last_mid = None
            published.clear()
            info = client.publish(topic, json.dumps(event, separators=(",", ":")), qos=1)
            last_mid = info.mid
            if info.rc != mqtt.MQTT_ERR_SUCCESS or not published.wait(15):
                break
        print(f"puback_ok={ack_count}/32")
        path = ROOT / ".local" / "w06-last-run.json"
        path.parent.mkdir(mode=0o700, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"prefix": prefix, "puback_ok": ack_count}) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        client.disconnect()
        client.loop_stop()


def _event(event_id, device_id):
    return {"event_id": event_id, "device_id": device_id,
            "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "type": "status"}


if __name__ == "__main__":
    main()