import argparse
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import downstream_intent
import release_contract
import test_release_contract


class DownstreamIntentTest(unittest.TestCase):
    def metadata_file(self, root: Path) -> Path:
        path = root / "release_metadata.json"
        path.write_bytes(
            release_contract.canonical_bytes(
                test_release_contract.ReleaseContractTest._metadata()
            )
        )
        return path

    def test_round_trip_is_canonical_and_content_addressed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata_path = self.metadata_file(root)
            args = argparse.Namespace(
                source_repo="Otzaria/SefariaExport",
                metadata=str(metadata_path),
                output_dir=str(root),
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                downstream_intent.build_command(args)
            intent_path = root / output.getvalue().strip()
            intent, metadata, metadata_sha = downstream_intent.load_and_validate(
                intent_path, metadata_path, "Otzaria/SefariaExport"
            )
            self.assertEqual(
                f"sefaria:123:1:{metadata['tag']}:{metadata_sha}",
                intent["correlation_id"],
            )
            self.assertEqual(
                f"downstream-intent-{metadata_sha}.json", intent_path.name
            )
            self.assertEqual(
                release_contract.canonical_bytes(intent), intent_path.read_bytes()
            )

    def test_identity_mutations_are_rejected(self):
        metadata = test_release_contract.ReleaseContractTest._metadata()
        digest = "a" * 64
        valid = downstream_intent.build_intent(
            "Otzaria/SefariaExport", metadata, digest
        )
        mutations = (
            ("schema_version", 2),
            ("schema_version", True),
            ("source_run_id", True),
            ("source_run_attempt", 1.0),
            ("source_tag", "different"),
            ("source_release_metadata_sha256", "b" * 64),
            ("target_repo", "attacker/repo"),
            ("target_workflow", "other.yml"),
            ("correlation_id", "wrong"),
        )
        for field, replacement in mutations:
            with self.subTest(field=field):
                candidate = dict(valid)
                candidate[field] = replacement
                with self.assertRaises(release_contract.ContractError):
                    downstream_intent.validate_intent(
                        candidate, metadata, digest, "Otzaria/SefariaExport"
                    )

    def test_duplicate_keys_and_noncanonical_bytes_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata_path = self.metadata_file(root)
            metadata_sha = release_contract.sha256_file(metadata_path)
            name = f"downstream-intent-{metadata_sha}.json"
            intent_path = root / name
            intent_path.write_text(
                '{"schema_version":1,"schema_version":1}', encoding="utf-8"
            )
            with self.assertRaises(release_contract.ContractError):
                downstream_intent.load_and_validate(
                    intent_path, metadata_path, "Otzaria/SefariaExport"
                )

            value = downstream_intent.build_intent(
                "Otzaria/SefariaExport",
                test_release_contract.ReleaseContractTest._metadata(),
                metadata_sha,
            )
            intent_path.write_text(
                __import__("json").dumps(value, indent=2), encoding="utf-8"
            )
            with self.assertRaises(release_contract.ContractError):
                downstream_intent.load_and_validate(
                    intent_path, metadata_path, "Otzaria/SefariaExport"
                )


if __name__ == "__main__":
    unittest.main()
