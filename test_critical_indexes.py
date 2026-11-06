"""The index helper can return successfully without a usable links index."""
import contextlib
import io
import unittest
from unittest import mock

from create_critical_indexes import ensure_and_verify_indexes, verify_links_query_plan


def explained(winning, rejected=None):
    return {"queryPlanner": {"winningPlan": winning, "rejectedPlans": rejected or []}}


def indexed_plan(name="refs.0_1", direction="forward", key=None):
    return {"stage": "FETCH", "inputStage": {
        "stage": "IXSCAN", "indexName": name, "direction": direction,
        "keyPattern": key or {"refs.0": 1},
    }}


class LinksQueryPlanTests(unittest.TestCase):
    def test_explains_exact_cursor_shape_without_executing_it(self):
        db = mock.Mock()
        db.command.return_value = explained(indexed_plan())
        result = verify_links_query_plan(db)
        db.command.assert_called_once_with({
            "explain": {"find": "links", "filter": {}, "sort": {"refs.0": 1}},
            "verbosity": "queryPlanner",
            "maxTimeMS": 10000,
        })
        db.links.find.assert_not_called()
        self.assertEqual(result, {"stages": ["FETCH", "IXSCAN"], "indexes": ["refs.0_1"]})

    def test_valid_reverse_and_compound_indexes_are_not_rejected(self):
        for name, direction, key in (
            ("reverse", "backward", {"refs.0": -1}),
            ("compound", "forward", {"refs.0": 1, "type": 1}),
        ):
            with self.subTest(name=name):
                db = mock.Mock()
                db.command.return_value = explained(indexed_plan(name, direction, key))
                self.assertEqual(verify_links_query_plan(db)["indexes"], [name])

    def test_rejected_sort_does_not_fail_the_winning_index_scan(self):
        db = mock.Mock()
        db.command.return_value = explained(indexed_plan(), [{"stage": "SORT"}])
        self.assertEqual(verify_links_query_plan(db)["stages"], ["FETCH", "IXSCAN"])

    def test_empty_collection_eof_is_safe(self):
        db = mock.Mock()
        db.command.return_value = explained({"stage": "EOF"})
        self.assertEqual(verify_links_query_plan(db), {"stages": ["EOF"], "indexes": []})

    def test_sbe_uses_the_logical_query_plan(self):
        db = mock.Mock()
        db.command.return_value = explained({
            "queryPlan": indexed_plan(),
            "slotBasedPlan": {"stages": "internal text, not a logical stage tree"},
        })
        self.assertEqual(verify_links_query_plan(db)["stages"], ["FETCH", "IXSCAN"])

    def test_blocking_sort_is_rejected_in_classic_sbe_and_nested_plans(self):
        bad = {"stage": "SORT", "inputStage": {"stage": "COLLSCAN"}}
        plans = [
            bad,
            {"queryPlan": bad},
            {"stage": "SHARD_MERGE", "shards": [
                {"winningPlan": indexed_plan()}, {"winningPlan": bad},
            ]},
            {"stage": "FETCH", "inputStages": [indexed_plan(), bad]},
        ]
        for plan in plans:
            with self.subTest(plan=plan):
                db = mock.Mock()
                db.command.return_value = explained(plan)
                with self.assertRaisesRegex(RuntimeError, "blocking SORT.*refs.0"):
                    verify_links_query_plan(db)

    def test_unknown_explain_shape_does_not_claim_success(self):
        for result in ({}, None, {"queryPlanner": None}, {"queryPlanner": {}},
                       explained({}), explained(None)):
            with self.subTest(result=result):
                db = mock.Mock()
                db.command.return_value = result
                with self.assertRaisesRegex(RuntimeError, "no winning stages"):
                    verify_links_query_plan(db)

    def test_command_failure_propagates(self):
        db = mock.Mock()
        error = TimeoutError("query planning timed out")
        db.command.side_effect = error
        with self.assertRaises(TimeoutError) as raised:
            verify_links_query_plan(db)
        self.assertIs(raised.exception, error)


class IndexCreationTests(unittest.TestCase):
    def test_swallowed_creation_failure_is_caught_before_success_message(self):
        def upstream():
            # The real upstream helper prints OperationFailure and returns.
            print("Collection: links; IndexKeySpecsConflict")

        db = mock.Mock()
        db.command.return_value = explained({
            "stage": "SORT", "inputStage": {"stage": "COLLSCAN"},
        })
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(RuntimeError, "blocking SORT"):
                ensure_and_verify_indexes(upstream, db)
        self.assertIn("IndexKeySpecsConflict", output.getvalue())
        self.assertNotIn("✅", output.getvalue())
        self.assertNotIn("All Sefaria indexes ensured", output.getvalue())

    def test_verification_follows_creation_and_reports_only_checked_plan(self):
        order = []
        db = mock.Mock()
        db.command.side_effect = lambda _: (order.append("explain") or explained(indexed_plan()))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            ensure_and_verify_indexes(lambda: order.append("create"), db)
        self.assertEqual(order, ["create", "explain"])
        self.assertIn("✅ Links export plan verified", output.getvalue())
        self.assertIn("stages=FETCH,IXSCAN; indexes=refs.0_1", output.getvalue())
        self.assertNotIn("All Sefaria indexes ensured", output.getvalue())

    def test_unswallowed_creation_failure_stops_without_query_or_success(self):
        db = mock.Mock()
        upstream = mock.Mock(side_effect=RuntimeError("database disconnected"))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(RuntimeError, "disconnected"):
                ensure_and_verify_indexes(upstream, db)
        db.command.assert_not_called()
        self.assertNotIn("✅", output.getvalue())


if __name__ == "__main__":
    unittest.main()
