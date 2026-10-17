import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from scripts.resources import ResourceLedgerError, deployment_target
from scripts import w5_db


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


db_up = load_module("w5_db_up", ROOT / "deploy" / "db-up.py")
matrix = load_module("w5_idempotency_matrix", ROOT / "tests" / "idempotency_matrix.py")


class ResourceLedgerTests(unittest.TestCase):
    def test_resolves_latest_live_round_and_ignores_terminated_round(self):
        resources = {"rounds": [
            {"instance": {"id": "i-0dead", "state": "terminated"},
             "key_pair": {"name": "old-key"}},
            {"instance": {"id": "i-0beef", "state": "running"},
             "key_pair": {"name": "w03-owner-key"}},
        ], "other_week": {"unchanged": True}}
        self.assertEqual(deployment_target(resources), {
            "instance_id": "i-0beef", "key_name": "w03-owner-key", "state": "running"})
        self.assertEqual(resources["other_week"], {"unchanged": True})

    def test_supports_legacy_flat_ledger(self):
        self.assertEqual(deployment_target({
            "instance_id": "i-0123", "key_name": "legacy-key", "other_week": {"id": "kept"}
        }), {"instance_id": "i-0123", "key_name": "legacy-key", "state": "running"})

    def test_missing_target_fields_fail_closed(self):
        cases = (
            {"rounds": [{"instance": {"id": "i-0123", "state": "running"}}]},
            {"rounds": [{"instance": {"id": "not-an-instance", "state": "running"},
                         "key_pair": {"name": "key"}}]},
            {"instance_id": "i-0123"},
            {"rounds": [{"instance": {"id": "i-0123", "state": "terminated"}}],
             "instance_id": "i-0bad", "key_name": "stale-key"},
        )
        for resources in cases:
            with self.subTest(resources=resources), self.assertRaises(ResourceLedgerError):
                deployment_target(resources)



class W5LegacyRdsTests(unittest.TestCase):
    def test_flat_ledger_uses_verified_aws_instance(self):
        ledger = {
            "instance_id": "i-0123",
            "key_name": "old-key",
            "sg_id": "sg-0123",
            "state": "running",
        }
        aws_result = {
            "Instances": [{
                "State": {"Name": "running"},
                "KeyName": "old-key",
                "SubnetId": "subnet-0123",
                "SecurityGroups": [{"GroupId": "sg-0123"}],
            }]
        }
        with mock.patch.object(db_up, "describe_one", return_value=aws_result):
            row = db_up.current_round(ledger, "us-east-1")

        self.assertEqual(row["subnet_id"], "subnet-0123")
        self.assertEqual(row["security_group"]["id"], "sg-0123")

    def test_flat_ledger_rejects_security_group_mismatch(self):
        ledger = {
            "instance_id": "i-0123",
            "key_name": "old-key",
            "sg_id": "sg-wrong",
            "state": "running",
        }
        aws_result = {
            "Instances": [{
                "State": {"Name": "running"},
                "KeyName": "old-key",
                "SubnetId": "subnet-0123",
                "SecurityGroups": [{"GroupId": "sg-real"}],
            }]
        }
        with mock.patch.object(db_up, "describe_one", return_value=aws_result):
            with self.assertRaises(SystemExit):
                db_up.current_round(ledger, "us-east-1")


