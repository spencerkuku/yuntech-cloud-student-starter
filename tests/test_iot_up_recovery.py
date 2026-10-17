import json
from pathlib import Path
import re
import subprocess
import tempfile
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "iot-up.sh"


def load_iot_up_functions(iot_dir, ledger):
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    source = SCRIPT.read_text(encoding="utf-8")
    match = re.search(
        r"^exec python3 - \"\$ROOT\" <<'PY'\n(.*?)^PY$",
        source,
        re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise AssertionError("embedded iot-up Python program not found")
    embedded = match.group(1)
    embedded = embedded.split("\ntry:\n    main()\n", 1)[0] + "\n"
    namespace = {"__name__": "iot_up_recovery_test"}
    exec(compile(embedded, str(SCRIPT), "exec"), namespace)
    namespace["ROOT"] = iot_dir.parents[1]
    namespace["IOT_DIR"] = iot_dir
    namespace["LEDGER"] = ledger
    return namespace


class IotUpRecoveryTests(unittest.TestCase):
    certificate_id = "a" * 64
    response = {
        "certificateId": certificate_id,
        "certificatePem": "-----BEGIN CERTIFICATE-----\ntest\n-----END CERTIFICATE-----\n",
        "keyPair": {
            "PrivateKey": "-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----\n",
        },
    }

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        base = Path(self.tempdir.name)
        self.iot_dir = base / ".local" / "iot"
        self.iot_dir.mkdir(parents=True, mode=0o700)
        self.ledger = base / ".local" / "resources.json"
        self.ledger.write_text(
            json.dumps({"w3_instance_id": "i-preserved", "w5_database": "preserved"}),
            encoding="utf-8",
        )
        self.module = load_iot_up_functions(self.iot_dir, self.ledger)
        self.run_aws_mock = mock.patch.object(self.module["subprocess"], "run")
        self.run_aws = self.run_aws_mock.start()
        self.addCleanup(self.run_aws_mock.stop)
        self.addCleanup(self.tempdir.cleanup)

    def paths(self):
        return (
            self.iot_dir / ".gw.create.json",
            self.iot_dir / ".gw.create.state.json",
            self.iot_dir / "gw.cert.pem",
            self.iot_dir / "gw.private.key",
        )

    def mock_successful_create(self):
        payload = (json.dumps(self.response) + "\n").encode()

        def create(*args, **kwargs):
            kwargs["stdout"].write(payload)
            return types.SimpleNamespace(returncode=0, stderr=b"")

        self.run_aws.side_effect = create

    def test_timeout_keeps_state_and_retry_does_not_create_again(self):
        self.run_aws.side_effect = subprocess.TimeoutExpired(["aws"], 90)
        with self.assertRaises(self.module["LabError"]):
            self.module["ensure_certificate"]("gateway")

        response, state, _, _ = self.paths()
        self.assertTrue(state.is_file())
        self.assertTrue(response.is_file())
        with self.assertRaises(self.module["LabError"]):
            self.module["ensure_certificate"]("gateway")
        self.run_aws.assert_called_once()

    def test_blank_response_keeps_state_and_retry_does_not_create_again(self):
        self.run_aws.return_value = types.SimpleNamespace(returncode=0, stderr=b"")
        with self.assertRaises(self.module["LabError"]):
            self.module["ensure_certificate"]("gateway")

        _, state, _, _ = self.paths()
        self.assertTrue(state.is_file())
        with self.assertRaises(self.module["LabError"]):
            self.module["ensure_certificate"]("gateway")
        self.run_aws.assert_called_once()

    def test_nonzero_empty_response_is_preserved_and_not_retried(self):
        self.run_aws.return_value = types.SimpleNamespace(returncode=1, stderr=b"request failed")
        with self.assertRaises(self.module["LabError"]):
            self.module["ensure_certificate"]("gateway")

        response, state, _, _ = self.paths()
        self.assertTrue(response.is_file())
        self.assertEqual(response.read_bytes(), b"")
        self.assertTrue(state.is_file())
        with self.assertRaises(self.module["LabError"]):
            self.module["ensure_certificate"]("gateway")
        self.run_aws.assert_called_once()

    def test_interrupted_local_save_resumes_without_second_create(self):
        self.mock_successful_create()
        write_once = self.module["write_once"]

        def fail_on_private_key(path, content):
            if path.name == "gw.private.key":
                raise OSError("simulated local write failure")
            write_once(path, content)

        with mock.patch.dict(self.module, {"write_once": fail_on_private_key}):
            with self.assertRaises(OSError):
                self.module["ensure_certificate"]("gateway")

        response, state, cert, key = self.paths()
        self.assertTrue(response.is_file())
        self.assertTrue(state.is_file())
        self.assertTrue(cert.is_file())
        self.assertFalse(key.exists())

        self.assertEqual(
            self.module["ensure_certificate"]("gateway"),
            self.certificate_id,
        )
        self.run_aws.assert_called_once()
        self.assertFalse(response.exists())
        self.assertFalse(state.exists())
        self.assertTrue(cert.is_file())
        self.assertTrue(key.is_file())
        saved_ledger = json.loads(self.ledger.read_text(encoding="utf-8"))
        self.assertEqual(saved_ledger["w3_instance_id"], "i-preserved")
        self.assertEqual(saved_ledger["w5_database"], "preserved")
        self.assertEqual(saved_ledger["iot_gw_cert_id"], self.certificate_id)

    def test_interrupted_after_ledger_write_only_cleans_verified_state(self):
        self.mock_successful_create()
        record_resource = self.module["record_resource"]

        def fail_after_ledger_write(key, value):
            record_resource(key, value)
            raise OSError("simulated interruption after ledger write")

        with mock.patch.dict(self.module, {"record_resource": fail_after_ledger_write}):
            with self.assertRaises(OSError):
                self.module["ensure_certificate"]("gateway")

        response, state, cert, key = self.paths()
        self.assertTrue(response.is_file())
        self.assertTrue(state.is_file())
        self.assertTrue(cert.is_file())
        self.assertTrue(key.is_file())

        self.assertEqual(
            self.module["ensure_certificate"]("gateway"),
            self.certificate_id,
        )
        self.run_aws.assert_called_once()
        self.assertFalse(response.exists())
        self.assertFalse(state.exists())

    def test_state_marker_without_response_stops_without_create(self):
        _, state, _, _ = self.paths()
        state.write_text('{"state":"request_may_have_been_sent"}\n', encoding="utf-8")

        with self.assertRaises(self.module["LabError"]):
            self.module["ensure_certificate"]("gateway")
        self.run_aws.assert_not_called()


if __name__ == "__main__":
    unittest.main()
