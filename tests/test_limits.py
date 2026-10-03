"""Tests for a campaign's limits: config.json `fixed`, `bounds` and `budget`, and solver lessons.

Config shape is checked by campaign.load_config, parameter names against the
adapter by runner.resolve_limits; single runs and batches are refused before
anything runs, a batch stops launching once the budget is spent, and `brief` /
`status` show the limits.

Run from the repo root with:  python3 -m unittest discover -s tests -t .
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import unittest
from pathlib import Path

import analysis
import campaign
import records
import runner
from adapters import toy
from tests.test_core import _CliTest, _make_campaign, _ScratchDirTest

PLAN_LESSONS = {"applies": [], "tests": [], "rejects": []}


def _write_campaign(root: Path, config: dict) -> Path:
    """The campaign root/c with this config.json, replacing any earlier one."""
    d = root / "c"
    d.mkdir(parents=True, exist_ok=True)
    (d / campaign.CONFIG_NAME).write_text(json.dumps(config))
    return d


def _limits(campaign_dir: Path) -> runner.Limits:
    return runner.resolve_limits(toy, runner.build_parser(toy, "c"), campaign.load_config(campaign_dir), campaign_dir)


class TestConfigShape(_ScratchDirTest):
    """Malformed fixed / bounds / budget are config errors at load, naming the key."""

    def _error(self, config: dict) -> str:
        d = _write_campaign(self.root, {"adapter": "toy", **config})
        with self.assertRaises(campaign.CampaignError) as ctx:
            campaign.load_config(d)
        return str(ctx.exception)

    def test_valid_limits_are_read(self):
        d = _make_campaign(self.root, "c", {"adapter": "toy", "fixed": {"dim": 4},
                                            "bounds": {"step_size": [0.01, 1.0]}, "budget": {"runs": 5, "hours": 2}})
        config = campaign.load_config(d)
        self.assertEqual((dict(config.fixed), dict(config.bounds), config.budget),
                         ({"dim": 4}, {"step_size": (0.01, 1.0)}, campaign.Budget(5, 2)))

    def test_absent_limits_mean_no_constraint(self):
        config = campaign.load_config(_make_campaign(self.root, "c", {"adapter": "toy"}))
        self.assertEqual((dict(config.fixed), dict(config.bounds), config.budget.limited), ({}, {}, False))

    def test_fixed_value_must_be_a_string_or_number(self):
        self.assertIn("\"fixed\" value for 'dim' must be a string or number", self._error({"fixed": {"dim": [4]}}))
        self.assertIn("\"fixed\" value for 'dim'", self._error({"fixed": {"dim": True}}))

    def test_bounds_must_be_ordered_number_pairs(self):
        for bad in ([1], [5, 1], ["a", 2], 3):
            self.assertIn("\"bounds\" for 'dim' must be [min, max]", self._error({"bounds": {"dim": bad}}))

    def test_param_in_fixed_and_bounds_is_refused(self):
        self.assertIn("['dim'] set in both", self._error({"fixed": {"dim": 4}, "bounds": {"dim": [1, 5]}}))

    def test_budget_is_validated(self):
        self.assertIn("unknown keys ['days']", self._error({"budget": {"days": 1}}))
        self.assertIn("\"budget.runs\" must be an integer >= 1", self._error({"budget": {"runs": 0}}))
        self.assertIn("\"budget.runs\" must be an integer >= 1", self._error({"budget": {"runs": True}}))
        self.assertIn("\"budget.hours\" must be a number > 0", self._error({"budget": {"hours": -1}}))


class TestResolveLimits(_ScratchDirTest):
    """fixed / bounds keys must be run-spec params of the adapter; fixed values parse like batch values."""

    def _error(self, config: dict) -> str:
        d = _write_campaign(self.root, {"adapter": "toy", **config})
        with self.assertRaises(campaign.CampaignError) as ctx:
            _limits(d)
        return str(ctx.exception)

    def test_unknown_param_is_named_with_the_known_ones(self):
        message = self._error({"bounds": {"dimm": [1, 2]}})
        self.assertIn("\"bounds\" key 'dimm' is not a parameter of adapter 'toy'", message)
        self.assertIn("dim, inject, maxiter, mode, noise, problem, seed, step_size", message)

    def test_execution_and_core_flags_are_refused(self):
        self.assertIn("'timeout' is an execution flag", self._error({"bounds": {"timeout": [1, 9]}}))
        self.assertIn("'replicate' is set by the harness", self._error({"fixed": {"replicate": 1}}))

    def test_fixed_value_must_parse(self):
        self.assertIn("\"fixed\" value for 'problem' is invalid: ", self._error({"fixed": {"problem": "cube"}}))
        self.assertIn("invalid int value: 'x'", self._error({"fixed": {"dim": "x"}}))

    def test_bounded_param_must_be_numeric(self):
        self.assertIn("\"bounds\" key 'problem' is not numeric", self._error({"bounds": {"problem": [1, 2]}}))

    def test_fixed_values_are_parsed_by_their_flag(self):
        config = {"fixed": {"step_size": "0.5", "dim": 4, "mode": "optimize"}}
        limits = _limits(_make_campaign(self.root, "c", {"adapter": "toy", **config}))
        self.assertEqual(dict(limits.fixed), {"step_size": 0.5, "dim": 4, "mode": "optimize"})


class TestViolations(unittest.TestCase):
    """A run's parsed values (defaults included) are checked against fixed and inclusive bounds."""

    LIMITS = runner.Limits({"dim": 4}, {"step_size": (0.01, 1.0), "seed": (0, 10)}, campaign.Budget())

    def _args(self, **values) -> argparse.Namespace:
        return argparse.Namespace(**{"dim": 4, "step_size": 0.5, "seed": 3, **values})

    def test_compliant_run_and_inclusive_edges_pass(self):
        self.assertEqual(self.LIMITS.violations(self._args()), [])
        self.assertEqual(self.LIMITS.violations(self._args(step_size=1.0, seed=0)), [])

    def test_each_violation_is_reported(self):
        found = self.LIMITS.violations(self._args(dim=2, step_size=2.0, seed=None))
        self.assertEqual(found, ["dim=2, fixed at 4", "step_size=2 outside bounds [0.01, 1]",
                                 "seed=None outside bounds [0, 10]"])

    def test_skipped_params_are_not_checked(self):
        self.assertEqual(self.LIMITS.violations(self._args(dim=2, seed=99), skip=("dim", "seed")), [])


