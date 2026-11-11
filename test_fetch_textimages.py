import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("fetch_textimages", ROOT / "16a_fetch_textimages.py")
fetch_textimages = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fetch_textimages)


def raster_bytes(format: str, color: str = "white") -> bytes:
    output = io.BytesIO()
    with Image.new("RGB", (2, 2), color) as image:
        image.save(output, format=format)
    return output.getvalue()


PNG = raster_bytes("PNG")


def book_fixture(root: Path, *urls: str, filename: str = "merged.json") -> Path:
    book = root / "json" / "Talmud" / "Sukkah" / filename
    book.parent.mkdir(parents=True, exist_ok=True)
    text = [f'<img src="{url}">' for url in urls]
    book.write_text(json.dumps({"text": text}, ensure_ascii=False), encoding="utf-8")
    return book


class UrlScanTest(unittest.TestCase):
    def test_scans_both_hosts_and_deduplicates_across_versions(self):
        first = "https://textimages.sefaria.org/a/b.png"
        second = "https://storage.googleapis.com/textimages.sefaria.org/c d.png"
        with tempfile.TemporaryDirectory() as tmp:
            exports = Path(tmp)
            book_fixture(exports, first, second)
            book_fixture(exports, second, filename="V.JSON")
            (exports / "json" / "directory.json").mkdir()
            self.assertEqual(fetch_textimages.collect_urls(exports / "json"), [second, first])

    def test_cache_name_and_request_encoding_match_the_embedder(self):
        url = "https://textimages.sefaria.org/a b/ש%20a.png"
        self.assertEqual(fetch_textimages.cache_name(url), hashlib.sha256(url.encode()).hexdigest())
        self.assertEqual(
            fetch_textimages.encode_for_request(url),
            "https://textimages.sefaria.org/a%20b/%D7%A9%20a.png",
        )


class BundleTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.exports = Path(tmp.name)

    def test_stores_bytes_and_sidecar_under_the_hash_of_the_url(self):
        url = "https://textimages.sefaria.org/ok.png"
        book_fixture(self.exports, url)
        report = fetch_textimages.run(self.exports, fetch=lambda _url: PNG)
        path = self.exports / "textimages" / fetch_textimages.cache_name(url)
        self.assertEqual(path.read_bytes(), PNG)
        self.assertEqual(path.with_suffix(".url").read_bytes(), url.encode())
        self.assertEqual(report, {"attempted": 1, "stored": 1, "failures": []})

    def test_reports_placeholder_html_and_definitive_http_errors(self):
        placeholder_url = "https://textimages.sefaria.org/p.png"
        html_url = "https://textimages.sefaria.org/h.png"
        missing_url = "https://textimages.sefaria.org/m.png"
        book_fixture(self.exports, placeholder_url, html_url, missing_url)
        placeholder = raster_bytes("PNG", "gray")

        def fetch(url):
            if url == placeholder_url:
                return placeholder
            if url == html_url:
                return b"\xef\xbb\xbf<!DOCTYPE html><html>blocked</html>"
            raise fetch_textimages.FinalError("http 404")

        with patch.object(fetch_textimages, "FILTER_PLACEHOLDER_SHA256", {hashlib.sha256(placeholder).hexdigest()}):
            report = fetch_textimages.run(self.exports, fetch=fetch)
        self.assertEqual(report["stored"], 0)
        self.assertEqual(
            {failure["url"]: failure["reason"] for failure in report["failures"]},
            {placeholder_url: "content-filter-placeholder", html_url: "html-instead-of-image", missing_url: "http 404"},
        )
        self.assertEqual(list((self.exports / "textimages").iterdir()), [])

    def test_second_run_replaces_the_previous_bundle(self):
        gone = "https://textimages.sefaria.org/gone.png"
        failed = "https://textimages.sefaria.org/failed.png"
        book_fixture(self.exports, gone, failed)
        self.assertEqual(fetch_textimages.run(self.exports, fetch=lambda _url: PNG)["stored"], 2)
        book_fixture(self.exports, failed)

        def fetch(_url):
            raise fetch_textimages.FinalError("http 404")

        report = fetch_textimages.run(self.exports, fetch=fetch)
        self.assertEqual(report["stored"], 0)
        self.assertEqual(report["failures"], [{"url": failed, "reason": "http 404"}])
        self.assertEqual(list((self.exports / "textimages").iterdir()), [])

    def test_missing_json_dir_reports_failure_and_clears_previous_bundle(self):
        bundle = self.exports / "textimages"
        bundle.mkdir()
        (bundle / "old-image").write_bytes(PNG)
        with patch.dict(os.environ, {"SEFARIA_EXPORT_PATH": str(self.exports)}):
            self.assertEqual(fetch_textimages.main(), 0)
        self.assertEqual(list(bundle.iterdir()), [])
        report = json.loads((self.exports / "metadata" / "textimages_report.json").read_text(encoding="utf-8"))
        self.assertIn("scan_error", report)

    def test_write_failure_is_reported(self):
        url = "https://textimages.sefaria.org/w.png"
        book_fixture(self.exports, url)
        with patch.object(fetch_textimages, "atomic_write", side_effect=OSError("disk full")):
            report = fetch_textimages.run(self.exports, fetch=lambda _url: PNG)
        self.assertEqual(report["failures"], [{"url": url, "reason": "write failed: disk full"}])