class W5DatabaseBootstrapTests(unittest.TestCase):
    def test_endpoint_replaces_pending_and_preserves_private_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / "db.env"
            with mock.patch.object(w5_db.secrets, "token_urlsafe", return_value="fixture-password"):
                password = w5_db.load_or_create_password({}, env_path)
            self.assertEqual(password, "fixture-password")
            self.assertEqual(w5_db.read_db_env(env_path), {"DB_PASSWORD": "fixture-password"})
            self.assertFalse(w5_db.record_available_endpoint(
                env_path, "creating", False, None, password))
            self.assertNotIn("DB_HOST", w5_db.read_db_env(env_path))
            self.assertFalse(w5_db.record_available_endpoint(
                env_path, "available", True, "public.example.invalid", password))
            self.assertTrue(w5_db.record_available_endpoint(
                env_path, "available", False, "db.example.invalid", password))
            values = w5_db.read_db_env(env_path)
            self.assertEqual(values["DB_HOST"], "db.example.invalid")
            self.assertEqual(values["DB_PASSWORD"], "fixture-password")
            self.assertEqual(stat.S_IMODE(env_path.stat().st_mode), 0o600)

    def test_existing_password_is_reused_and_missing_recorded_password_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / "db.env"
            w5_db.write_db_env(env_path, {"DB_PASSWORD": "fixture-password"})
            with mock.patch.object(w5_db.secrets, "token_urlsafe") as generate:
                self.assertEqual(w5_db.load_or_create_password({}, env_path), "fixture-password")
                generate.assert_not_called()
            with self.assertRaises(w5_db.DbSettingsError):
                w5_db.load_or_create_password({"db_instance": {"identifier": "w05-test"}},
                                              Path(directory) / "missing.env")
            partial_env = Path(directory) / "partial.env"
            w5_db.write_db_env(partial_env, {"DB_HOST": "existing.example.invalid"})
            with self.assertRaises(w5_db.DbSettingsError):
                w5_db.load_or_create_password({}, partial_env)

    def test_rds_create_uses_private_file_uri_and_removes_request_file(self):
        with tempfile.TemporaryDirectory() as directory:
            captured = {}

            def fake_run_aws(args, region):
                request_path = Path(args[-1].removeprefix("file://"))
                captured.update(args=args, region=region,
                                parameters=json.loads(request_path.read_text(encoding="utf-8")),
                                mode=stat.S_IMODE(request_path.stat().st_mode))
                return {"DBInstance": {"DBInstanceIdentifier": "w05-test"}}

            password = "fixture-password-not-a-real-secret"
            w5_db.create_db_instance(fake_run_aws, "us-east-1", {
                "DBInstanceIdentifier": "w05-test",
                "MasterUserPassword": password,
            }, directory)
            self.assertEqual(captured["args"][:3], ["rds", "create-db-instance", "--cli-input-json"])
            self.assertNotIn(password, " ".join(captured["args"]))
            self.assertEqual(captured["parameters"]["MasterUserPassword"], password)
            self.assertEqual(captured["mode"], 0o600)
            self.assertEqual(list(Path(directory).glob(".rds-create-*.json")), [])

    def test_rds_create_cleans_request_file_when_cli_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            def fail_after_read(args, region):
                self.assertTrue(Path(args[-1].removeprefix("file://")).is_file())
                raise RuntimeError("mock failure")

            with self.assertRaises(RuntimeError):
                w5_db.create_db_instance(fail_after_read, "us-east-1",
                                         {"MasterUserPassword": "fixture-only"}, directory)
            self.assertEqual(list(Path(directory).glob(".rds-create-*.json")), [])

    def test_recorded_rds_is_reused_and_orphan_remote_record_stops(self):
        self.assertFalse(db_up.should_create_rds({"db_instance": {"identifier": "w05-test"}}))
        self.assertTrue(db_up.should_create_rds({}))
        with self.assertRaises(SystemExit):
            db_up.should_create_rds({}, {"DBInstanceIdentifier": "w05-test"})
        self.assertEqual(db_up.recorded_rds_identifier(
            {"db_instance": {"identifier": "w05-owner-pg"}}, "w05-owner-pg"),
            "w05-owner-pg")
        with self.assertRaises(SystemExit):
            db_up.recorded_rds_identifier(
                {"db_instance": {"identifier": "w05-someone-pg"}}, "w05-owner-pg")

    def test_identity_gate_failure_stops_before_db_up(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            (root / "deploy").mkdir()
            verify = root / "scripts" / "verify-aws.sh"
            verify.write_text("#!/usr/bin/env bash\nprintf 'mock identity denied\\n' >&2\nexit 17\n",
                              encoding="utf-8")
            verify.chmod(0o755)
            sentinel = root / "db-up-ran"
            (root / "deploy" / "db-up.py").write_text(
                "from pathlib import Path\nPath('db-up-ran').touch()\n", encoding="utf-8")
            script = root / "deploy" / "db-up.sh"
            script.write_text((ROOT / "deploy" / "db-up.sh").read_text(encoding="utf-8"),
                              encoding="utf-8")
            result = subprocess.run(["bash", str(script)], cwd=root,
                                    capture_output=True, text=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("identity verification failed", result.stderr)
            self.assertFalse(sentinel.exists())


class IdempotencyMatrixTests(unittest.TestCase):
    def test_authorization_stays_in_python_request_not_subprocess_argv(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = b'{"ok":true}'
        token = "fixture-token-not-a-real-credential"
        with mock.patch.object(matrix, "urlopen", return_value=response) as open_url, \
                mock.patch.object(matrix.subprocess, "run") as subprocess_run:
            status, body = matrix.curl_json(
                "http://127.0.0.1/events", headers={"Authorization": f"Bearer {token}"})
        request = open_url.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), f"Bearer {token}")
        self.assertEqual((status, body), (200, {"ok": True}))
        subprocess_run.assert_not_called()

    def test_matrix_fails_if_any_row_or_health_check_fails(self):
        health = {"version": "a" * 40, "db_configured": True}
        rows = [{"number": number, "passed": True} for number in range(1, 6)]
        self.assertTrue(matrix.matrix_passed(200, health, rows))
        for index in range(5):
            failed = [dict(row) for row in rows]
            failed[index]["passed"] = False
            with self.subTest(row=index + 1):
                self.assertFalse(matrix.matrix_passed(200, health, failed))
        self.assertFalse(matrix.matrix_passed(200, {**health, "db_configured": False}, rows))
        self.assertFalse(matrix.matrix_passed(500, health, rows))

    def test_matrix_output_redacts_tokens_inside_nested_bodies(self):
        token = "fixture-token-not-a-real-credential"
        safe = matrix.redact({"body": {"detail": token}}, (token,))
        self.assertEqual(safe, {"body": {"detail": "[REDACTED]"}})

    def test_matrix_main_returns_nonzero_and_redacts_tokens_on_failed_rows(self):
        reporter = "reporter-fixture-token"
        operator = "operator-fixture-token"
        responses = [
            (200, {"version": "a" * 40, "db_configured": True}),
            (201, {"event_id": "test-event", "received_at": "now"}),
            (200, {"event_id": "test-event", "authorization": reporter}),
            (409, {"error": "duplicate_event_id"}),
        ]
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with mock.patch.object(matrix, "get_host", return_value=("127.0.0.1", "i-0123")), \
                    mock.patch.object(matrix, "get_tokens", return_value=(reporter, operator)), \
                    mock.patch.object(matrix, "curl_json", side_effect=responses), \
                    mock.patch.object(matrix.Path, "home", return_value=Path(directory)), \
                    redirect_stdout(output):
                result = matrix.main()
        self.assertNotEqual(result, 0)
        self.assertNotIn(reporter, output.getvalue())
        self.assertNotIn(operator, output.getvalue())


if __name__ == "__main__":
    unittest.main()