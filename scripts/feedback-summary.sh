#!/usr/bin/env bash
# Print the copy-pasteable usage and feedback summary of one kube-agents install.
# Read-only: kubectl get on the PlatformAgent resource, plus one kubectl exec
# into the gateway pod for the optional task and token figures. Every flag is
# passed through to agents/platform/scripts/feedback_summary.py (--namespace,
# --name, --context, --kubeconfig, --no-exec).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GENERATOR="$SCRIPT_DIR/../agents/platform/scripts/feedback_summary.py"

exec python3 "$GENERATOR" "$@"
