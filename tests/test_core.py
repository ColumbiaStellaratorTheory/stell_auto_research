"""Tests for the solver-agnostic harness core, using the toy reference adapter.

Stdlib unittest only (the harness has no test-framework dependency).
Run from the repo root with:  python3 -m unittest discover -s tests -t .

Unit tests cover campaign selection, config parsing, adapter loading, record
construction, and artifact retention. The end-to-end tests run `run.py` as a
subprocess against a scratch campaigns directory, so they exercise the real CLI,
the toy solver subprocess, and both result stores.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

import adapter
import contract
import run
from adapters import toy

REPO_ROOT = Path(__file__).resolve().parents[1]


def _make_campaign(root: Path, name: str, config: dict) -> Path:
    campaign_dir = root / name
    campaign_dir.mkdir(parents=True)
    (campaign_dir / run.CONFIG_NAME).write_text(json.dumps(config))
    return campaign_dir


class _ScratchDirTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)


class TestResolveCampaign(_ScratchDirTest):
    """Campaign selection is explicit unless exactly one campaign exists."""

    def test_named_campaign_is_selected(self):
        _make_campaign(self.root, "a", {"adapter": "toy"})
        _make_campaign(self.root, "b", {"adapter": "toy"})
        self.assertEqual(run.resolve_campaign("b", self.root), self.root / "b")

    def test_only_campaign_is_selected_when_unnamed(self):
        _make_campaign(self.root, "solo", {"adapter": "toy"})
        self.assertEqual(run.resolve_campaign(None, self.root), self.root / "solo")

    def test_unnamed_with_several_campaigns_refuses_and_lists_them(self):
        _make_campaign(self.root, "a", {"adapter": "toy"})
        _make_campaign(self.root, "b", {"adapter": "toy"})
        with self.assertRaisesRegex(run.CampaignError, r"2 campaigns exist \(a, b\)"):
            run.resolve_campaign(None, self.root)

    def test_no_campaign_points_to_setup(self):
        with self.assertRaisesRegex(run.CampaignError, "setup-harness"):
            run.resolve_campaign(None, self.root)

    def test_unknown_name_lists_existing_campaigns(self):
        _make_campaign(self.root, "a", {"adapter": "toy"})
        with self.assertRaisesRegex(run.CampaignError, "existing campaigns: a"):
            run.resolve_campaign("missing", self.root)

    def test_directory_without_config_is_not_a_campaign(self):
        (self.root / "notes").mkdir()
        _make_campaign(self.root, "real", {"adapter": "toy"})
        self.assertEqual(run.list_campaigns(self.root), ["real"])


class TestLoadConfig(_ScratchDirTest):
    """config.json names the adapter and optional default env vars."""

    def test_adapter_and_env_are_read(self):
        d = _make_campaign(self.root, "c", {"adapter": "toy", "env": {"SOLVER_ROOT": "/s"}})
        config = run.load_config(d)
        self.assertEqual((config.adapter, dict(config.env)), ("toy", {"SOLVER_ROOT": "/s"}))

    def test_env_defaults_to_empty(self):
        d = _make_campaign(self.root, "c", {"adapter": "toy"})
        self.assertEqual(dict(run.load_config(d).env), {})

    def test_missing_adapter_is_rejected(self):
        d = _make_campaign(self.root, "c", {"env": {}})
        with self.assertRaisesRegex(run.CampaignError, '"adapter" must be a non-empty string'):
            run.load_config(d)

    def test_non_string_env_value_is_rejected(self):
        d = _make_campaign(self.root, "c", {"adapter": "toy", "env": {"THREADS": 4}})
        with self.assertRaisesRegex(run.CampaignError, '"env" must map names to string values'):
            run.load_config(d)

    def test_malformed_json_is_rejected(self):
        d = self.root / "c"
        d.mkdir()
        (d / run.CONFIG_NAME).write_text("{not json")
        with self.assertRaisesRegex(run.CampaignError, "cannot read"):
            run.load_config(d)


class TestApplyEnv(unittest.TestCase):
    """Config env vars fill gaps; values already in the environment win."""

    def test_shell_value_takes_precedence(self):
        environ = {"SOLVER_ROOT": "/from/shell"}
        run.apply_env({"SOLVER_ROOT": "/from/config", "DATA_DIR": "/d"}, environ)
        self.assertEqual(environ, {"SOLVER_ROOT": "/from/shell", "DATA_DIR": "/d"})


class TestResolveLayout(unittest.TestCase):
    """The layout keeps each campaign's results inside its own directory."""

    def test_results_and_artifacts_live_in_the_campaign(self):
        layout = run.resolve_layout(Path("/c/demo"), {})
        self.assertEqual(layout.db_path, Path("/c/demo/results.db"))
        self.assertEqual(layout.jsonl_path, Path("/c/demo/results.jsonl"))
        self.assertEqual(layout.artifacts_dir, Path("/c/demo/artifacts"))

    def test_env_overrides_scratch_and_artifacts(self):
        layout = run.resolve_layout(
            Path("/c/demo"), {"OUTPUT_BASE": "/scratch", "ARTIFACTS_DIR": "/keep", "KEEP_ARTIFACTS": "pass"}
        )
        self.assertEqual(
            (layout.output_base, layout.artifacts_dir, layout.keep_artifacts),
            (Path("/scratch"), Path("/keep"), "pass"),
        )

    def test_invalid_keep_artifacts_falls_back_to_none(self):
        layout = run.resolve_layout(Path("/c/demo"), {"KEEP_ARTIFACTS": "bogus"})
        self.assertEqual(layout.keep_artifacts, "none")