class TestBudget(_ScratchDirTest):
    """A budget is spent when either the run count or the recorded hours reach it."""

    def test_exhausted_by_runs_or_hours(self):
        budget = campaign.Budget(runs=3, hours=1)
        self.assertIsNone(budget.exhausted(campaign.Usage(2, 3599.0)))
        self.assertIn("run budget spent: 3 of 3 runs", budget.exhausted(campaign.Usage(3, 0.0)))
        self.assertIn("hours budget spent: 1.00 of 1 h", budget.exhausted(campaign.Usage(0, 3600.0)))
        self.assertEqual(budget.runs_left(campaign.Usage(7, 0.0)), 0)

    def test_usage_counts_every_record_and_its_elapsed_time(self):
        layout = self._layout()
        for i, (status, elapsed) in enumerate((("pass", 10.0), ("crash", 5.5), ("fail", None))):
            records.write_run_record(layout.runs_dir, {
                "id": f"r{i}", "adapter": "toy", "mode": "optimize", "target": "sphere", "status": status,
                "status_reason": "ok", "created_at": f"2026-01-0{i + 1}", "elapsed": elapsed,
            })
        records.rebuild(layout, toy)
        self.assertEqual(records.usage(layout, toy), campaign.Usage(3, 15.5))
        self.assertEqual(campaign.Usage.of(records.load_runs(layout, toy)), campaign.Usage(3, 15.5))


