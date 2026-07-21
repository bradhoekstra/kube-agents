#!/usr/bin/env python3
# profile_scaffold.py - Shared helper to create + overlay a Hermes profile from a baked template.
#
# Used at two points:
#   - Container startup (deploy/shared/docker-entrypoint.sh) scaffolds the static
#     `platform` specialist profile from /opt/platform-template.
#   - Runtime (cluster_agent_profile.py) scaffolds per-cluster profiles from
#     /opt/cluster-template.
#
# Personas are separated by profile identity, persona (SOUL.md), and scoped
# toolset (config.yaml) — all shipped in the template and overlaid here onto the
# profile home under $HERMES_HOME/profiles/<name>. Executable scripts are NOT
# part of a template: they live in the shared /opt/data/scripts and are reachable
# by every profile.

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


def log(msg: str) -> None:
    print(f"[PROFILE-SCAFFOLD] {msg}", file=sys.stderr)


def profiles_base(hermes_home: Path) -> Path:
    # Hermes stores each named profile at $HERMES_HOME/profiles/<name>.
    return hermes_home / "profiles"


def _run_env(hermes_home: Path) -> dict[str, str]:
    """Env for subprocesses: HOME -> /tmp (writable creds) and HERMES_HOME pinned."""
    return {**os.environ, "HOME": "/tmp", "HERMES_HOME": str(hermes_home)}


def ensure_profile(name: str, description: str, hermes_home: Path) -> Path:
    """Register a Hermes profile (idempotent) and return its home path."""
    home = profiles_base(hermes_home) / name
    if not home.exists():
        try:
            subprocess.run(
                ["hermes", "profile", "create", name, "--no-skills", "--description", description],
                check=True, capture_output=True, text=True, timeout=60, env=_run_env(hermes_home),
            )
        except subprocess.CalledProcessError as e:
            raise SystemExit(
                f"ERROR: 'hermes profile create {name}' failed: {e.stderr.strip() or e.stdout.strip()}"
            )
    if not home.is_dir():
        raise SystemExit(f"ERROR: expected profile home not found after create: {home}")
    return home


def overlay_template(
    home: Path,
    template_dir: Path,
    plugins_dir: Path | None = None,
    items: tuple[str, ...] | None = None,
) -> None:
    """Copy a baked template onto a profile home (overwrites).

    If `items` is given, only those top-level names are overlaid; otherwise the
    entire template directory content is copied. Optionally overlays shared
    plugins (otel, etc.) into <home>/plugins for observability parity.
    """
    if not template_dir.is_dir():
        raise SystemExit(f"ERROR: template dir not found: {template_dir}")
    names = items if items is not None else tuple(p.name for p in template_dir.iterdir())
    for item_name in names:
        src = template_dir / item_name
        if not src.exists():
            continue
        dest = home / item_name
        if src.is_dir():
            shutil.copytree(src, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dest)
    if plugins_dir and plugins_dir.is_dir():
        shutil.copytree(plugins_dir, home / "plugins", dirs_exist_ok=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Create and overlay a Hermes profile from a template.")
    ap.add_argument("--name", required=True)
    ap.add_argument("--template", required=True, help="Baked template dir to overlay onto the profile home.")
    ap.add_argument("--description", default="", help="Profile description (surfaced in discovery).")
    ap.add_argument("--plugins", default="", help="Optional shared plugins dir to overlay for observability.")
    args = ap.parse_args()

    hermes_home = Path(os.environ.get("HERMES_HOME", "/opt/data"))
    home = ensure_profile(args.name, args.description, hermes_home)
    overlay_template(home, Path(args.template), Path(args.plugins) if args.plugins else None)
    print(str(home))


if __name__ == "__main__":
    main()
