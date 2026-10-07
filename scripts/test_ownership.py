#!/usr/bin/env python3
"""Keep docs/ownership.md in step with the tree and with OWNERS.

Run: cd scripts && python3 -m unittest test_ownership

The ownership page is hand-written, and the three ways it rots are all silent:
a directory is renamed and its row still names the old path, a person leaves
and their login stays as an area's primary, or a new top-level entry lands in
AGENTS.md's Repository Layout that no area's key paths include. Each of those reads as
a complete page to the next person who opens it.
"""

import re
import sys
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
OWNERSHIP_DOC = REPO / "docs" / "ownership.md"
OWNERS_FILE = REPO / "OWNERS"
AGENTS_FILE = REPO / "AGENTS.md"

# The one table, by the heading it sits under.
TABLE_HEADING = "## Areas, services and roles"
# The AGENTS.md section whose top-level `- `path`:` bullets must each have an area row.
LAYOUT_HEADING = "## Repository Layout"

# Column positions: the area, its key paths, then the two people.
COL_SUBJECT = 0
COL_PATHS = 1
COL_PRIMARY = 2
COL_BACKUP = 3
# A primary that is honestly nobody; the page explains it.
UNOWNED = "unowned"
# The only person cells that are not a login. Closed on purpose: a cell with a
# space in it would otherwise be a place to write a login the test never checks.
PROSE_FALLBACKS = ("the owner of the area it designs, or its `Author:` line where one exists",)

BACKTICKED_RE = re.compile(r"`([^`]+)`")
# A backticked token the page means as a path: it has a slash or an extension,
# and no glob or field punctuation (`gke-*`, `owner:`).
PATH_SHAPED_RE = re.compile(r"^[\w./-]+$")
# A top-level Repository Layout bullet: no indent, a backticked path, a colon.
LAYOUT_ENTRY_RE = re.compile(r"^- `([^`]+)`:")
# A GitHub login, as OWNERS writes one.
LOGIN_RE = re.compile(r"^[A-Za-z0-9-]+$")


def _section(path, heading):
    """The lines of `path` under `heading`, up to the next heading of the same level."""
    lines = path.read_text().splitlines()
    try:
        start = lines.index(heading) + 1
    except ValueError:
        raise AssertionError(f"{path.relative_to(REPO)} has no {heading!r} heading")
    body = []
    for line in lines[start:]:
        if line.startswith("## "):
            break
        body.append(line)
    return body


def _table_rows(lines):
    """Body rows of the first Markdown table in `lines`, as lists of stripped cells."""
    rows = [line for line in lines if line.startswith("|")]
    # Drop the header row and the `| --- |` separator under it.
    body = rows[2:]
    return [[cell.strip() for cell in row.strip().strip("|").split("|")] for row in body]


def _owners_logins():
    """Lower-cased, as scripts/request_reviewers.py reads them: GitHub logins are case-insensitive."""
    data = yaml.safe_load(OWNERS_FILE.read_text())
    return {login.lower() for login in data.get("approvers", []) + data.get("reviewers", [])}


def _layout_entries():
    entries = []
    for line in _section(AGENTS_FILE, LAYOUT_HEADING):
        match = LAYOUT_ENTRY_RE.match(line)
        if match:
            entries.append(match.group(1))
    return entries


def _is_path_shaped(token):
    return bool(PATH_SHAPED_RE.match(token)) and ("/" in token or "." in token)


class OwnershipDocTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = _table_rows(_section(OWNERSHIP_DOC, TABLE_HEADING))
        cls.logins = _owners_logins()
        cls.layout = _layout_entries()
        for name, value in (
            ("ownership table", cls.rows),
            ("OWNERS", cls.logins),
            ("AGENTS.md Repository Layout", cls.layout),
        ):
            if not value:
                raise AssertionError(f"{name} is empty; nothing to check")

    def _paths(self):
        """Every path-shaped backticked token in the table, with the row it is on."""
        for row in self.rows:
            for cell in row:
                for token in BACKTICKED_RE.findall(cell):
                    if _is_path_shaped(token):
                        yield row[COL_SUBJECT], token

    def test_every_path_exists(self):
        paths = list(self._paths())
        self.assertTrue(paths, "the table names no paths at all; the scan is broken, not the page")
        missing = [f"{subject}: {path}" for subject, path in paths if not (REPO / path).exists()]
        self.assertEqual(missing, [], "the page names paths that are not in the tree")

    def test_every_layout_entry_has_its_own_row(self):
        key_paths = {token for row in self.rows for token in BACKTICKED_RE.findall(row[COL_PATHS])}
        uncovered = [entry for entry in self.layout if entry not in key_paths]
        self.assertEqual(
            uncovered, [], "AGENTS.md Repository Layout entries that are no area's key path"
        )

    def _check_person(self, cell, subject, column, problems):
        if cell in ("", UNOWNED) or cell in PROSE_FALLBACKS:
            return
        if not LOGIN_RE.match(cell) or cell.lower() not in self.logins:
            problems.append(
                f"{subject} / {column}: {cell!r} is not a login in OWNERS "
                f"(one login per cell, {UNOWNED!r}, or a fallback from PROSE_FALLBACKS)"
            )

    def test_every_person_is_in_owners(self):
        problems = []
        for subject, primary, backup in self._people():
            self._check_person(primary, subject, "primary", problems)
            self._check_person(backup, subject, "backup", problems)
        self.assertEqual(problems, [])

    def _people(self):
        for row in self.rows:
            yield row[COL_SUBJECT], row[COL_PRIMARY], row[COL_BACKUP]

    def test_primary_is_never_blank(self):
        blank = [subject for subject, primary, _ in self._people() if primary == ""]
        self.assertEqual(blank, [], f"rows with no primary (write {UNOWNED!r} if that is the truth)")


if __name__ == "__main__":
    sys.exit(unittest.main())
