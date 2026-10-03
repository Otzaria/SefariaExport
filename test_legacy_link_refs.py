"""Legacy link refs: links whose refs predate a Sefaria schema change.

The bug these pin down: every export dropped 445 link documents as
`refs_unparsable`, and the log named only the first 30 - all of them Jastrow
headwords, because links are sorted by `refs.0`. 253 of the 445 were
mesorat-hashas parallels of Tanna DeBei Eliyahu Zuta, a book Otzaria ships,
still addressed through the old "Seder Eliyahu Zuta" node.

Like test_export_accounting.py these run without a Sefaria checkout or a Mongo
instance: the exporter imports `sefaria.*` lazily, so stubs in `sys.modules`
are enough.
"""
import contextlib
import csv
import io
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

import run_exports
from run_exports import (
    new_form_link_exists,
    parse_link_refs,
    rewrite_legacy_ref,
    unparsable_ref_title,
)

ZUTA_OLD = "Tanna DeBei Eliyahu Zuta, Seder Eliyahu Zuta 4:1"
ZUTA_NEW = "Tanna DeBei Eliyahu Zuta 4:1"
ZUTA_OLD_1 = "Tanna DeBei Eliyahu Zuta, Seder Eliyahu Zuta 1:1"
ZUTA_NEW_1 = "Tanna DeBei Eliyahu Zuta 1:1"
RULE = "tanna_debei_eliyahu_zuta_default_node"


class StubInputError(Exception):
    pass


class StubIndex:
    def __init__(self, title, category):
        self.title = title
        self.categories = [category]


class StubNode:
    def __init__(self, depth):
        self.depth = depth


class StubRef:
    """Accepts only the refs in VALID, like Ref() on a real library."""

    VALID = {
        ZUTA_NEW: ("Tanna DeBei Eliyahu Zuta", "Midrash", 2, [4, 1]),
        "Tanna DeBei Eliyahu Zuta 1:1": ("Tanna DeBei Eliyahu Zuta", "Midrash", 2, [1, 1]),
        "Vayikra Rabbah 9:3": ("Vayikra Rabbah", "Midrash", 2, [9, 3]),
        "Pesachim 36a": ("Pesachim", "Talmud", 2, [71]),
        "Genesis 1:1": ("Genesis", "Tanakh", 2, [1, 1]),
    }

    def __init__(self, tref):
        if tref not in self.VALID:
            raise StubInputError(tref)
        title, category, depth, sections = self.VALID[tref]
        self.tref = tref
        self.book = title
        self.index = StubIndex(title, category)
        self.index_node = StubNode(depth)
        self.sections = sections


class RewriteRuleTest(unittest.TestCase):
    def test_the_old_zuta_node_maps_to_the_default_node(self):
        self.assertEqual(rewrite_legacy_ref(ZUTA_OLD),
                         (ZUTA_NEW, "tanna_debei_eliyahu_zuta_default_node"))

    def test_every_chapter_keeps_its_number(self):
        for n in range(1, 16):
            new, _ = rewrite_legacy_ref(f"Tanna DeBei Eliyahu Zuta, Seder Eliyahu Zuta {n}:1")
            self.assertEqual(new, f"Tanna DeBei Eliyahu Zuta {n}:1")

    def test_the_additions_node_is_not_touched(self):
        self.assertIsNone(rewrite_legacy_ref(
            "Tanna DeBei Eliyahu Zuta, Additions to Seder Eliyahu Zuta, Mavo 7"))

    def test_unknown_and_non_string_refs_have_no_rule(self):
        self.assertIsNone(rewrite_legacy_ref("A Dictionary of the Talmud, טַוָּום 1"))
        self.assertIsNone(rewrite_legacy_ref(None))


class ParseLinkRefsTest(unittest.TestCase):
    def parse(self, refs):
        return parse_link_refs(refs, StubRef, StubInputError)

    def test_valid_refs_are_never_rewritten(self):
        orefs, trefs, rules = self.parse(["Vayikra Rabbah 9:3", "Tanna DeBei Eliyahu Zuta 1:1"])
        self.assertEqual(trefs, ["Vayikra Rabbah 9:3", "Tanna DeBei Eliyahu Zuta 1:1"])
        self.assertEqual(rules, [])
        self.assertEqual([o.tref for o in orefs], trefs)

    def test_a_legacy_side_is_rewritten_and_named(self):
        orefs, trefs, rules = self.parse(["Vayikra Rabbah 9:3", ZUTA_OLD])
        self.assertEqual(trefs, ["Vayikra Rabbah 9:3", ZUTA_NEW])
        self.assertEqual(orefs[1].tref, ZUTA_NEW)
        self.assertEqual(rules, ["tanna_debei_eliyahu_zuta_default_node"])

    def test_an_unknown_bad_ref_still_raises(self):
        with self.assertRaises(StubInputError):
            self.parse(["Vayikra Rabbah 9:3", "Sheet 279"])

    def test_a_rewrite_that_does_not_parse_still_raises(self):
        # Chapter 99 matches the rule but is not a real ref.
        with self.assertRaises(StubInputError):
            self.parse(["Vayikra Rabbah 9:3", "Tanna DeBei Eliyahu Zuta, Seder Eliyahu Zuta 99:1"])