class TestSolverLessons(_ScratchDirTest):
    """brief lists the solver's lessons from lessons/<adapter NAME>.md; no file, no line."""

    def _brief(self) -> str:
        d = _make_campaign(self.root / "campaigns", "demo", {"adapter": "toy"})
        environ = {campaign.MACHINE_DIR_ENV: str(self.root / "machine"), campaign.MAX_PARALLEL_ENV: "1"}
        config = campaign.load_config(d)
        layout = campaign.resolve_layout(d, environ)
        return runner.brief(toy, layout, config, _limits(d), environ)

    def _patch_dir(self, path: Path) -> None:
        self.addCleanup(setattr, runner, "SOLVER_LESSONS_DIR", runner.SOLVER_LESSONS_DIR)
        runner.SOLVER_LESSONS_DIR = path

    def test_latest_titles_are_listed(self):
        lessons = self.root / "lessons"
        lessons.mkdir()
        (lessons / "toy.md").write_text(
            "# Solver Lessons\n\n## YYYY-MM-DD — short title\n\n## 2026-01-01 — a\n- source: x\n\n## 2026-01-02 — b\n"
        )
        self._patch_dir(lessons)
        self.assertIn("solver lessons: 2 entries; latest: 2026-01-01 a; 2026-01-02 b", self._brief())

    def test_missing_file_omits_the_line(self):
        self._patch_dir(self.root / "none")
        brief = self._brief()
        self.assertIn("lessons: none yet", brief)
        self.assertNotIn("solver lessons", brief)

    def test_repo_ships_an_empty_toy_lessons_file(self):
        path = runner.SOLVER_LESSONS_DIR / f"{toy.NAME}.md"
        text = path.read_text()
        self.assertEqual(analysis.lesson_titles(text), [])
        self.assertIn("- source:", text)


class TestRenderBriefLimits(_ScratchDirTest):
    """Limit lines follow the head; both lesson lines show even before the first run."""

    def test_no_runs_still_shows_limits_and_lessons(self):
        text = analysis.render_brief("c", "toy", [], {}, [], ["2026-01-01 a"], ["constraints: none", "budget: x"])
        self.assertEqual(text.splitlines(), [
            "campaign c · adapter toy · 0 runs: 0 pass, 0 fail, 0 crash", "constraints: none", "budget: x",
            "no runs yet", "lessons: none yet", "solver lessons: 1 entries; latest: 2026-01-01 a",
        ])

    def test_limit_lines_text(self):
        limits = runner.Limits({"dim": 4}, {"step_size": (0.01, 1.0)}, campaign.Budget(runs=10, hours=2))
        self.assertEqual(runner.limit_lines(limits, campaign.Usage(4, 1800.0)), [
            "constraints: fixed dim=4 · bounds step_size in [0.01, 1]",
            "budget: 4 of 10 runs used (6 left) · 0.50 of 2 h used (1.50 h left)",
        ])
        none = runner.Limits({}, {}, campaign.Budget())
        self.assertEqual(runner.limit_lines(none, campaign.Usage(4, 1800.0)),
                         ["constraints: none", "budget: none · 4 runs, 0.50 h recorded"])


