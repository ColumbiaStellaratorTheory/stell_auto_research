"""Tests for batch.py: batch-file validation, spec expansion, promotion, early stop.

Run from the repo root with:  python3 -m unittest discover -s tests -t .
"""

from __future__ import annotations

import unittest

import batch

GOALS = {"error": "min", "score": "max", "note": None}
LESSONS = {"applies": [], "tests": [], "rejects": []}


def _batch(*stages, **extra) -> dict:
    return {"hypothesis": "h", "lessons": LESSONS, "stages": list(stages), **extra}


def _view(rid: str, status: str = "pass", **values) -> dict:
    return {"id": rid, "status": status, "values": values}


class TestValidation(unittest.TestCase):
    """Every problem in a batch file is reported at once, before anything runs."""

    def test_all_problems_are_listed(self):
        raw = {"lessons": {"applies": "x"}, "stages": [{"runs": [{"campaign": "c", "dim": [1]}], "replicates": 0}]}
        with self.assertRaises(batch.BatchError) as ctx:
            batch.parse_batch(raw, GOALS)
        message = str(ctx.exception)
        for expected in ("'hypothesis'", "'lessons'", "'campaign' is set by the harness",
                         "'dim' must be a string or number", "'replicates'"):
            self.assertIn(expected, message)

    def test_promotion_must_rank_by_a_goal_metric_of_an_earlier_stage(self):
        raw = _batch({"name": "a"}, {"name": "b", "from": "zzz", "select": {"top": 1, "by": "note"}})
        with self.assertRaises(batch.BatchError) as ctx:
            batch.parse_batch(raw, GOALS)
        self.assertIn("'from' must name an earlier stage", str(ctx.exception))
        self.assertIn("'select.by' must be \"front\" or a goal metric ['error', 'score']", str(ctx.exception))

    def test_bad_range_is_rejected(self):
        raw = _batch({"halton": {"n": 4, "ranges": {"x": [5, 1]}}})
        with self.assertRaisesRegex(batch.BatchError, "min < max"):
            batch.parse_batch(raw, GOALS)

    def test_log_range_needs_positive_min(self):
        raw = _batch({"lhs": {"n": 4, "ranges": {"x": [0, 1, "log"]}}})
        with self.assertRaisesRegex(batch.BatchError, "log range for 'x' needs min > 0"):
            batch.parse_batch(raw, GOALS)

    def test_default_early_stop(self):
        self.assertEqual(batch.parse_batch(_batch({}), GOALS).same_crash_stop, batch.DEFAULT_SAME_CRASH_STOP)


class TestExpansion(unittest.TestCase):
    """A stage expands to base + each point, once per replicate."""

    def _planned(self, stage: dict) -> list[batch.PlannedRun]:
        return batch.plan_stage(batch.parse_batch(_batch(stage), GOALS).stages[0])

    def test_stage_without_points_runs_base_once(self):
        planned = self._planned({"base": {"dim": 3}})
        self.assertEqual([(dict(p.spec), p.replicate) for p in planned], [({"dim": 3}, 0)])

    def test_grid_is_a_cartesian_product_times_replicates(self):
        planned = self._planned({"base": {"problem": "sphere"}, "grid": {"dim": [2, 3], "seed": [1, 2]}, "replicates": 2})
        self.assertEqual(len(planned), 8)
        self.assertEqual(dict(planned[0].spec), {"problem": "sphere", "dim": 2, "seed": 1})
        self.assertEqual([p.replicate for p in planned[:2]], [0, 1])

    def test_explicit_runs_override_base(self):
        planned = self._planned({"base": {"dim": 2}, "runs": [{"dim": 5}]})
        self.assertEqual(dict(planned[0].spec), {"dim": 5})

    def test_samplers_stay_in_range_and_are_deterministic(self):
        stage = {"halton": {"n": 20, "ranges": {"x": [1, 100, "log"], "k": [2, 5, "int"]}},
                 "lhs": {"n": 10, "seed": 3, "ranges": {"y": [0.0, 1.0]}}}
        first, second = self._planned(stage), self._planned(stage)
        self.assertEqual([dict(p.spec) for p in first], [dict(p.spec) for p in second])
        self.assertEqual(len(first), 30)
        for p in first[:20]:
            self.assertTrue(1 <= p.spec["x"] <= 100)
            self.assertIn(p.spec["k"], (2, 3, 4, 5))
        ys = sorted(p.spec["y"] for p in first[20:])
        self.assertTrue(all(i / 10 <= y < (i + 1) / 10 for i, y in enumerate(ys)), "one point per stratum")

    def test_halton_points_are_the_radical_inverse(self):
        self.assertEqual(batch.halton_points(3, 2), [[0.5, 1 / 3], [0.25, 2 / 3], [0.75, 1 / 9]])


class TestPromotion(unittest.TestCase):
    """A `from` stage promotes the best passing runs and carries their params."""

    def _stage(self, by: str, top: int = 2) -> batch.Stage:
        raw = _batch({"name": "screen"}, {"name": "confirm", "from": "screen", "select": {"top": top, "by": by},
                                          "base": {"solver": "full"}, "carry": ["w"], "replicates": 2})
        return batch.parse_batch(raw, GOALS).stages[1]

    def test_ranks_by_metric_goal(self):
        results = [_view("a", error=3.0), _view("b", error=1.0), _view("c", status="fail", error=0.1), _view("d", error=2.0)]
        self.assertEqual([r["id"] for r in batch.select_runs(self._stage("error"), results, GOALS)], ["b", "d"])
        self.assertEqual([r["id"] for r in batch.select_runs(self._stage("score"), [_view("x", score=1.0), _view("y", score=5.0)], GOALS)], ["y", "x"])

    def test_front_selection(self):
        results = [_view("a", error=1.0, score=1.0), _view("b", error=2.0, score=2.0), _view("c", error=3.0, score=0.5)]
        self.assertEqual([r["id"] for r in batch.select_runs(self._stage("front", top=5), results, GOALS)], ["a", "b"])

    def test_carried_params_and_parent(self):
        planned = batch.plan_promotion(self._stage("error"), [_view("a", error=1.0)], {"a": {"w": 7, "other": 1}})
        self.assertEqual([(dict(p.spec), p.replicate, p.parent_run_id) for p in planned],
                         [({"solver": "full", "w": 7}, 0, "a"), ({"solver": "full", "w": 7}, 1, "a")])


class TestEarlyStop(unittest.TestCase):
    """Launching stops when the last N runs crashed the same way."""

    def _crash(self, signature: str) -> dict:
        return {"status": "crash", "crash_signature": signature, "status_reason": "exit_1"}

    def test_same_crash_stops(self):
        completed = [{"status": "pass"}, self._crash("E"), self._crash("E")]
        self.assertIn("crashed the same way: E", batch.should_stop(completed, 2))

    def test_different_crashes_or_a_pass_do_not_stop(self):
        self.assertIsNone(batch.should_stop([self._crash("E"), self._crash("F")], 2))
        self.assertIsNone(batch.should_stop([self._crash("E"), {"status": "pass"}], 2))

    def test_zero_disables(self):
        self.assertIsNone(batch.should_stop([self._crash("E")] * 5, 0))


if __name__ == "__main__":
    unittest.main()