class UnparsableTitleTest(unittest.TestCase):
    def test_keys_group_by_book(self):
        self.assertEqual(unparsable_ref_title("A Dictionary of the Talmud, אַלּוֹאין 1"),
                         "A Dictionary of the Talmud")
        self.assertEqual(unparsable_ref_title("Shulchan Aruch HaRav 1:1:3"), "Shulchan Aruch HaRav")
        self.assertEqual(unparsable_ref_title("Nefesh David on Zohar 1:238a"), "Nefesh David on Zohar")
        self.assertEqual(unparsable_ref_title("Sheet 33:1"), "Sheet")
        self.assertEqual(unparsable_ref_title(None), "None")


# --------------------------------------------------------------------------
# The links loop, end to end
# --------------------------------------------------------------------------


class FakeLinks:
    def __init__(self, docs):
        self._docs = docs

    def estimated_document_count(self):
        return len(self._docs)

    def find(self):
        docs = self._docs

        class Cursor:
            def sort(self, spec):
                return iter(sorted(docs, key=lambda d: d["refs"][0]))

        return Cursor()

    def find_one(self, query, projection=None):
        wanted = query["refs"]["$all"]
        for d in self._docs:
            if all(r in d["refs"] for r in wanted):
                return {"_id": d["_id"]}
        return None


class NewFormLinkExistsTest(unittest.TestCase):
    LINKS = FakeLinks([
        {"_id": 1, "refs": ["Genesis 1:1", ZUTA_NEW_1], "type": ""},
        {"_id": 2, "refs": ["Vayikra Rabbah 9:3", ZUTA_OLD], "type": "mesorat hashas"},
    ])

    def test_an_existing_pair_is_found_in_either_order(self):
        self.assertTrue(new_form_link_exists(self.LINKS, ("Genesis 1:1", ZUTA_NEW_1)))
        self.assertTrue(new_form_link_exists(self.LINKS, (ZUTA_NEW_1, "Genesis 1:1")))

    def test_the_legacy_document_itself_does_not_count(self):
        self.assertFalse(new_form_link_exists(self.LINKS, ("Vayikra Rabbah 9:3", ZUTA_NEW)))

    def test_one_shared_side_is_not_enough(self):
        self.assertFalse(new_form_link_exists(self.LINKS, ("Genesis 1:1", ZUTA_NEW)))


def unicodecsv_shim():
    """`unicodecsv.writer` over a binary file - all the exporter uses."""
    mod = types.ModuleType("unicodecsv")

    class Writer:
        def __init__(self, f):
            self._f = f

        def writerow(self, row):
            buf = io.StringIO()
            csv.writer(buf, lineterminator="\r\n").writerow(row)
            self._f.write(buf.getvalue().encode("utf-8"))

    mod.writer = Writer
    return mod


