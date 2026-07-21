#!/usr/bin/env python3
"""fleet_status_refresh.py - Platform-cron fan-out that refreshes fleet status.

Run as a `no_agent` cron job on the Platform Agent. Because cluster subagents are
transient (one-shot invocations, no persistent gateway/cron of their own), the
Platform Agent drives status production: this script enumerates the per-cluster
Cluster Agent profiles and invokes each one-shot to publish its current status via
the `write_handover` tool. Each cluster agent derives its own cluster/location from
its profile identity, so this script passes no identity — only the pinned KUBECONFIG.

Records land at /opt/data/fleet/clusters/<cluster>/<location>/<type>.json, which the
Platform Agent reads with plain file tools.

Resilient by design: one cluster failing does not fail the whole refresh. The script
prints a per-cluster summary and exits 0 so the cron job is not marked failed.
"""

import os
import subprocess
import sys
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/opt/data"))
PROFILES_BASE = HERMES_HOME / "profiles"

# Base/orchestrator profiles that are never Cluster Agents. The Platform Agent
# itself runs under the "platform" profile; "default" is Hermes' fallback home.
# Skip both so the fan-out only ever invokes real per-cluster profiles.
RESERVED_PROFILES = {"default", "platform"}

PUBLISH_PROMPT = (
    "Publish your current health and utilization status via write_handover. "
    "Use the publish-status skill. Reply with a brief acknowledgement only."
)

# Per-cluster invocation timeout (a one-shot diagnostic + publish turn).
INVOKE_TIMEOUT_SECONDS = int(os.environ.get("FLEET_REFRESH_TIMEOUT", "300"))


def _cluster_profiles() -> list[Path]:
    """Cluster Agent profile homes = every profile dir (except reserved base
    profiles) that has a pinned kubeconfig (written by cluster_agent_profile.py at
    scaffold time)."""
    if not PROFILES_BASE.is_dir():
        return []
    homes = []
    for home in sorted(PROFILES_BASE.iterdir()):
        if not home.is_dir() or home.name in RESERVED_PROFILES:
            continue
        if (home / "kubeconfig.yaml").exists():
            homes.append(home)
    return homes


def _invoke(home: Path) -> tuple[bool, str]:
    name = home.name
    env = {
        **os.environ,
        "HOME": "/tmp",
        "HERMES_HOME": str(HERMES_HOME),
        "KUBECONFIG": str(home / "kubeconfig.yaml"),
    }
    try:
        result = subprocess.run(
            ["hermes", "-p", name, "-z", PUBLISH_PROMPT],
            capture_output=True, text=True, timeout=INVOKE_TIMEOUT_SECONDS, env=env,
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {INVOKE_TIMEOUT_SECONDS}s"
    except Exception as e:  # noqa: BLE001 - never let one cluster abort the fan-out
        return False, f"invoke error: {e}"
    if result.returncode != 0:
        return False, (result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}")
    return True, "ok"


def main() -> int:
    homes = _cluster_profiles()
    if not homes:
        print("fleet-status-refresh: no cluster profiles to refresh.")
        return 0

    ok = 0
    lines = []
    for home in homes:
        succeeded, detail = _invoke(home)
        ok += 1 if succeeded else 0
        lines.append(f"  - {home.name}: {'OK' if succeeded else 'FAILED — ' + detail}")

    print(f"fleet-status-refresh: refreshed {ok}/{len(homes)} cluster(s)")
    print("\n".join(lines))
    return 0  # resilient: individual failures are reported, not fatal


if __name__ == "__main__":
    sys.exit(main())
