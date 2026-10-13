#!/usr/bin/env python3
"""Utility to test an MQTT connection for either the gateway or consumer role."""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

try:
    import paho.mqtt.client as mqtt
except Exception as exc:  # pragma: no cover
    raise SystemExit(f"paho-mqtt is required: {exc}")


ROOT = Path(__file__).resolve().parents[1]
IOT_DIR = ROOT / ".local" / "iot"


def load_endpoint():
    endpoint = (IOT_DIR / "endpoint").read_text(encoding="utf-8").strip()
    return endpoint


def role_paths(role):
    role = role.lower()
    if role not in {"gw", "consumer"}:
        raise ValueError("role must be 'gw' or 'consumer'")
    if role == "gw":
        return {
            "ca": IOT_DIR / "AmazonRootCA1.pem",
            "cert": IOT_DIR / "gw.cert.pem",
            "key": IOT_DIR / "gw.private.key",
            "client_id": f"gw-{os.environ.get('CODE', 'local')}",
        }
    return {
        "ca": IOT_DIR / "AmazonRootCA1.pem",
        "cert": IOT_DIR / "consumer.cert.pem",
        "key": IOT_DIR / "consumer.private.key",
        "client_id": f"consumer-{os.environ.get('CODE', 'local')}",
    }


class Checker:
    def __init__(self, role, target, wait_seconds=30):
        self.role = role
        self.target = target
        self.wait_seconds = wait_seconds
        self.message_seen = False
        self.client = mqtt.Client(client_id=role_paths(role)["client_id"], protocol=mqtt.MQTTv5, callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
        self.client.on_connect = self.on_connect
        self.client.on_publish = self.on_publish
        self.client.on_subscribe = self.on_subscribe
        self.client.on_message = self.on_message
        self.client.tls_set(ca_certs=str(IOT_DIR / "AmazonRootCA1.pem"), certfile=str(role_paths(role)["cert"]), keyfile=str(role_paths(role)["key"]))
        self.client.tls_insecure_set(False)
        self.publish_event = threading.Event()

    @staticmethod
    def reason_value(reason_code):
        return getattr(reason_code, "value", reason_code)

    def on_connect(self, client, userdata, flags, reason_code, properties=None):
        print(f"connack={self.reason_value(reason_code)}")
        if self.target.kind == "sub":
            client.subscribe(self.target.topic, qos=1)
        elif self.target.kind == "pub":
            payload = json.dumps({"check": "t2", "code": os.environ.get("CODE", "local")})
            client.publish(self.target.topic, payload, qos=1)

    def on_publish(self, client, userdata, mid, reason_code=None, properties=None):
        print(f"puback={self.reason_value(reason_code)}")
        self.publish_event.set()

    def on_subscribe(self, client, userdata, mid, reason_code_list, properties=None):
        codes = [self.reason_value(code) for code in reason_code_list]
        print(f"suback={codes[0] if codes else 'missing'}")

    def on_message(self, client, userdata, message):
        print(f"recv topic={message.topic} payload={message.payload.decode('utf-8', 'replace')}")
        self.message_seen = True

    def run(self):
        self.client.connect(load_endpoint(), 8883, 60)
        self.client.loop_start()
        if self.target.kind == "pub":
            self.publish_event.wait(self.wait_seconds)
        else:
            time.sleep(self.wait_seconds)
        self.client.loop_stop()
        self.client.disconnect()


class Target:
    def __init__(self, kind, topic):
        self.kind = kind
        self.topic = topic


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as", dest="role", required=True, choices=["gw", "consumer"])
    parser.add_argument("--pub", dest="publish_topic")
    parser.add_argument("--sub", dest="subscribe_topic")
    parser.add_argument("--wait", type=int, default=10)
    args = parser.parse_args()

    if args.publish_topic and args.subscribe_topic:
        raise SystemExit("choose either --pub or --sub")
    if not args.publish_topic and not args.subscribe_topic:
        raise SystemExit("choose --pub or --sub")

    target = Target("pub", args.publish_topic) if args.publish_topic else Target("sub", args.subscribe_topic)
    Checker(args.role, target, args.wait).run()


if __name__ == "__main__":
    main()
