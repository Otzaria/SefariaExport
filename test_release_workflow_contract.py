import re
import subprocess
import unittest
from pathlib import Path

# `uses:` as a YAML key, with the trailing text kept so the `# vX.Y.Z` comment
# that documents a SHA pin can be checked.  A `#` comment line cannot match,
# because the key has to be the first thing on the line.
USES_LINE = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<ref>\S+)(?P<trailer>.*)$")

# GitHub's own actions are trusted differently from everyone else's, and this
# repo pins them by major tag so they keep receiving security fixes.
FIRST_PARTY_OWNERS = frozenset({"actions", "github"})


class ReleaseWorkflowContractTest(unittest.TestCase):
    def test_link_visibility_contract_runs_in_release_validation(self):
        root = Path(__file__).resolve().parent
        for relative in (".github/workflows/release.yml", ".github/workflows/reconcile-downstream.yml"):
            workflow = (root / relative).read_text(encoding="utf-8")
            self.assertIn("test_link_visibility.py", workflow, relative)

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

    def test_every_third_party_action_in_every_workflow_is_pinned_to_a_commit(self):
        """`free-disk-space` was pinned by hand, and then two `docker/*` actions
        arrived on floating majors — in the one job that holds `contents: write`
        and a token that publishes immutable releases, and that no PR check ever
        exercises.  Pin the rule instead of the instance: every workflow, every
        third-party action, a full 40-character commit plus the tag it came
        from, and `actions/*` on a major tag."""
        root = Path(__file__).resolve().parent
        workflows = sorted((root / ".github/workflows").glob("*.y*ml"))
        self.assertTrue(workflows, "no workflows found to scan")
        third_party, first_party = [], []
        for path in workflows:
            lines = path.read_text(encoding="utf-8").splitlines()
            for number, line in enumerate(lines, 1):
                match = USES_LINE.match(line)
                if match is None:
                    continue
                ref = match.group("ref")
                where = f"{path.name}:{number}: {line.strip()}"
                # Actions living in this repository, and raw images, are not a
                # third-party supply chain.
                if ref.startswith(("./", ".\\", "docker://")):
                    continue
                self.assertIn("@", ref, where)
                action, _, version = ref.partition("@")
                if action.split("/", 1)[0] in FIRST_PARTY_OWNERS:
                    first_party.append(action)
                    # A version tag or a commit — anything but a branch, which
                    # is what `@main` / `@master` would be.
                    self.assertRegex(version, r"^(v\d+(\.\d+){0,2}|[0-9a-f]{40})$", where)
                    continue
                third_party.append(action)
                self.assertRegex(version, r"^[0-9a-f]{40}$", where)
                # A bare 40-character SHA is unreadable, so the convention
                # carries the tag it was resolved from next to it.
                self.assertRegex(match.group("trailer"), r"#\s*v\d+\.\d+\.\d+", where)
        # Every assertion above is inside the loop, so a regex that stopped
        # matching would make this test pass while checking nothing.
        self.assertGreaterEqual(len(third_party), 3, third_party)
        self.assertGreaterEqual(len(first_party), 4, first_party)

    def test_the_docker_actions_are_pinned_to_what_their_major_tags_resolved_to(self):
        """`docker/setup-buildx-action@v4` was v4.3.0 and
        `docker/build-push-action@v7` was v7.3.0 when these were pinned, so the
        pin changed nothing about what runs — it only stopped the tags moving
        underneath a job with `contents: write`."""
        root = Path(__file__).resolve().parent
        for relative in (".github/workflows/release.yml",
                         ".github/workflows/docker-build.yml"):
            workflow = (root / relative).read_text(encoding="utf-8")
            self.assertIn(
                "docker/setup-buildx-action@37fe631027851001ddb9b187196cc803df7f5f0e",
                workflow, relative)
            self.assertIn(
                "docker/build-push-action@53b7df96c91f9c12dcc8a07bcb9ccacbed38856a",
                workflow, relative)
            self.assertNotIn("docker/setup-buildx-action@v", workflow, relative)
            self.assertNotIn("docker/build-push-action@v", workflow, relative)

    def test_the_image_is_built_on_pull_requests_against_the_release_context(self):
        """release.yml is `workflow_dispatch` only, so a commit that changed the
        base OS, the interpreter provisioning and the package set reached a
        green PR with zero build coverage.  A PR build is worthless if it drifts
        from the build the weekly runs, so the two are pinned together."""
        root = Path(__file__).resolve().parent
        build = (root / ".github/workflows/docker-build.yml").read_text(encoding="utf-8")
        release = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
        # Every needle below is a whole line, newline to newline, at its real
        # indentation.  Unanchored, `assertIn` is satisfied by any superstring:
        # `file: Dockerfile.ci`, `timeout-minutes: 400` and `contents: read-all`
        # all contain the shorter needle, and a `#` comment can contain any of
        # them, so the drift these assertions exist to catch would pass.
        self.assertIn("\n  pull_request:\n", build)
        self.assertIn("\npermissions:\n  contents: read\n", build)
        self.assertIn("\nconcurrency:\n", build)
        self.assertIn("\n    timeout-minutes: 40\n", build)
        # Coverage, not delivery: nothing is published and nothing is loaded.
        self.assertIn("\n          push: false\n", build)
        self.assertIn("\n          load: false\n", build)
        # The same build inputs release.yml uses.  Neither file passes
        # `build-args` or selects a `target`, and if one ever starts, both have
        # to.
        for shared in ("\n          context: .\n", "\n          file: Dockerfile\n"):
            self.assertIn(shared, build, shared)
            self.assertIn(shared, release, shared)
        for absent in ("\n          build-args:", "\n          target:"):
            self.assertNotIn(absent, build, absent)
            self.assertNotIn(absent, release, absent)
        # Path-filtered to the Dockerfile's build inputs: the Dockerfile, what
        # the context excludes, and the `COPY *.sh *.py ./` list.
        for filtered in ("\n      - Dockerfile\n", "\n      - .dockerignore\n",
                         "\n      - '*.sh'\n", "\n      - '*.py'\n"):
            self.assertIn(filtered, build, filtered)

    def test_the_export_report_leaves_the_runner_when_the_run_fails(self):
        """The write-failure gate in run_exports.py returns non-zero before
        17_build_combined_archive.sh runs, and the archive is the only thing
        that normally carries export_report.json off the runner — so on the one
        run where the report matters most it used to be unreachable, while the
        gate's own message pointed at its path."""
        root = Path(__file__).resolve().parent
        workflow = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
        exporter = (root / "run_exports.py").read_text(encoding="utf-8")
        compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
        upload = "- name: Upload the export report when the run fails"
        start = workflow.index(upload)
        step = workflow[start:workflow.index("- name: Replay every mongod warning and error")]
        self.assertIn("if: failure()", step)
        self.assertIn("uses: actions/upload-artifact@", step)
        self.assertIn("if-no-files-found: ignore", step)
        self.assertIn("retention-days: 7", step)
        # The path is only correct because of these three facts together.
        self.assertIn("path: exports/metadata/export_report.json", step)
        self.assertIn('EXPORT_REPORT_DIRNAME = "metadata"', exporter)
        self.assertIn('EXPORT_REPORT_FILENAME = "export_report.json"', exporter)
        self.assertIn("./exports:/app/exports", compose)
        # It can only upload what the export step produced.
        self.assertLess(
            workflow.index("run: docker compose up --abort-on-container-exit"), start)

    def test_the_apt_remove_pass_stays_off(self):
        """`large-packages` freed 4.4 GiB the job never needs and cost 1,352 log
        lines, 882 of them `Package 'php-...' is not installed, so not removed`.
        Peak disk during run 33987734987 was 42G of 145G."""
        root = Path(__file__).resolve().parent
        workflow = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn("large-packages: false", workflow)
        self.assertNotIn("large-packages: true", workflow)

    def test_mongod_is_quiet_and_never_streamed_into_the_job_log(self):
        """6,294 mongod lines / 2.51 MB — 54% of the bytes of run 33987734987 —
        and 2,208 of them were emitted after the DB was dropped, during zstd."""
        root = Path(__file__).resolve().parent
        workflow = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
        compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn('"mongod", "--quiet"', compose)
        self.assertIn("docker compose up --abort-on-container-exit --no-attach mongodb",
                      workflow)

    def test_mongod_warnings_and_errors_are_replayed_before_teardown(self):
        """Cutting the stream must never cut the diagnostics with it."""
        root = Path(__file__).resolve().parent
        workflow = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
        replay = "- name: Replay every mongod warning and error"
        self.assertIn(replay, workflow)
        self.assertIn('grep -E \'"s":"(W|E|F)"\'', workflow)
        # It has to read the container logs while the container still exists.
        self.assertLess(workflow.index(replay),
                        workflow.index("- name: Stop and clean up containers"))

    def test_the_mongod_replay_step_cannot_redden_a_green_job(self):
        """`shell: bash` is `bash --noprofile --norc -eo pipefail`, so errexit and
        pipefail are ON.  A clean run has no W/E/F lines, `grep` then exits 1, and
        without these guards a diagnostics step fails the whole weekly."""
        root = Path(__file__).resolve().parent
        workflow = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
        start = workflow.index("- name: Replay every mongod warning and error")
        step = workflow[start:workflow.index("- name: Stop and clean up containers")]
        self.assertIn("set +e", step)
        self.assertIn("set +o pipefail", step)
        self.assertIn('grep -E \'"s":"(W|E|F)"\' || true', step)
        self.assertIn("exit 0", step)

    def test_export_image_is_built_once_against_a_reusable_layer_cache(self):
        root = Path(__file__).resolve().parent
        workflow = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
        compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("cache-from: type=gha", workflow)
        # The cached build and the compose run have to name the same image, or
        # `up` silently rebuilds everything the cached build just produced.
        self.assertIn("tags: sefaria-exporter:local", workflow)
        self.assertIn("image: sefaria-exporter:local", compose)
        # setup-buildx-action gives buildx the docker-container driver, which
        # keeps the result out of the local daemon unless it is loaded back —
        # without this line `up` rebuilds the image the cached build produced.
        self.assertIn("load: true", workflow)
        self.assertIn("run: docker compose up --abort-on-container-exit", workflow)
        self.assertNotIn("docker compose up --build", workflow)

    def test_initial_baseline_skips_only_the_downstream_dispatch(self):
        root = Path(__file__).resolve().parent
        workflow = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn('--allow-initial-baseline "$ALLOW_INITIAL"', workflow)
        self.assertIn('echo "is_initial=$is_initial" >> "$GITHUB_OUTPUT"', workflow)
        self.assertIn("if: steps.previous.outputs.is_initial == 'false'", workflow)
        self.assertEqual(2, workflow.count("if: steps.previous.outputs.is_initial == 'false'"))

    def test_distinct_exports_use_the_durable_pending_queue(self):
        root = Path(__file__).resolve().parent
        workflow = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn("group: sefaria-export-release", workflow)
        self.assertIn("queue: max", workflow)

    def test_published_release_carries_a_durable_downstream_intent(self):
        root = Path(__file__).resolve().parent
        workflow = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn("python3 downstream_intent.py build", workflow)
        self.assertIn('intent_assets+=("$INTENT_ASSET")', workflow)
        self.assertIn("python3 downstream_intent.py validate", workflow)
        self.assertIn(
            'python3 reconcile_downstream.py --source-repo "$SOURCE_REPO" --tag "$TAG"',
            workflow,
        )
        self.assertNotIn("for attempt in 1 2 3", workflow)

    def test_scheduled_reconciler_recovers_missing_root(self):
        root = Path(__file__).resolve().parent
        workflow = (root / ".github/workflows/reconcile-downstream.yml").read_text(encoding="utf-8")
        self.assertIn("schedule:", workflow)
        self.assertIn("python3 reconcile_downstream.py", workflow)
        self.assertIn("secrets.PIPELINE_TOKEN", workflow)

    def test_dead_letter_acknowledgements_are_gated_wherever_the_reconciler_runs(self):
        """The store is read on both call sites, and it fails closed, so the
        gate that proves it parses has to precede the reconciler in both."""
        root = Path(__file__).resolve().parent
        self.assertTrue((root / "acknowledged_dead_letters.json").is_file())
        for relative in (".github/workflows/release.yml", ".github/workflows/reconcile-downstream.yml"):
            workflow = (root / relative).read_text(encoding="utf-8")
            self.assertIn("test_dead_letter_acknowledgements.py", workflow, relative)
            self.assertLess(
                workflow.index("test_dead_letter_acknowledgements.py"),
                workflow.index("python3 reconcile_downstream.py"),
                relative,
            )

    def test_weekly_dispatch_has_an_exact_adoptable_identity(self):
        root = Path(__file__).resolve().parent
        workflow = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn("orchestration_id:", workflow)
        self.assertIn("Sefaria immutable export orchestration=${{ inputs.orchestration_id", workflow)
        self.assertIn("^weekly:[1-9][0-9]*:[1-9][0-9]*$", workflow)


if __name__ == "__main__":
    unittest.main()
