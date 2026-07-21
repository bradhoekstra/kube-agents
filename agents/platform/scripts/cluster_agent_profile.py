#!/usr/bin/env python3
# cluster_agent_profile.py - Manage per-cluster Cluster Agent Hermes profiles.
#
# The Platform Agent runs this from its terminal to dynamically create, delete,
# list, and invoke the Cluster Agent profile for a specific GKE cluster, inside
# its own pod. One profile per managed cluster; it persists until the cluster is
# deleted.
#
# Mechanism (verified against the shipped Hermes CLI):
#   - `hermes profile create <name>` registers an isolated profile and stores its
#     home at $HERMES_HOME/profiles/<name>. Because HERMES_HOME is the data PVC
#     (/opt/data) in the pod, profiles persist across restarts automatically.
#   - We then overlay the baked Cluster Agent template (/opt/cluster-template/:
#     SOUL.md, AGENTS.md, config.yaml, skills/) onto that home, pin a kubeconfig
#     scoped to the target cluster, and write the cluster identity into USER.md.
#   - Delegation is a one-shot profile call: `hermes -p <name> -z "<task>"`.

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

TEMPLATE_DIR = Path(os.environ.get("CLUSTER_TEMPLATE_DIR", "/opt/cluster-template"))
SHARED_PLUGINS_DIR = Path(os.environ.get("SHARED_PLUGINS_DIR", "/opt/defaults/plugins"))
HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/opt/data"))
# Hermes stores each profile at $HERMES_HOME/profiles/<name> (persists on the data PVC).
PROFILES_BASE = HERMES_HOME / "profiles"

# Files/dirs from the template to overlay onto the created profile home.
OVERLAY_ITEMS = ("SOUL.md", "AGENTS.md", "config.yaml", "skills")
MAX_NAME_LEN = 63


def log(msg: str) -> None:
    print(f"[CLUSTER-PROFILE] {msg}", file=sys.stderr)


