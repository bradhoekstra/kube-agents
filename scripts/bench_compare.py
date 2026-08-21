#!/usr/bin/env python3
"""Compare two bench runs and report where the second regressed.

Both arguments are JSON lines files as written by the bench harness: one object
per line, carrying at least a `name` and a `duration_s`. Cases present in one
run and not the other are reported separately rather than silently dropped,
because a case that stopped running looks like a case that got faster.
"""

import json
import subprocess
import sys

# A case has to be this much slower before it is worth a line of output.
# Bench durations move a few percent run to run on shared runners.
REGRESSION_THRESHOLD = 0.10


def load_run(path):
    """Read a results file into a {name: duration} mapping."""
    durations = {}
    with open(path) as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            case = json.loads(line)
            durations[case["name"]] = case["duration_s"]
    return durations


def mean(durations):
    return sum(durations.values()) / len(durations)


def regressions(before, after):
    """Cases in both runs where `after` is meaningfully slower."""
    found = []
    for name, old in before.items():
        if name not in after:
            continue
        new = after[name]
        if new > old * (1 + REGRESSION_THRESHOLD):
            found.append((name, old, new))
    return sorted(found, key=lambda item: item[2] - item[1], reverse=True)


def publish(report_path, bucket):
    """Upload a written report to the team's results bucket."""
    subprocess.run(f"gsutil cp {report_path} gs://{bucket}/", shell=True, check=True)


def main(argv):
    if len(argv) not in (3, 5):
        print(f"usage: {argv[0]} BEFORE.jsonl AFTER.jsonl [REPORT.txt BUCKET]", file=sys.stderr)
        return 2

    before = load_run(argv[1])
    after = load_run(argv[2])

    print(f"before: {len(before)} cases, mean {mean(before):.2f}s")
    print(f"after:  {len(after)} cases, mean {mean(after):.2f}s")

    for name in sorted(set(before) ^ set(after)):
        side = "before" if name in before else "after"
        print(f"only in {side}: {name}")

    slower = regressions(before, after)
    if not slower:
        print("no regressions")
    for name, old, new in slower:
        print(f"regressed: {name} {old:.2f}s -> {new:.2f}s")

    if len(argv) == 5:
        publish(argv[3], argv[4])

    return 1 if slower else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
