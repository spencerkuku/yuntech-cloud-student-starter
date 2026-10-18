#!/usr/bin/env python3
"""Forward allowlisted MQTT events to the local inspection API."""
import json
import os
import re
import ssl
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import paho.mqtt.client as mqtt

API_URL = "http://127.0.0.1:8080/events"
PORT = 8883
EVENT_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
REQUIRED_SETTINGS = (
    "IOT_ENDPOINT",
    "IOT_TOPIC",
    "IOT_CLIENT_ID",
    "IOT_DEVICE_IDS",
    "IOT_CA",
    "IOT_CERT",
    "IOT_KEY",
    "REPORTER_TOKEN",
)


def load_config():
    config = {name: os.environ.get(name, "") for name in REQUIRED_SETTINGS}
    if any(not value for value in config.values()):
        raise ValueError("required consumer environment settings are missing")
    device_ids = frozenset(value.strip() for value in config["IOT_DEVICE_IDS"].split(",") if value.strip())
    if not device_ids:
        raise ValueError("IOT_DEVICE_IDS is empty")
    config["device_ids"] = device_ids
    return config


def safe_event_id(event):
    if isinstance(event, dict):
        event_id = event.get("event_id")
        if isinstance(event_id, str) and EVENT_ID_PATTERN.fullmatch(event_id):
            return event_id
    return "invalid"


def send_event(event, token):
    body = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = Request(
        API_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=5) as response:
            return response.status
    except HTTPError as exc:
        return exc.code
    except (URLError, TimeoutError, OSError):
        return None


def process_payload(payload, device_ids, token, post=send_event):
    try:
        event = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "invalid", "rejected"

    event_id = safe_event_id(event)
    if not isinstance(event, dict):
        return event_id, "rejected"
    if not isinstance(event.get("device_id"), str) or event["device_id"] not in device_ids:
        return event_id, "rejected"

    status = post(event, token)
    if status == 201:
        return event_id, "created"
    if status == 200:
        return event_id, "duplicate"
    if status is not None and 400 <= status < 500:
        return event_id, "rejected"
    return event_id, "error"


def handle_payload(payload, device_ids, token, post=send_event):
    event_id, result = process_payload(payload, device_ids, token, post)
    print(f"event_id={event_id} result={result}", flush=True)


def main():
    try:
        config = load_config()
    except ValueError as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        return 1

    def on_connect(client, userdata, flags, reason_code, properties):
        code = int(getattr(reason_code, "value", reason_code))
        if code >= 128:
            print("STOP: MQTT connection was rejected.", file=sys.stderr, flush=True)
            return
        result, _ = client.subscribe(config["IOT_TOPIC"], qos=1)
        if result != mqtt.MQTT_ERR_SUCCESS:
            print("STOP: MQTT subscription could not be started.", file=sys.stderr, flush=True)

    def on_subscribe(client, userdata, mid, reason_codes, properties):
        for reason_code in reason_codes:
            code = int(getattr(reason_code, "value", reason_code))
            print(f"subscribed rc={code}", flush=True)

    def on_message(client, userdata, message):
        handle_payload(
            message.payload,
            config["device_ids"],
            config["REPORTER_TOKEN"],
        )

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=config["IOT_CLIENT_ID"],
        protocol=mqtt.MQTTv5,
    )
    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_message = on_message
    try:
        client.tls_set(
            ca_certs=config["IOT_CA"],
            certfile=config["IOT_CERT"],
            keyfile=config["IOT_KEY"],
            cert_reqs=ssl.CERT_REQUIRED,
            tls_version=ssl.PROTOCOL_TLS_CLIENT,
        )
        client.tls_insecure_set(False)
        client.connect(config["IOT_ENDPOINT"], PORT, keepalive=60)
        client.loop_forever()
    except (OSError, ValueError, RuntimeError, mqtt.MQTTException):
        print("STOP: MQTT consumer stopped because connection setup failed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
