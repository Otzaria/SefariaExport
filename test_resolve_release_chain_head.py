import hashlib
import tempfile
import unittest
from pathlib import Path

from resolve_release_chain_head import (
    ChainHeadError,
    PublishedMetadata,
    find_chain_head,
    resolution_result,
    validate_initial_permission,
    verify_release_asset_contract,
)


class ResolveReleaseChainHeadTest(unittest.TestCase):
    def node(self, tag, digest, previous=None):
        return PublishedMetadata(tag, digest * 64, previous)

    def test_legacy_releases_do_not_hide_the_unique_metadata_head(self):
        base = self.node("base", "a")
        head = self.node("head", "b", base.identity)
        self.assertEqual(head, find_chain_head([head, base]))

    def test_no_metadata_anywhere_is_the_only_empty_chain(self):
        self.assertIsNone(find_chain_head([]))
        self.assertEqual(
            {
                "schema_version": 1,
                "has_immutable_chain": False,
                "is_initial": True,
                "tag": "",
                "metadata_sha256": "",
            },
            resolution_result(None),
        )

    def test_initial_flag_is_allowed_exactly_once(self):
        initial = resolution_result(None)
        validate_initial_permission(initial, True)
        with self.assertRaises(ChainHeadError):
            validate_initial_permission(initial, False)

        head = self.node("head", "a")
        established = resolution_result(head)
        self.assertFalse(established["is_initial"])
        validate_initial_permission(established, False)
        with self.assertRaises(ChainHeadError):
            validate_initial_permission(established, True)

    def test_fork_is_rejected(self):
        base = self.node("base", "a")
        one = self.node("one", "b", base.identity)
        two = self.node("two", "c", base.identity)
        with self.assertRaises(ChainHeadError):
            find_chain_head([base, one, two])

    def test_missing_previous_metadata_is_rejected(self):
        child = self.node("child", "b", ("missing", "c" * 64))
        with self.assertRaises(ChainHeadError):
            find_chain_head([child])

    def release_fixture(self, root: Path):
        metadata_path = root / "release_metadata.json"
        metadata_path.write_bytes(b"{}")
        descriptor = lambda name, payload: {
            "name": name,
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        metadata = {
            "archive": {"parts": [descriptor("export.tar.zst", b"archive")]},
            "manifest": descriptor("manifest.txt", b"manifest"),
            "titles": descriptor("titles.json", b"titles"),
            "changelog": {**descriptor("changelog_diff.json", b"change"), "old_tag": "", "new_tag": "tag"},
        }
        descriptors = [
            {"name": "release_metadata.json", "size": 2, "sha256": hashlib.sha256(b"{}").hexdigest()},
            metadata["archive"]["parts"][0],
            metadata["manifest"],
            metadata["titles"],
            {key: metadata["changelog"][key] for key in ("name", "size", "sha256")},
        ]
        api_assets = [
            {"name": item["name"], "size": item["size"], "digest": "sha256:" + item["sha256"]}
            for item in descriptors
        ]
        return metadata_path, metadata, api_assets

    def test_complete_release_assets_are_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            path, metadata, assets = self.release_fixture(Path(directory))
            verify_release_asset_contract(metadata, path, assets)

    def test_partial_release_missing_mandatory_titles_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path, metadata, assets = self.release_fixture(Path(directory))
            assets = [asset for asset in assets if asset["name"] != "titles.json"]
            with self.assertRaises(ChainHeadError):
                verify_release_asset_contract(metadata, path, assets)

    def test_api_digest_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path, metadata, assets = self.release_fixture(Path(directory))
            assets[1]["digest"] = "sha256:" + "0" * 64
            with self.assertRaises(ChainHeadError):
                verify_release_asset_contract(metadata, path, assets)


if __name__ == "__main__":
    unittest.main()
