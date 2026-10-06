#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../../.." && pwd)"
exec python3 "$ROOT/practice/spencerku/w3/deploy/w3_aws.py" up