#!/usr/bin/env python3
"""The projects a fleet audit sweeps, read from the install's declared scope.

The fleet audits resolved their project scope as the host project plus every
project `gcloud projects list` returns: every project the agent's identity can
see, which is not the scope the install declared. A project the identity could
list but `spec.scope` never named was swept, and a declared project the
identity could not list was not, so the boundary the operator drew for
discovery (docs/designs/multi-project-scope.md) did not bound the audits.

This module hands an audit the resolved set instead: the reconcile's snapshot,
`fleet_scope.json`, rewritten at the data volume's root on every run, lists each
project the scope resolved to with the outcome the reconcile read it with. An
audit sweeps the projects read `ok` or `api-disabled`, names the ones the scope declares but the
reconcile could not read, so the run accounts for them rather than reading as
a full sweep, and lists nothing. An install that declares no scope has drawn
no boundary; the answer is None there, and the audit keeps the listing it had,
which is also what it falls back to when no snapshot exists yet or the file
does not parse (the first hour after an install, a volume that was reset).

Read in the agent pod, where the snapshot is, by the platform_control MCP
server's `fleet_scope` tool (platform_mcp_server.py), which hands the agent
the resolved set and the arguments to pass the collectors. The collectors
themselves cannot read the file: they run in the shell sandbox, whose
/opt/data is a separate volume at the same path (deploy/sandbox/entrypoint.sh
says nothing is copied across), so the agent carries the scope from the tool
to the collector's `--scope-projects` and `--scope-unread`. The reader is
deliberately independent of cluster_agent_reconcile.py, which writes the
snapshot and owns its shape: that module's import has side effects.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

# Where the reconcile writes the snapshot: the data volume's root, beside the
# profiles directory. PLATFORM_AGENT_HOME names it in every process on the
# agent pod (docker-entrypoint.sh), where HERMES_HOME does not: the gateway
# runs with HERMES_HOME at the root, but a platform worker and the governance
# jobs' ticks run with HERMES_HOME at the profile home beneath it
# (profile_cron_tick.py), which is where the MCP server that reads this module
# runs for a worker, so
# a snapshot path keyed on HERMES_HOME would look a level too deep and find
# nothing. gitops_workspace.agent_home() reads the same variable for the same
# reason.
AGENT_HOME_ENV = "PLATFORM_AGENT_HOME"
DEFAULT_AGENT_HOME = "/opt/data"
SNAPSHOT_FILE = "fleet_scope.json"

# The snapshot's vocabulary (cluster_agent_reconcile.py owns it; the design's
# §5 is its contract). A row read `ok` holds clusters the install can reach;
# any other outcome names a project the scope declares and this install could
# not read this run. A `retiring` row is a project the scope no longer names,
# kept only until its profiles are pruned, so it is not in the sweep.
OUTCOME_OK = "ok"
# A declared project whose Kubernetes Engine API is off holds no GKE cluster
# and is swept: the GKE collectors count it empty rather than partial (the GCE
# and networking SOPs' "counts as empty, not skipped: recording it as a loss
# would pin every run partial"), and the Compute and networking audits read it
# like any other.
OUTCOME_API_DISABLED = "api-disabled"
OUTCOME_UNKNOWN = "unknown"
# The collectors' two scope flags, which `collector_args` spells with the
# constants the collectors parse them by, so the two cannot drift apart.
from fleet_scope_args import SCOPE_PROJECTS_FLAG, SCOPE_UNREAD_FLAG  # noqa: E402
STATE_IN_SCOPE = "in-scope"
# Whether the CR carried a spec.scope block this run, which the reconcile records
# beside `declared`: a present block with every list empty is the host-only
# boundary, not the absence of one. A run that could not read the block carries
# the last declaration and records present: false beside it; that is still a
# boundary. A snapshot from a reconcile that predates the key is read by its
# lists alone.
PRESENT_KEY = "present"
# Whether the reconcile could read the operator's render this run, written beside
# `present` (cluster_agent_reconcile.py). Together they tell the two shapes of
# `present: false` apart: the render was readable and carried no block, which
# is an operator who removed the scope and so no boundary; or the render could
# not be read, and the reconcile carried the last declaration forward, which is
# still that boundary, host-only or not. A snapshot without this key is read by
# its lists.
READABLE_KEY = "readable"
# The keys under `declared` whose presence means the install drew a boundary,
# for a snapshot without the present flag.
DECLARED_SCOPE_KEYS = ("projects", "folders", "organizations", "sharedVpcHosts", "metricsScopes")


@dataclass(frozen=True)
class ScopeTargets:
    """What the declared scope resolved to, for an audit's scope accounting."""

    # The projects to sweep, in the snapshot's order (the management project
    # first, then the explicit projects, the selectors' and the containers'
    # members, as the reconcile lists them): the rows read `ok`, and the rows
    # read `api-disabled`, which the collectors count empty or read as usual.
    projects: tuple[str, ...]
    # Declared projects this install could not read, with the outcome the
    # reconcile recorded: `denied`, `unreachable`, `over-cap`.
    unread: tuple[tuple[str, str], ...]
    resolved_at: str | None
    path: str

    def collector_args(self) -> str:
        """The arguments that hand this scope to a collector: `--scope-projects`
        with the sweep, and `--scope-unread` naming each declared project the
        install could not read, as `project=outcome`. With nothing readable the
        unread flag goes alone, so a collector handed it still knows a scope
        was declared and reports that rather than listing; empty only when the
        scope resolved to no row at all."""
        args = []
        if self.projects:
            args.append(f"{SCOPE_PROJECTS_FLAG} {','.join(self.projects)}")
        if self.unread:
            args.append(f"{SCOPE_UNREAD_FLAG} " + ",".join(f"{project}={outcome}" for project, outcome in self.unread))
        return " ".join(args)


