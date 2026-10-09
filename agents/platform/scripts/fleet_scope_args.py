#!/usr/bin/env python3
"""The two flags that carry the install's declared scope into a fleet-audit
collector, and their parsing.

The collectors run in the shell sandbox, whose data volume is not the agent
pod's, so they cannot read the reconcile's scope snapshot (fleet_scope.json).
The platform_control MCP server's `fleet_scope` tool reads it in the agent pod
and hands the agent `collector_args`; the agent appends those to the collector
command, and the collector sweeps exactly what they name. Shared here rather
than copied into each collector: the image and the shell sandbox both ship
this file under /opt/defaults/scripts beside the other scripts the collectors
import (credential_proxy_client.py), and a checkout runs it from the
repository through the same search path.
"""

from __future__ import annotations

import argparse
import os
import re

SCOPE_PROJECTS_FLAG = "--scope-projects"
SCOPE_UNREAD_FLAG = "--scope-unread"
SCOPE_PROJECTS_HELP = (
    "the install's declared scope, as the platform_control fleet_scope tool reports it: one comma-separated "
    "list of project IDs, passed once (a space-separated list only works quoted). Sweep exactly these, listing "
    "nothing; paste the tool's collector_args. Omit on an install that declares no scope, where the collector "
    "enumerates every project the identity can list"
)
SCOPE_UNREAD_HELP = (
    "project=outcome entries from the same tool: declared projects this install could not read, "
    "recorded as a coverage gap rather than silently absent"
)
# The partial-scope note a collector carries when the tool named unread projects.
DECLARED_SCOPE_UNREAD_NOTE = (
    "the install's declared scope names {count} project(s) this install could not read ({named}), "
    "per the platform_control fleet_scope tool; their clusters are not in this run."
)
# What a run with a declared scope but no readable project reports instead of
# sweeping: the collector exits with this rather than listing, which is the
# fall-through the flags exist to remove.
DECLARED_SCOPE_EMPTY_ERROR = (
    "the install's declared scope has no project this install could read this run{gap}; nothing was swept "
    "and nothing was listed"
)
# Set by the operator on the shell sandbox container (shell_sandbox_manifests.go,
# envScopeDeclared): "true" when the PlatformAgent carries a spec.scope block.
# A collector run there without the tool's flags must not list, because the
# listing is exactly the sweep the block exists to bound; it reports this
# instead and the agent passes collector_args. Unset, as in a checkout, means
# nothing is known and the flags alone decide.
SCOPE_DECLARED_ENV = "KUBEAGENTS_SCOPE_DECLARED"
SCOPE_DECLARED_TRUE = "true"
DECLARED_SCOPE_ARGS_MISSING_ERROR = (
    "this install declares a scope (spec.scope on the PlatformAgent) but the collector was run without the "
    "platform_control fleet_scope tool's collector_args, so it cannot know which projects are inside it; nothing "
    "was swept and nothing was listed. Call fleet_scope and pass its collector_args verbatim"
)
# The tail the project-level audits put on an unenumerated-projects row when the
# scope was declared: the fleet's size is known there, which is the point.
DECLARED_SCOPE_TAIL = "The declared scope names no other project."
SCOPE_ARG_SEPARATORS = r"[,\s]+"
SCOPE_UNREAD_OUTCOME_SEPARATOR = "="
SCOPE_UNREAD_DEFAULT_OUTCOME = "unknown"


def add_scope_arguments(parser: argparse.ArgumentParser) -> None:
    """The two flags, on every collector alike."""
    parser.add_argument(SCOPE_PROJECTS_FLAG, default=None, help=SCOPE_PROJECTS_HELP)
    parser.add_argument(SCOPE_UNREAD_FLAG, default=None, help=SCOPE_UNREAD_HELP)


def parse_scope_projects(value: str | None) -> list[str]:
    """The project IDs `--scope-projects` carries, each once, in the order given;
    empty when it was not passed. A repeated id would otherwise be swept twice
    and every finding on it filed twice."""
    return list(dict.fromkeys(p for p in re.split(SCOPE_ARG_SEPARATORS, value or "") if p))


def parse_scope_unread(value: str | None) -> list[tuple[str, str]]:
    """The (project, outcome) pairs `--scope-unread` carries; an entry without an
    outcome reads as unknown."""
    unread = []
    for entry in (e for e in re.split(SCOPE_ARG_SEPARATORS, value or "") if e):
        project, _, outcome = entry.partition(SCOPE_UNREAD_OUTCOME_SEPARATOR)
        unread.append((project, outcome or SCOPE_UNREAD_DEFAULT_OUTCOME))
    return unread


def unread_note(unread: list[tuple[str, str]]) -> str | None:
    """The coverage gap the unread projects are, or None when there are none."""
    if not unread:
        return None
    named = ", ".join(f"{project} ({outcome})" for project, outcome in unread)
    return DECLARED_SCOPE_UNREAD_NOTE.format(count=len(unread), named=named)


class DeclaredScope:
    """The scope a collector was handed, one instance per collector module:
    `set` from the parsed flags in main, read by the project resolver. Not
    declared (neither flag passed) means the collector enumerates as it did
    before scopes. Declared with no project means the install drew a boundary
    and could read nothing inside it: the collector reports `empty_error()`
    and sweeps nothing, never the listing."""

    def __init__(self) -> None:
        self.declared = False
        self.args_missing = False
        self.projects: list[str] | None = None
        self.unread: list[tuple[str, str]] = []

    def set(self, scope_projects: str | None, scope_unread: str | None) -> None:
        """Records `--scope-projects` and `--scope-unread`. Either flag passed,
        even blank, is a declared scope; `projects` is then the list, possibly
        empty. Neither flag on a sandbox whose operator says the install
        declares a scope (SCOPE_DECLARED_ENV) is a declared scope too, with
        nothing to sweep and `args_missing` set, so the run reports rather than
        lists. `projects` is None only when nothing declares a scope."""
        flagged = scope_projects is not None or scope_unread is not None
        self.args_missing = not flagged and os.environ.get(SCOPE_DECLARED_ENV, "").strip().lower() == SCOPE_DECLARED_TRUE
        self.declared = flagged or self.args_missing
        self.projects = parse_scope_projects(scope_projects) if self.declared else None
        self.unread = parse_scope_unread(scope_unread)

    def empty_error(self) -> str:
        """The error a declared scope with nothing to sweep reports: the flags
        were not passed on an install that declares a scope, or they named no
        readable project."""
        if self.args_missing:
            return DECLARED_SCOPE_ARGS_MISSING_ERROR
        note = self.note()
        return DECLARED_SCOPE_EMPTY_ERROR.format(gap=f" ({note})" if note else "")

    def note(self) -> str | None:
        """The coverage gap `--scope-unread` names, or None when every declared
        project was read."""
        return unread_note(self.unread)
