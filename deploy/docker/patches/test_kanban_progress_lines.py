"""Unit tests for the kanban progress lines installed by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import unittest

from kanban_handoff_clip import ELLIPSIS
from kanban_progress_lines import DEFAULT_NOTE_LIMIT, progress_note

RUN_URL = "https://github.com/gke-agentic/adamparco-infra/actions/runs/9912345678"


class ProgressNoteTest(unittest.TestCase):
    def test_a_deliberate_note_is_delivered(self):
        self.assertEqual(
            progress_note({"note": "Scanned 3 of 7 clusters; no drift so far."}),
            "Scanned 3 of 7 clusters; no drift so far.",
        )

    def test_an_auto_heartbeat_is_silent(self):
        # The ~2,100 heartbeat rows on the live board are all payload=None.
        # This empty return is the whole reason widening TERMINAL_KINDS does
        # not turn every tool call into a chat message.
        self.assertEqual(progress_note(None), "")

    def test_a_payload_without_a_note_is_silent(self):
        self.assertEqual(progress_note({}), "")
        self.assertEqual(progress_note({"stage": "scanning"}), "")
        self.assertEqual(progress_note({"note": None}), "")

    def test_a_blank_note_is_silent(self):
        self.assertEqual(progress_note({"note": ""}), "")
        self.assertEqual(progress_note({"note": "   \n "}), "")

    def test_a_non_mapping_payload_is_silent(self):
        for payload in ("a bare string", 42, ["note", "x"], object()):
            with self.subTest(payload=payload):
                self.assertEqual(progress_note(payload), "")

    def test_surrounding_whitespace_is_stripped(self):
        self.assertEqual(progress_note({"note": "  working  \n"}), "working")

    def test_an_overlong_note_is_clipped_on_a_token_boundary(self):
        note = " ".join(f"step{i}" for i in range(200))
        clipped = progress_note({"note": note})
        self.assertLessEqual(len(clipped), DEFAULT_NOTE_LIMIT)
        self.assertTrue(clipped.endswith(ELLIPSIS))
        body = clipped[: -len(ELLIPSIS)]
        for token in body.split():
            self.assertIn(token, note.split(), f"token {token!r} was cut")

    def test_a_url_is_dropped_rather_than_severed(self):
        note = ("filler " * 60) + RUN_URL
        clipped = progress_note({"note": note})
        self.assertLessEqual(len(clipped), DEFAULT_NOTE_LIMIT)
        # Either the whole link or none of it — never a prefix that 404s.
        self.assertNotIn("https://", clipped)

    def test_a_note_that_ends_in_a_url_within_budget_keeps_it_whole(self):
        note = "Kicked off the rollout: " + RUN_URL
        self.assertLess(len(note), DEFAULT_NOTE_LIMIT)
        self.assertIn(RUN_URL, progress_note({"note": note}))

    def test_the_limit_is_honoured_at_every_width(self):
        note = "Reconciling the fleet inventory across every managed cluster. " * 10
        for limit in range(1, 320):
            with self.subTest(limit=limit):
                self.assertLessEqual(len(progress_note({"note": note}, limit)), limit)

    def test_the_default_limit_is_a_ping_not_a_report(self):
        # Deliberately far below the completion handoff's 1200: a worker with
        # more than this to say should be completing the card, not pinging it.
        self.assertLessEqual(DEFAULT_NOTE_LIMIT, 500)


if __name__ == "__main__":
    unittest.main()
