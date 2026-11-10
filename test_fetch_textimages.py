import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("fetch_textimages", ROOT / "16a_fetch_textimages.py")
fetch_textimages = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fetch_textimages)

PNG = b"\x89PNG\r\n\x1a\nimage"
PLACEHOLDER = b"\x89PNG netfree placeholder"


def write_export(root: Path, *bodies: str) -> Path:
    exports = root / "exports"
    book = exports / "json" / "Talmud" / "Sukkah"
    book.mkdir(parents=True)
    for index, body in enumerate(bodies):
        (book / ("v%d.json" % index)).write_text(body, encoding="utf-8")
    return exports


class UrlScanTest(unittest.TestCase):
    def test_scans_both_hosts_and_trims_json_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            exports = write_export(
                Path(tmp),
                '{"t": "<img src=\\"https://textimages.sefaria.org/a/b.png\\"> x"}',
                '{"t": "<img src=\'https://storage.googleapis.com/textimages.sefaria.org/c d.png\'>."}',
            )
            self.assertEqual(
                fetch_textimages.collect_urls(exports / "json"),
                [
                    "https://storage.googleapis.com/textimages.sefaria.org/c d.png",
                    "https://textimages.sefaria.org/a/b.png",
                ],
            )

    def test_cache_name_and_request_encoding_match_the_embedder(self):
        url = "https://textimages.sefaria.org/a b/ש.png"
        self.assertEqual(fetch_textimages.cache_name(url), hashlib.sha256(url.encode()).hexdigest())
        self.assertEqual(
            fetch_textimages.encode_for_request(url),
            "https://textimages.sefaria.org/a%20b/%D7%A9.png",
        )


class RunTest(unittest.TestCase):
    def run_with(self, bodies, fetch):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        exports = write_export(Path(tmp.name), *bodies)
        report = fetch_textimages.run(exports, fetch=fetch, sleep=lambda _s: None)
        return exports, report

    def test_stores_bytes_and_sidecar_under_the_hash_of_the_url(self):
        url = "https://textimages.sefaria.org/ok.png"
        exports, report = self.run_with(['{"t": "\\"%s\\""}' % url], lambda _u: PNG)
        name = fetch_textimages.cache_name(url)
        self.assertEqual((exports / "textimages" / name).read_bytes(), PNG)
        self.assertEqual((exports / "textimages" / (name + ".url")).read_text(), url)
        self.assertEqual((report["attempted"], report["stored"], report["failures"]), (1, 1, []))

    def test_placeholder_html_and_final_errors_are_left_out_and_reported(self):
        urls = {
            "https://textimages.sefaria.org/p.png": PLACEHOLDER,
            "https://textimages.sefaria.org/h.png": b"<!DOCTYPE html><html>blocked</html>",
        }
        original = fetch_textimages.FILTER_PLACEHOLDER_SHA256
        fetch_textimages.FILTER_PLACEHOLDER_SHA256 = frozenset({hashlib.sha256(PLACEHOLDER).hexdigest()})
        self.addCleanup(lambda: setattr(fetch_textimages, "FILTER_PLACEHOLDER_SHA256", original))

        def fetch(url):
            if url.endswith("/gone.png"):
                raise fetch_textimages.FinalError("http 404")
            return urls[url]

        body = '{"t": "%s"}' % " ".join('\\"%s\\"' % u for u in [*urls, "https://textimages.sefaria.org/gone.png"])
        exports, report = self.run_with([body], fetch)
        self.assertEqual(report["stored"], 0)
        self.assertEqual(
            sorted(f["reason"] for f in report["failures"]),
            ["content-filter-placeholder", "html-instead-of-image", "http 404"],
        )
        self.assertEqual(list((exports / "textimages").iterdir()), [])

    def test_transient_failures_are_retried_then_succeed(self):
        calls = []

        def fetch(url):
            calls.append(url)
            if len(calls) < 3:
                raise OSError("timed out")
            return PNG

        _exports, report = self.run_with(['{"t": "\\"https://textimages.sefaria.org/r.png\\""}'], fetch)
        self.assertEqual((len(calls), report["stored"]), (3, 1))

    def test_real_placeholder_hash_is_the_known_netfree_picture(self):
        self.assertIn(
            "1a00712621da6964deac5b491a5532e15c8854c00de7fba692818e0e042db311",
            fetch_textimages.FILTER_PLACEHOLDER_SHA256,
        )


class BestEffortTest(unittest.TestCase):
    def test_write_failure_is_reported_not_raised(self):
        original = fetch_textimages.atomic_write
        fetch_textimages.atomic_write = lambda *_a: (_ for _ in ()).throw(OSError("disk full"))
        self.addCleanup(lambda: setattr(fetch_textimages, "atomic_write", original))
        with tempfile.TemporaryDirectory() as tmp:
            exports = write_export(Path(tmp), '{"t": "\\"https://textimages.sefaria.org/w.png\\""}')
            report = fetch_textimages.run(exports, fetch=lambda _u: PNG, sleep=lambda _s: None)
        self.assertEqual(report["failures"][0]["reason"], "write failed: disk full")

    def test_dropped_connection_mid_body_is_retried(self):
        import http.client
        calls = []

        def fetch(_url):
            calls.append(1)
            if len(calls) < 2:
                raise OSError("IncompleteRead")
            return PNG

        with tempfile.TemporaryDirectory() as tmp:
            exports = write_export(Path(tmp), '{"t": "\\"https://textimages.sefaria.org/i.png\\""}')
            report = fetch_textimages.run(exports, fetch=fetch, sleep=lambda _s: None)
        self.assertEqual((len(calls), report["stored"]), (2, 1))
        self.assertTrue(issubclass(http.client.IncompleteRead, http.client.HTTPException))

    def test_missing_json_dir_does_not_fail_the_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("SEFARIA_EXPORT_PATH")
            os.environ["SEFARIA_EXPORT_PATH"] = tmp
            self.addCleanup(lambda: os.environ.pop("SEFARIA_EXPORT_PATH") if old is None
                            else os.environ.__setitem__("SEFARIA_EXPORT_PATH", old))
            self.assertEqual(fetch_textimages.main(), 0)

    def test_uppercase_json_extension_is_scanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            exports = write_export(Path(tmp))
            (exports / "json" / "Talmud" / "Sukkah" / "V.JSON").write_text(
                '{"t": "\\"https://textimages.sefaria.org/u.png\\""}', encoding="utf-8")
            self.assertEqual(fetch_textimages.collect_urls(exports / "json"),
                             ["https://textimages.sefaria.org/u.png"])


class PipelineOrderTest(unittest.TestCase):
    def test_images_are_fetched_after_the_manifest_and_before_the_archive(self):
        steps = [l.strip() for l in (ROOT / "entrypoint.sh").read_text().splitlines() if l.startswith("./")]
        fetch, manifest, archive = (steps.index(s) for s in
                                    ("./16a_fetch_textimages.sh", "./15a_generate_manifest.sh", "./17_build_combined_archive.sh"))
        self.assertTrue(manifest < fetch < archive)


if __name__ == "__main__":
    unittest.main()
