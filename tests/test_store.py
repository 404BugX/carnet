"""Behavioural tests for the data layer.

Only `store.py` is imported here: it has no Hermes dependency, so the suite runs
with the standard library alone — no Hermes install, no network, no embedding
model. Each test targets a decision that would break silently: schema
migrations, the note lifecycle, the memory wipe used when a profile was copied,
and what the score reported by `recall` actually means.

    python3 -m unittest discover -s tests
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from store import Carnet, coverage, looks_like_fragment  # noqa: E402


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "carnet"
        self.store = Carnet(self.root)
        self.store.init_schema()

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    # -- schema ---------------------------------------------------------
    def test_schema_creation_is_idempotent(self) -> None:
        """Opening the same base twice must not fail or wipe anything."""
        added = self.store.add_fact("The project lives in a folder on disk.",
                                    importance=1, confidence="certain")
        self.assertTrue(added.get("id"))

        again = Carnet(self.root)
        again.init_schema()
        self.assertEqual(again.count()["facts"], 1)
        self.assertTrue(again.meta_get("schema_version"))
        again.close()

    # -- notes ----------------------------------------------------------
    def test_note_keeps_kind_and_session_then_closes(self) -> None:
        added = self.store.add_note("Count the syllables of the word green",
                                    "2999-01-01T00:00:00+00:00",
                                    kind="question", session_id="session-1")
        self.assertTrue(added["id"].startswith("n-"))

        pending = self.store.active_notes(limit=10)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["kind"], "question")
        self.assertEqual(pending[0]["session_id"], "session-1")

        self.assertTrue(self.store.close_note(added["id"]))
        self.assertEqual(self.store.active_notes(limit=10), [])

    def test_unknown_note_kind_falls_back_to_fact(self) -> None:
        added = self.store.add_note("Something to keep in mind",
                                    "2999-01-01T00:00:00+00:00", kind="banana")
        self.assertEqual(added["kind"], "fact")

    # -- profile clone ---------------------------------------------------
    def test_reset_memory_empties_everything_but_the_schema(self) -> None:
        self.store.add_fact("A durable fact about the project.",
                            importance=1, confidence="certain")
        self.store.add_fact("A second one so the count is not accidental.",
                            importance=1, confidence="probable")
        self.store.add_note("A note that must disappear too",
                            "2999-01-01T00:00:00+00:00")

        removed = self.store.reset_memory(reason="test")
        self.assertEqual(removed, 2)
        self.assertEqual(self.store.count()["facts"], 0)
        self.assertEqual(self.store.active_notes(limit=10), [])

        # The base must stay usable, not just empty.
        self.store.add_fact("Written after the reset.", importance=1, confidence="certain")
        self.assertEqual(self.store.count()["facts"], 1)

    # -- recall contract -------------------------------------------------
    def test_coverage_measures_the_share_of_query_words_present(self) -> None:
        text = "The user prefers short answers in French."
        self.assertEqual(coverage("user prefers answers", text), 1.0)
        self.assertEqual(coverage("user prefers answers about horses", text), 0.6)
        self.assertEqual(coverage("crystal cormorant under the square", text), 0.0)

    def test_coverage_ignores_a_query_made_only_of_stopwords(self) -> None:
        self.assertEqual(coverage("ok thanks a lot", "Any fact at all"), 0.0)

    def test_coverage_ignores_accents_and_case(self) -> None:
        self.assertEqual(coverage("préfère réponses", "PREFERE des REPONSES"), 1.0)

    # -- search ----------------------------------------------------------
    def test_search_finds_a_fact_by_one_of_its_terms(self) -> None:
        self.store.add_fact("The build uses a local SQLite database.",
                            importance=1, confidence="certain", topic="storage")
        self.store.add_fact("Unrelated note about a garden.",
                            importance=1, confidence="certain", topic="misc")

        hits = self.store.search("sqlite")
        self.assertEqual(len(hits), 1)
        self.assertIn("SQLite", hits[0]["row"]["text"])

    # -- fragments -------------------------------------------------------
    def test_a_real_sentence_is_not_a_fragment(self) -> None:
        ok = "The build uses a local SQLite file, never a remote server."
        self.assertEqual(looks_like_fragment(ok), "")
        self.assertEqual(looks_like_fragment(ok, strict=True), "")

    def test_a_piece_of_someone_else_s_sentence_is_refused(self) -> None:
        from store import looks_like_fragment as lf

        for broken in (
            "injectée est impossible à clôturer",     # starts mid-sentence
            "alors que normalement ça doit être fait",  # starts on a connector
            "a ete injecter au tout debut la",          # dictated noise
            "le fait",                                  # too short
            "Something that stops mid-thought,",        # ends mid-sentence
        ):
            self.assertNotEqual(lf(broken), "", broken)

    def test_the_core_layer_is_held_to_a_longer_minimum(self) -> None:
        from store import looks_like_fragment as lf

        short = "Answers in French."
        self.assertEqual(lf(short), "")
        self.assertNotEqual(lf(short, strict=True), "")


if __name__ == "__main__":
    unittest.main()