class ImageValidationTest(unittest.TestCase):
    def test_decodes_supported_rasters_and_accepts_svg(self):
        for format in ("PNG", "JPEG", "GIF", "WEBP"):
            with self.subTest(format=format):
                self.assertIsNone(fetch_textimages.rejection_reason(raster_bytes(format)))
        svg = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0"/></svg>'
        self.assertIsNone(fetch_textimages.rejection_reason(svg))

    def test_rejects_incomplete_images_and_non_image_responses(self):
        bodies = (
            PNG[:-1], raster_bytes("GIF")[:-1], raster_bytes("JPEG")[:-2], b"<svg><path>", b'{"error":"backend down"}',
            b"plain text", b"<?xml version='1.0'?><html/>",
        )
        for body in bodies:
            with self.subTest(body=body[:32]):
                self.assertIsNotNone(fetch_textimages.rejection_reason(body))

    def test_bounds_total_decoded_pixels_for_animations(self):
        output = io.BytesIO()
        with Image.new("RGB", (2, 2), "black") as first, Image.new("RGB", (2, 2), "white") as second:
            first.save(output, format="GIF", save_all=True, append_images=[second])
        with patch.object(fetch_textimages, "MAX_IMAGE_PIXELS", 7):
            self.assertEqual(fetch_textimages.rejection_reason(output.getvalue()), "too-many-image-pixels")


class HttpDownloadTest(unittest.TestCase):
    def start_server(self, responses):
        calls = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                status, headers, body = responses[min(len(calls), len(responses) - 1)]
                calls.append(self.path)
                self.send_response(status)
                for header, value in headers.items():
                    self.send_header(header, str(value))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        self.enterContext(patch.dict(os.environ, {"no_proxy": "127.0.0.1"}))
        return f"http://127.0.0.1:{server.server_port}/diagram.png", calls

    def test_short_response_is_retried_against_real_http_server(self):
        headers = {"Content-Type": "image/png", "Content-Length": len(PNG)}
        url, calls = self.start_server([(200, headers, PNG[:17]), (200, headers, PNG)])
        backoffs = []
        self.assertEqual(fetch_textimages.download(url, sleep=backoffs.append), PNG)
        self.assertEqual(len(calls), 2)
        self.assertEqual(backoffs, [2])

    def test_retries_transient_status_but_not_definitive_status(self):
        headers = {"Content-Length": len(PNG)}
        url, calls = self.start_server([(503, {}, b""), (429, {}, b""), (200, headers, PNG)])
        backoffs = []
        self.assertEqual(fetch_textimages.download(url, sleep=backoffs.append), PNG)
        self.assertEqual(len(calls), 3)
        self.assertEqual(backoffs, [2, 6])
        url, calls = self.start_server([(404, {}, b"")])
        with self.assertRaisesRegex(fetch_textimages.FinalError, "http 404"):
            fetch_textimages.download(url, sleep=backoffs.append)
        self.assertEqual(len(calls), 1)

    def test_oversized_and_partial_content_responses_are_left_out(self):
        limit = len(PNG)
        cases = (
            (200, {"Content-Length": limit + 1}, "oversized"),
            (200, {}, "oversized"),
            (206, {"Content-Length": len(PNG)}, "unexpected http 206"),
        )
        for status, headers, expected in cases:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                url, calls = self.start_server([(status, headers, PNG + b"x")])
                out = Path(tmp)
                with patch.object(fetch_textimages, "MAX_IMAGE_BYTES", limit):
                    reason = fetch_textimages.fetch_one(url, out, fetch_textimages.http_get, sleep=lambda _: None)
                self.assertIn(expected, reason)
                self.assertEqual(len(calls), 1)
                self.assertEqual(list(out.iterdir()), [])


class PipelineTest(unittest.TestCase):
    def test_images_are_fetched_after_the_manifest_and_before_the_archive(self):
        entrypoint = (ROOT / "entrypoint.sh").read_text(encoding="utf-8")
        self.assertLess(entrypoint.index("./15a_generate_manifest.sh"), entrypoint.index("./16a_fetch_textimages.sh"))
        self.assertLess(entrypoint.index("./16a_fetch_textimages.sh"), entrypoint.index("./17_build_combined_archive.sh"))

    @unittest.skipUnless(shutil.which("bash") and shutil.which("sha256sum"), "manifest tools unavailable")
    def test_manifest_excludes_previous_bundle_and_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exports = root / "exports"
            book_fixture(exports)
            bundle = exports / "textimages"
            bundle.mkdir()
            (bundle / "old-image").write_bytes(PNG)
            metadata = exports / "metadata"
            metadata.mkdir()
            (metadata / "textimages_report.json").write_text("{}", encoding="utf-8")
            (metadata / "export_report.json").write_text("{}", encoding="utf-8")
            env = {**os.environ, "SEFARIA_EXPORT_PATH": str(exports), "GITHUB_WORKSPACE": str(root)}
            result = subprocess.run(
                ["bash", str(ROOT / "15a_generate_manifest.sh")], env=env, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = (root / "output" / "manifest.txt").read_text(encoding="utf-8").splitlines()
            self.assertEqual(
                [line.split("  ", 1)[1] for line in lines],
                ["./json/Talmud/Sukkah/merged.json", "./metadata/export_report.json"],
            )


if __name__ == "__main__":
    unittest.main()
