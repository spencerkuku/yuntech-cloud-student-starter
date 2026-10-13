# W6 IoT implementation

This directory contains the W6 additions built on the existing W5 service:

- `app/consumer.py`: MQTT consumer with device allow-list enforcement.
- `app/service.py`: W5 persistence service plus `device_id` filtering.
- `deploy/iot-up.sh`: creates the two exact-scope IoT certificates and policies.
- `deploy/deploy.sh`: deploys the service and consumer with protected IoT files.
- `tools/mqtt_check.py`: tests the gateway and consumer MQTT identities.
- `tools/gateway_sim.py`: publishes the 32-message W6 batch.
- `tests/w06_reconcile.py`: reconciles PUBACKs, consumer results, database rows, and filtering.

Copy `w6.env.example` to `.local/w6.env` and keep all `.local/` contents out of
Git and reports. Run the scripts from the repository root after reviewing the
resource scope in `labs/06-iot/README.md`.