def _run_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Env for subprocesses: HOME -> /tmp (writable creds) and HERMES_HOME pinned."""
    return {**os.environ, "HOME": "/tmp", "HERMES_HOME": str(HERMES_HOME), **(extra or {})}


def _validate(value: str, field: str) -> None:
    if not value or not re.match(r"^[a-zA-Z0-9._-]+$", value):
        raise SystemExit(f"ERROR: invalid {field}: {value!r}")


def profile_name(project: str, cluster: str, location: str) -> str:
    """Derive a stable, sanitized profile name for a target cluster.

    Mirrors the kubeconfig naming convention in platform_mcp_server.switch_kube_context.
    """
    raw = f"cluster-{project}-{cluster}-{location}".lower()
    name = re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9-]+", "-", raw)).strip("-")
    if len(name) > MAX_NAME_LEN:
        digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
        name = f"{name[: MAX_NAME_LEN - 9]}-{digest}"
    return name


def profile_home(name: str) -> Path:
    return PROFILES_BASE / name


def _overlay_template(home: Path) -> None:
    """Copy the Cluster Agent template onto the profile home (overwrites)."""
    for item_name in OVERLAY_ITEMS:
        src = TEMPLATE_DIR / item_name
        if not src.exists():
            continue
        dest = home / item_name
        if src.is_dir():
            shutil.copytree(src, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dest)
    # Bring in shared plugins (otel, etc.) for observability parity.
    if SHARED_PLUGINS_DIR.is_dir():
        shutil.copytree(SHARED_PLUGINS_DIR, home / "plugins", dirs_exist_ok=True)


def cmd_create(args: argparse.Namespace) -> None:
    for field, value in (("project", args.project), ("cluster", args.cluster), ("location", args.location)):
        _validate(value, field)
    name = profile_name(args.project, args.cluster, args.location)
    home = profile_home(name)

    if not TEMPLATE_DIR.is_dir():
        raise SystemExit(f"ERROR: cluster template dir not found: {TEMPLATE_DIR}")

    # 1. Register the profile with Hermes (idempotent: skip if its home already exists).
    if not home.exists():
        description = f"Read-only Cluster Agent for GKE cluster {args.cluster} ({args.project}/{args.location})."
        try:
            subprocess.run(
                ["hermes", "profile", "create", name, "--no-skills", "--description", description],
                check=True, capture_output=True, text=True, timeout=60, env=_run_env(),
            )
        except subprocess.CalledProcessError as e:
            raise SystemExit(f"ERROR: 'hermes profile create {name}' failed: {e.stderr.strip() or e.stdout.strip()}")
    if not home.is_dir():
        raise SystemExit(f"ERROR: expected profile home not found after create: {home}")

    # 2. Overlay the Cluster Agent persona, scoped config, and skills.
    _overlay_template(home)

    # 3. Pin a kubeconfig scoped to the target cluster.
    kubeconfig = home / "kubeconfig.yaml"
    env = _run_env({"KUBECONFIG": str(kubeconfig)})
    try:
        subprocess.run(
            [
                "gcloud", "container", "clusters", "get-credentials", args.cluster,
                f"--location={args.location}", f"--project={args.project}",
            ],
            check=True, capture_output=True, text=True, timeout=60, env=env,
        )
    except subprocess.CalledProcessError as e:
        raise SystemExit(f"ERROR: failed to fetch credentials for '{args.cluster}': {e.stderr.strip()}")
    except subprocess.TimeoutExpired:
        raise SystemExit(f"ERROR: timed out fetching credentials for '{args.cluster}'.")

    # 4. Write the fixed cluster identity into USER.md.
    (home / "USER.md").write_text(
        "# Cluster Agent Context\n\n"
        "This Cluster Agent is permanently scoped to the following GKE cluster:\n\n"
        f"- project: {args.project}\n"
        f"- cluster: {args.cluster}\n"
        f"- location: {args.location}\n\n"
        f"KUBECONFIG: {kubeconfig}\n",
        encoding="utf-8",
    )

    print(name)


def cmd_delete(args: argparse.Namespace) -> None:
    name = profile_name(args.project, args.cluster, args.location)
    home = profile_home(name)
    try:
        subprocess.run(
            ["hermes", "profile", "delete", name, "-y"],
            check=True, capture_output=True, text=True, timeout=30, env=_run_env(),
        )
    except Exception as e:  # noqa: BLE001 - tolerate an already-absent profile
        log(f"'hermes profile delete {name}' failed (continuing to clean up home): {e}")
    if home.exists():
        shutil.rmtree(home, ignore_errors=True)
    print(name)


def cmd_list(_args: argparse.Namespace) -> None:
    if PROFILES_BASE.is_dir():
        for name in sorted(p.name for p in PROFILES_BASE.iterdir() if p.is_dir() and p.name != "default"):
            print(name)


def cmd_invoke(args: argparse.Namespace) -> None:
    """One-shot delegation to a cluster's profile.

    Personas never exchange context directly: we pass ONLY a pointer to a shared
    work item. The Cluster Agent reads the request from the work-item store, does
    the work, and writes its findings back there (see the worklog.py shared state
    and the cluster-agent-lifecycle skill). Its chat reply is just an ack.
    """
    name = profile_name(args.project, args.cluster, args.location)
    home = profile_home(name)
    if not home.is_dir():
        raise SystemExit(f"ERROR: profile '{name}' does not exist. Run 'create' first.")
    pointer = f"Please work on work item {args.work_item}."
    env = _run_env({"KUBECONFIG": str(home / "kubeconfig.yaml")})
    result = subprocess.run(
        ["hermes", "-p", name, "-z", pointer],
        capture_output=True, text=True, env=env,
    )
    sys.stdout.write(result.stdout)
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        sys.exit(result.returncode)


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage per-cluster Cluster Agent Hermes profiles.")
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("create", "delete", "invoke"):
        sp = sub.add_parser(name, help=f"{name} a cluster profile")
        sp.add_argument("--project", required=True)
        sp.add_argument("--cluster", required=True)
        sp.add_argument("--location", required=True)
        if name == "invoke":
            sp.add_argument(
                "--work-item", dest="work_item", required=True,
                help="ID of the shared work item to delegate (from worklog.py). Only a pointer is sent — never context.",
            )

    sub.add_parser("list", help="List existing cluster profiles")

    args = parser.parse_args()
    handlers = {"create": cmd_create, "delete": cmd_delete, "list": cmd_list, "invoke": cmd_invoke}
    handlers[args.command](args)


if __name__ == "__main__":
    main()
