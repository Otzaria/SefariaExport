import subprocess
import unittest
from pathlib import Path


class ReleaseWorkflowContractTest(unittest.TestCase):
    def test_legacy_release_mutators_fail_closed(self):
        root = Path(__file__).resolve().parent
        for name in ("20_create_or_update_release.sh", "21_upload_release_assets.sh"):
            result = subprocess.run(
                ["bash", str(root / name)], text=True, capture_output=True, check=False
            )
            self.assertNotEqual(0, result.returncode)
            self.assertIn(".github/workflows/release.yml", result.stderr)

    def test_display_assets_are_compared_after_remote_download(self):
        root = Path(__file__).resolve().parent
        workflow = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn("for display_asset in CHANGELOG.md forum_changelog_diff.json", workflow)
        self.assertIn('cmp "$display_asset" "release-verification/$display_asset"', workflow)

    def test_free_disk_space_action_is_pinned_to_a_full_commit(self):
        root = Path(__file__).resolve().parent
        workflow = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn(
            "jlumbroso/free-disk-space@54081f138730dfa15788a46383842cd2f914a1be",
            workflow,
        )
        self.assertNotIn("jlumbroso/free-disk-space@main", workflow)

    def test_initial_baseline_skips_only_the_downstream_dispatch(self):
        root = Path(__file__).resolve().parent
        workflow = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn('--allow-initial-baseline "$ALLOW_INITIAL"', workflow)
        self.assertIn('echo "is_initial=$is_initial" >> "$GITHUB_OUTPUT"', workflow)
        self.assertIn("if: steps.previous.outputs.is_initial == 'false'", workflow)
        self.assertEqual(1, workflow.count("if: steps.previous.outputs.is_initial == 'false'"))


if __name__ == "__main__":
    unittest.main()
