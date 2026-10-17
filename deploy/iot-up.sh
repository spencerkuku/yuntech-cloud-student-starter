#!/usr/bin/env bash
set -euo pipefail
set +x

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if ! bash scripts/verify-aws.sh; then
	printf 'STOP: Learner Lab identity verification failed; no IoT resources were changed.\n' >&2
	exit 1
fi

exec python3 - "$ROOT" <<'PY'
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from urllib.parse import unquote

ROOT = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(ROOT / "scripts"))
from lab import LabError, clean_env, context, error_code, run_aws, verify

REGION = "us-east-1"
OWNER = "g02-lin"
IOT_DIR = ROOT / ".local" / "iot"
LEDGER = ROOT / ".local" / "resources.json"
POLICY_NAMES = {
    "gateway": f"cxe-{OWNER}-gw",
    "consumer": f"cxe-{OWNER}-consumer",
}


def stop(message):
    raise LabError(message)


def ensure_private_directory(path):
    if path.is_symlink() or path.parent.is_symlink():
        stop(f"Refusing symlinked credential directory: {path.relative_to(ROOT)}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def atomic_write(path, content):
    if path.is_symlink() or path.parent.is_symlink():
        stop(f"Refusing symlinked local file: {path.relative_to(ROOT)}")
    fd, temp_name = tempfile.mkstemp(prefix=".iot-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def write_once(path, content):
    if path.is_symlink():
        stop(f"Refusing symlinked credential file: {path.relative_to(ROOT)}")
    if path.exists():
        if path.read_bytes() != content:
            stop(f"Existing credential differs; refusing to overwrite {path.relative_to(ROOT)}")
        os.chmod(path, 0o600)
        return
    fd, temp_name = tempfile.mkstemp(prefix=".iot-key-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temp_name, path)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != content:
                stop(f"Existing credential differs; refusing to overwrite {path.relative_to(ROOT)}")
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def load_ledger():
    if LEDGER.is_symlink() or not LEDGER.is_file():
        stop(".local/resources.json is missing or is not a regular file; refusing to replace the W3-W5 ledger")
    try:
        resources = json.loads(LEDGER.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        stop(".local/resources.json is unreadable or invalid JSON; refusing to replace it")
    if not isinstance(resources, dict):
        stop(".local/resources.json must contain a JSON object")
    return resources


def save_ledger(resources):
    atomic_write(LEDGER, (json.dumps(resources, indent=2) + "\n").encode("utf-8"))


def record_resource(key, value):
    resources = load_ledger()
    if key in resources and resources[key] != value:
        stop(f"Resource ledger entry {key} conflicts with the expected IoT resource")
    if key not in resources:
        resources[key] = value
        save_ledger(resources)


def parse_create_response(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        cert_id = data["certificateId"]
        cert_pem = data["certificatePem"].encode("utf-8")
        private_key = data["keyPair"]["PrivateKey"].encode("utf-8")
    except (OSError, json.JSONDecodeError, KeyError, TypeError, UnicodeError):
        stop(f"Incomplete protected certificate response at {path.relative_to(ROOT)}; refusing to create another certificate")
    if not isinstance(cert_id, str) or not re.fullmatch(r"[A-Fa-f0-9]{64}", cert_id):
        stop(f"Invalid certificate ID in {path.relative_to(ROOT)}")
    if not cert_pem.startswith(b"-----BEGIN CERTIFICATE-----") or not private_key.startswith(b"-----BEGIN"):
        stop(f"Incomplete certificate material in {path.relative_to(ROOT)}")
    return cert_id, cert_pem, private_key


def create_response_file(path):
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    os.fchmod(fd, 0o600)
    command = [
        "aws", "--profile", "learnerlab", "--region", REGION, "--no-cli-pager",
        "iot", "create-keys-and-certificate", "--set-as-active", "--output", "json",
    ]
    try:
        with os.fdopen(fd, "wb") as output:
            result = subprocess.run(
                command,
                env=clean_env(REGION),
                stdout=output,
                stderr=subprocess.PIPE,
                timeout=90,
                check=False,
            )
            output.flush()
            os.fsync(output.fileno())
    except subprocess.TimeoutExpired:
        stop(f"Certificate creation timed out; protected response retained at {path.relative_to(ROOT)}. Inspect before retrying.")
    except FileNotFoundError:
        stop("AWS CLI is missing; open the course devcontainer")
    except OSError as exc:
        stop(f"Could not securely save certificate response ({type(exc).__name__})")
    if result.returncode:
        if path.stat().st_size == 0:
            path.unlink()
        code = error_code(result.stderr.decode("utf-8", errors="replace"))
        stop(f"iot create-keys-and-certificate failed ({code}); no response was printed")


def ensure_certificate(kind):
    ledger_key = "iot_gw_cert_id" if kind == "gateway" else "iot_consumer_cert_id"
    basename = "gw" if kind == "gateway" else "consumer"
    response_file = IOT_DIR / f".{basename}.create.json"
    cert_file = IOT_DIR / f"{basename}.cert.pem"
    key_file = IOT_DIR / f"{basename}.private.key"
    resources = load_ledger()

    if ledger_key in resources:
        cert_id = resources[ledger_key]
        if not isinstance(cert_id, str) or not re.fullmatch(r"[A-Fa-f0-9]{64}", cert_id):
            stop(f"Invalid certificate ID in .local/resources.json ({ledger_key})")
        if not cert_file.is_file() or not key_file.is_file():
            stop(f"Ledger contains {ledger_key}, but its local certificate files are missing; refusing to recreate it")
        os.chmod(cert_file, 0o600)
        os.chmod(key_file, 0o600)
        if response_file.exists():
            recovered_id, _, _ = parse_create_response(response_file)
            if recovered_id != cert_id:
                stop(f"Protected response conflicts with .local/resources.json ({ledger_key})")
            response_file.unlink()
        return cert_id

    if response_file.is_symlink():
        stop(f"Refusing symlinked certificate response: {response_file.relative_to(ROOT)}")
    create_response_file(response_file)
    os.chmod(response_file, 0o600)
    cert_id, cert_pem, private_key = parse_create_response(response_file)
    write_once(cert_file, cert_pem)
    write_once(key_file, private_key)
    record_resource(ledger_key, cert_id)
    response_file.unlink()
    return cert_id


def make_policy(kind, account):
    if kind == "gateway":
        statements = [
            {
                "Effect": "Allow",
                "Action": "iot:Connect",
                "Resource": f"arn:aws:iot:{REGION}:{account}:client/gw-{OWNER}",
            },
            {
                "Effect": "Allow",
                "Action": "iot:Publish",
                "Resource": f"arn:aws:iot:{REGION}:{account}:topic/cxe/{OWNER}/gw/events",
            },
        ]
    else:
        statements = [
            {
                "Effect": "Allow",
                "Action": "iot:Connect",
                "Resource": f"arn:aws:iot:{REGION}:{account}:client/consumer-{OWNER}",
            },
            {
                "Effect": "Allow",
                "Action": "iot:Subscribe",
                "Resource": f"arn:aws:iot:{REGION}:{account}:topicfilter/cxe/{OWNER}/gw/events",
            },
            {
                "Effect": "Allow",
                "Action": "iot:Receive",
                "Resource": f"arn:aws:iot:{REGION}:{account}:topic/cxe/{OWNER}/gw/events",
            },
        ]
    document = {"Version": "2012-10-17", "Statement": statements}
    if "*" in json.dumps(document):
        stop("Refusing to create a policy containing a wildcard")
    return document


def normalized_policy(policy):
    try:
        text = unquote(policy["policyDocument"])
        return json.loads(text)
    except (KeyError, TypeError, json.JSONDecodeError):
        stop("IoT policy document is unreadable; refusing to reuse it")


def ensure_policy(kind, account):
    ledger_key = "iot_gw_policy" if kind == "gateway" else "iot_consumer_policy"
    name = POLICY_NAMES[kind]
    resources = load_ledger()
    if ledger_key in resources and resources[ledger_key] != name:
        stop(f"Resource ledger entry {ledger_key} conflicts with the expected policy name")

    expected = make_policy(kind, account)
    known = run_aws(["iot", "list-policies", "--scope", "Local"], REGION)
    existing = any(item.get("policyName") == name for item in known.get("policies", []))
    if existing:
        current = run_aws(["iot", "get-policy", "--policy-name", name], REGION)
        if normalized_policy(current) != expected:
            stop(f"Existing IoT policy {name} differs from the least-privilege policy; refusing to overwrite it")
    else:
        run_aws(
            [
                "iot", "create-policy", "--policy-name", name,
                "--policy-document", json.dumps(expected, separators=(",", ":")),
            ],
            REGION,
        )
    record_resource(ledger_key, name)
    return name


def download_ca():
    path = IOT_DIR / "AmazonRootCA1.pem"
    if path.is_symlink():
        stop("Refusing symlinked AmazonRootCA1.pem")
    if path.exists():
        content = path.read_bytes()
    else:
        try:
            with urllib.request.urlopen(
                "https://www.amazontrust.com/repository/AmazonRootCA1.pem",
                timeout=30,
            ) as response:
                content = response.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            stop(f"Could not download Amazon Root CA 1 ({type(exc).__name__})")
        atomic_write(path, content)
    try:
        if not content.startswith(b"-----BEGIN CERTIFICATE-----"):
            raise ValueError("invalid PEM")
        import ssl
        ssl.PEM_cert_to_DER_cert(content.decode("ascii"))
    except (UnicodeError, ValueError):
        stop("AmazonRootCA1.pem is not a valid PEM certificate")
    os.chmod(path, 0o600)


def main():
    ensure_private_directory(IOT_DIR)
    lock_path = IOT_DIR / ".iot-up.lock"
    if lock_path.is_symlink():
        stop("Refusing symlinked IoT setup lock")
    lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    os.fchmod(lock_fd, 0o600)
    with os.fdopen(lock_fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ctx = context()
        if ctx.get("region") != REGION:
            stop("Learner Lab context region must be us-east-1")
        verify(ctx)
        load_ledger()
        account = ctx["account"]
        endpoint_data = run_aws(
            ["iot", "describe-endpoint", "--endpoint-type", "iot:Data-ATS"],
            REGION,
        )
        endpoint = endpoint_data.get("endpointAddress")
        if not isinstance(endpoint, str) or not re.fullmatch(
            r"[A-Za-z0-9.-]+\.iot\.us-east-1\.amazonaws\.com", endpoint
        ):
            stop("AWS returned an invalid us-east-1 IoT Data-ATS endpoint")
        atomic_write(IOT_DIR / "endpoint", (endpoint + "\n").encode("ascii"))

        gateway_id = ensure_certificate("gateway")
        consumer_id = ensure_certificate("consumer")
        gateway_policy = ensure_policy("gateway", account)
        consumer_policy = ensure_policy("consumer", account)

        run_aws(
            [
                "iot", "attach-policy", "--policy-name", gateway_policy,
                "--target", f"arn:aws:iot:{REGION}:{account}:cert/{gateway_id}",
            ],
            REGION,
        )
        run_aws(
            [
                "iot", "attach-policy", "--policy-name", consumer_policy,
                "--target", f"arn:aws:iot:{REGION}:{account}:cert/{consumer_id}",
            ],
            REGION,
        )

        record_resource("iot_gw_policy", gateway_policy)
        record_resource("iot_consumer_policy", consumer_policy)
        download_ca()

        for cert_id in (gateway_id, consumer_id):
            certificate = run_aws(
                ["iot", "describe-certificate", "--certificate-id", cert_id],
                REGION,
            )
            status = certificate.get("certificateDescription", {}).get("status")
            if status != "ACTIVE":
                stop("An IoT certificate is not ACTIVE; inspect its recorded ID before retrying")
        print("IoT setup complete: gateway and consumer certificates are ACTIVE.")


try:
    main()
except LabError as exc:
    print(f"STOP: {exc}", file=sys.stderr)
    raise SystemExit(1) from None
except (OSError, ValueError, TypeError) as exc:
    print(f"STOP: IoT setup failed ({type(exc).__name__}); inspect local state before retrying.", file=sys.stderr)
    raise SystemExit(1) from None
PY
