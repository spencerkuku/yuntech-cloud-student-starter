#!/usr/bin/env bash
# deploy/db-up.sh — W5: create private RDS (PostgreSQL) per spec
set -euo pipefail
set +x

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if ! bash scripts/verify-aws.sh; then
	printf 'STOP: Learner Lab identity verification failed; no W5 resources were created.\n' >&2
	exit 1
fi
python3 deploy/db-up.py
