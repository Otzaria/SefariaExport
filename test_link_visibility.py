"""Link-visibility masks: the three display filters Sefaria applies in get_links().

These run without a Sefaria checkout — the helpers only touch `.sections` and
`.index_node.depth`, so a stub Ref is enough to pin the semantics. The subtle
one is the second filter: it measures the OTHER side against the OTHER side's
own depth, not the anchor's.
"""
import tempfile
import json
import unittest
from pathlib import Path
from unittest import mock

from run_exports import (
    SUPPRESS_ANCHOR_NOT_SEGMENT,
    SUPPRESS_OTHER_TOO_COARSE,
    SUPPRESS_WHOLE_PEREK,
    SUPPRESS_WHOLE_PARASHA,
    SUPPRESSION_BITS,
    _sefaria_project_sha,
    _side_mask,
)


class StubNode:
    def __init__(self, depth):
        self.depth = depth


class StubRef:
    def __init__(self, sections, depth):
        self.sections = sections
        self.index_node = StubNode(depth) if depth is not None else object()


class SideMaskTest(unittest.TestCase):
    def test_mask_names_match_the_cross_repo_contract(self):
        contract = json.loads(
            (Path(__file__).parent / "link_visibility_contract_v1.json").read_text(encoding="utf-8")
        )
        self.assertEqual(contract["schemaVersion"], 1)
        self.assertEqual(
            contract["maskBits"],
            {str(bit): name for bit, name in SUPPRESSION_BITS.items()},
        )

    def test_segment_level_link_is_displayed(self):
        anchor = StubRef([28, 1], 2)      # Bava Batra 28a:1
        other = StubRef([5, 1], 2)        # Migdal Oz 5:1
        self.assertEqual(_side_mask(anchor, other, "Bava Batra 28a:1", set(), set()), 0)

    def test_non_segment_anchor_is_suppressed(self):
        anchor = StubRef([28], 2)         # whole daf, no segment
        other = StubRef([5, 1], 2)
        mask = _side_mask(anchor, other, "Bava Batra 28a", set(), set())
        self.assertTrue(mask & SUPPRESS_ANCHOR_NOT_SEGMENT)

    def test_too_coarse_other_side_uses_its_own_depth(self):
        # The other side is two levels above ITS OWN segment depth.
        anchor = StubRef([28, 1], 2)
        other = StubRef([5], 3)
        mask = _side_mask(anchor, other, "Bava Batra 28a:1", set(), set())
        self.assertTrue(mask & SUPPRESS_OTHER_TOO_COARSE)
        # One level above is still fine — the filter is `+ 1 < depth`.
        self.assertFalse(
            _side_mask(anchor, StubRef([5, 1], 3), "Bava Batra 28a:1", set(), set())
            & SUPPRESS_OTHER_TOO_COARSE
        )

    def test_whole_perek_anchor_is_suppressed(self):
        anchor = StubRef([28, 1], 2)
        other = StubRef([5, 1], 2)
        ref = "Bava Batra 28a:1-60b:22"
        mask = _side_mask(anchor, other, ref, {ref}, set())
        self.assertEqual(mask, SUPPRESS_WHOLE_PEREK)

    def test_whole_parasha_anchor_is_suppressed(self):
        anchor = StubRef([25, 1], 2)
        other = StubRef([3, 2], 2)
        ref = "Exodus 25:1-27:19"
        mask = _side_mask(anchor, other, ref, set(), {ref})
        self.assertEqual(mask, SUPPRESS_WHOLE_PARASHA)

    def test_bits_accumulate(self):
        """A side can satisfy several filters — hence a mask, not one reason."""
        anchor = StubRef([28], 2)                 # not segment level
        other = StubRef([5], 3)                   # too coarse
        ref = "Bava Batra 28a"
        mask = _side_mask(anchor, other, ref, {ref}, set())
        self.assertEqual(
            mask,
            SUPPRESS_ANCHOR_NOT_SEGMENT | SUPPRESS_OTHER_TOO_COARSE | SUPPRESS_WHOLE_PEREK,
        )

    def test_missing_depth_is_suppressed_not_ignored(self):
        """Sefaria does `continue` when node_depth is None; so do we."""
        anchor = StubRef([1, 1], None)
        other = StubRef([1, 1], 2)
        self.assertTrue(
            _side_mask(anchor, other, "X 1:1", set(), set()) & SUPPRESS_ANCHOR_NOT_SEGMENT
        )

    def test_sides_are_independent(self):
        """The reported case: the perek side is hidden, the citing side is not."""
        bb = StubRef([28, 1], 2)
        mo = StubRef([5, 1], 2)
        perek = "Bava Batra 28a:1-60b:22"
        self.assertEqual(_side_mask(bb, mo, perek, {perek}, set()), SUPPRESS_WHOLE_PEREK)
        self.assertEqual(_side_mask(mo, bb, "Migdal Oz 5:1", {perek}, set()), 0)


class MergeRuleTest(unittest.TestCase):
    """Downstream contract, pinned here because the export defines the inputs.

    SeforimLibrary derives one `link` row from (sourceLineId, targetLineId,
    connectionTypeId), so several CSV rows collapse onto one linkId — measured
    at ~78K rows for OTHER alone. A merged side may only be suppressed when
    EVERY contributing row is suppressed. Reasons are diagnostic and are
    therefore OR-ed only after that independent visibility decision.
    """

    @staticmethod
    def merged(*masks):
        if any(mask == 0 for mask in masks):
            return 0
        result = 0
        for mask in masks:
            result |= mask
        return result

    def test_one_visible_contribution_keeps_the_side_visible(self):
        self.assertEqual(self.merged(SUPPRESS_WHOLE_PEREK, 0), 0)

    def test_all_suppressed_keeps_all_reasons(self):
        self.assertEqual(
            self.merged(
                SUPPRESS_WHOLE_PEREK | SUPPRESS_ANCHOR_NOT_SEGMENT,
                SUPPRESS_WHOLE_PEREK,
            ),
            SUPPRESS_WHOLE_PEREK | SUPPRESS_ANCHOR_NOT_SEGMENT,
        )

    def test_disjoint_reasons_still_suppress(self):
        self.assertEqual(
            self.merged(SUPPRESS_WHOLE_PEREK, SUPPRESS_WHOLE_PARASHA),
            SUPPRESS_WHOLE_PEREK | SUPPRESS_WHOLE_PARASHA,
        )


class SefariaProjectShaTest(unittest.TestCase):
    def test_default_checkout_is_next_to_the_export_script(self):
        sha = "b" * 40
        with mock.patch("subprocess.check_output", return_value=sha + "\n") as call:
            self.assertEqual(_sefaria_project_sha(), sha)
        self.assertEqual(
            call.call_args.kwargs["cwd"],
            Path(__file__).resolve().parent / "Sefaria-Project",
        )

    def test_reads_the_checkout_commit_from_git(self):
        sha = "a" * 40
        with mock.patch("subprocess.check_output", return_value=sha + "\n") as call:
            self.assertEqual(_sefaria_project_sha(Path("/checkout")), sha)
        self.assertEqual(call.call_args.kwargs["cwd"], Path("/checkout"))

    def test_missing_or_invalid_sha_fails_closed(self):
        with mock.patch("subprocess.check_output", return_value="\n"):
            with self.assertRaisesRegex(RuntimeError, "invalid Sefaria-Project commit"):
                _sefaria_project_sha(Path(tempfile.gettempdir()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