class TestLimitsEndToEnd(_CliTest):
    """run.py refuses runs that break fixed / bounds or exceed the budget, recording nothing."""

    def _config(self, **limits) -> None:
        (self.demo / campaign.CONFIG_NAME).write_text(json.dumps({"adapter": "toy", **limits}))

    def _plan(self, *stages, **extra) -> Path:
        path = self.root / "plan.json"
        path.write_text(json.dumps({"hypothesis": "h", "lessons": PLAN_LESSONS, "stages": list(stages), **extra}))
        return path

    def _seed_elapsed(self, seconds: float) -> None:
        """Record one finished run that took `seconds`, as if from an earlier session."""
        layout = campaign.resolve_layout(self.demo, {})
        records.write_run_record(layout.runs_dir, {
            "id": "earlier", "adapter": "toy", "mode": "optimize", "target": "sphere", "status": "pass",
            "status_reason": "ok", "created_at": "2026-01-01", "elapsed": seconds, "params": {},
        })
        records.rebuild(layout, toy)

    def test_bounds_violation_is_refused_and_nothing_recorded(self):
        self._config(bounds={"step_size": [0.01, 1.0]})
        proc = self._run("--problem", "sphere", "--step-size", "2")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("run refused, it breaks", proc.stderr)
        self.assertIn("step_size=2 outside bounds [0.01, 1]", proc.stderr)
        self.assertFalse((self.demo / "runs").exists())
        self.assertEqual(self._json("--problem", "sphere", "--step-size", "1", "--maxiter", "50")["status"], "pass")

    def test_fixed_applies_to_defaults_too(self):
        self._config(fixed={"dim": 4})
        proc = self._run("--problem", "sphere", "--maxiter", "50")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("dim=2, fixed at 4", proc.stderr)
        self.assertEqual(self._json("--problem", "sphere", "--maxiter", "50", "--dim", "4")["status"], "pass")

    def test_spent_run_budget_refuses_new_runs_but_answers_recorded_ones(self):
        self._config(budget={"runs": 1})
        first = self._json("--problem", "sphere", "--maxiter", "50")
        proc = self._run("--problem", "sphere", "--maxiter", "60")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("run refused, run budget spent: 1 of 1 runs recorded", proc.stderr)
        self.assertEqual(self._json("--problem", "sphere", "--maxiter", "50")["duplicate_of"], first["id"])
        self.assertEqual(len(self._run_files()), 1)

    def test_spent_hours_budget_refuses_new_runs(self):
        self._config(budget={"hours": 1})
        self._seed_elapsed(3600.0)
        proc = self._run("--problem", "sphere", "--maxiter", "50")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("hours budget spent: 1.00 of 1 h recorded", proc.stderr)
        self.assertEqual(len(self._run_files()), 1)

    def test_replay_is_exempt_but_counts(self):
        original = self._json("--problem", "sphere", "--maxiter", "50")
        self._config(fixed={"dim": 5}, budget={"runs": 1})
        proc = self._run("replay", original["id"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(self._run_files()), 2)
        self.assertIn("2 of 1 runs used (0 left)", self._run("brief").stdout)

    def test_config_error_stops_every_command_and_shows_in_status(self):
        self._config(fixed={"timeout": 5})
        for flags in (("--problem", "sphere"), ("brief",), ("rebuild",)):
            proc = self._run(*flags)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("\"fixed\" key 'timeout' is an execution flag", proc.stderr)
        self.assertIn("campaign demo: ", self._run("status").stdout)
        self.assertFalse((self.demo / "runs").exists())

    def test_brief_and_status_show_limits(self):
        self._config(fixed={"problem": "sphere"}, bounds={"maxiter": [10, 100]}, budget={"runs": 3})
        self._json("--problem", "sphere", "--maxiter", "50")
        brief = self._run("brief").stdout
        for expected in ("constraints: fixed problem=sphere · bounds maxiter in [10, 100]",
                         "budget: 1 of 3 runs used (2 left)", "solver lessons: none yet"):
            self.assertIn(expected, brief)
        status = self._run("status").stdout
        self.assertIn("  constraints: fixed problem=sphere", status)
        self.assertIn("  budget: 1 of 3 runs used (2 left)", status)

    def test_batch_with_a_violation_is_refused_up_front(self):
        self._config(bounds={"dim": [2, 3]})
        plan = self._plan({"name": "s", "base": {"problem": "sphere", "maxiter": 50}, "grid": {"dim": [2, 3, 4]}})
        for flags in (("--dry-run",), ()):
            proc = self._run("batch", str(plan), *flags)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("batch refused; nothing was launched", proc.stderr)
            self.assertIn("s run 2 {'problem': 'sphere', 'maxiter': 50, 'dim': 4}: dim=4 outside bounds [2, 3]", proc.stderr)
        self.assertFalse((self.demo / "runs").exists())
        self.assertFalse((self.demo / "batches").exists())

    def test_promotion_base_is_checked_except_carried_params(self):
        self._config(bounds={"dim": [3, 4]})
        screen = {"name": "s", "base": {"problem": "sphere", "maxiter": 50}, "grid": {"dim": [3, 4]}}
        confirm = {"name": "c", "from": "s", "select": {"top": 1, "by": "objective_J"}, "base": {"maxiter": 60}}
        refused = self._run("batch", str(self._plan(screen, confirm)), "--dry-run")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("c base {'maxiter': 60}: dim=2 outside bounds [3, 4]", refused.stderr)
        carried = self._run("batch", str(self._plan(screen, {**confirm, "carry": ["dim"]})), "--dry-run")
        self.assertEqual(carried.returncode, 0, carried.stderr)

    def test_batch_beyond_the_run_budget_is_refused_up_front(self):
        self._config(budget={"runs": 4})
        self._json("--problem", "sphere", "--maxiter", "50", "--dim", "2")
        screen = {"name": "s", "base": {"problem": "sphere", "maxiter": 50}, "grid": {"dim": [2, 3, 4]}}
        fits = self._run("batch", str(self._plan(screen)), "--dry-run")
        self.assertEqual(fits.returncode, 0, fits.stderr)
        self.assertIn("s: 2 to run, 1 already recorded", fits.stdout)
        self.assertIn("budget: 1 of 4 runs used (3 left)", fits.stdout)
        self.assertIn("this batch: 2 new runs", fits.stdout)
        confirm = {"name": "c", "from": "s", "select": {"top": 1, "by": "objective_J"}, "replicates": 2,
                   "base": {"maxiter": 60}, "carry": ["dim"]}
        proc = self._run("batch", str(self._plan(screen, confirm)))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("this batch plans up to 4 new runs, but 3 of the 4-run budget remain", proc.stderr)
        self.assertEqual(len(self._run_files()), 1)

    def test_batch_stops_launching_once_the_hours_budget_is_spent(self):
        # Each hung run is killed at its 1 s timeout; the budget (0.72 s) is spent by the first.
        self._config(budget={"hours": 0.0002})
        plan = self._plan({"base": {"problem": "sphere", "inject": "hang", "timeout": 1}, "grid": {"dim": [2, 3, 4]}},
                          early_stop={"same_crash": 0})
        proc = self._run("batch", str(plan), "--parallel", "1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("stopped: hours budget spent", proc.stdout)
        self.assertEqual(len(self._run_files()), 1)
        record = json.loads(next((self.demo / "batches").glob("*.json")).read_text())
        self.assertEqual(record["status"], "stopped")

    def test_batch_with_the_budget_already_spent_is_refused(self):
        self._config(budget={"hours": 1})
        self._seed_elapsed(3600.0)
        plan = self._plan({"base": {"problem": "sphere", "maxiter": 50}})
        proc = self._run("batch", str(plan), "--dry-run")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("hours budget spent: 1.00 of 1 h recorded; this batch plans up to 1 new runs", proc.stderr)

    def test_in_process_dry_run_reports_violations(self):
        layout = campaign.resolve_layout(self.demo, {})
        limits = runner.Limits({"dim": 4}, {}, campaign.Budget())
        plan = self._plan({"name": "s", "grid": {"dim": [3, 4]}})
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(campaign.HarnessError, "dim=3, fixed at 4"):
            runner.run_batch(toy, layout, runner.build_parser(toy, "demo"), "demo", plan, 1, True, limits)


if __name__ == "__main__":
    unittest.main()
