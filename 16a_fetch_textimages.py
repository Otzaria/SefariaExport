#!/usr/bin/env python3
"""Download every textimages.sefaria.org image the export references into exports/textimages/.

SeforimLibrary can embed these images directly from the export. Layout, URL
grammar and request encoding mirror its SefariaImageEmbedder:
`<sha256(url)>` holds the bytes, `<sha256(url)>.url` the URL that produced them.

Individual image failures are reported and left out. Every run starts with
an empty bundle so an earlier export cannot contribute stale files.
"""
import hashlib
import http.client
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image, ImageSequence

URL_MARKER = b"textimages.sefaria.org/"
URL_RE = re.compile(
    r"https://(?:storage\.googleapis\.com/)?textimages\.sefaria\.org/[^\"'<>\\\n\r\t]+"
)
TRAILING = " \\'\",.;)]}"
REQUEST_UNSAFE = set(' "<>\\^`{|}')
MAX_IMAGE_BYTES = 5 * 1024 * 1024
# A small compressed file can still be expensive to decode. Count all frames.
MAX_IMAGE_PIXELS = 25_000_000
RASTER_FORMATS = ("PNG", "JPEG", "GIF", "WEBP")
RASTER_TRAILERS = {"PNG": bytes.fromhex("0000000049454e44ae426082"), "GIF": b";"}
SVG_ROOTS = {"svg", "{http://www.w3.org/2000/svg}svg"}
BACKOFF_SECONDS = (2, 6)
PARALLELISM = 16
USER_AGENT = "SefariaExport-textimages/1.0"
# SHA-256 of the picture NetFree serves instead of an image; keep in step with
# SeforimLibrary's SefariaImageEmbedder.FILTER_PLACEHOLDER_SHA256.
FILTER_PLACEHOLDER_SHA256 = frozenset(
    {"1a00712621da6964deac5b491a5532e15c8854c00de7fba692818e0e042db311"}
)


class FinalError(Exception):
    """A definitive answer; retrying only delays the run."""


def collect_urls(json_dir: Path) -> list[str]:
    urls = set()
    for path in json_dir.rglob("*"):
        if path.suffix.lower() != ".json" or not path.is_file():
            continue
        data = path.read_bytes()
        if URL_MARKER not in data:
            continue
        for match in URL_RE.finditer(data.decode("utf-8", errors="replace")):
            urls.add(match.group(0).rstrip(TRAILING))
    return sorted(urls)


