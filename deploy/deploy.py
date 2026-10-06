#!/usr/bin/env python3
"""Deploy the committed inspection service and its private configuration to one EC2 host."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local"
APP_ENV = LOCAL / "app.env"
DB_ENV = LOCAL / "db.env"
PACKAGER = ROOT / "deploy" / "make_user_data.py"


class DeployError(Exception):
    pass


def read_env(path, required_keys):
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o777 != 0o600:
        raise DeployError(f"{path.name} must be a regular file with mode 600.")
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise DeployError(f"{path.name} contains a malformed setting.")
        key, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in values:
            raise DeployError(f"{path.name} contains an invalid or duplicate setting.")
        if not re.fullmatch(r"[A-Za-z0-9._~_-]+", value):
            raise DeployError(f"{path.name} contains an invalid setting.")
        values[key] = value
    if any(not values.get(key) for key in required_keys):
        raise DeployError(f"{path.name} is missing a required setting.")
    return values


def ssh_base(key, user, host):
    return [
        "ssh", "-i", str(key), "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=10", f"{user}@{host}",
    ]


def run_ssh(command, key, user, host, stdin=None):
    try:
        result = subprocess.run(
            [*ssh_base(key, user, host), command],
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=900,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DeployError(f"SSH operation failed ({type(exc).__name__}); no secret values were printed.") from exc
    if result.returncode != 0:
        raise DeployError("SSH operation failed; inspect the host and credentials without printing secret files.")
    return result.stdout


def package_committed_code(commit, output):
    try:
        subprocess.run(
            [sys.executable, str(PACKAGER), commit, str(output)],
            cwd=ROOT,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise DeployError(f"Could not package the committed code ({type(exc).__name__}).") from exc


def current_commit():
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD^{commit}"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise DeployError(f"Could not identify the committed version ({type(exc).__name__}).") from exc
    commit = result.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise DeployError("HEAD is not a valid commit SHA.")
    return commit


def health_check(host, expected_version):
    url = f"http://{host}/health"
    deadline = time.monotonic() + 90
    last_status = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                last_status = response.status
                payload = json.loads(response.read())
            if (last_status == 200 and payload.get("version") == expected_version
                    and payload.get("auth_configured") is True
                    and payload.get("db_configured") is True):
                print("Health verification: HTTP 200; version matches; auth_configured=true; db_configured=true.")
                return
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            pass
        time.sleep(3)
    raise DeployError(f"Health verification failed; last HTTP status was {last_status!r}.")


def run(args):
    key = Path(args.key).expanduser()
    if key.is_symlink() or not key.is_file() or key.stat().st_mode & 0o777 != 0o600:
        raise DeployError("SSH key must be a regular file with mode 600.")
    key = key.resolve()
    if not re.fullmatch(r"[A-Za-z0-9.-]+", args.host):
        raise DeployError("Host must be an IPv4 address or DNS name.")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", args.user):
        raise DeployError("Invalid SSH user.")

    app_values = read_env(APP_ENV, ("REPORTER_TOKEN", "OPERATOR_TOKEN"))
    db_values = read_env(DB_ENV, ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD"))
    if db_values["DB_HOST"] == "pending":
        raise DeployError("RDS endpoint is still pending; wait for db-up.sh to finish.")
    merged = dict(app_values)
    for key_name, value in db_values.items():
        if key_name in merged:
            raise DeployError("The app and database environment files contain duplicate setting names.")
        merged[key_name] = value
    env_bytes = "".join(f"{name}={value}\n" for name, value in merged.items()).encode("utf-8")
    commit = current_commit()

    LOCAL.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, package_name = tempfile.mkstemp(prefix=".w5-package-", suffix=".sh", dir=LOCAL)
    os.close(fd)
    package_path = Path(package_name)
    package_path.unlink()
    try:
        package_committed_code(commit, package_path)
        print(f"Deployment target: {args.user}@{args.host}")
        print(f"Committed version: {commit}")
        if not sys.stdin.isatty() or input("Type yes to deploy and restart inspection: ").strip() != "yes":
            raise DeployError("Cancelled; no remote changes were made.")
        run_ssh("sudo bash -s", key, args.user, args.host, package_path.read_bytes())
        run_ssh(
            "sudo sh -c 'umask 077; cat > /etc/inspection/app.env; chown root:root /etc/inspection/app.env; chmod 600 /etc/inspection/app.env'",
            key, args.user, args.host, env_bytes,
        )
        run_ssh("sudo systemctl restart inspection", key, args.user, args.host)
        health_check(args.host, commit)
    finally:
        package_path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="Current EC2 public IPv4 address or DNS name")
    parser.add_argument("--key", required=True, help="Path to the existing EC2 SSH private key")
    parser.add_argument("--user", default="ec2-user")
    args = parser.parse_args()
    try:
        run(args)
    except (DeployError, OSError, ValueError) as exc:
        message = str(exc) if isinstance(exc, DeployError) else type(exc).__name__
        print("STOP: " + message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
