#!/usr/bin/env python3
"""Simulate ten devices publishing 32 MQTT events."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import ssl
import tempfile
import threading
import time

import paho.mqtt.client as mqtt

ROOT = Path(__file__).resolve().parents[1]
IOT_DIR = ROOT / ".local" / "iot"
RUN_FILE = ROOT / ".local" / "w06-last-run.json"
VALID_CODE = re.compile(r"^[A-Za-z0-9-]{1,40}$")
VALID_RUN = re.compile(r"^r[1-9][0-9]*$")


def make_events(code, run):
    prefix = f"{code}-{run}"
    if not VALID_CODE.fullmatch(prefix):
        raise ValueError("Invalid event prefix")

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    events = []

    for device in range(1, 11):
        for number in range(1, 4):
            events.append({
                "event_id": f"{prefix}-d{device:02d}-{number:02d}",
                "device_id": f"{code}-d{device:02d}",
                "observed_at": now,
                "type": "status",
            })

    events.append(dict(events[0]))
    events.append({
        "event_id": f"{prefix}-d99-01",
        "device_id": f"{code}-d99",
        "observed_at": now,
        "type": "status",
    })
    return prefix, events


def write_result(prefix, count):
    RUN_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix=".w06-run-", dir=RUN_FILE.parent
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump({"prefix": prefix, "puback_ok": count}, file)
            file.write("\n")
        os.replace(temp_path, RUN_FILE)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def run_gateway(code, run):
    prefix, events = make_events(code, run)
    topic = f"cxe/{code}/gw/events"

    endpoint = (IOT_DIR / "endpoint").read_text(
        encoding="ascii"
    ).strip()

    connected = threading.Event()
    lock = threading.Lock()
    acks = {}
    connection = {"code": None}

    def on_connect(client, userdata, flags, reason_code, properties):
        connection["code"] = int(
            getattr(reason_code, "value", reason_code)
        )
        connected.set()

    def on_publish(client, userdata, mid, reason_code, properties):
        code_value = int(getattr(reason_code, "value", reason_code))
        with lock:
            acks[mid] = code_value

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"gw-{code}",
        protocol=mqtt.MQTTv5,
    )
    client.on_connect = on_connect
    client.on_publish = on_publish

    client.tls_set(
        ca_certs=str(IOT_DIR / "AmazonRootCA1.pem"),
        certfile=str(IOT_DIR / "gw.cert.pem"),
        keyfile=str(IOT_DIR / "gw.private.key"),
        cert_reqs=ssl.CERT_REQUIRED,
        tls_version=ssl.PROTOCOL_TLS_CLIENT,
    )
    client.tls_insecure_set(False)

    acknowledged = 0

    try:
        client.connect(endpoint, 8883, keepalive=60)
        client.loop_start()

        if not connected.wait(15):
            raise RuntimeError("MQTT CONNACK timeout")
        if connection["code"] != 0:
            raise RuntimeError(
                f"MQTT connection rejected: {connection['code']}"
            )

        for event in events:
            payload = json.dumps(
                event, separators=(",", ":"), ensure_ascii=False
            )
            info = client.publish(topic, payload, qos=1)
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                raise RuntimeError("MQTT publish failed")

            deadline = time.monotonic() + 15
            ack = None

            while time.monotonic() < deadline:
                with lock:
                    ack = acks.pop(info.mid, None)
                if ack is not None:
                    break
                time.sleep(0.02)

            if ack is None:
                raise RuntimeError("PUBACK timeout")
            if ack >= 128:
                raise RuntimeError(f"PUBACK rejected: {ack}")

            acknowledged += 1

    finally:
        write_result(prefix, acknowledged)
        if client.is_connected():
            client.disconnect()
        client.loop_stop()

    print(f"prefix={prefix}")
    print(f"puback_ok={acknowledged}/32")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", required=True)
    parser.add_argument("--run", required=True)
    args = parser.parse_args()

    if not VALID_CODE.fullmatch(args.code):
        parser.error("Invalid code")
    if not VALID_RUN.fullmatch(args.run):
        parser.error("Run must be r1, r2, ...")
    if not VALID_CODE.fullmatch(f"{args.code}-{args.run}"):
        parser.error("Combined prefix exceeds 40 characters")
    if len(args.code) + 4 > 32:
        parser.error("Device ID exceeds 32 characters")

    try:
        run_gateway(args.code, args.run)
    except (OSError, ValueError, RuntimeError, mqtt.MQTTException) as exc:
        print(f"ERROR: Gateway failed ({type(exc).__name__})")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
