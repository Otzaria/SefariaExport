import hashlib
import json
import tempfile
import unittest
import argparse
import contextlib
import io
from pathlib import Path

import release_contract


class ReleaseContractTest(unittest.TestCase):
    def test_combined_descriptor_is_sorted_and_hashes_the_stream(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "b.part-01").write_bytes(b"second")
            (root / "a.part-00").write_bytes(b"first")
            result = release_contract.combined_descriptor([root / "b.part-01", root / "a.part-00"])
            self.assertEqual(["a.part-00", "b.part-01"], [part["name"] for part in result["parts"]])
            self.assertEqual(hashlib.sha256(b"firstsecond").hexdigest(), result["sha256"])
            self.assertEqual(11, result["size"])

    def test_metadata_rejects_wrong_chain(self):
        metadata = self._metadata()
        metadata["changelog"]["old_tag"] = "wrong"
        with self.assertRaises(release_contract.ContractError):
            release_contract.validate_metadata(metadata)

    def test_metadata_rejects_boolean_schema_version(self):
        metadata = self._metadata()
        metadata["schema_version"] = True
        with self.assertRaises(release_contract.ContractError):
            release_contract.validate_metadata(metadata)

    def test_canonical_bytes_are_stable_and_utf8(self):
        left = {"z": 1, "א": "עברית"}
        right = {"א": "עברית", "z": 1}
        self.assertEqual(release_contract.canonical_bytes(left), release_contract.canonical_bytes(right))
        self.assertNotIn(b"\\u", release_contract.canonical_bytes(left))

    def test_duplicate_json_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate.json"
            path.write_text('{"a":1,"a":2}', encoding="utf-8")
            with self.assertRaises(release_contract.ContractError):
                release_contract.read_json(path)

    def test_build_and_validate_complete_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "export.tar.zst"
            manifest = root / "manifest.txt"
            titles = root / "titles.json"
            changelog = root / "changelog_diff.json"
            archive.write_bytes(b"archive")
            manifest.write_text("0" * 64 + "  ./json/book/merged.json\n", encoding="utf-8")
            titles.write_text('{"Book":"ספר"}', encoding="utf-8")
            changelog.write_text('{"old_tag":"","new_tag":"2026-07-16_18-00-123-1"}', encoding="utf-8")
            output = root / "release_metadata.json"
            args = argparse.Namespace(
                archive_part=[str(archive)],
                manifest=str(manifest),
                titles=str(titles),
                changelog=str(changelog),
                tag="2026-07-16_18-00-123-1",
                run_id=123,
                run_attempt=1,
                source_commit="1" * 40,
                previous_tag="",
                previous_metadata_sha256="",
                output=str(output),
            )
            with contextlib.redirect_stdout(io.StringIO()):
                release_contract.build(args)
            metadata = release_contract.read_json(output)
            release_contract.validate_metadata(metadata, root)
            self.assertEqual(release_contract.canonical_bytes(metadata), output.read_bytes())

    @staticmethod
    def _metadata():
        digest = "0" * 64
        return {
            "schema_version": 1,
            "tag": "2026-07-16_18-00-123-1",
            "run_id": 123,
            "run_attempt": 1,
            "source_commit": "1" * 40,
            "previous": {"tag": "old", "metadata_sha256": digest},
            "archive": {
                "sha256": digest,
                "size": 0,
                "parts": [{"name": "export.tar.zst", "size": 0, "sha256": digest}],
            },
            "manifest": {"name": "manifest.txt", "size": 0, "sha256": digest},
            "titles": {"name": "titles.json", "size": 0, "sha256": digest},
            "changelog": {
                "name": "changelog_diff.json",
                "size": 0,
                "sha256": digest,
                "old_tag": "old",
                "new_tag": "2026-07-16_18-00-123-1",
            },
        }


if __name__ == "__main__":
    unittest.main()
