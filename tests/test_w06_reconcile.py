import importlib.util
import io
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock
from urllib.error import URLError

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests" / "w06_reconcile.py"
SPEC = importlib.util.spec_from_file_location("w06_reconcile", SCRIPT)
reconcile = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reconcile)


class PrefixAndLocalResultTests(unittest.TestCase):
    def test_builds_expected_prefix_and_rejects_invalid_inputs(self):
        self.assertEqual(reconcile.build_prefix("g02-lin", "r1"), "g02-lin-r1")
        for code, run in (
            ("bad/code", "r1"),
            ("g02-lin", "r0"),
            ("g02-lin", "r1;id"),
            ("a" * 37, "r1"),
            ("a" * 30, "r1"),
        ):
            with self.subTest(code=code, run=run), self.assertRaises(reconcile.ReconcileError):
                reconcile.build_prefix(code, run)

    def test_reads_gateway_result_and_requires_matching_prefix(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "w06-last-run.json"
            path.write_text(
                json.dumps({"prefix": "g02-lin-r1", "puback_ok": 32}),
                encoding="utf-8",
            )
            self.assertEqual(reconcile.load_gateway_result(path, "g02-lin-r1"), 32)
            with self.assertRaises(reconcile.ReconcileError):
                reconcile.load_gateway_result(path, "g02-lin-r2")
            path.write_text('{"prefix":"g02-lin-r1","puback_ok":true}', encoding="utf-8")
            with self.assertRaises(reconcile.ReconcileError):
                reconcile.load_gateway_result(path, "g02-lin-r1")

    def test_reads_count_only_valid_consumer_results(self):
        result_lines = "\n".join(
            f"      {count} result={name}"
            for name, count in (
                ("created", 2),
                ("duplicate", 1),
                ("rejected", 1),
                ("error", 1),
            )
        )
        self.assertEqual(
            reconcile.parse_consumer_counts(result_lines),
            {"created": 2, "duplicate": 1, "rejected": 1, "error": 1},
        )

    def test_parses_database_count_and_rejects_malformed_results(self):
        counts, total = reconcile.parse_remote_result(
            "COUNTS_BEGIN\n  30 result=created\n"
            "  1 result=duplicate\n  1 result=rejected\nCOUNTS_END\nDBCOUNT=30\n"
        )
        self.assertEqual(counts, {"created": 30, "duplicate": 1, "rejected": 1, "error": 0})
        self.assertEqual(total, 30)
        for output in (
            "garbage",
            "COUNTS_BEGIN\nCOUNTS_END\nDBCOUNT=not-a-number\n",
            "COUNTS_BEGIN\nCOUNTS_END\nDBCOUNT=1\nDBCOUNT=2\n",
        ):
            with self.subTest(output=output), self.assertRaises(reconcile.ReconcileError):
                reconcile.parse_remote_result(output)


class RemoteAndNetworkTests(unittest.TestCase):
    def test_ssh_uses_deploy_target_user_timeouts_and_parameterized_sql(self):
        result = subprocess.CompletedProcess(
            args=["ssh"],
            returncode=0,
            stdout=b"COUNTS_BEGIN\n  32 result=created\nCOUNTS_END\nDBCOUNT=30\n",
            stderr=b"",
        )
        with mock.patch.object(reconcile.subprocess, "run", return_value=result) as run:
            counts, total = reconcile.ssh_query(
                "203.0.113.10",
                Path("/home/student/.ssh/w03-key"),
                "g02-lin-r1",
            )
        self.assertEqual(counts["created"], 32)
        self.assertEqual(total, 30)
        command = run.call_args.args[0]
        self.assertIn("ec2-user@203.0.113.10", command)
        self.assertIn("ConnectTimeout=10", command)
        self.assertEqual(run.call_args.kwargs["timeout"], reconcile.SSH_TIMEOUT)
        remote_script = run.call_args.kwargs["input"].decode()
        self.assertIn("psql -X -qAt", remote_script)
        self.assertIn("-v ON_ERROR_STOP=1 -v prefix=\"$1\"", remote_script)
        self.assertIn(
            "left(event_id, length(:'prefix')) = :'prefix'",
            reconcile.REMOTE_QUERY,
        )
        self.assertIn('grep -F -- "event_id=${prefix}-"', remote_script)
        self.assertNotIn("g02-lin-r1", remote_script)

    def test_ssh_errors_do_not_expose_stderr_secrets(self):
        secret_output = b"secret-token db-password private-key"
        with mock.patch.object(
            reconcile.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(["ssh"], 255, b"", secret_output),
        ):
            with self.assertRaises(reconcile.ReconcileError) as caught:
                reconcile.ssh_query("203.0.113.10", Path("/key"), "g02-lin-r1")
        self.assertNotIn("secret-token", str(caught.exception))
        self.assertNotIn("db-password", str(caught.exception))
        self.assertNotIn("private-key", str(caught.exception))

        with mock.patch.object(
            reconcile.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["ssh"], 20, output=secret_output),
        ):
            with self.assertRaises(reconcile.ReconcileError) as caught:
                reconcile.ssh_query("203.0.113.10", Path("/key"), "g02-lin-r1")
        self.assertNotIn("secret-token", str(caught.exception))

    def test_ssh_query_classifies_temporary_transport_and_permanent_failures(self):
        temporary_failure = subprocess.CompletedProcess(
            ["ssh"], 255, b"", b"ssh: connect to host 203.0.113.10 port 22: Connection timed out"
        )
        with mock.patch.object(
            reconcile.subprocess, "run", return_value=temporary_failure
        ), self.assertRaises(reconcile.RetryableSSHError):
            reconcile.ssh_query("203.0.113.10", Path("/key"), "g02-lin-r1")

        for result in (
            subprocess.CompletedProcess(["ssh"], 255, b"", b"Permission denied"),
            subprocess.CompletedProcess(["ssh"], 1, b"", b"psql: permission denied"),
            subprocess.CompletedProcess(["ssh"], 0, b"malformed", b""),
        ):
            with self.subTest(result=result), mock.patch.object(
                reconcile.subprocess, "run", return_value=result
            ), self.assertRaises(reconcile.ReconcileError) as caught:
                reconcile.ssh_query("203.0.113.10", Path("/key"), "g02-lin-r1")
            self.assertNotIsInstance(caught.exception, reconcile.RetryableSSHError)

    def test_http_errors_and_timeout_are_safe(self):
        with mock.patch.object(reconcile, "urlopen", side_effect=URLError("secret-token")):
            with self.assertRaises(reconcile.ReconcileError) as caught:
                reconcile.fetch_json("http://127.0.0.1/health")
        self.assertNotIn("secret-token", str(caught.exception))

        response = mock.MagicMock()
        response.status = 200
        response.read.return_value = b'{"events":[]}'
        response.__enter__.return_value = response
        with mock.patch.object(reconcile, "urlopen", return_value=response) as open_url:
            reconcile.fetch_json("http://127.0.0.1/events")
        self.assertEqual(open_url.call_args.kwargs["timeout"], reconcile.HTTP_TIMEOUT)
        with mock.patch.object(reconcile, "urlopen", return_value=response) as open_url:
            reconcile.fetch_json("http://127.0.0.1/events", "fixture-operator-token")
        request = open_url.call_args.args[0]
        self.assertTrue(
            request.get_header("Authorization") == "Bearer fixture-operator-token",
            "operator credentials must be sent using Bearer authorization",
        )

    def test_filtered_event_ids_match_only_current_prefix_and_query_is_encoded(self):
        payload = {
            "events": [
                {"device_id": "g02-lin-d01", "event_id": "g02-lin-r1-d01-01"},
                {"device_id": "g02-lin-d01", "event_id": "g02-lin-r1-d01-02"},
                {"device_id": "g02-lin-d01", "event_id": "g02-lin-r2-d01-01"},
                {"device_id": "g02-lin-d02", "event_id": "g02-lin-r1-d02-01"},
                {"event_id": "g02-lin-r1-d01-03"},
                {"device_id": "g02-lin-d01", "event_id": "other"},
            ]
        }
        with mock.patch.object(reconcile, "fetch_json", return_value=payload) as fetch:
            ids = reconcile.read_filtered_events(
                "203.0.113.10", "g02-lin", "g02-lin-r1", "fixture-operator-token"
            )
        self.assertEqual(ids, ["g02-lin-r1-d01-01", "g02-lin-r1-d01-02"])
        request_url = fetch.call_args.args[0]
        self.assertTrue(request_url.endswith("?device_id=g02-lin-d01"))
        self.assertEqual(fetch.call_args.args[1], "fixture-operator-token")

    def test_retries_temporary_ssh_failure_then_recovers(self):
        now = [0]
        successful_counts = {
            "created": 30,
            "duplicate": 1,
            "rejected": 1,
            "error": 0,
        }

        def fake_ssh(*args, **kwargs):
            if len(query_calls) == 0:
                query_calls.append(now[0])
                raise reconcile.RetryableSSHError("temporary connection failure")
            query_calls.append(now[0])
            return successful_counts.copy(), 30

        def advance(seconds):
            now[0] += seconds

        query_calls = []
        with mock.patch.object(reconcile, "ssh_query", side_effect=fake_ssh):
            counts, total = reconcile.collect_remote_counts(
                "203.0.113.10",
                Path("/key"),
                "g02-lin-r1",
                clock=lambda: now[0],
                sleep=advance,
            )
        self.assertEqual(query_calls, [0, 5])
        self.assertEqual(counts, successful_counts)
        self.assertEqual(total, 30)

    def test_permanent_ssh_failure_stops_without_retry(self):
        sleep = mock.Mock()
        with mock.patch.object(
            reconcile,
            "ssh_query",
            side_effect=reconcile.ReconcileError("remote reconciliation query failed"),
        ) as query:
            with self.assertRaises(reconcile.ReconcileError) as caught:
                reconcile.collect_remote_counts(
                    "203.0.113.10",
                    Path("/key"),
                    "g02-lin-r1",
                    clock=lambda: 0,
                    sleep=sleep,
                )
        self.assertNotIsInstance(caught.exception, reconcile.RetryableSSHError)
        query.assert_called_once()
        sleep.assert_not_called()

    def test_repeated_ssh_timeouts_stop_at_retry_deadline(self):
        now = [0]

        def advance(seconds):
            now[0] += seconds

        with mock.patch.object(
            reconcile,
            "ssh_query",
            side_effect=reconcile.RetryableSSHError("temporary SSH timeout"),
        ) as query:
            with self.assertRaisesRegex(
                reconcile.ReconcileError, "before the retry deadline"
            ):
                reconcile.collect_remote_counts(
                    "203.0.113.10",
                    Path("/key"),
                    "g02-lin-r1",
                    clock=lambda: now[0],
                    sleep=advance,
                )
        self.assertEqual(query.call_count, reconcile.POLL_TIMEOUT // reconcile.POLL_INTERVAL)

    def test_retry_polls_at_five_second_intervals_until_32_or_timeout(self):
        now = [0]
        counts = {"created": 10, "duplicate": 0, "rejected": 0, "error": 0}

        def fake_ssh(*args, **kwargs):
            return counts.copy(), 9

        def advance(seconds):
            now[0] += seconds
            if now[0] >= 10:
                counts["created"] = 32

        with mock.patch.object(reconcile, "ssh_query", side_effect=fake_ssh) as query:
            result_counts, db_count = reconcile.collect_remote_counts(
                "203.0.113.10",
                Path("/key"),
                "g02-lin-r1",
                clock=lambda: now[0],
                sleep=advance,
            )
        self.assertEqual(result_counts["created"], 32)
        self.assertEqual(db_count, 9)
        self.assertEqual(query.call_count, 3)
        self.assertEqual([call.kwargs["timeout"] for call in query.call_args_list],
                         [20, 20, 20])

    def test_reads_current_public_ip_from_verified_learnerlab_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            ssh_dir = home / ".ssh"
            ssh_dir.mkdir()
            key_path = ssh_dir / "w03-key"
            key_path.write_text("offline fixture only", encoding="utf-8")
            key_path.chmod(0o600)
            fake_lab = types.ModuleType("lab")
            fake_lab.LabError = RuntimeError
            fake_lab.context = lambda: {"account": "123456789012", "region": "us-east-1"}
            fake_lab.verify = mock.Mock()
            fake_lab.run_aws = mock.Mock(return_value={
                "State": "running",
                "PublicIp": "203.0.113.25",
                "KeyName": "w03-key",
            })
            fake_resources = types.ModuleType("resources")
            fake_resources.ResourceLedgerError = ValueError
            fake_resources.load_deployment_target = mock.Mock(return_value={
                "instance_id": "i-0123456789abcdef0",
                "key_name": "w03-key",
                "state": "running",
            })
            with mock.patch.dict(
                sys.modules,
                {"lab": fake_lab, "resources": fake_resources},
            ), mock.patch.object(reconcile.Path, "home", return_value=home), \
                    mock.patch.object(reconcile, "RESOURCES_FILE", home / "resources.json"):
                ip, resolved_key = reconcile.resolve_target_and_ip()
            self.assertEqual(ip, "203.0.113.25")
            self.assertEqual(resolved_key, key_path)
            fake_lab.verify.assert_called_once()
            self.assertEqual(fake_lab.run_aws.call_args.args[1], "us-east-1")


class CredentialsAndReportTests(unittest.TestCase):
    def test_reads_operator_token_without_echo_and_requires_private_permissions(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "app.env"
            secret = "fixture-only-operator-token"
            path.write_text(
                f"REPORTER_TOKEN=reporter\nOPERATOR_TOKEN={secret}\nDB_PASSWORD=db-secret\n",
                encoding="utf-8",
            )
            path.chmod(0o600)
            token = reconcile.load_operator_token(path)
            self.assertEqual(token, secret)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            output = io.StringIO()
            with mock.patch.object(reconcile, "print_report") as print_report:
                self.assertNotIn(secret, output.getvalue())
                print_report.assert_not_called()

    def test_report_format_contains_expected_values_without_credentials(self):
        output = io.StringIO()
        health = {"version": "a" * 40, "db_configured": True}
        counts = {"created": 30, "duplicate": 1, "rejected": 1, "error": 0}
        with mock.patch("sys.stdout", output):
            passed = reconcile.print_report(
                health,
                "g02-lin-r1",
                32,
                counts,
                30,
                ["g02-lin-r1-d01-01", "g02-lin-r1-d01-02", "g02-lin-r1-d01-03"],
            )
        text = output.getvalue()
        self.assertTrue(passed)
        self.assertIn("version=" + "a" * 40, text)
        self.assertIn("db_configured=true", text)
        self.assertIn("prefix=g02-lin-r1", text)
        self.assertIn("puback_ok=32/32", text)
        self.assertIn("consumer_complete=true", text)
        self.assertIn("consumer created=30 duplicate=1 rejected=1 error=0", text)
        self.assertIn("database_count=30", text)
        self.assertIn("d01_count=3", text)
        self.assertIn("reconcile=PASS", text)
        self.assertNotIn("token", text.lower())
        self.assertNotIn("password", text.lower())

    def test_main_reports_success_and_nonzero_for_reconciliation_mismatches(self):
        expected_counts = {
            "created": 30,
            "duplicate": 1,
            "rejected": 1,
            "error": 0,
        }
        cases = (
            (
                "success",
                32,
                expected_counts,
                30,
                ["g02-lin-r1-d01-01", "g02-lin-r1-d01-02", "g02-lin-r1-d01-03"],
                0,
                "reconcile=PASS",
            ),
            (
                "partial consumer",
                32,
                {"created": 20, "duplicate": 0, "rejected": 0, "error": 0},
                20,
                ["g02-lin-r1-d01-01"],
                1,
                "consumer_complete=false",
            ),
            (
                "database mismatch",
                32,
                expected_counts,
                29,
                ["g02-lin-r1-d01-01", "g02-lin-r1-d01-02", "g02-lin-r1-d01-03"],
                1,
                "reconcile_failures=database_count",
            ),
            (
                "d01 mismatch",
                32,
                expected_counts,
                30,
                ["g02-lin-r1-d01-01", "g02-lin-r1-d01-02"],
                1,
                "reconcile_failures=d01_count",
            ),
        )
        for name, puback, counts, db_count, event_ids, expected_exit, report_marker in cases:
            with self.subTest(name=name):
                output = io.StringIO()
                with (
                    mock.patch.object(
                        reconcile, "validate_last_run_prefix", return_value=puback
                    ),
                    mock.patch.object(
                        reconcile,
                        "resolve_target_and_ip",
                        return_value=("203.0.113.10", Path("/offline/key")),
                    ),
                    mock.patch.object(
                        reconcile,
                        "read_health",
                        return_value={"version": "test-version", "db_configured": True},
                    ),
                    mock.patch.object(
                        reconcile, "load_operator_token", return_value="fixture-token"
                    ),
                    mock.patch.object(
                        reconcile,
                        "collect_remote_counts",
                        return_value=(counts, db_count),
                    ),
                    mock.patch.object(
                        reconcile, "read_filtered_events", return_value=event_ids
                    ),
                    mock.patch("sys.stdout", output),
                ):
                    exit_code = reconcile.main(["--code", "g02-lin", "--run", "r1"])

                report = output.getvalue()
                self.assertEqual(exit_code, expected_exit)
                self.assertIn(report_marker, report)
                self.assertIn(f"puback_ok={puback}/32", report)
                self.assertIn(f"database_count={db_count}", report)
                self.assertIn(f"d01_count={len(event_ids)}", report)
                self.assertNotIn("fixture-token", report)


if __name__ == "__main__":
    unittest.main()