class LinksLoopTest(unittest.TestCase):
    DOCS = [
        # New pair: rewritten and written.
        {"_id": 1, "refs": ["Vayikra Rabbah 9:3", ZUTA_OLD], "type": "mesorat hashas"},
        # A new-form link, and the same pair again in the old form, reversed
        # and with another type: the legacy copy must be skipped.
        {"_id": 2, "refs": ["Genesis 1:1", ZUTA_NEW_1], "type": ""},
        {"_id": 3, "refs": [ZUTA_OLD_1, "Genesis 1:1"], "type": "mesorat hashas"},
        # Both sides legacy: one document, two sides, one rule.
        {"_id": 6, "refs": [ZUTA_OLD_1, ZUTA_OLD], "type": "mesorat hashas"},
        {"_id": 4, "refs": ["A Dictionary of the Talmud, אַלּוֹאין 1", "Genesis 1:1"], "type": ""},
        {"_id": 5, "refs": ["Pesachim 36a", "Shulchan Aruch HaRav 1:1:3"], "type": ""},
    ]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        database = types.ModuleType("sefaria.system.database")
        database.db = types.SimpleNamespace(links=FakeLinks(self.DOCS))
        exceptions = types.ModuleType("sefaria.system.exceptions")
        exceptions.InputError = StubInputError
        text_mod = types.ModuleType("sefaria.model.text")
        text_mod.Ref = StubRef
        helper_text = types.ModuleType("sefaria.helper.text")
        helper_text.get_parasha_ref_set = lambda: set()
        helper_text.get_talmud_perek_ref_set = lambda: set()
        modules = {
            "sefaria": types.ModuleType("sefaria"),
            "sefaria.system": types.ModuleType("sefaria.system"),
            "sefaria.system.database": database,
            "sefaria.system.exceptions": exceptions,
            "sefaria.model": types.ModuleType("sefaria.model"),
            "sefaria.model.text": text_mod,
            "sefaria.helper": types.ModuleType("sefaria.helper"),
            "sefaria.helper.text": helper_text,
            "unicodecsv": unicodecsv_shim(),
        }
        for name, mod in modules.items():
            self.addCleanup(sys.modules.pop, name, None)
            sys.modules[name] = mod
        env = mock.patch.dict(os.environ, {"SEFARIA_EXPORT_PATH": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        sha = mock.patch.object(run_exports, "_sefaria_project_sha", return_value="0" * 40)
        sha.start()
        self.addCleanup(sha.stop)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.stats = run_exports.run_links_export_extended()
        self.log = buf.getvalue()
        with open(os.path.join(self.tmp.name, "links", "links0.csv"), encoding="utf-8") as f:
            self.rows = list(csv.reader(f))[1:]

    def test_the_identity_still_holds(self):
        c = self.stats["counts"]
        self.assertEqual(self.stats["links"], 6)
        self.assertEqual(c["written"], 3)
        self.assertEqual(c["refs_unparsable"], 2)
        self.assertEqual(c["legacy_duplicate"], 1)
        self.assertEqual(self.stats["links"], c["written"] + c["refs_unparsable"]
                         + c["refs_malformed"] + c["legacy_duplicate"])
        self.assertEqual(len(self.rows), c["written"])
        self.assertIn("+ legacy_duplicate=1;", self.log)

    def test_a_legacy_row_already_present_in_the_new_form_is_skipped_whatever_its_type(self):
        genesis = [r for r in self.rows if "Genesis 1:1" in (r[0], r[1])]
        self.assertEqual(len(genesis), 1)
        self.assertEqual((genesis[0][0], genesis[0][1], genesis[0][2]),
                         ("Genesis 1:1", ZUTA_NEW_1, ""))

    def test_the_csv_carries_the_rewritten_refs(self):
        pairs = {(r[0], r[1]) for r in self.rows}
        self.assertIn(("Vayikra Rabbah 9:3", ZUTA_NEW), pairs)
        self.assertIn((ZUTA_NEW_1, ZUTA_NEW), pairs)
        self.assertIn(("Genesis 1:1", ZUTA_NEW_1), pairs)
        self.assertFalse(any("Seder Eliyahu Zuta" in r[0] + r[1] for r in self.rows))
        zuta = next(r for r in self.rows if r[1] == ZUTA_NEW)
        self.assertEqual(zuta[4], "Tanna DeBei Eliyahu Zuta")

    def test_rewrites_count_documents_and_sides_separately(self):
        r = self.stats["legacy_ref_rewrites"]
        self.assertEqual(r, {
            "links": 3,
            "written": 2,
            "skipped_existing": 1,
            "sides": 4,
            "by_rule": {RULE: 3},
        })
        self.assertEqual(r["links"], r["written"] + r["skipped_existing"])
        self.assertEqual(r["skipped_existing"], self.stats["counts"]["legacy_duplicate"])
        self.assertEqual(sum(r["by_rule"].values()), r["links"])  # one rule here
        self.assertIn("legacy refs rewritten on 3 links (4 sides): written=2, "
                      "skipped_existing=1", self.log)

    def test_every_unparsable_pair_is_named_and_grouped_by_the_failing_side(self):
        self.assertEqual(sorted(self.stats["names"]["refs_unparsable"]), sorted([
            "A Dictionary of the Talmud, אַלּוֹאין 1 ↔ Genesis 1:1",
            "Pesachim 36a ↔ Shulchan Aruch HaRav 1:1:3",
        ]))
        self.assertEqual(self.stats["unparsable_by_title"],
                         {"A Dictionary of the Talmud": 1, "Shulchan Aruch HaRav": 1})
        self.assertIn("unparsable by title:", self.log)

    def test_the_report_names_more_than_the_log_cap(self):
        many = [{"_id": i, "refs": [f"Sheet {i}", "Genesis 1:1"], "type": ""}
                for i in range(run_exports.NAMES_IN_LOG + 5)]
        database = sys.modules["sefaria.system.database"]
        database.db = types.SimpleNamespace(links=FakeLinks(many))
        with contextlib.redirect_stdout(io.StringIO()):
            stats = run_exports.run_links_export_extended()
        self.assertEqual(len(stats["names"]["refs_unparsable"]), run_exports.NAMES_IN_LOG + 5)
        self.assertEqual(stats["unparsable_by_title"], {"Sheet": run_exports.NAMES_IN_LOG + 5})
        json.dumps(stats, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
