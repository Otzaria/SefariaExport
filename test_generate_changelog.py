"""Tests for the display (forum) copy of the changelog and its blacklist filtering.

The display run must hide exactly what SeforimLibrary will not import (title, path
and author blacklists), and must still announce a book that escaped the blacklist
because Sefaria renamed or moved it.  The machine run must stay unfiltered.
"""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import generate_changelog as gc


def sha(n):
    return f"{n:064x}"


class Fixture:
    """Two manifests, two titles maps and a current export with schemas + authors.json."""

    def __init__(self, root):
        self.root = Path(root)
        self.exports = self.root / "exports"
        (self.exports / "schemas").mkdir(parents=True)
        self.old, self.new = {}, {}
        self.old_titles, self.new_titles = {}, {}
        self.lists = {}

    def book(self, side, category, en, he, digest):
        manifest, titles = (self.old, self.old_titles) if side == "old" else (self.new, self.new_titles)
        manifest[f"./json/{category}/{en}/merged.json"] = sha(digest)
        titles[en] = he

    def version(self, category, en, filename, title):
        rel = f"json/{category}/{en}/{filename}.json"
        self.new[f"./{rel}"] = sha(1000 + len(self.new))
        path = self.exports / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"versionTitle": title}), encoding="utf-8")

    def schema(self, en, he, he_categories, authors=()):
        doc = {"title": en, "heTitle": he, "heCategories": list(he_categories),
               "authors": [dict(a) for a in authors], "schema": {"title": en, "heTitle": he}}
        path = self.exports / "schemas" / (en.replace(" ", "_") + ".json")
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    def authors_json(self, records):
        (self.exports / "authors.json").write_text(json.dumps(records, ensure_ascii=False),
                                                   encoding="utf-8")

    def blacklist(self, name, *lines):
        path = self.root / name
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.lists[name] = path

    def run(self, filtered=True, exports=True):
        def write(name, data):
            path = self.root / name
            if name.endswith(".txt"):
                path.write_text("".join(f"{v}  {k}\n" for k, v in sorted(data.items())), encoding="utf-8")
            else:
                path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            return str(path)

        out = self.root / "diff.json"
        argv = ["generate_changelog.py", write("old.txt", self.old), write("new.txt", self.new),
                str(self.root / "CHANGELOG.md"), "--new-tag", "new", "--old-tag", "old",
                "--json", str(out), "--titles", write("titles.json", self.new_titles),
                "--prev-titles", write("prev_titles.json", self.old_titles)]
        if exports:
            argv += ["--exports-dir", str(self.exports)]
        if filtered:
            for flag, name in (("--blacklist", "books.txt"), ("--authors-blacklist", "authors.txt")):
                if name in self.lists:
                    argv += [flag, str(self.lists[name])]
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch("sys.argv", argv), contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            code = gc.main()
        self.stderr = stderr.getvalue()
        self.changelog = (self.root / "CHANGELOG.md").read_text(encoding="utf-8") if code == 0 else ""
        return code, (json.loads(out.read_text(encoding="utf-8")) if code == 0 else None)


class DisplayFilterTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.f = Fixture(tmp.name)
        # One untouched book on both sides, so every diff has a baseline.
        self.f.book("old", "Tanakh/Torah", "Genesis", "בראשית", 1)
        self.f.book("new", "Tanakh/Torah", "Genesis", "בראשית", 1)
        self.f.schema("Genesis", "בראשית", ["תנ\"ך", "תורה"])

    def ens(self, diff, key):
        return [b.get("en", b.get("new_en")) for b in diff["books"][key]]

    def test_hebrew_rename_out_of_the_blacklist_is_announced(self):
        self.f.book("old", "Musar", "Kav HaYashar", "קב הישר", 2)
        self.f.book("new", "Musar", "Kav HaYashar", "ספר קב הישר", 2)
        self.f.schema("Kav HaYashar", "ספר קב הישר", ["מוסר"])
        self.f.blacklist("books.txt", "קב הישר")
        code, diff = self.f.run()
        self.assertEqual(0, code)
        self.assertEqual([], diff["books"]["he_renamed"])
        [entry] = diff["books"]["previously_blocked"]
        self.assertEqual(("Kav HaYashar", "ספר קב הישר", "title", "קב הישר"),
                         (entry["en"], entry["he"], entry["reason"], entry["old_name"]))
        self.assertIn("No longer blacklisted", self.f.changelog)

    def test_english_rename_out_of_the_blacklist_is_announced(self):
        self.f.book("old", "Musar", "Kav Hayashar", "קב הישר", 3)
        self.f.book("new", "Musar", "Kav HaYashar HaShalem", "קב הישר השלם", 3)
        self.f.schema("Kav HaYashar HaShalem", "קב הישר השלם", ["מוסר"])
        self.f.blacklist("books.txt", "Kav Hayashar")
        code, diff = self.f.run()
        self.assertEqual(0, code)
        self.assertEqual([], diff["books"]["en_renamed"])
        self.assertEqual([], diff["books"]["added"])
        [entry] = diff["books"]["previously_blocked"]
        self.assertEqual(("Kav HaYashar HaShalem", "Kav Hayashar", "title", "Kav Hayashar"),
                         (entry["en"], entry["old_en"], entry["reason"], entry["old_name"]))

    def test_rename_into_the_blacklist_is_dropped_not_announced(self):
        self.f.book("old", "Musar", "Kav HaYashar", "קב הישר", 2)
        self.f.book("new", "Musar", "Kav HaYashar", "ספר קב הישר", 2)
        self.f.schema("Kav HaYashar", "ספר קב הישר", ["מוסר"])
        self.f.blacklist("books.txt", "ספר קב הישר")
        code, diff = self.f.run()
        self.assertEqual(0, code)
        self.assertEqual([], diff["books"]["he_renamed"])
        self.assertEqual([], diff["books"]["previously_blocked"])

    def test_rename_outside_the_blacklist_stays_a_rename(self):
        self.f.book("old", "Musar", "Kav HaYashar", "קב הישר", 2)
        self.f.book("new", "Musar", "Kav HaYashar", "ספר קב הישר", 2)
        self.f.schema("Kav HaYashar", "ספר קב הישר", ["מוסר"])
        self.f.blacklist("books.txt", "ספר אחר")
        code, diff = self.f.run()
        self.assertEqual(0, code)
        self.assertEqual(["Kav HaYashar"], self.ens(diff, "he_renamed"))
        self.assertEqual([], diff["books"]["previously_blocked"])

    def test_author_blacklist_matches_schema_names_and_authors_json_forms(self):
        self.f.book("new", "Musar", "Book A", "ספר א", 4)
        self.f.schema("Book A", "ספר א", ["מוסר"], [{"he": "פלוני אלמוני", "slug": "a"}])
        self.f.book("new", "Musar", "Book B", "ספר ב", 5)
        self.f.schema("Book B", "ספר ב", ["מוסר"], [{"he": "יוסף כהן", "slug": "b"}])
        self.f.book("new", "Musar", "Book C", "ספר ג", 6)
        self.f.schema("Book C", "ספר ג", ["מוסר"], [{"he": "שמעון לוי", "slug": "c"}])
        self.f.version("Musar", "Book A", "Some Edition", "Some Edition")
        self.f.authors_json([{"slug": "b", "titles": [{"lang": "he", "text": "הרב יוסף כהן"},
                                                      {"lang": "en", "text": "Yosef Cohen"}]}])
        self.f.blacklist("authors.txt", "פלוני אלמוני", "הרב יוסף כהן")
        code, diff = self.f.run()
        self.assertEqual(0, code)
        self.assertEqual(["Book C"], self.ens(diff, "added"))
        self.assertEqual([], diff["versions"]["added"])

    def test_path_entry_matches_the_flattened_hebrew_path(self):
        self.f.book("new", "Talmud/Bavli", "Chiddushim", "חידושים", 7)
        self.f.schema("Chiddushim", "חידושים", ["תלמוד", "בבלי", "ראשונים"])
        self.f.book("new", "Talmud/Bavli", "Other", "אחר", 8)
        self.f.schema("Other", "אחר", ["תלמוד", "בבלי", "ראשונים"])
        self.f.blacklist("books.txt", "\\תלמוד\\בבלי\\ראשונים\\חידושים")
        code, diff = self.f.run()
        self.assertEqual(0, code)
        self.assertEqual(["Other"], self.ens(diff, "added"))

    def test_hebrew_rename_out_of_a_path_entry_is_announced_with_the_old_path(self):
        self.f.book("old", "Musar", "Kav HaYashar", "קב הישר", 2)
        self.f.book("new", "Musar", "Kav HaYashar", "ספר קב הישר", 2)
        self.f.schema("Kav HaYashar", "ספר קב הישר", ["מוסר"])
        self.f.blacklist("books.txt", "מוסר/קב הישר")
        code, diff = self.f.run()
        self.assertEqual(0, code)
        [entry] = diff["books"]["previously_blocked"]
        self.assertEqual(("path", "מוסר/קב הישר"), (entry["reason"], entry["old_name"]))

    def test_a_move_with_path_entries_present_is_announced_as_unknown(self):
        self.f.book("old", "Musar", "Kav HaYashar", "קב הישר", 2)
        self.f.book("new", "Halakhah", "Kav HaYashar", "קב הישר", 2)
        self.f.schema("Kav HaYashar", "קב הישר", ["הלכה"])
        self.f.blacklist("books.txt", "מוסר/ספר אחר")
        code, diff = self.f.run()
        self.assertEqual(0, code)
        self.assertEqual([], diff["books"]["moved"])
        [entry] = diff["books"]["previously_blocked"]
        self.assertEqual(("path-unknown", None), (entry["reason"], entry["old_name"]))

    def test_removed_books_are_matched_by_title_only(self):
        self.f.book("old", "Musar", "Gone", "ספר שהוסר", 9)
        self.f.blacklist("books.txt", "ספר שהוסר")
        code, diff = self.f.run()
        self.assertEqual(0, code)
        self.assertEqual([], diff["books"]["removed"])

    def test_machine_run_is_unfiltered_and_keeps_its_schema(self):
        self.f.book("old", "Musar", "Kav HaYashar", "קב הישר", 2)
        self.f.book("new", "Musar", "Kav HaYashar", "ספר קב הישר", 2)
        self.f.schema("Kav HaYashar", "ספר קב הישר", ["מוסר"])
        self.f.blacklist("books.txt", "קב הישר")
        code, diff = self.f.run(filtered=False)
        self.assertEqual(0, code)
        self.assertEqual({"added", "removed", "he_renamed", "en_renamed", "moved", "content_changed"},
                         set(diff["books"]))
        self.assertEqual(["Kav HaYashar"], self.ens(diff, "he_renamed"))

    def test_author_or_path_entries_without_the_export_fail_loudly(self):
        self.f.blacklist("authors.txt", "פלוני אלמוני")
        code, _ = self.f.run(exports=False)
        self.assertEqual(1, code)
        self.assertIn("--exports-dir", self.f.stderr)

    def test_title_blacklist_alone_still_works_without_the_export(self):
        self.f.book("new", "Musar", "Kav HaYashar", "קב הישר", 2)
        self.f.blacklist("books.txt", "קב הישר")
        code, diff = self.f.run(exports=False)
        self.assertEqual(0, code)
        self.assertEqual([], diff["books"]["added"])


class NormalizationTest(unittest.TestCase):
    def test_title_keys_collapse_only_java_whitespace(self):
        self.assertEqual("a b c", gc.normalize_title_key(" A \t B_C "))
        self.assertEqual("a\u00a0b", gc.normalize_title_key("a\u00a0b"))

    def test_path_entries_follow_normalize_priority_entry(self):
        self.assertEqual("תלמוד בבלי/ראשונים/חי״ד",
                         gc.normalize_path_entry("/תלמוד/בבלי/ ראשונים //חי\"ד"))
        self.assertEqual("תלמוד ירושלמי/א", gc.book_path(["תלמוד", "ירושלמי"], "א"))


if __name__ == "__main__":
    unittest.main()
