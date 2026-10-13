#!/usr/bin/env python3
"""MQTT consumer that validates device IDs and forwards accepted events to the local API."""
from __future__ import annotations

import json
import os
import sys
import time
from urllib import error, request

try:
    import paho.mqtt.client as mqtt
except Exception:  # pragma: no cover - the deployment image installs paho-mqtt.
    mqtt = None


def parse_device_ids(raw: str):
    return [item.strip() for item in raw.split(",") if item.strip()]


def send_event(event: dict, token: str):
    payload = json.dumps(event).encode("utf-8")
    req = request.Request(
        "http://127.0.0.1:8080/events",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=5) as response:
            status = response.status
            body = response.read().decode("utf-8")
            return status, json.loads(body) if body else {}
    except error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8")
            data = json.loads(body) if body else {}
        except Exception:
            data = {}
        return exc.code, data
    except Exception:
        return 599, {"error": "connect_error"}


class Consumer:
    def __init__(self):
        self.endpoint = os.environ.get("IOT_ENDPOINT", "")
        self.topic = os.environ.get("IOT_TOPIC", "")
        self.client_id = os.environ.get("IOT_CLIENT_ID", "consumer-local")
        self.ca = os.environ.get("IOT_CA", "")
        self.cert = os.environ.get("IOT_CERT", "")
        self.key = os.environ.get("IOT_KEY", "")
        self.reporter_token = os.environ.get("REPORTER_TOKEN", "")
        self.allowed = set(parse_device_ids(os.environ.get("IOT_DEVICE_IDS", "")))
        self.client = mqtt.Client(client_id=self.client_id, protocol=mqtt.MQTTv5, callback_api_version=mqtt.CallbackAPIVersion.VERSION2)

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            client.subscribe(self.topic, qos=1)
        else:
            print(f"subscribed rc={reason_code}", flush=True)

    def _on_subscribe(self, client, userdata, mid, reason_code_list, properties=None):
        reason_code = reason_code_list[0] if reason_code_list else "missing"
        print(f"subscribed rc={getattr(reason_code, 'value', reason_code)}", flush=True)

    def _on_message(self, client, userdata, message):
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except Exception:
            payload = {}
        event_id = payload.get("event_id", "unknown")
        device_id = payload.get("device_id", "")
        if device_id not in self.allowed:
            print(f"event_id={event_id} result=rejected", flush=True)
            return
        status, body = send_event(payload, self.reporter_token)
        if status == 201:
            result = "created"
        elif status == 200:
            result = "duplicate"
        elif 400 <= status < 500:
            result = "rejected"
        else:
            result = "error"
        print(f"event_id={event_id} result={result}", flush=True)

    def run(self):
        if not self.endpoint or not self.topic or not self.ca or not self.cert or not self.key:
            print("consumer not configured", file=sys.stderr)
            return 1
        self.client.on_connect = self._on_connect
        self.client.on_subscribe = self._on_subscribe
        self.client.on_message = self._on_message
        self.client.tls_set(ca_certs=self.ca, certfile=self.cert, keyfile=self.key)
        self.client.tls_insecure_set(False)
        self.client.enable_logger()
        self.client.connect(self.endpoint, 8883, 60)
        self.client.loop_forever(timeout=1.0)
        return 0


def main():
    if mqtt is None:
        print("paho-mqtt is required to run the MQTT consumer.", file=sys.stderr)
        return 2
    return Consumer().run()


if __name__ == "__main__":
    raise SystemExit(main())
