#!/usr/bin/env python3
"""Keep the always-loaded agent instruction files inside the harness's budget.

``AGENTS.md`` and ``CLAUDE.md`` are not documents an agent opens when it needs
them -- a coding harness loads them into the context window at the start of
every session, in every checkout, before the first prompt. Their size is a tax
on every task done in this repository, and nothing about paying it is visible
at review time: a pull request that adds a well-argued paragraph to ``AGENTS.md``
looks exactly like one that does not.

That is how this file came to exist. ``AGENTS.md`` went from 14.5k to 42.7k
characters in nine days -- eleven separate pull requests, each adding a rule
that deserved to be there -- and the first anyone noticed was Claude Code
printing ``AGENTS.md is over the 40.0k-char limit`` at startup. The warning is
only a warning: the file is still loaded whole, so nothing breaks loudly. It
just gets more expensive, indefinitely, until someone re-reads the whole file
and splits it again.

So the budget is checked rather than watched. The remedy when this fails is
almost never to delete a rule -- it is to move the *mechanics* out to a
document the agent opens when it is carrying the rule out, the way
``docs/pull-request-workflow.md`` holds the commands whose rules live in
``AGENTS.md``. Raising ``BUDGET`` is the other option, and it is a real one, but
it should be a decision someone argues for in a pull request rather than the
path of least resistance.

Standard library only, so it runs in CI and in a bare clone.

Usage::

    python3 scripts/check_context_budget.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Chars. Claude Code warns at 40k; this sits below it so the check fires while
# there is still room to land the fix, rather than after the warning is already
# on everyone's screen.
BUDGET = 38_000

# CLAUDE.md pulls AGENTS.md in with an `@AGENTS.md` line, which the harness
# expands in place. Counting the two files naively would therefore charge
# AGENTS.md twice and leave the import line itself uncounted; the import lines
# are dropped instead, so the total is what actually lands in the window.
FILES = ("AGENTS.md", "CLAUDE.md")


def is_import(line: str) -> bool:
    """True for a harness import directive (``@AGENTS.md``) on its own line.

    Only a bare ``@path`` counts. A line that merely mentions an ``@`` inside
    prose is content and is charged as such.
    """
    stripped = line.strip()
    return stripped.startswith("@") and " " not in stripped and len(stripped) > 1


def loaded_size(path: Path) -> int:
    """Characters this file contributes to the context window."""
    return sum(
        len(line) for line in path.read_text(encoding="utf-8").splitlines(keepends=True)
        if not is_import(line)
    )


def main() -> int:
    sizes = {}
    for name in FILES:
        path = REPO / name
        if not path.is_file():
            print(f"MISSING  {name} -- expected at the repository root")
            return 1
        sizes[name] = loaded_size(path)

    total = sum(sizes.values())
    breakdown = ", ".join(f"{name} {size / 1000:.1f}k" for name, size in sizes.items())

    if total > BUDGET:
        over = total - BUDGET
        print(
            f"FAIL: the always-loaded instruction files total {total / 1000:.1f}k chars "
            f"({breakdown}), {over / 1000:.1f}k over the {BUDGET / 1000:.0f}k budget.\n"
            "\n"
            "These files are loaded into every session in every checkout, so this is a\n"
            "cost paid by every task in the repository. Prefer moving mechanics out over\n"
            "deleting a rule: the commands for a procedure belong in a document the agent\n"
            "opens while carrying it out (docs/pull-request-workflow.md is the worked\n"
            "example), while the rule, its trigger, and its reason stay in AGENTS.md.\n"
            "Raising BUDGET in scripts/check_context_budget.py is a legitimate answer too,\n"
            "but argue for it in the pull request."
        )
        return 1

    print(
        f"Always-loaded instruction files total {total / 1000:.1f}k chars ({breakdown}), "
        f"{(BUDGET - total) / 1000:.1f}k under the {BUDGET / 1000:.0f}k budget."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
