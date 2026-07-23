#!/usr/bin/env python3
"""reconcile_cluster_agents.py — deterministically reconcile GKE clusters with Cluster
Agent profiles. Intended to run as a `no_agent` cron job on the gateway's ticking profile.

Source of truth = the GKE resource label ``managed-by-kube-agents=true``. For every cluster
carrying that label there should be exactly one Cluster Agent profile; every profile whose
cluster is gone OR no longer carries the label is pruned.

  desired = clusters in the project with resourceLabels.managed-by-kube-agents=true
            (minus RESERVED_CLUSTERS, e.g. the management cluster)
  actual  = <HERMES_HOME>/profiles/cluster-* profiles, keyed by their stamped
            ``cluster_identity`` (project/cluster/location) — a deterministic map that does
            not depend on parsing the sanitized/hashed profile name.

  create  = desired − actual   -> cluster_agent_profile.py create
  prune   = actual − desired   -> cluster_agent_profile.py delete   (cluster gone OR unlabeled)

No LLM. Idempotent and resilient: individual failures are logged, the run still exits 0 so a
transient error never marks the cron failed. Set RECONCILE_DRY_RUN=1 to preview with no changes.

Env knobs:
  RECONCILE_PROJECT   GCP project (default: `gcloud config get-value project`)
  RECONCILE_LABEL     label key (default: managed-by-kube-agents)
  RECONCILE_EXCLUDE   comma-separated cluster names never reconciled (default: kage-management)
  RECONCILE_DRY_RUN   "1"/"true" -> preview only
"""

import os
import re
import subprocess
import sys
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/opt/data"))
PROFILES_BASE = HERMES_HOME / "profiles"
CLUSTER_AGENT_PROFILE = HERMES_HOME / "scripts" / "cluster_agent_profile.py"
# cluster_agent_profile.py lazily imports pyyaml on the create path, so drive it with the
# Hermes venv interpreter (which has pyyaml) rather than whatever runs this cron script.
CLUSTER_AGENT_PY = os.environ.get("CLUSTER_AGENT_PY", "/opt/hermes/.venv/bin/python3")

LABEL = os.environ.get("RECONCILE_LABEL", "managed-by-kube-agents")
RESERVED = {c for c in os.environ.get("RECONCILE_EXCLUDE", "kage-management").split(",") if c}
DRY_RUN = os.environ.get("RECONCILE_DRY_RUN", "").lower() not in ("", "0", "false", "no")


def _project() -> str:
    p = os.environ.get("RECONCILE_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT")
    if p:
        return p
    try:
        r = subprocess.run(
            ["gcloud", "config", "get-value", "project"],
            capture_output=True, text=True, timeout=30,
        )
        return r.stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def _desired(project: str) -> set:
    """Clusters (project, name, location) carrying the managed-by-kube-agents label."""
    try:
        r = subprocess.run(
            ["gcloud", "container", "clusters", "list", "--project", project,
             f"--filter=resourceLabels.{LABEL}=true", "--format=value(name,location)"],
            capture_output=True, text=True, timeout=120,
        )
    except Exception as e:  # noqa: BLE001
        print(f"reconcile: ERROR listing clusters: {e}", file=sys.stderr)
        return set()
    out = set()
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] not in RESERVED:
            out.add((project, parts[0], parts[1]))
    return out


def _read_identity(config_path: Path):
    """Parse the `cluster_identity` block from a profile config.yaml (no yaml dependency)."""
    if not config_path.exists():
        return None
    ident, in_block = {}, False
    for raw in config_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if re.match(r"^cluster_identity:\s*$", raw):
            in_block = True
            continue
        if in_block:
            m = re.match(r"^\s+(project|cluster|location):\s*(.+?)\s*$", raw)
            if m:
                ident[m.group(1)] = m.group(2).strip().strip("\"'")
            elif re.match(r"^\S", raw):  # dedent → block ended
                break
    if all(k in ident for k in ("project", "cluster", "location")):
        return (ident["project"], ident["cluster"], ident["location"])
    return None


def _actual() -> dict:
    """Map identity tuple -> profile dir name for every cluster-* profile.

    Profiles whose identity can't be read are keyed as ('?', '?', <dirname>) so they surface
    as anomalies and are never auto-deleted (we can't safely resolve their create/delete args).
    """
    res = {}
    if not PROFILES_BASE.is_dir():
        return res
    for d in sorted(PROFILES_BASE.iterdir()):
        if not d.is_dir() or not d.name.startswith("cluster-"):
            continue
        ident = _read_identity(d / "config.yaml")
        res[ident if ident else ("?", "?", d.name)] = d.name
    return res


def _run(action: str, project: str, cluster: str, location: str):
    cmd = [CLUSTER_AGENT_PY, str(CLUSTER_AGENT_PROFILE), action,
           "--project", project, "--cluster", cluster, "--location", location]
    env = {**os.environ, "HOME": "/tmp", "HERMES_HOME": str(HERMES_HOME)}
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=env)
    except Exception as e:  # noqa: BLE001
        return False, f"exec error: {e}"
    ok = r.returncode == 0
    return ok, (r.stdout.strip() or r.stderr.strip() or f"exit {r.returncode}")


def main() -> int:
    project = _project()
    if not project:
        print("reconcile-cluster-agents: no project resolved; nothing to do.")
        return 0

    desired = _desired(project)
    actual = _actual()
    actual_known = {k for k in actual if k[0] != "?"}

    to_create = sorted(desired - actual_known)
    to_prune = sorted(k for k in actual if k not in desired)  # gone OR unlabeled OR unknown-id
    keep = sorted(desired & actual_known)

    print(f"reconcile-cluster-agents: project={project} label={LABEL} dry_run={DRY_RUN}")
    print(f"  desired={len(desired)} profiles={len(actual)} keep={len(keep)} "
          f"create={len(to_create)} prune={len(to_prune)}")

    for project_, cluster, location in to_create:
        if DRY_RUN:
            print(f"  + CREATE {cluster}/{location}")
            continue
        ok, msg = _run("create", project_, cluster, location)
        print(f"  + CREATE {cluster}/{location}: {'OK' if ok else 'FAILED — ' + msg}")

    for key in to_prune:
        name = actual[key]
        if key[0] == "?":
            print(f"  ! ANOMALY  {name}: unreadable cluster_identity — skipping delete, inspect manually")
            continue
        project_, cluster, location = key
        if DRY_RUN:
            print(f"  - PRUNE  {cluster}/{location} ({name})")
            continue
        ok, msg = _run("delete", project_, cluster, location)
        print(f"  - PRUNE  {cluster}/{location} ({name}): {'OK' if ok else 'FAILED — ' + msg}")

    for _project, cluster, location in keep:
        print(f"  = KEEP   {cluster}/{location}")

    return 0  # resilient: never fail the cron on a transient error


if __name__ == "__main__":
    sys.exit(main())
