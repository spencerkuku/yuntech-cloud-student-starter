#!/usr/bin/env python3
"""Consume the owner's IoT topic and forward only known devices to the API."""
import json
import os
import ssl
import urllib.error
import urllib.request

import paho.mqtt.client as mqtt


def reason_value(reason):
    try:
        return int(reason)
    except (TypeError, ValueError):
        return str(reason)


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
    except urllib.error.HTTPError as exc:
        return exc.code
    except (OSError, urllib.error.URLError):
        return None


def main():
    endpoint = os.environ["IOT_ENDPOINT"]
    topic = os.environ["IOT_TOPIC"]
    client_id = os.environ["IOT_CLIENT_ID"]
    allowed = {item for item in os.environ.get("IOT_DEVICE_IDS", "").split(",") if item}
    reporter_token = os.environ["REPORTER_TOKEN"]

    def on_connect(client, userdata, flags, reason_code, properties):
        print(f"connected rc={reason_value(reason_code)}", flush=True)
        client.subscribe(topic, qos=1)

    def on_subscribe(client, userdata, mid, reason_codes, properties):
        code = reason_codes[0] if reason_codes else 128
        print(f"subscribed rc={reason_value(code)}", flush=True)

    def on_message(client, userdata, message):
        event_id = "unknown"
        result = "error"
        try:
            event = json.loads(message.payload.decode("utf-8"))
            event_id = event.get("event_id", "unknown")
            if event.get("device_id") not in allowed:
                result = "rejected"
            else:
                status = post_event(event, reporter_token)
                if status == 201:
                    result = "created"
                elif status == 200:
                    result = "duplicate"
                elif status is not None and 400 <= status < 500:
                    result = "rejected"
                else:
                    result = "error"
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError, TypeError):
            result = "rejected"
        print(f"event_id={event_id} result={result}", flush=True)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id,
                         protocol=mqtt.MQTTv5)
    client.tls_set(ca_certs=os.environ["IOT_CA"], certfile=os.environ["IOT_CERT"],
                   keyfile=os.environ["IOT_KEY"], cert_reqs=ssl.CERT_REQUIRED,
                   tls_version=ssl.PROTOCOL_TLS_CLIENT)
    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_message = on_message
    client.connect(endpoint, 8883, keepalive=60)
    client.loop_forever()


if __name__ == "__main__":
    main()