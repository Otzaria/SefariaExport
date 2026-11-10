#!/usr/bin/env python3
"""Download every textimages.sefaria.org image the export references into exports/textimages/.

The consumer (SeforimLibrary's SefariaImageEmbedder) reads this directory
before it touches the network, so its build host never has to reach Sefaria's
image CDN. Layout, URL grammar and request encoding mirror that embedder:
`<sha256(url)>` holds the bytes, `<sha256(url)>.url` the URL that produced them.

Best effort: a failed image is reported and left out (the consumer fetches it
itself); it never fails the export.
"""
import hashlib
import http.client
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

URL_MARKER = b"textimages.sefaria.org/"
URL_RE = re.compile(
    r"https://(?:storage\.googleapis\.com/)?textimages\.sefaria\.org/[^\"'<>\\\n\r\t]+"
)
TRAILING = " \\'\",.;)]}"
REQUEST_UNSAFE = set(' "<>\\^`{|}')
MAX_IMAGE_BYTES = 5 * 1024 * 1024
ATTEMPTS = 3
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


def collect_urls(json_dir: Path) -> list:
    urls = set()
    for path in sorted(p for p in json_dir.rglob("*") if p.suffix.lower() == ".json"):
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


def rejection_reason(data: bytes):
    if len(data) == 0:
        return "empty"
    if len(data) > MAX_IMAGE_BYTES:
        return "oversized (%d bytes)" % len(data)
    head = data[:512].decode("utf-8", errors="replace").lstrip().lower()
    if head.startswith("<!doctype html") or head.startswith("<html"):
        return "html-instead-of-image"
    if hashlib.sha256(data).hexdigest() in FILTER_PLACEHOLDER_SHA256:
        return "content-filter-placeholder"
    return None


def http_get(url: str) -> bytes:
    request = urllib.request.Request(
        encode_for_request(url), headers={"User-Agent": USER_AGENT, "Accept": "image/*"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read(MAX_IMAGE_BYTES + 1)
    except urllib.error.HTTPError as error:
        retriable = error.code in (408, 429) or error.code >= 500
        if retriable:
            raise OSError("http %d" % error.code)
        raise FinalError("http %d" % error.code)
    except http.client.HTTPException as error:  # e.g. IncompleteRead: the connection dropped mid-body
        raise OSError(str(error))


def download(url: str, fetch=http_get, sleep=time.sleep) -> bytes:
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return fetch(url)
        except FinalError:
            raise
        except OSError:
            if attempt == ATTEMPTS:
                raise
            sleep(BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)])
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


def fetch_one(url: str, out_dir: Path, fetch, sleep):
    """Returns None on success, else the reason the image is left out."""
    name = cache_name(url)
    try:
        data = download(url, fetch=fetch, sleep=sleep)
    except FinalError as error:
        return str(error)
    except Exception as error:  # timeouts, resets, exhausted retries
        return "%s: %s" % (type(error).__name__, error)
    reason = rejection_reason(data)
    if reason:
        return reason
    try:
        atomic_write(out_dir, out_dir / (name + ".url"), url.encode("utf-8"))
        atomic_write(out_dir, out_dir / name, data)
    except OSError as error:
        return "write failed: %s" % error
    return None


def run(exports_dir: Path, fetch=http_get, sleep=time.sleep) -> dict:
    json_dir = exports_dir / "json"
    if not json_dir.is_dir():
        raise FileNotFoundError("missing %s" % json_dir)
    urls = collect_urls(json_dir)
    out_dir = exports_dir / "textimages"
    out_dir.mkdir(exist_ok=True)
    with ThreadPoolExecutor(max_workers=PARALLELISM) as pool:
        results = list(pool.map(lambda u: (u, fetch_one(u, out_dir, fetch, sleep)), urls))
    failures = [{"url": u, "reason": r} for u, r in results if r]
    return {"attempted": len(urls), "stored": len(urls) - len(failures), "failures": failures}


def main() -> int:
    exports = Path(os.environ.get("SEFARIA_EXPORT_PATH") or Path.cwd() / "exports")
    try:
        report = run(exports)
        out_dir = Path(os.environ.get("GITHUB_WORKSPACE") or Path.cwd()) / "output"
        out_dir.mkdir(exist_ok=True)
        (out_dir / "textimages_report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
        )
    except Exception as error:  # best effort: the consumer downloads whatever is missing
        print("::warning::textimages step skipped: %s: %s" % (type(error).__name__, error))
        return 0
    print("textimages: attempted=%d stored=%d failed=%d"
          % (report["attempted"], report["stored"], len(report["failures"])))
    for failure in report["failures"]:
        print("::warning::textimage not bundled (%s): %s" % (failure["reason"], failure["url"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