class TestLoadAdapter(unittest.TestCase):
    """Adapters are named by module path and must implement the contract."""

    def test_bare_name_resolves_inside_adapters_package(self):
        self.assertIs(adapter.load_adapter("toy"), toy)

    def test_dotted_name_is_imported_as_given(self):
        self.assertEqual(adapter.module_path("examples.banana.simsopt_banana"), "examples.banana.simsopt_banana")

    def test_unknown_adapter_is_reported(self):
        with self.assertRaisesRegex(adapter.AdapterError, "cannot import adapter 'adapters.nope'"):
            adapter.load_adapter("nope")

    def test_incomplete_adapter_names_missing_members(self):
        pkg = types.ModuleType("fakepkg")
        incomplete = types.ModuleType("fakepkg.incomplete")
        incomplete.NAME = "x"
        for name, module in (("fakepkg", pkg), ("fakepkg.incomplete", incomplete)):
            sys.modules[name] = module
            self.addCleanup(sys.modules.pop, name, None)
        with self.assertRaisesRegex(adapter.AdapterError, "missing SOLVER_MODES, TARGET_FLAG"):
            adapter.load_adapter("fakepkg.incomplete")


class TestBuildRecord(unittest.TestCase):
    """_build_record stores the full run spec and projects canonical metrics."""

    def _args(self, **overrides) -> argparse.Namespace:
        values = {"campaign": "demo", "solver": "optimize", "problem": "rastrigin", "dim": 4, "seed": 7}
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_target_comes_from_the_adapters_target_flag(self):
        record = run._build_record(toy, self._args(), contract.ExperimentOutcome("pass", "ok"), 0.0)
        self.assertEqual(record["equilibrium"], "rastrigin")
        self.assertEqual(record["coil_type"], "toy")

    def test_params_hold_every_flag_except_campaign(self):
        record = run._build_record(toy, self._args(), contract.ExperimentOutcome("pass", "ok"), 0.0)
        self.assertEqual(
            record["params"], {"solver": "optimize", "problem": "rastrigin", "dim": 4, "seed": 7}
        )

    def test_column_metric_projected_and_unknown_metric_kept_in_overflow(self):
        outcome = contract.ExperimentOutcome(
            "pass", "ok", metrics={"objective_J": 0.5, "distance_to_optimum": 0.1}
        )
        record = run._build_record(toy, self._args(), outcome, 0.0)
        self.assertEqual(record["objective_J"], 0.5)
        self.assertEqual(record["metrics"], {"distance_to_optimum": 0.1})

    def test_nan_metric_and_nan_param_are_cleaned_to_none(self):
        outcome = contract.ExperimentOutcome("fail", "incomplete_metrics", metrics={"objective_J": float("nan")})
        record = run._build_record(toy, self._args(seed=float("nan")), outcome, 0.0)
        self.assertIsNone(record["objective_J"])
        self.assertIsNone(record["params"]["seed"])

    def test_validated_and_group_carried_through(self):
        outcome = contract.ExperimentOutcome("pass", "ok", validated="pass", experiment_group="exp-7")
        record = run._build_record(toy, self._args(), outcome, 0.0)
        self.assertEqual((record["validated"], record["experiment_group"]), ("pass", "exp-7"))


class TestAdapterErrorPath(_ScratchDirTest):
    """An adapter that raises is recorded as a crash that still carries the full spec."""

    def test_unexpected_adapter_error_records_full_spec(self):
        captured: dict = {}
        layout = run.Layout(self.root, self.root / "scratch", self.root / "artifacts", "none")
        broken = types.SimpleNamespace(
            NAME="broken", TARGET_FLAG="problem", run_experiment=lambda _a, _d: 1 / 0
        )
        original = run._emit_result
        self.addCleanup(setattr, run, "_emit_result", original)
        run._emit_result = lambda _layout, rec: captured.update(rec)

        run._run_experiment(broken, layout, argparse.Namespace(solver="optimize", problem="sphere", dim=3))

        self.assertEqual(captured["status"], "crash")
        self.assertTrue(captured["status_reason"].startswith("adapter_error"), captured["status_reason"])
        self.assertEqual(captured["params"], {"solver": "optimize", "problem": "sphere", "dim": 3})