def cache_name(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def encode_for_request(url: str) -> str:
    out = []
    for byte in url.encode("utf-8"):
        ch = chr(byte)
        out.append(ch if 0x21 <= byte <= 0x7E and ch not in REQUEST_UNSAFE else "%%%02X" % byte)
    return "".join(out)


def rejection_reason(data: bytes) -> str | None:
    if not data:
        return "empty"
    if len(data) > MAX_IMAGE_BYTES:
        return f"oversized ({len(data)} bytes)"
    if hashlib.sha256(data).hexdigest() in FILTER_PLACEHOLDER_SHA256:
        return "content-filter-placeholder"
    head = data[:512].decode("utf-8-sig", errors="replace").lstrip().lower()
    if head.startswith("<!doctype html") or head.startswith("<html"):
        return "html-instead-of-image"
    if head.startswith("<") or data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            root = ET.fromstring(data)
        except (ET.ParseError, LookupError, ValueError):
            return "invalid-svg-or-non-image"
        return None if root.tag in SVG_ROOTS else "not-an-image"

    try:
        with Image.open(io.BytesIO(data), formats=RASTER_FORMATS) as image:
            if image.width * image.height > MAX_IMAGE_PIXELS:
                return "too-many-image-pixels"
            # The decoder can render PNG/GIF pixels even if the file ends
            # before the final PNG IEND chunk or GIF trailer.
            trailer = RASTER_TRAILERS.get(image.format)
            if trailer and not data.endswith(trailer):
                return "incomplete-image"
            # PNG's verify checks chunk integrity. Reopen to also check that
            # the pixels decode, since verify is format-specific.
            image.verify()
        with Image.open(io.BytesIO(data), formats=RASTER_FORMATS) as image:
            pixels = 0
            for frame in ImageSequence.Iterator(image):
                pixels += frame.width * frame.height
                if pixels > MAX_IMAGE_PIXELS:
                    return "too-many-image-pixels"
                frame.load()
    except Image.DecompressionBombError:
        return "too-many-image-pixels"
    except (OSError, SyntaxError, ValueError, EOFError):
        return "invalid-or-unsupported-image"
    return None


def http_get(url: str) -> bytes:
    request = urllib.request.Request(
        encode_for_request(url), headers={"User-Agent": USER_AGENT, "Accept": "image/*"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status != 200:
                raise FinalError(f"unexpected http {response.status}")
            # http.client tracks the expected size only when Content-Length
            # governs framing. Its bounded read may return early without raising.
            expected = response.length
            if expected is not None and expected > MAX_IMAGE_BYTES:
                raise FinalError(f"oversized ({expected} bytes)")
            data = response.read(MAX_IMAGE_BYTES + 1)
            if expected is not None and len(data) != expected:
                raise OSError(f"incomplete response ({len(data)}/{expected} bytes)")
            return data
    except urllib.error.HTTPError as error:
        error.close()
        if error.code in (408, 429) or error.code >= 500:
            raise OSError(f"http {error.code}") from error
        raise FinalError(f"http {error.code}") from error
    except http.client.HTTPException as error:  # e.g. IncompleteRead: the connection dropped mid-body
        raise OSError(str(error)) from error


def download(url: str, fetch=http_get, sleep=time.sleep) -> bytes:
    for attempt in range(len(BACKOFF_SECONDS) + 1):
        try:
            return fetch(url)
        except OSError:
            if attempt == len(BACKOFF_SECONDS):
                raise
            sleep(BACKOFF_SECONDS[attempt])
    raise AssertionError("unreachable")


def atomic_write(directory: Path, target: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".part")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def fetch_one(url: str, out_dir: Path, fetch, sleep) -> str | None:
    """Returns None on success, else the reason the image is left out."""
    name = cache_name(url)
    try:
        data = download(url, fetch=fetch, sleep=sleep)
    except FinalError as error:
        return str(error)
    except (OSError, ValueError) as error:
        return f"{type(error).__name__}: {error}"
    reason = rejection_reason(data)
    if reason:
        return reason
    try:
        atomic_write(out_dir, out_dir / (name + ".url"), url.encode("utf-8"))
        atomic_write(out_dir, out_dir / name, data)
    except OSError as error:
        return f"write failed: {error}"
    return None


def run(exports_dir: Path, fetch=http_get, sleep=time.sleep) -> dict:
    out_dir = exports_dir / "textimages"
    # Clear before scanning, including when the scan fails. The packager tars
    # the entire tree, so an inability to clear stale files must stop it.
    try:
        shutil.rmtree(out_dir)
    except FileNotFoundError:
        pass
    out_dir.mkdir(parents=True)

    json_dir = exports_dir / "json"
    try:
        if not json_dir.is_dir():
            raise FileNotFoundError(f"missing {json_dir}")
        urls = collect_urls(json_dir)
    except OSError as error:
        return {"attempted": 0, "stored": 0, "failures": [], "scan_error": str(error)}

    with ThreadPoolExecutor(max_workers=PARALLELISM) as pool:
        results = pool.map(lambda url: fetch_one(url, out_dir, fetch, sleep), urls)
        failures = [{"url": url, "reason": reason} for url, reason in zip(urls, results) if reason]
    return {"attempted": len(urls), "stored": len(urls) - len(failures), "failures": failures}


def main() -> int:
    workspace = Path(os.environ.get("GITHUB_WORKSPACE") or Path.cwd())
    exports = Path(os.environ.get("SEFARIA_EXPORT_PATH") or workspace / "exports")
    report_path = exports / "metadata" / "textimages_report.json"
    report_path.unlink(missing_ok=True)
    report = run(exports)
    try:
        report_path.parent.mkdir(exist_ok=True)
        atomic_write(report_path.parent, report_path, json.dumps(report, indent=2, sort_keys=True).encode("utf-8"))
    except OSError as error:
        print(f"::warning::could not write textimages report: {error}")
    if "scan_error" in report:
        print(f"::warning::could not scan textimages: {report['scan_error']}")
    print(f"textimages: attempted={report['attempted']} stored={report['stored']} failed={len(report['failures'])}")
    for failure in report["failures"]:
        print(f"::warning::textimage not bundled ({failure['reason']}): {failure['url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
