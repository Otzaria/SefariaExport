"""The authors export: Sefaria's full author-name vocabulary, not just `he`.

These run without a Sefaria checkout or a Mongo instance — `run_authors_export`
imports `sefaria.system.database` lazily, so a stub module in `sys.modules` is
enough to pin the behaviour.
"""
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

from run_exports import (
    AUTHORS_EXPORT_FILENAME,
    _author_titles,
    _primary,
    run_authors_export,
)


class FakeCursor:
    """Mimics the chained `.find(...).sort(...)` the exporter uses."""

    def __init__(self, docs):
        self._docs = docs
        self.sort_calls = []

    def sort(self, spec):
        self.sort_calls.append(spec)
        key = spec[0][0]
        reverse = spec[0][1] == -1
        self._docs = sorted(self._docs, key=lambda d: d.get(key) or "", reverse=reverse)
        return self

    def __iter__(self):
        return iter(self._docs)


class FakeTopics:
    def __init__(self, docs):
        self.docs = docs
        self.queries = []
        self.cursor = None

    def find(self, query, projection=None):
        self.queries.append((query, projection))
        matched = [d for d in self.docs if all(d.get(k) == v for k, v in query.items())]
        self.cursor = FakeCursor(matched)
        return self.cursor


class AuthorsExportTest(unittest.TestCase):
    def _install_fake_db(self, docs):
        topics = FakeTopics(docs)
        db = types.SimpleNamespace(topics=topics)
        mod = types.ModuleType("sefaria.system.database")
        mod.db = db
        pkg = types.ModuleType("sefaria")
        system = types.ModuleType("sefaria.system")
        for name, m in (
            ("sefaria", pkg),
            ("sefaria.system", system),
            ("sefaria.system.database", mod),
        ):
            self.addCleanup(sys.modules.pop, name, None)
            sys.modules[name] = m
        return topics

    def _run(self, docs):
        topics = self._install_fake_db(docs)
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("SEFARIA_EXPORT_PATH")
            os.environ["SEFARIA_EXPORT_PATH"] = tmp
            try:
                run_authors_export()
                path = Path(tmp) / AUTHORS_EXPORT_FILENAME
                return json.loads(path.read_text(encoding="utf-8")), topics
            finally:
                if old is None:
                    os.environ.pop("SEFARIA_EXPORT_PATH", None)
                else:
                    os.environ["SEFARIA_EXPORT_PATH"] = old

    # --- the point of the whole change: honorific forms survive ---

    def test_honorific_and_acronym_forms_are_exported(self):
        out, _ = self._run([
            {
                "slug": "nissim-of-gerona",
                "subclass": "author",
                "titles": [
                    {"text": "Nissim of Gerona", "lang": "en", "primary": True},
                    {"text": "נסים מגירונה", "lang": "he", "primary": True},
                    {"text": 'רבנו נסים מגירונה (ר"ן)', "lang": "he"},
                    {"text": 'ר"ן', "lang": "he"},
                ],
            },
        ])
        self.assertEqual(1, len(out))
        rec = out[0]
        self.assertEqual("nissim-of-gerona", rec["slug"])
        self.assertEqual("נסים מגירונה", rec["primaryHe"])
        self.assertEqual("Nissim of Gerona", rec["primaryEn"])
        texts = [t["text"] for t in rec["titles"]]
        self.assertIn('רבנו נסים מגירונה (ר"ן)', texts)
        self.assertIn('ר"ן', texts)

    def test_only_author_topics_are_queried(self):
        _, topics = self._run([
            {"slug": "a", "subclass": "author", "titles": [{"text": "א", "lang": "he"}]},
            {"slug": "prayer", "subclass": "concept", "titles": [{"text": "Prayer", "lang": "en"}]},
        ])
        query, projection = topics.queries[0]
        self.assertEqual({"subclass": "author"}, query)
        self.assertEqual(0, projection["_id"])

    # --- determinism: the archive must be reproducible across runs ---

    def test_records_are_sorted_by_slug(self):
        out, topics = self._run([
            {"slug": "zzz", "subclass": "author", "titles": [{"text": "ז", "lang": "he"}]},
            {"slug": "aaa", "subclass": "author", "titles": [{"text": "א", "lang": "he"}]},
            {"slug": "mmm", "subclass": "author", "titles": [{"text": "מ", "lang": "he"}]},
        ])
        self.assertEqual(["aaa", "mmm", "zzz"], [r["slug"] for r in out])
        self.assertEqual([[["slug", 1]]], topics.cursor.sort_calls)

    def test_titles_order_is_stable_primary_first(self):
        titles = _author_titles({
            "titles": [
                {"text": "b", "lang": "he"},
                {"text": "a", "lang": "he"},
                {"text": "z", "lang": "he", "primary": True},
                {"text": "c", "lang": "en"},
            ],
        })
        self.assertEqual(
            [("z", "he"), ("c", "en"), ("a", "he"), ("b", "he")],
            [(t["text"], t["lang"]) for t in titles],
        )

    # --- hygiene ---

    def test_blank_and_duplicate_titles_are_dropped(self):
        titles = _author_titles({
            "titles": [
                {"text": "  ", "lang": "he"},
                {"text": "", "lang": "he"},
                {"text": " א ", "lang": "he"},
                {"text": "א", "lang": "he"},
                {"text": "א", "lang": "en"},
            ],
        })
        self.assertEqual([("א", "en"), ("א", "he")], [(t["text"], t["lang"]) for t in titles])

    def test_author_with_no_usable_title_is_skipped(self):
        out, _ = self._run([
            {"slug": "keeps", "subclass": "author", "titles": [{"text": "א", "lang": "he"}]},
            {"slug": "empty", "subclass": "author", "titles": [{"text": "  ", "lang": "he"}]},
            {"slug": "none", "subclass": "author"},
        ])
        self.assertEqual(["keeps"], [r["slug"] for r in out])

    def test_author_with_no_slug_is_skipped(self):
        out, _ = self._run([
            {"slug": "", "subclass": "author", "titles": [{"text": "א", "lang": "he"}]},
            {"slug": "ok", "subclass": "author", "titles": [{"text": "ב", "lang": "he"}]},
        ])
        self.assertEqual(["ok"], [r["slug"] for r in out])

    def test_primary_falls_back_to_first_title_in_language(self):
        titles = [
            {"text": "שני", "lang": "he", "primary": False},
            {"text": "ראשון", "lang": "he", "primary": False},
        ]
        self.assertEqual("שני", _primary(titles, "he"))
        self.assertEqual("", _primary(titles, "en"))

    # --- fail loudly ---

    def test_empty_result_raises_instead_of_writing_an_empty_file(self):
        self._install_fake_db([
            {"slug": "prayer", "subclass": "concept", "titles": [{"text": "Prayer", "lang": "en"}]},
        ])
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["SEFARIA_EXPORT_PATH"] = tmp
            try:
                with self.assertRaises(RuntimeError):
                    run_authors_export()
                self.assertFalse((Path(tmp) / AUTHORS_EXPORT_FILENAME).exists())
            finally:
                os.environ.pop("SEFARIA_EXPORT_PATH", None)


