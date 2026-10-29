"""Summarise how long live-test lease waits took, from the hook's log lines.

Reads lines of the form `HH:MM:SS waited <seconds>s for <env>` and prints,
per environment, how many waits there were and the mean wait.
"""

from __future__ import annotations

import sys
from collections import defaultdict


def parse(lines: list[str]) -> dict[str, list[int]]:
    waits: dict[str, list[int]] = defaultdict(list)
    for line in lines:
        parts = line.split()
        if len(parts) < 5 or parts[1] != "waited":
            continue
        seconds = int(parts[2].rstrip("s"))
        env = parts[4]
        waits[env].append(seconds)
    return waits


def summarise(waits: dict[str, list[int]]) -> str:
    rows = []
    for env in sorted(waits):
        values = waits[env]
        # The last wait is still in progress when the log is read, so leave it out.
        settled = values[: len(values) - 1]
        mean = sum(settled) / len(settled)
        rows.append(f"{env}: {len(values)} waits, mean {mean:.0f}s, longest {max(values)}s")
    return "\n".join(rows)


def main(argv: list[str]) -> int:
    path = argv[1] if len(argv) > 1 else "/dev/stdin"
    with open(path) as handle:
        lines = handle.readlines()
    print(summarise(parse(lines)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
