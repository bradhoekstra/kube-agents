"""`check_literal_from` in hack/check-image-inventory.sh fails when an agent
plugin Dockerfile's literal `FROM` pin and the inventory's entry move apart,
and fails closed, saying why, on a Dockerfile that is not plain ASCII, or that
holds anything other than comment lines, blank lines, exactly one single-line
FROM and COPY lines.

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
        (root / "Dockerfile").write_text(dockerfile, encoding="utf-8")
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

    def test_docker_grammar_of_one_from_line_is_read(self):
        # What Docker accepts on a single FROM line is accepted here: a
        # lowercase keyword, leading blanks, --flag words before the reference,
        # a CR before the newline, and comment lines anywhere, including ones
        # that end in a backslash (a commented-out `docker build \` block).
        for text in (
            f"from busybox:{_PIN}\n",
            f"  FROM busybox:{_PIN}\n",
            f"FROM --platform=linux/amd64 busybox:{_PIN}\n",
            f"FROM --platform=linux/amd64 --no-cache busybox:{_PIN}\n",
            f"FROM busybox:{_PIN}\r\nCOPY files/ /\r\n",
            f"# docker build --platform linux/amd64 \\\n#   -t plugin .\nFROM busybox:{_PIN}\nCOPY files/ /\n",
        ):
            with self.subTest(text=text):
                result = _run_check(text)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_continuations_fail_closed_with_the_reason(self):
        # The fence does not join lines, so any instruction line ending in a
        # backslash -- a wrapped FROM, a comment inside it, an escaped escape
        # above a second stage -- is refused for that stated reason, never
        # reported as a drifted or an in-step pin.
        for text in (
            f"FROM --platform=linux/amd64 \\\n    busybox:{_PIN}\n",
            f"FROM --platform=linux/amd64 \\ \n    busybox:{_PIN}\n",
            f"FROM --platform=linux/amd64 \\\n  # amd64 only\n  busybox:{_PIN}\n",
            f"FROM busybox:{_PIN} AS base\nRUN echo \\\\\nFROM busybox:{_OTHER_PIN}\nCOPY files/ /\n",
        ):
            with self.subTest(text=text):
                result = _run_check(text)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("ends in a backslash", result.stderr)
                self.assertNotIn("FROM pins", result.stderr)

    def test_more_or_fewer_than_one_from_fails_closed(self):
        # A second stage or no FROM at all: the fence names the count and what
        # to write instead of guessing a stage.
        for text, count in (
            (f"FROM golang:1.27-alpine AS build\nCOPY a /b\nFROM busybox:{_PIN}\n", "2"),
            (f"FROM busybox:{_PIN} AS unused\nfrom busybox:{_OTHER_PIN}\n", "2"),
            ("COPY files/ /\n", "0"),
        ):
            with self.subTest(text=text):
                result = _run_check(text)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"{count} FROM lines", result.stderr)
                self.assertNotIn("FROM pins", result.stderr)

    def test_any_other_instruction_fails_closed_naming_the_line(self):
        # The shape plugin_image.sh defines is FROM and COPY lines; a RUN, a
        # heredoc body line or a backtick continuation is refused by name, so
        # the crane and docker builders never disagree on a file this passes.
        for text, line in (
            (f"FROM busybox:{_PIN}\nRUN rm -rf /bin\nCOPY files/ /\n", "RUN rm -rf /bin"),
            (f"FROM busybox:{_PIN}\nCOPY <<EOF /x\nFROM busybox:{_OTHER_PIN}\nEOF\n", "EOF"),
            (f"# escape=`\nFROM --platform=linux/amd64 `\n  busybox:{_PIN}\n", f"  busybox:{_PIN}"),
        ):
            with self.subTest(text=text):
                result = _run_check(text)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"line '{line}' is neither FROM nor COPY", result.stderr)
                self.assertNotIn("FROM pins", result.stderr)

    def test_non_ascii_bytes_fail_closed(self):
        # Docker strips a BOM and trims Unicode space before reading a
        # keyword; the fence does not try to, and refuses the file by reason,
        # so a second stage behind a non-breaking space cannot pass as drift.
        for text in (
            f"\ufeffFROM busybox:{_PIN}\nCOPY files/ /\n",
            f"FROM busybox:{_PIN} AS base\n\u00a0FROM busybox:{_OTHER_PIN}\nCOPY files/ /\n",
        ):
            with self.subTest(text=text):
                result = _run_check(text)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("outside printable ASCII", result.stderr)
                self.assertNotIn("FROM pins", result.stderr)

    def test_script_calls_the_check_for_plugin_dockerfiles(self):
        text = _SCRIPT.read_text()
        for call in _CALL_SITES:
            self.assertIn(call, text)


if __name__ == "__main__":
    unittest.main()