def snapshot_path(agent_home: str | os.PathLike | None = None) -> Path:
    """Where the reconcile's snapshot lives: the data volume's root, as
    PLATFORM_AGENT_HOME names it, or the root given."""
    root = agent_home if agent_home is not None else (os.environ.get(AGENT_HOME_ENV) or DEFAULT_AGENT_HOME)
    return Path(root) / SNAPSHOT_FILE


def declared_scope_targets(agent_home: str | os.PathLike | None = None) -> ScopeTargets | None:
    """The declared scope's resolved projects, or None when the install declared
    no scope, no snapshot exists, or the file is not a snapshot -- the cases in
    which the caller enumerates as it did before this module existed."""
    path = snapshot_path(agent_home)
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("projects"), list):
        return None
    declared = parsed.get("declared")
    if not isinstance(declared, dict):
        return None
    # A declaration is in force when the block was present this run, or when the
    # reconcile could not read the render and carried the last declaration
    # forward (present: false, readable: false): the boundary stands, host-only
    # or not. A readable render with no block is an operator who removed the
    # scope: no boundary, whatever lists the reconcile still carries. A snapshot
    # that predates the two keys is read by its lists.
    present = parsed.get(PRESENT_KEY)
    readable = parsed.get(READABLE_KEY)
    if present is not True:
        if isinstance(readable, bool):
            if readable:
                return None
        elif not any(isinstance(declared.get(key), list) and declared.get(key) for key in DECLARED_SCOPE_KEYS):
            return None
    projects: list[str] = []
    unread: list[tuple[str, str]] = []
    for row in parsed["projects"]:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        if row.get("state", STATE_IN_SCOPE) != STATE_IN_SCOPE:
            continue
        project = str(row["id"])
        if row.get("outcome") in (OUTCOME_OK, OUTCOME_API_DISABLED):
            projects.append(project)
        else:
            unread.append((project, str(row.get("outcome") or OUTCOME_UNKNOWN)))
    resolved_at = parsed.get("resolvedAt")
    return ScopeTargets(
        projects=tuple(projects),
        unread=tuple(unread),
        resolved_at=str(resolved_at) if isinstance(resolved_at, str) else None,
        path=str(path),
    )
