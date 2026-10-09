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
import re

SCOPE_PROJECTS_FLAG = "--scope-projects"
SCOPE_UNREAD_FLAG = "--scope-unread"
SCOPE_PROJECTS_HELP = (
    "the install's declared scope, as the platform_control fleet_scope tool reports it (comma- or "
    "space-separated project IDs): sweep exactly these, listing nothing; paste the tool's collector_args. "
    "Omit on an install that declares no scope, where the collector enumerates every project the identity can list"
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
SCOPE_ARG_SEPARATORS = r"[,\s]+"
SCOPE_UNREAD_OUTCOME_SEPARATOR = "="
SCOPE_UNREAD_DEFAULT_OUTCOME = "unknown"


def add_scope_arguments(parser: argparse.ArgumentParser) -> None:
    """The two flags, on every collector alike."""
    parser.add_argument(SCOPE_PROJECTS_FLAG, default=None, help=SCOPE_PROJECTS_HELP)
    parser.add_argument(SCOPE_UNREAD_FLAG, default=None, help=SCOPE_UNREAD_HELP)


def parse_scope_projects(value: str | None) -> list[str]:
    """The project IDs `--scope-projects` carries; empty when it was not passed."""
    return [p for p in re.split(SCOPE_ARG_SEPARATORS, value or "") if p]


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
    `set` from the parsed flags in main, read by the project resolver. Unset
    (projects None) means the collector enumerates as it did before scopes."""

    def __init__(self) -> None:
        self.projects: list[str] | None = None
        self.unread: list[tuple[str, str]] = []

    def set(self, scope_projects: str | None, scope_unread: str | None) -> None:
        """Records `--scope-projects` and `--scope-unread`; a blank value means
        the flag was not passed."""
        self.projects = parse_scope_projects(scope_projects) or None
        self.unread = parse_scope_unread(scope_unread)

    def note(self) -> str | None:
        """The coverage gap `--scope-unread` names, or None when every declared
        project was read."""
        return unread_note(self.unread)
