#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../../.." && pwd)"
if [[ "${1:-}" == "--stop" ]]; then
    shift
    [[ "$#" -eq 0 ]] || { echo "usage: $0 [--stop]" >&2; exit 2; }
    exec python3 "$ROOT/practice/spencerku/w3/deploy/w3_aws.py" stop
fi
[[ "$#" -eq 0 ]] || { echo "usage: $0 [--stop]" >&2; exit 2; }
exec python3 "$ROOT/practice/spencerku/w3/deploy/w3_aws.py" down