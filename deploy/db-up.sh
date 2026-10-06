#!/usr/bin/env bash
# deploy/db-up.sh — W5: create private RDS (PostgreSQL) per spec
set -euo pipefail
set +x

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

bash scripts/verify-aws.sh || die "identity gate failed"
python3 deploy/db-up.py
