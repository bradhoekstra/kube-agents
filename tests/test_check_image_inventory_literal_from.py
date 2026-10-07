"""`check_literal_from` in hack/check-image-inventory.sh fails when an agent
plugin Dockerfile's literal `FROM` pin and the inventory's entry move apart.

The plugin images pin `busybox:musl` by digest in a literal FROM rather than an
ARG pair, so `check_base_image` never reaches them. CI only ever runs the script
on a tree where this check passes, so its fail path would otherwise execute
nowhere. The function is lifted from the script's own text and run under bash
against a synthetic inventory and Dockerfile, as
tests/test_check_image_inventory_go_directive.py does for the Go directive.
"""

import pathlib
import subprocess
import sys
import tempfile
import unittest

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from _lift_shell import lift_function  # noqa: E402

_REPO_ROOT = _HERE.parent
_SCRIPT = _REPO_ROOT / "hack" / "check-image-inventory.sh"

# Lifted by name: a rename fails here loudly instead of silently shrinking
# what is tested. repo_of and pin_of read images.json through jq and are
# stubbed below instead, as test_check_image_inventory_operator_pins.py does:
# jq is not on the Python test runner's PATH.
_LIFTED_FUNCTIONS = ("fail", "normalise", "check_literal_from")

# The call sites, asserted present because the lift below supplies its own.
_CALL_SITES = (
    "check_literal_from busybox agentplugins/pubsub-platform/Dockerfile",
    "check_literal_from busybox agentplugins/gke-stockout-investigator/Dockerfile",
)

_NAME = "busybox"
_REPOSITORY = "docker.io/library/busybox"
_PIN = "musl@sha256:" + "a" * 64
_OTHER_PIN = "musl@sha256:" + "b" * 64


def _run_check(dockerfile: str, pin: str = _PIN) -> subprocess.CompletedProcess:
    text = _SCRIPT.read_text()
    functions = "".join(lift_function(name, text, _SCRIPT) for name in _LIFTED_FUNCTIONS)
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / "Dockerfile").write_text(dockerfile)
        script = (
            "set -u\nstatus=0\nINVENTORY=images.json\n"
            f'repo_of() {{ echo "{_REPOSITORY}"; }}\n'
            f'pin_of() {{ echo "{pin}"; }}\n'
            + functions
            + f"check_literal_from {_NAME} Dockerfile\nexit $status\n"
        )
        return subprocess.run(
            ["bash", "-c", script], cwd=root, capture_output=True, text=True, check=False
        )


class CheckLiteralFromTest(unittest.TestCase):
    def test_matching_pin_passes(self):
        result = _run_check(f"FROM busybox:{_PIN}\nCOPY files/ /\n")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_library_prefix_is_normalised(self):
        result = _run_check(f"FROM {_REPOSITORY}:{_PIN}\nCOPY files/ /\n")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_drifted_digest_fails_naming_the_dockerfile(self):
        result = _run_check(f"FROM busybox:{_OTHER_PIN}\nCOPY files/ /\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Dockerfile: FROM pins", result.stderr)
        self.assertIn(_PIN, result.stderr)

    def test_drifted_inventory_fails(self):
        result = _run_check(f"FROM busybox:{_PIN}\nCOPY files/ /\n", pin=_OTHER_PIN)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(_OTHER_PIN, result.stderr)

    def test_last_from_is_the_one_compared(self):
        multi_stage = f"FROM golang:1.27-alpine AS build\nRUN true\nFROM busybox:{_PIN}\nCOPY --from=build /x /\n"
        self.assertEqual(_run_check(multi_stage).returncode, 0)
        wrong_last = f"FROM busybox:{_PIN} AS unused\nFROM busybox:{_OTHER_PIN}\n"
        self.assertNotEqual(_run_check(wrong_last).returncode, 0)

    def test_docker_grammar_variants_are_read(self):
        # Docker accepts a lowercase keyword, leading blanks and --flag words
        # before the reference; a last stage written any of those ways must be
        # the one compared, and a flag must never be taken for the image.
        for text in (
            f"from busybox:{_PIN}\n",
            f"  FROM busybox:{_PIN}\n",
            f"FROM --platform=linux/amd64 busybox:{_PIN}\n",
            f"FROM --platform=linux/amd64 --no-cache busybox:{_PIN}\n",
        ):
            with self.subTest(text=text):
                result = _run_check(text)
                self.assertEqual(result.returncode, 0, result.stderr)
        for text in (
            f"FROM busybox:{_PIN} AS build\nfrom busybox:{_OTHER_PIN}\n",
            f"FROM busybox:{_PIN} AS build\n  FROM busybox:{_OTHER_PIN}\n",
        ):
            with self.subTest(text=text):
                result = _run_check(text)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(_OTHER_PIN, result.stderr)

    def test_backslash_continuations_are_joined(self):
        # A line ending in `\` is one instruction with the next in Docker's
        # grammar: a wrapped FROM is read whole, and a `from …` continuation
        # inside a RUN is part of the RUN, not a stage.
        wrapped = f"FROM --platform=linux/amd64 \\\n    busybox:{_PIN}\nCOPY files/ /\n"
        result = _run_check(wrapped)
        self.assertEqual(result.returncode, 0, result.stderr)
        wrapped_drift = f"FROM \\\n  busybox:{_OTHER_PIN}\n"
        result = _run_check(wrapped_drift)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(_OTHER_PIN, result.stderr)
        run_continuation = (
            f"FROM busybox:{_PIN}\n"
            'RUN python3 -c "import json; \\\n'
            'from pathlib import Path; print(Path.cwd())"\n'
        )
        result = _run_check(run_continuation)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_comment_lines_and_trailing_blanks_follow_docker(self):
        # Docker strips comment lines before joining and accepts blanks after
        # the backslash; a comment ending in `\` continues nothing.
        passing = (
            f"FROM --platform=linux/amd64 \\ \n    busybox:{_PIN}\n",
            f"FROM --platform=linux/amd64 \\\n  # amd64 is the only platform the operator schedules\n  busybox:{_PIN}\n",
            f"# docker build --platform linux/amd64 \\\n#   -t plugin .\nFROM busybox:{_PIN}\nCOPY files/ /\n",
        )
        for text in passing:
            with self.subTest(text=text):
                result = _run_check(text)
                self.assertEqual(result.returncode, 0, result.stderr)
        hidden_drift = (
            f"FROM busybox:{_PIN} AS base\n"
            "# keep the shipping stage last \\\n"
            f"FROM busybox:{_OTHER_PIN}\nCOPY files/ /\n"
        )
        result = _run_check(hidden_drift)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(_OTHER_PIN, result.stderr)

    def test_missing_from_fails(self):
        result = _run_check("COPY files/ /\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("<unset>", result.stderr)

    def test_script_calls_the_check_for_plugin_dockerfiles(self):
        text = _SCRIPT.read_text()
        for call in _CALL_SITES:
            self.assertIn(call, text)


if __name__ == "__main__":
    unittest.main()
