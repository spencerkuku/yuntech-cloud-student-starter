import importlib.util
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("gateway_sim", ROOT / "tools/gateway_sim.py")
gateway_sim = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway_sim)


class GatewayEventContract(unittest.TestCase):
    def test_event_id_uses_device_suffix_and_utc_timestamp(self):
        event = gateway_sim.build_event("g03-m2-r1", "g03-m2-d01", 1)

        self.assertEqual(event["event_id"], "g03-m2-r1-d01-01")
        self.assertEqual(event["device_id"], "g03-m2-d01")
        self.assertEqual(event["observed_at"][-1], "Z")
        self.assertEqual(datetime.fromisoformat(event["observed_at"].replace("Z", "+00:00")).utcoffset().total_seconds(), 0)

    def test_gateway_waits_for_successful_connack_before_publishing(self):
        class Client:
            def __init__(self, reason_code):
                self.reason_code = reason_code
                self.started = False

            def connect(self, endpoint, port, keepalive):
                self.started = True

            def loop_start(self):
                self.on_connect(self, None, None, self.reason_code)

            def loop_stop(self):
                pass

            def disconnect(self):
                pass

        client = Client(0)
        gateway_sim.connect_and_wait(client, "broker.example")
        self.assertTrue(client.started)

        with self.assertRaisesRegex(RuntimeError, "reason code 135"):
            gateway_sim.connect_and_wait(Client(135), "broker.example")


if __name__ == "__main__":
    unittest.main()
