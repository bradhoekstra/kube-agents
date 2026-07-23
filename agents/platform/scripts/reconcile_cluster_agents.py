#!/usr/bin/env python3
"""reconcile_cluster_agents.py — deterministically reconcile GKE clusters with Cluster
Agent profiles. Intended to run as a `no_agent` cron job on the gateway's ticking profile.

Policy: **every cluster in the project gets a Cluster Agent profile, except the management
cluster where kube-agents itself runs** (and any names in RECONCILE_EXCLUDE).

  desired = all clusters in the project − self(management) − RECONCILE_EXCLUDE
  actual  = <HERMES_HOME>/profiles/cluster-* profiles, keyed by their stamped
            ``cluster_identity`` (project/cluster/location) — a deterministic map that does
            not depend on parsing the sanitized/hashed profile name.

  create  = desired − actual   -> cluster_agent_profile.py create
  prune   = actual  − desired  -> cluster_agent_profile.py delete   (cluster gone, or it IS
                                  the management cluster / excluded)

The management cluster is identified **by self-identity, not by name**: the pod asks the GKE
metadata server which cluster its node belongs to (`instance/attributes/cluster-name` +
`cluster-location`). This works no matter what the customer named the cluster. If self-identity
cannot be determined, the run aborts WITHOUT changes (fail-safe) so we never create a profile
for the management cluster or mis-prune.

No LLM. Idempotent; individual create/delete failures are logged and the run still exits 0.
Set RECONCILE_DRY_RUN=1 to preview.

Env knobs:
  RECONCILE_PROJECT   GCP project (default: metadata project-id, then gcloud config)
  RECONCILE_EXCLUDE   comma-separated extra cluster names to never manage
  RECONCILE_DRY_RUN   "1"/"true" -> preview only
"""

import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/opt/data"))
PROFILES_BASE = HERMES_HOME / "profiles"
CLUSTER_AGENT_PROFILE = HERMES_HOME / "scripts" / "cluster_agent_profile.py"
# cluster_agent_profile.py lazily imports pyyaml on the create path, so drive it with the
# Hermes venv interpreter (which has pyyaml) rather than whatever runs this cron script.
CLUSTER_AGENT_PY = os.environ.get("CLUSTER_AGENT_PY", "/opt/hermes/.venv/bin/python3")

EXTRA_EXCLUDE = {c for c in os.environ.get("RECONCILE_EXCLUDE", "").split(",") if c}
DRY_RUN = os.environ.get("RECONCILE_DRY_RUN", "").lower() not in ("", "0", "false", "no")

_MD_BASE = "http://metadata.google.internal/computeMetadata/v1/"


def _metadata(path: str):
    """Read a GKE/GCE metadata value, or None if unavailable."""
    try:
        req = urllib.request.Request(_MD_BASE + path, headers={"Metadata-Flavor": "Google"})
        return urllib.request.urlopen(req, timeout=5).read().decode().strip()
    except Exception:  # noqa: BLE001
        return None


def _project() -> str:
    p = os.environ.get("RECONCILE_PROJECT") or _metadata("project/project-id")
    if p:
        return p
    try:
        r = subprocess.run(["gcloud", "config", "get-value", "project"],
                           capture_output=True, text=True, timeout=30)
        return r.stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def _self_cluster():
    """(name, location) of the management cluster this pod runs on, or None if unknown."""
    name = _metadata("instance/attributes/cluster-name")
    location = _metadata("instance/attributes/cluster-location")
    if name and location:
        return (name, location)
    return None


def _all_clusters(project: str) -> set:
    """Every cluster (project, name, location) in the project."""
    try:
        r = subprocess.run(
            ["gcloud", "container", "clusters", "list", "--project", project,
             "--format=value(name,location)"],
            capture_output=True, text=True, timeout=120,
        )
    except Exception as e:  # noqa: BLE001
        print(f"reconcile: ERROR listing clusters: {e}", file=sys.stderr)
        return set()
    out = set()
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
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

    Profiles whose identity can't be read are keyed ('?', '?', <dirname>) so they surface as
    anomalies and are never auto-deleted (we can't resolve their create/delete args).
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
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=env)
    except Exception as e:  # noqa: BLE001
        return False, f"exec error: {e}"
    ok = r.returncode == 0
    return ok, (r.stdout.strip() or r.stderr.strip() or f"exit {r.returncode}")


def main() -> int:
    project = _project()
    if not project:
        print("reconcile-cluster-agents: no project resolved; aborting (no changes).")
        return 0

    me = _self_cluster()
    if me is None:
        # Fail-safe: without self-identity we can't exclude the management cluster.
        print("reconcile-cluster-agents: could not self-identify the management cluster via "
              "the metadata server; aborting WITHOUT changes to avoid mis-managing it.")
        return 0
    self_name, self_loc = me

    all_clusters = _all_clusters(project)
    # Exclude the management (self) cluster and any RECONCILE_EXCLUDE names, by name.
    desired = {c for c in all_clusters if c[1] not in ({self_name} | EXTRA_EXCLUDE)}

    actual = _actual()
    actual_known = {k for k in actual if k[0] != "?"}

    to_create = sorted(desired - actual_known)
    to_prune = sorted(k for k in actual if k not in desired)  # gone, or is mgmt/excluded, or unknown-id
    keep = sorted(desired & actual_known)

    print(f"reconcile-cluster-agents: project={project} "
          f"management={self_name}/{self_loc} (excluded) dry_run={DRY_RUN}")
    print(f"  clusters={len(all_clusters)} desired={len(desired)} profiles={len(actual)} "
          f"keep={len(keep)} create={len(to_create)} prune={len(to_prune)}")

    for _proj, cluster, location in to_create:
        if DRY_RUN:
            print(f"  + CREATE {cluster}/{location}")
            continue
        ok, msg = _run("create", project, cluster, location)
        print(f"  + CREATE {cluster}/{location}: {'OK' if ok else 'FAILED — ' + msg}")

    for key in to_prune:
        name = actual[key]
        if key[0] == "?":
            print(f"  ! ANOMALY  {name}: unreadable cluster_identity — skipping delete, inspect manually")
            continue
        _proj, cluster, location = key
        if DRY_RUN:
            print(f"  - PRUNE  {cluster}/{location} ({name})")
            continue
        ok, msg = _run("delete", _proj, cluster, location)
        print(f"  - PRUNE  {cluster}/{location} ({name}): {'OK' if ok else 'FAILED — ' + msg}")

    for _proj, cluster, location in keep:
        print(f"  = KEEP   {cluster}/{location}")

    return 0  # resilient: never fail the cron on a transient error


if __name__ == "__main__":
    sys.exit(main())