class TestFinalizeRunDir(_ScratchDirTest):
    """_finalize_run_dir keeps or discards the run dir per keep_artifacts."""

    def _layout(self, keep: str) -> run.Layout:
        return run.Layout(self.root, self.root / "scratch", self.root / "artifacts", keep)

    def _run_dir(self) -> Path:
        d = self.root / "scratch" / "run_1"
        d.mkdir(parents=True)
        (d / "results.json").write_text("{}")
        return d

    def test_pass_policy_keeps_passing_run(self):
        src = self._run_dir()
        run._finalize_run_dir(self._layout("pass"), src, "pass", "id-pass-1")
        self.assertTrue((self.root / "artifacts" / "id-pass-1" / "results.json").exists())
        self.assertFalse(src.exists(), "run dir should be moved, not copied")

    def test_pass_policy_discards_failing_run(self):
        src = self._run_dir()
        run._finalize_run_dir(self._layout("pass"), src, "fail", "id-fail-1")
        self.assertFalse(src.exists())
        self.assertFalse((self.root / "artifacts" / "id-fail-1").exists())

    def test_none_policy_discards_passing_run(self):
        src = self._run_dir()
        run._finalize_run_dir(self._layout("none"), src, "pass", "id-pass-2")
        self.assertFalse(src.exists())
        self.assertFalse((self.root / "artifacts").exists(), "no artifacts dir under 'none'")

    def test_all_policy_keeps_failing_run(self):
        src = self._run_dir()
        run._finalize_run_dir(self._layout("all"), src, "fail", "id-fail-2")
        self.assertTrue((self.root / "artifacts" / "id-fail-2" / "results.json").exists())


class TestEndToEnd(_ScratchDirTest):
    """run.py as the agent calls it: a real toy run recorded in the campaign's stores."""

    def setUp(self):
        super().setUp()
        self.campaigns = self.root / "campaigns"
        self.demo = _make_campaign(self.campaigns, "demo", {"adapter": "toy"})

    def _run(self, *flags: str) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            run.CAMPAIGNS_DIR_ENV: str(self.campaigns),
            "OUTPUT_BASE": str(self.root / "scratch"),
        }
        for inherited in (run.CAMPAIGN_ENV, "KEEP_ARTIFACTS", "ARTIFACTS_DIR"):
            env.pop(inherited, None)
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "run.py"), *flags],
            capture_output=True, text=True, env=env, cwd=REPO_ROOT, timeout=60,
        )

    def _rows(self) -> list[tuple]:
        with sqlite3.connect(self.demo / "results.db") as db:
            return db.execute(
                "SELECT status, status_reason, equilibrium, json_extract(params, '$.dim') FROM runs"
            ).fetchall()

    def test_passing_run_is_recorded_in_db_and_jsonl(self):
        proc = self._run("--problem", "sphere", "--dim", "3", "--maxiter", "500")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        printed = json.loads(proc.stdout)
        self.assertEqual((printed["status"], printed["equilibrium"]), ("pass", "sphere"))
        self.assertEqual(self._rows(), [("pass", "ok", "sphere", 3)])
        logged = [json.loads(line) for line in (self.demo / "results.jsonl").read_text().splitlines()]
        self.assertEqual([r["id"] for r in logged], [printed["id"]])

    def test_same_seed_reproduces_the_same_objective(self):
        first = json.loads(self._run("--problem", "rastrigin", "--seed", "11").stdout)
        second = json.loads(self._run("--problem", "rastrigin", "--seed", "11").stdout)
        self.assertEqual(first["objective_J"], second["objective_J"])

    def test_solver_crash_is_recorded_as_crash(self):
        proc = self._run("--inject", "crash")
        self.assertEqual(json.loads(proc.stdout)["status_reason"], "exit_3")
        self.assertEqual(self._rows()[0][:2], ("crash", "exit_3"))

    def test_hung_solver_is_recorded_as_timeout(self):
        proc = self._run("--inject", "hang", "--timeout", "1")
        self.assertEqual(json.loads(proc.stdout)["status_reason"], "timeout")

    def test_nan_objective_is_recorded_as_fail(self):
        proc = self._run("--inject", "nan")
        printed = json.loads(proc.stdout)
        self.assertEqual((printed["status"], printed["status_reason"]), ("fail", "incomplete_metrics"))

    def test_second_campaign_requires_explicit_selection(self):
        _make_campaign(self.campaigns, "other", {"adapter": "toy"})
        refused = self._run("--problem", "sphere")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("2 campaigns exist", refused.stderr)
        selected = self._run("--campaign", "other", "--problem", "sphere", "--maxiter", "50")
        self.assertEqual(selected.returncode, 0, selected.stderr)
        self.assertTrue((self.campaigns / "other" / "results.db").exists())
        self.assertFalse((self.demo / "results.db").exists(), "run leaked into the wrong campaign")

    def test_config_env_is_applied_before_the_run(self):
        kept = _make_campaign(self.campaigns, "keeper", {"adapter": "toy", "env": {"KEEP_ARTIFACTS": "all"}})
        proc = self._run("--campaign", "keeper", "--problem", "sphere", "--maxiter", "50")
        run_id = json.loads(proc.stdout)["id"]
        self.assertTrue((kept / "artifacts" / run_id / "results.json").exists(), proc.stderr)

    def test_unknown_adapter_exits_with_message(self):
        _make_campaign(self.campaigns, "broken", {"adapter": "nope"})
        proc = self._run("--campaign", "broken")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("cannot import adapter 'adapters.nope'", proc.stderr)


if __name__ == "__main__":
    unittest.main()
