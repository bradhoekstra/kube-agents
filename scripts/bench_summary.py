#!/usr/bin/env python3
"""Summarise a bench run's per-case durations.

Reads a JSON lines file where each line is one case result, and prints the
count, the mean duration, and the slowest case.
"""

import json
import sys


def load_cases(path):
    cases = []
    with open(path) as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            cases.append(json.loads(line))
    return cases


def mean_duration(cases):
    total = sum(case["duration_s"] for case in cases)
    return total / len(cases)


def slowest(cases):
    return max(cases, key=lambda case: case["duration_s"])


def main(argv):
    if len(argv) != 2:
        print(f"usage: {argv[0]} RESULTS.jsonl", file=sys.stderr)
        return 2
    cases = load_cases(argv[1])
    print(f"cases:   {len(cases)}")
    print(f"mean:    {mean_duration(cases):.2f}s")
    print(f"slowest: {slowest(cases)['name']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
