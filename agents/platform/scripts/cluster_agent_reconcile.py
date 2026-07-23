#!/usr/bin/env python3
# cluster_agent_reconcile.py - Prune orphaned Cluster Agent profiles.
#
# Cluster Agents are Hermes profiles on the data PVC ($HERMES_HOME/profiles/<name>),
# one per managed GKE cluster, each stamped with a `cluster_identity` block in its
# config.yaml. They are created/deleted when the Platform Agent follows the
# onboarding/teardown skills — but a cluster deleted out-of-band (gcloud, a removed
# Config Connector CR, ...) leaves its profile orphaned forever.
#
# This deterministic engine closes that loop. Per run it enumerates the managed
# profiles, resolves each one's target cluster from its `cluster_identity`, and
# deletes the profile IFF its GKE cluster is *definitively* gone (a NotFound/404
# from `gcloud container clusters describe`). Every other error path — auth,
# network, timeout, quota, an unreadable identity — is treated as "unknown" and
# the profile is left untouched: we never delete on ambiguity.
#
# It runs as a `no_agent` cron job on the profile the gateway actually ticks (the
# `default`/chat profile — see docs/designs/fleet-handover-retirement.md §4).
# Scripts and the profiles PVC are shared pod-wide, so it operates on every
# profile regardless of which profile ticks it. It is resilient (always exit 0)
# and posts a Google Chat summary only when it actually prunes something.

import argparse
import json
import os
import subprocess
import sys

from cluster_agent_profile import (
    RESERVED_PROFILES,  # noqa: F401 - re-exported for callers/tests; used indirectly via list_profiles
    delete_profile,
    list_profiles,
    profile_home,
    read_cluster_identity,
)

DESCRIBE_TIMEOUT_SECONDS = 30


def log(msg: str) -> None:
    print(f"[CLUSTER-RECONCILE] {msg}", file=sys.stderr)


def _run_env() -> dict[str, str]:
    """HOME -> /tmp so gcloud can read/write credentials on the writable scratch disk."""
    return {**os.environ, "HOME": "/tmp"}


def _cluster_exists(project: str, cluster: str, location: str) -> bool | None:
    """Return True if the GKE cluster exists, False if it definitively does not, None if unknown.

    Mirrors platform_mcp_server.verify_gke_cluster's classification: a NotFound/404
    is the *only* signal that authorizes deletion. Any other failure (auth, network,
    timeout, quota) returns None so the caller leaves the profile in place.
    """
    cmd = [
        "gcloud", "container", "clusters", "describe", cluster,
        f"--location={location}", f"--project={project}", "--format=json(status, id)",
    ]
    try:
        subprocess.run(
            cmd, capture_output=True, text=True, check=True,
            timeout=DESCRIBE_TIMEOUT_SECONDS, env=_run_env(),
        )
        return True
    except subprocess.CalledProcessError as e:
        stderr = e.stderr or ""
        if "NotFound" in stderr or "not found" in stderr.lower() or "404" in stderr:
            return False
        log(f"describe {cluster} ({project}/{location}) failed (treating as unknown): {stderr.strip()}")
        return None
    except subprocess.TimeoutExpired:
        log(f"describe {cluster} ({project}/{location}) timed out (treating as unknown).")
        return None
    except Exception as e:  # noqa: BLE001 - any unexpected failure is 'unknown', never 'absent'
        log(f"describe {cluster} ({project}/{location}) errored (treating as unknown): {e}")
        return None


def reconcile(dry_run: bool = False) -> dict:
    """Prune Cluster Agent profiles whose GKE cluster is definitively gone.

    Returns a structured report dict with the profile names in each outcome bucket.
    Isolated per-profile: one bad profile never aborts the sweep.
    """
    report: dict[str, list] = {
        "pruned": [],            # orphan removed (or would be, under --dry-run)
        "kept": [],              # cluster still exists
        "skipped_no_identity": [],  # config.yaml lacked a usable cluster_identity
        "skipped_error": [],     # liveness check was inconclusive (auth/network/etc.)
    }

    profiles = list_profiles()
    log(f"Reconciling {len(profiles)} managed profile(s){' (dry-run)' if dry_run else ''}.")

    for name in profiles:
        identity = read_cluster_identity(profile_home(name))
        if identity is None:
            log(f"{name}: no readable cluster_identity — skipping (never delete unverifiable profiles).")
            report["skipped_no_identity"].append(name)
            continue

        exists = _cluster_exists(**identity)
        if exists is True:
            report["kept"].append(name)
            continue
        if exists is None:
            report["skipped_error"].append(name)
            continue

        # exists is False -> definitive NotFound -> orphan.
        if dry_run:
            log(f"{name}: cluster {identity['cluster']} ({identity['project']}/{identity['location']}) "
                f"is gone — WOULD prune (dry-run).")
        else:
            log(f"{name}: cluster {identity['cluster']} ({identity['project']}/{identity['location']}) "
                f"is gone — pruning.")
            delete_profile(name)
        report["pruned"].append(name)

    return report


def _format_notification(report: dict) -> str:
    lines = [f"🧹 *Cluster Agent reconcile* — pruned {len(report['pruned'])} orphaned profile(s):"]
    for name in report["pruned"]:
        lines.append(f"  • `{name}` (GKE cluster no longer exists)")
    if report["skipped_error"]:
        lines.append(
            f"⚠️ {len(report['skipped_error'])} profile(s) could not be verified this run "
            f"(left untouched): {', '.join(f'`{n}`' for n in report['skipped_error'])}."
        )
    return "\n".join(lines)


def _notify(message: str) -> None:
    """Post a summary to the user's Google Chat home channel (best-effort)."""
    try:
        subprocess.run(
            ["hermes", "send", "--to", "google_chat", message],
            capture_output=True, text=True, check=True, timeout=30, env=_run_env(),
        )
    except Exception as e:  # noqa: BLE001 - notification is best-effort; never fail the run
        log(f"Failed to post reconcile notification: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prune Cluster Agent profiles whose GKE cluster no longer exists."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report what would be pruned without deleting anything or notifying.",
    )
    args = parser.parse_args()

    try:
        report = reconcile(dry_run=args.dry_run)
    except Exception as e:  # noqa: BLE001 - resilient: a cron producer must always exit 0
        log(f"Reconcile aborted unexpectedly: {e}")
        return

    log(
        "Done: pruned={pruned} kept={kept} no_identity={skipped_no_identity} "
        "unknown={skipped_error}.".format(**{k: len(v) for k, v in report.items()})
    )

    if args.dry_run:
        print(json.dumps(report, indent=2))
        return

    # Notify only when there's something actionable to report (avoid idle hourly noise).
    if report["pruned"]:
        _notify(_format_notification(report))


if __name__ == "__main__":
    main()
