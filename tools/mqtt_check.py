#!/usr/bin/env python3
import argparse
import math
from pathlib import Path
import ssl
import sys
import threading
import time

import paho.mqtt.client as mqtt

ROOT = Path(__file__).resolve().parents[1]
IOT_DIR = ROOT / ".local" / "iot"
OWNER = "g02-lin"
PORT = 8883
TEST_PAYLOAD = b'{"check":"t2","code":"g02-lin"}'


def reason_value(reason_code):
    return int(getattr(reason_code, "value", reason_code))


def make_parser():
    parser = argparse.ArgumentParser(description="Check an AWS IoT Core MQTT identity.")
    parser.add_argument("--as", dest="identity", choices=("gw", "consumer"), required=True)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--pub", metavar="TOPIC")
    actions.add_argument("--sub", metavar="TOPIC_FILTER")
    parser.add_argument("--wait", type=float, help="Seconds to wait for messages after SUBACK.")
    return parser


def read_configuration(identity):
    basename = "gw" if identity == "gw" else "consumer"
    client_id = f"gw-{OWNER}" if identity == "gw" else f"consumer-{OWNER}"
    endpoint_path = IOT_DIR / "endpoint"
    ca_path = IOT_DIR / "AmazonRootCA1.pem"
    cert_path = IOT_DIR / f"{basename}.cert.pem"
    key_path = IOT_DIR / f"{basename}.private.key"
    for path in (endpoint_path, ca_path, cert_path, key_path):
        if not path.is_file():
            raise FileNotFoundError(f"Required IoT file is missing: {path.relative_to(ROOT)}")
    endpoint = endpoint_path.read_text(encoding="ascii").strip()
    if not endpoint or any(char.isspace() for char in endpoint):
        raise ValueError("IoT endpoint file is empty or malformed")
    return endpoint, client_id, ca_path, cert_path, key_path


def main():
    parser = make_parser()
    args = parser.parse_args()
    if args.wait is not None and args.sub is None:
        parser.error("--wait can only be used with --sub")
    wait_seconds = 60.0 if args.wait is None else args.wait
    if not math.isfinite(wait_seconds) or wait_seconds < 0:
        parser.error("--wait must be a finite number zero or greater")

    try:
        endpoint, client_id, ca_path, cert_path, key_path = read_configuration(args.identity)
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    connack_event = threading.Event()
    state_lock = threading.Lock()
    state = {"connack": None, "pubacks": {}, "subacks": {}}

    def on_connect(client, userdata, flags, reason_code, properties):
        code = reason_value(reason_code)
        with state_lock:
            state["connack"] = code
        print(f"connack={code}", flush=True)
        connack_event.set()

    def on_publish(client, userdata, mid, reason_code, properties):
        code = reason_value(reason_code)
        with state_lock:
            state["pubacks"][mid] = code

    def on_subscribe(client, userdata, mid, reason_codes, properties):
        codes = [reason_value(code) for code in reason_codes]
        with state_lock:
            state["subacks"][mid] = codes

    def on_message(client, userdata, message):
        payload = message.payload.decode("utf-8", errors="replace")
        print(f"recv topic={message.topic} payload={payload}", flush=True)

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
        protocol=mqtt.MQTTv5,
    )
    client.on_connect = on_connect
    client.on_publish = on_publish
    client.on_subscribe = on_subscribe
    client.on_message = on_message
    try:
        client.tls_set(
            ca_certs=str(ca_path),
            certfile=str(cert_path),
            keyfile=str(key_path),
            cert_reqs=ssl.CERT_REQUIRED,
            tls_version=ssl.PROTOCOL_TLS_CLIENT,
        )
        client.tls_insecure_set(False)
        client.connect(endpoint, PORT, keepalive=60)
        client.loop_start()
    except (OSError, ValueError, RuntimeError, mqtt.MQTTException) as exc:
        print(f"ERROR: MQTT connection setup failed ({type(exc).__name__})", file=sys.stderr)
        return 1

    try:
        if not connack_event.wait(timeout=15):
            print("ERROR: timed out waiting for CONNACK", file=sys.stderr)
            return 1
        with state_lock:
            connack = state["connack"]
        if connack is None or connack >= 128:
            print("ERROR: broker rejected the MQTT connection", file=sys.stderr)
            return 1

        if args.pub is not None:
            info = client.publish(args.pub, payload=TEST_PAYLOAD, qos=1)
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                print(f"ERROR: publish failed (reason={info.rc})", file=sys.stderr)
                return 1
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                with state_lock:
                    ack = state["pubacks"].get(info.mid)
                if ack is not None:
                    print(f"puback={ack}", flush=True)
                    return 0 if ack < 128 else 1
                time.sleep(0.02)
            print("ERROR: timed out waiting for PUBACK", file=sys.stderr)
            return 1

        result, mid = client.subscribe(args.sub, qos=1)
        if result != mqtt.MQTT_ERR_SUCCESS:
            print(f"ERROR: subscribe failed (reason={result})", file=sys.stderr)
            return 1
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with state_lock:
                suback = state["subacks"].get(mid)
            if suback is not None:
                code = suback[0] if suback else 128
                print(f"suback={code}", flush=True)
                if code >= 128:
                    return 1
                break
            time.sleep(0.02)
        else:
            print("ERROR: timed out waiting for SUBACK", file=sys.stderr)
            return 1

        time.sleep(wait_seconds)
        return 0
    finally:
        if client.is_connected():
            client.disconnect()
        client.loop_stop()


if __name__ == "__main__":
    raise SystemExit(main())
