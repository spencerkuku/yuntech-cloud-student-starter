#!/usr/bin/env python3
"""Consume IoT events, enforce the device allow-list, and report to the API."""
import json
import os
import ssl
import sys
import urllib.error
import urllib.request

import paho.mqtt.client as mqtt


def settings():
    required = (
        "IOT_ENDPOINT", "IOT_TOPIC", "IOT_CLIENT_ID", "IOT_DEVICE_IDS",
        "IOT_CA", "IOT_CERT", "IOT_KEY", "REPORTER_TOKEN",
    )
    values = {name: os.environ.get(name, "").strip() for name in required}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise RuntimeError("missing consumer configuration: " + ", ".join(missing))
    values["devices"] = {item.strip() for item in values["IOT_DEVICE_IDS"].split(",") if item.strip()}
    return values


def post_event(event, token):
    request = urllib.request.Request(
        "http://127.0.0.1:8080/events",
        data=json.dumps(event, separators=(",", ":")).encode("utf-8"),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except (urllib.error.URLError, TimeoutError):
        return 599


def main():
    config = settings()
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=config["IOT_CLIENT_ID"],
        protocol=mqtt.MQTTv5,
    )
    client.tls_set(
        ca_certs=config["IOT_CA"],
        certfile=config["IOT_CERT"],
        keyfile=config["IOT_KEY"],
        cert_reqs=ssl.CERT_REQUIRED,
        tls_version=ssl.PROTOCOL_TLS_CLIENT,
    )

    def on_connect(mqtt_client, _userdata, _flags, reason_code, _properties):
        if reason_code.is_failure:
            print(f"connect failed rc={reason_code}", flush=True)
            return
        result, _ = mqtt_client.subscribe(config["IOT_TOPIC"], qos=1)
        if result != mqtt.MQTT_ERR_SUCCESS:
            print(f"subscribe failed rc={result}", flush=True)
            return
        print("subscribed rc=1", flush=True)

    def on_message(_mqtt_client, _userdata, message):
        try:
            event = json.loads(message.payload.decode("utf-8"))
            event_id = event.get("event_id", "")
            device_id = event.get("device_id", "")
            if device_id not in config["devices"]:
                print(f"event_id={event_id} result=rejected", flush=True)
                return
            status = post_event(event, config["REPORTER_TOKEN"])
            result = (
                "created" if status == 201 else
                "duplicate" if status == 200 else
                "rejected" if 400 <= status < 500 else
                "error"
            )
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError, TypeError):
            event_id = ""
            result = "rejected"
        print(f"event_id={event_id} result={result}", flush=True)

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(config["IOT_ENDPOINT"], 8883, keepalive=60)
    client.loop_forever()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"consumer startup failed: {type(error).__name__}", file=sys.stderr)
        raise
