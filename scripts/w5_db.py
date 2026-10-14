"""Private local W5 DB settings and secret-safe AWS CLI request helpers."""
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile


class DbSettingsError(ValueError):
    pass


def load_or_create_password(w5_resources, path):
    path = Path(path)
    values = read_db_env(path)
    password = values.get("DB_PASSWORD")
    if password:
        return password
    if "db_instance" in w5_resources:
        raise DbSettingsError("RDS is recorded but its local DB password is missing; refusing to replace it")
    if path.exists():
        raise DbSettingsError(".local/db.env exists without DB_PASSWORD; refusing to overwrite existing DB settings")
    password = secrets.token_urlsafe(32)
    write_db_env(path, {"DB_PASSWORD": password})
    return password


def read_db_env(path):
    path = Path(path)
    if path.is_symlink():
        raise DbSettingsError("refusing a symlink for .local/db.env")
    if not path.exists():
        return {}
    if not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise DbSettingsError(".local/db.env must be a regular file with mode 600")

    values = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in values:
            raise DbSettingsError(".local/db.env contains an invalid or duplicate setting")
        values[key] = value
    return values


def write_db_env(path, updates):
    path = Path(path)
    if path.is_symlink() or path.parent.is_symlink():
        raise DbSettingsError("refusing a symlink for DB settings")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    values = read_db_env(path)
    for key, value in updates.items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or not isinstance(value, str):
            raise DbSettingsError("invalid DB setting")
        if "\n" in value or "\r" in value:
            raise DbSettingsError("DB settings cannot contain line breaks")
        values[key] = value

    fd, temporary_name = tempfile.mkstemp(prefix=".db.env-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            for key, value in values.items():
                stream.write(f"{key}={value}\n")
        os.replace(temporary_name, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def record_available_endpoint(path, status, publicly_accessible, endpoint, password):
    if status != "available" or publicly_accessible is not False or not endpoint:
        return False
    write_db_env(path, {
        "DB_HOST": endpoint,
        "DB_NAME": "inspection",
        "DB_USER": "inspection",
        "DB_PASSWORD": password,
    })
    return True


def create_db_instance(run_aws, region, parameters, temp_dir):
    """Pass RDS create parameters through a mode-600 JSON file, never argv/env."""
    directory = Path(temp_dir)
    if directory.is_symlink():
        raise DbSettingsError("refusing a symlink for the temporary request directory")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    fd, request_name = tempfile.mkstemp(prefix=".rds-create-", suffix=".json", dir=directory)
    request_path = Path(request_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(parameters, stream, separators=(",", ":"))
            stream.write("\n")
        return run_aws(
            ["rds", "create-db-instance", "--cli-input-json", f"file://{request_path}"],
            region,
        )
    finally:
        request_path.unlink(missing_ok=True)