if __name__ == "__main__":
    unittest.main()


class AuthorsExportFailLoudTest(AuthorsExportTest):
    """The export must refuse to ship a file that silently lost every name."""

    def test_records_without_any_hebrew_title_raise(self):
        self._install_fake_db([
            # A plausible upstream language-code change: nothing is tagged `he`.
            {"slug": "a", "subclass": "author",
             "titles": [{"text": "A", "lang": "he-IL", "primary": True}]},
            {"slug": "b", "subclass": "author",
             "titles": [{"text": "B", "lang": "en", "primary": True}]},
        ])
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["SEFARIA_EXPORT_PATH"] = tmp
            try:
                with self.assertRaises(RuntimeError) as cm:
                    run_authors_export()
                self.assertIn("Hebrew", str(cm.exception))
                self.assertFalse((Path(tmp) / AUTHORS_EXPORT_FILENAME).exists())
            finally:
                os.environ.pop("SEFARIA_EXPORT_PATH", None)

    def test_one_hebrew_title_is_enough_to_pass(self):
        out, _ = self._run([
            {"slug": "a", "subclass": "author", "titles": [{"text": "A", "lang": "en"}]},
            {"slug": "b", "subclass": "author", "titles": [{"text": "ב", "lang": "he"}]},
        ])
        self.assertEqual(["a", "b"], [r["slug"] for r in out])
        self.assertEqual("", out[0]["primaryHe"])
        self.assertEqual("ב", out[1]["primaryHe"])
