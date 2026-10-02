"""Tests for analysis.py: crash signatures, Pareto fronts, lessons, and the brief.

Run from the repo root with:  python3 -m unittest discover -s tests -t .
"""

from __future__ import annotations

import unittest

import analysis


def _run(rid: str, status: str = "pass", created: str = "2026-01-01", group=("m", "t"), base="b", **values) -> dict:
    return {
        "id": rid, "mode": group[0], "target": group[1], "status": status,
        "status_reason": "ok" if status == "pass" else "exit_1", "crash_signature": None,
        "validated": None, "created_at": created, "replicate": 0, "seed": 1,
        "values": values, "spec_base": base,
    }


GOALS = {"error": "min", "score": "max", "note": None}


class TestCrashSignature(unittest.TestCase):
    """The signature names the failure and is stable across runs of the same failure."""

    def test_last_exception_line_wins_over_later_noise(self):
        log = "step 1\nTraceback (most recent call last):\n  File x\nValueError: surface goes back on itself\ncleanup done\n"
        self.assertEqual(analysis.crash_signature(log), "ValueError: surface goes back on itself")

    def test_paths_and_numbers_are_normalized(self):
        a = analysis.crash_signature("RuntimeError: cannot open /tmp/run_17/out.json after 3.5 s")
        b = analysis.crash_signature("RuntimeError: cannot open /tmp/run_99/out.json after 12 s")
        self.assertEqual(a, b)
        self.assertEqual(a, "RuntimeError: cannot open <path> after N s")

    def test_without_an_exception_the_last_line_is_used(self):
        self.assertEqual(analysis.crash_signature("starting\nAuthorization required\n\n"), "Authorization required")

    def test_empty_log_has_no_signature(self):
        self.assertIsNone(analysis.crash_signature("\n  \n"))


class TestParetoFront(unittest.TestCase):
    """The front keeps passing runs that no other run beats on every goal."""

    def test_dominated_run_is_excluded(self):
        runs = [_run("a", error=1.0, score=5.0), _run("b", error=2.0, score=4.0), _run("c", error=0.5, score=1.0)]
        goals = analysis.active_goals(runs, GOALS)
        self.assertEqual([r["id"] for r in analysis.pareto_front(runs, goals)], ["c", "a"])

    def test_max_goal_is_respected(self):
        runs = [_run("low", error=1.0, score=1.0), _run("high", error=1.0, score=2.0)]
        front = analysis.pareto_front(runs, analysis.active_goals(runs, GOALS))
        self.assertEqual([r["id"] for r in front], ["high"])

    def test_failed_runs_and_runs_missing_a_goal_are_not_candidates(self):
        runs = [_run("f", status="fail", error=0.1, score=9.0), _run("partial", error=0.1), _run("ok", error=1.0, score=1.0)]
        front = analysis.pareto_front(runs, analysis.active_goals(runs, GOALS))
        self.assertEqual([r["id"] for r in front], ["ok"])

    def test_metrics_without_a_goal_are_ignored(self):
        self.assertEqual(analysis.active_goals([_run("a", error=1.0, note=3.0)], GOALS), [("error", "min")])

    def test_fronts_are_per_group(self):
        runs = [_run("a", group=("m", "t1"), error=1.0), _run("b", group=("m", "t2"), error=5.0)]
        self.assertEqual(analysis.front_ids(runs, GOALS), {"a", "b"})

    def test_runs_since_front_change_counts_later_runs(self):
        runs = [
            _run("best", created="2026-01-01", error=1.0),
            _run("worse", created="2026-01-02", error=2.0),
            _run("crash", status="crash", created="2026-01-03"),
        ]
        self.assertEqual(analysis.runs_since_front_change(runs, GOALS), 2)
        self.assertIsNone(analysis.runs_since_front_change([_run("c", status="crash")], GOALS))


class TestLessons(unittest.TestCase):
    """Dated headings mark lesson entries; everything else is preamble."""

    TEXT = "# Lessons\n\n## Format\n\n## 2026-01-01 — first\n- kind: recipe\n\n## 2026-01-02 — second\n- kind: dead-end\n"

    def test_titles(self):
        self.assertEqual(analysis.lesson_titles(self.TEXT), ["2026-01-01 first", "2026-01-02 second"])


class TestRenderBrief(unittest.TestCase):
    """The brief stays fixed-size and reports the key campaign state."""

    def test_empty_campaign(self):
        self.assertIn("no runs yet", analysis.render_brief("c", "toy", [], GOALS, []))

    def test_sections_present(self):
        runs = [
            _run("r1", error=1.0, score=1.0, created="2026-01-01"),
            _run("r2", error=2.0, score=0.5, created="2026-01-02"),
            {**_run("r3", status="crash", created="2026-01-03"), "crash_signature": "ValueError: x"},
        ]
        text = analysis.render_brief("c", "toy", runs, GOALS, ["2026-01-01 first"])
        for expected in ("3 runs: 2 pass, 0 fail, 1 crash", "front m/t (1 of 1):", "1× ValueError: x",
                         "replicates (noise floor):", "runs since the newest front member: 2",
                         "lessons: 1 entries"):
            self.assertIn(expected, text)

    def test_size_is_capped(self):
        runs = [
            _run(f"r{i}", group=("m", f"t{i}"), created=f"2026-01-{i % 28 + 1:02d}", base=str(i), error=float(i), score=1.0)
            for i in range(200)
        ]
        lines = analysis.render_brief("c", "toy", runs, GOALS, []).splitlines()
        self.assertLess(len(lines), 50)
        self.assertTrue(any("more groups" in line for line in lines))


if __name__ == "__main__":
    unittest.main()
