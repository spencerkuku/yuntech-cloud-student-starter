#!/usr/bin/env python3
"""Check the two narrowly scoped IoT identities."""
import argparse
import json
import ssl
import threading
import time
from pathlib import Path

import paho.mqtt.client as mqtt

ROOT = Path(__file__).resolve().parents[1]
IOT = ROOT / ".local" / "iot"


def load_config(role):
    code = "g03-m2"
    return {
        "endpoint": (IOT / "endpoint").read_text(encoding="utf-8").strip(),
        "ca": IOT / "AmazonRootCA1.pem",
        "cert": IOT / f"{'gw' if role == 'gw' else 'consumer'}.cert.pem",
        "key": IOT / f"{'gw' if role == 'gw' else 'consumer'}.private.key",
        "client_id": f"{role}-{code}",
    }


def client_for(role):
    config = load_config(role)
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=config["client_id"],
        protocol=mqtt.MQTTv5,
    )
    client.tls_set(
        ca_certs=str(config["ca"]), certfile=str(config["cert"]),
        keyfile=str(config["key"]), cert_reqs=ssl.CERT_REQUIRED,
        tls_version=ssl.PROTOCOL_TLS_CLIENT,
    )
    return client


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--as", dest="role", choices=("gw", "consumer"), required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pub")
    group.add_argument("--sub")
    parser.add_argument("--wait", type=float, default=10)
    args = parser.parse_args()
    client = client_for(args.role)
    connected = threading.Event()
    pubacks = {}

    def on_connect(_client, _userdata, _flags, reason_code, _properties):
        print(f"connack={reason_code}", flush=True)
        connected.set()

    def on_publish(_client, _userdata, mid, reason_code, _properties):
        pubacks[mid] = reason_code

    def on_subscribe(_client, _userdata, _mid, reason_codes, _properties):
        print(f"suback={reason_codes[0]}", flush=True)

    def on_message(_client, _userdata, message):
        print(f"recv topic={message.topic} payload={message.payload.decode('utf-8')}", flush=True)

    client.on_connect = on_connect
    client.on_publish = on_publish
    client.on_subscribe = on_subscribe
    client.on_message = on_message
    config = load_config(args.role)
    client.connect(config["endpoint"], 8883, 60)
    client.loop_start()
    if not connected.wait(10):
        raise TimeoutError("MQTT connection timed out")
    if args.pub:
        info = client.publish(args.pub, json.dumps({"check": "t2", "code": "g03-m2"}), qos=1)
        deadline = time.monotonic() + args.wait
        while info.mid not in pubacks and time.monotonic() < deadline:
            time.sleep(0.05)
        print(f"puback={pubacks.get(info.mid, 128)}", flush=True)
    else:
        client.subscribe(args.sub, qos=1)
        time.sleep(args.wait)
    client.disconnect()
    client.loop_stop()


if __name__ == "__main__":
    main()
