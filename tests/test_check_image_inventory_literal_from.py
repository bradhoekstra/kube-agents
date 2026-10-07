"""`check_literal_from` in hack/check-image-inventory.sh fails when an agent
plugin Dockerfile's literal `FROM` pin and the inventory's entry move apart,
and fails closed, naming the line, on anything the two plugin builders
(`docker build` and the crane reader in agentplugins/lib/plugin_image.sh)
would not read the same way: the fence is derived from the crane reader's
grammar, not Docker's.

The plugin images pin `busybox:musl` by digest in a literal FROM rather than an
ARG pair, so `check_base_image` never reaches them. CI only ever runs the script
on a tree where this check passes, so its fail paths would otherwise execute
nowhere. The function is lifted from the script's own text and run under bash
against a synthetic Dockerfile, as
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

    def test_what_both_builders_read_alike_passes(self):
        # The crane reader matches `FROM `/`from ` and `COPY … /files*` after
        # trimming leading blanks; Docker reads the same lines the same way.
        # Comment lines may hold any text, a CR before the newline is ignored,
        # and a `# word=` comment below the first instruction is a comment.
        for text in (
            f"from busybox:{_PIN}\nCOPY files/ /\n",
            f"  FROM busybox:{_PIN}\n  copy files/ /files/\n",
            f"FROM busybox:{_PIN}\r\nCOPY files/ /\r\n",
            f"# docker build --platform linux/amd64 \\\n#   -t plugin .\nFROM busybox:{_PIN}\nCOPY files/ /\n",
            f"# Pinned by digest \u2014 see images.json \u2026\nFROM busybox:{_PIN}\nCOPY files/ /\n",
            f"FROM busybox:{_PIN}\n# platform=linux/amd64 is the only one the operator schedules\nCOPY files/ /\n",
        ):
            with self.subTest(text=text):
                result = _run_check(text)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_what_the_crane_reader_would_misread_fails_closed(self):
        # Forms Docker accepts and the crane reader does not: a mixed-case
        # keyword (crane builds on scratch), a flag before the base (crane
        # takes the flag as the base; --platform lets docker publish another
        # architecture), a RUN, a heredoc body line, a flagged COPY or one
        # whose destination crane does not honour. Each is refused by name,
        # never reported as a pin.
        for text, fragment in (
            (f"From busybox:{_PIN}\nCOPY files/ /\n", "is not FROM or COPY"),
            (f"FROM busybox:{_PIN}\nRUN rm -rf /bin\nCOPY files/ /\n", "line 'RUN rm -rf /bin' is not FROM or COPY"),
            (f"FROM busybox:{_PIN}\nCOPY <<EOF /x\nFROM busybox:{_OTHER_PIN}\nEOF\n", "line 'EOF' is not FROM or COPY"),
            (f"FROM --platform=linux/amd64 busybox:{_PIN}\nCOPY files/ /\n", "gives FROM a flag"),
            (f"FROM --platform=linux/arm64 busybox:{_PIN}\nCOPY files/ /\n", "gives FROM a flag"),
            (f"FROM busybox:{_PIN}\nCOPY --from=alpine:latest /bin/busybox /bin/busybox\nCOPY files/ /\n", "is not 'COPY <src> /'"),
            (f"FROM busybox:{_PIN}\n  copy --chown=1000:1000 files/ /\n", "is not 'COPY <src> /'"),
            (f"FROM busybox:{_PIN}\nCOPY files/ /opt/plugin\n", "is not 'COPY <src> /'"),
            (f"FROM busybox:{_PIN}\nCOPY <<EOF /\n", "is not 'COPY <src> /'"),
        ):
            with self.subTest(text=text):
                result = _run_check(text)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(fragment, result.stderr)
                self.assertNotIn("FROM pins", result.stderr)

    def test_continuations_fail_closed_with_the_reason(self):
        # The fence does not join lines, so any instruction line ending in a
        # backslash -- a wrapped FROM, a comment inside it, an escaped escape
        # above a second stage -- is refused for that stated reason.
        for text in (
            f"FROM --platform=linux/amd64 \\\n    busybox:{_PIN}\n",
            f"FROM busybox:{_PIN} \\ \n    AS base\n",
            f"FROM busybox:{_PIN} \\\n  # a comment inside\n  AS base\n",
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
            (f"FROM golang:1.27-alpine\nCOPY a /\nFROM busybox:{_PIN}\n", "2"),
            (f"FROM busybox:{_PIN}\nfrom busybox:{_OTHER_PIN}\n", "2"),
            ("COPY files/ /\n", "0"),
        ):
            with self.subTest(text=text):
                result = _run_check(text)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"{count} FROM lines", result.stderr)
                self.assertNotIn("FROM pins", result.stderr)

    def test_parser_directives_in_the_heading_fail_closed(self):
        # `# syntax=` names a frontend image BuildKit pulls and runs, unpinned.
        # Docker reads directives only in the leading comment window, and the
        # fence refuses any `# word=` line there by name, before comments are
        # dropped, so the line cannot vanish as one.
        for text, line in (
            (f"# syntax=docker/dockerfile:1\nFROM busybox:{_PIN}\nCOPY files/ /\n", "# syntax=docker/dockerfile:1"),
            (f"# a note first\n#escape=`\nFROM busybox:{_PIN}\n", "#escape=`"),
        ):
            with self.subTest(text=text):
                result = _run_check(text)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"line '{line}' has the shape of a parser directive", result.stderr)
                self.assertNotIn("FROM pins", result.stderr)

    def test_non_ascii_instruction_bytes_fail_closed(self):
        # Docker strips a BOM and trims Unicode space before reading a
        # keyword; the fence does not try to, and refuses the file by reason,
        # so a second stage behind a non-breaking space cannot pass as drift.
        for text in (
            f"\ufeffFROM busybox:{_PIN}\nCOPY files/ /\n",
            f"FROM busybox:{_PIN}\n\u00a0FROM busybox:{_OTHER_PIN}\nCOPY files/ /\n",
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
