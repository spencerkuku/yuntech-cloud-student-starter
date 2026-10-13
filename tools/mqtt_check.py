#!/usr/bin/env python3
import argparse
import json
import os
import ssl
import threading
import time
from pathlib import Path

import paho.mqtt.client as mqtt

ROOT = Path(__file__).resolve().parents[1]
IOT = ROOT / ".local" / "iot"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--as", dest="identity", choices=("gw", "consumer"), required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--pub")
    mode.add_argument("--sub")
    parser.add_argument("--wait", type=float, default=10)
    args = parser.parse_args()
    endpoint = (IOT / "endpoint").read_text(encoding="utf-8").strip()
    code = os.environ.get("IOT_CODE") or _code_from_config() or _code_from_topic(args.pub or args.sub)
    prefix = "gw-" if args.identity == "gw" else "consumer-"
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=prefix + code,
                         protocol=mqtt.MQTTv5)
    client.tls_set(ca_certs=str(IOT / "AmazonRootCA1.pem"),
                   certfile=str(IOT / (args.identity + ".cert.pem")),
                   keyfile=str(IOT / (args.identity + ".private.key")),
                   cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS_CLIENT)
    connected = threading.Event()
    completed = threading.Event()

    def on_connect(client, userdata, flags, reason_code, properties):
        print(f"connack={int(reason_code)}", flush=True)
        connected.set()
        if args.sub:
            client.subscribe(args.sub, qos=1)

    def on_subscribe(client, userdata, mid, reason_codes, properties):
        print(f"suback={int(reason_codes[0]) if reason_codes else 128}", flush=True)

    def on_publish(client, userdata, mid, reason_code, properties):
        print(f"puback={int(reason_code)}", flush=True)
        completed.set()

    def on_message(client, userdata, message):
        print(f"recv topic={message.topic} payload={message.payload.decode('utf-8', 'replace')}", flush=True)

    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_publish = on_publish
    client.on_message = on_message
    client.connect(endpoint, 8883, keepalive=60)
    client.loop_start()
    try:
        connected.wait(10)
        if args.pub:
            payload = json.dumps({"check": "t2", "code": code}, separators=(",", ":"))
            info = client.publish(args.pub, payload, qos=1)
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                print(f"puback={info.rc}", flush=True)
            completed.wait(10)
        else:
            time.sleep(max(0, args.wait))
    finally:
        client.disconnect()
        client.loop_stop()


def _code_from_topic(topic):
    parts = (topic or "").split("/")
    if len(parts) >= 3 and parts[0] == "cxe":
        return parts[1]
    return "unknown"


def _code_from_config():
    path = ROOT / ".local" / "config"
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("OWNER="):
            return line.split("=", 1)[1].strip()
    return ""


if __name__ == "__main__":
    main()