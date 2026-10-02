"""Tests for the solver-agnostic harness core, using the toy reference adapter.

Stdlib unittest only (the harness has no test-framework dependency).
Run from the repo root with:  python3 -m unittest discover -s tests -t .

Unit tests cover campaign selection, config, adapter lookup, the run spec and
its hash/seed, records, evidence, rebuild, and replay comparison. The
end-to-end tests run `run.py` as a subprocess against a scratch campaigns
directory, exercising the real CLI, the toy solver subprocess, the run files,
and the DB index.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

import adapter
import analysis
import contract
import locks
import machine
import run
from adapters import toy

REPO_ROOT = Path(__file__).resolve().parents[1]


def _make_campaign(root: Path, name: str, config: dict) -> Path:
    campaign_dir = root / name
    campaign_dir.mkdir(parents=True)
    (campaign_dir / run.CONFIG_NAME).write_text(json.dumps(config))
    return campaign_dir


def _toy_args(**overrides) -> argparse.Namespace:
    values = vars(run.build_parser(toy, "demo").parse_args([]))
    values.update(overrides)
    return argparse.Namespace(**values)


class _ScratchDirTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def _layout(self, keep: str = "none") -> run.Layout:
        return run.Layout(self.root, self.root / "scratch", self.root / "artifacts", keep)


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


class TestEnv(unittest.TestCase):
    """Config env fills gaps; the shell wins; required vars are checked before a run."""

    def test_shell_value_takes_precedence(self):
        environ = {"SOLVER_ROOT": "/from/shell"}
        run.apply_env({"SOLVER_ROOT": "/from/config", "DATA_DIR": "/d"}, environ)
        self.assertEqual(environ, {"SOLVER_ROOT": "/from/shell", "DATA_DIR": "/d"})

    def test_missing_required_env_names_the_variables_and_config(self):
        needy = types.SimpleNamespace(NAME="needy", REQUIRED_ENV=("A", "B"))
        with self.assertRaisesRegex(run.HarnessError, r"needs B: add to the \"env\" map in /c/config.json"):
            run.check_required_env(needy, {"A": "1"}, Path("/c"))


class TestResolveLayout(unittest.TestCase):
    """Everything a campaign produces lives inside its own directory by default."""

    def test_defaults_are_inside_the_campaign(self):
        layout = run.resolve_layout(Path("/c/demo"), {})
        self.assertEqual(
            (layout.runs_dir, layout.blobs_dir, layout.scratch_dir, layout.artifacts_dir, layout.db_path),
            (Path("/c/demo/runs"), Path("/c/demo/blobs"), Path("/c/demo/scratch"),
             Path("/c/demo/artifacts"), Path("/c/demo/results.db")),
        )

    def test_env_overrides_scratch_and_artifacts(self):
        layout = run.resolve_layout(
            Path("/c/demo"), {"OUTPUT_BASE": "/scratch", "ARTIFACTS_DIR": "/keep", "KEEP_ARTIFACTS": "pass"}
        )
        self.assertEqual(
            (layout.scratch_dir, layout.artifacts_dir, layout.keep_artifacts),
            (Path("/scratch"), Path("/keep"), "pass"),
        )

    def test_invalid_keep_artifacts_falls_back_to_none(self):
        self.assertEqual(run.resolve_layout(Path("/c"), {"KEEP_ARTIFACTS": "bogus"}).keep_artifacts, "none")


class TestLoadAdapter(unittest.TestCase):
    """Adapters are looked up in the static registry and must implement the contract."""

    def test_registered_adapter_is_returned(self):
        self.assertIs(adapter.load_adapter("toy"), toy)

    def test_unknown_adapter_lists_installed_ones(self):
        with self.assertRaisesRegex(adapter.AdapterError, r"no adapter 'nope'.*installed: toy"):
            adapter.load_adapter("nope")

    def test_incomplete_adapter_names_missing_members(self):
        incomplete = types.SimpleNamespace(NAME="x", MODES=("m",))
        with self.assertRaisesRegex(adapter.AdapterError, "missing TARGET_FLAG, REQUIRED_ENV"):
            adapter.load_adapter("x", {"x": incomplete})

    def test_every_registered_adapter_implements_the_contract(self):
        for name in adapter.REGISTRY:
            with self.subTest(adapter=name):
                adapter.load_adapter(name)


class TestRunSpec(unittest.TestCase):
    """The spec hash identifies what was asked; the derived seed makes runs reproducible."""

    def test_spec_holds_every_flag_except_core_flags(self):
        spec = run.run_spec(_toy_args(dim=4))
        self.assertNotIn("campaign", spec)
        self.assertNotIn("replicate", spec)
        self.assertEqual((spec["dim"], spec["mode"], spec["timeout"]), (4, "optimize", 60))

    def test_execution_flags_do_not_change_the_hash(self):
        a = run.spec_hash(toy, run.run_spec(_toy_args(timeout=60)), "v1")
        b = run.spec_hash(toy, run.run_spec(_toy_args(timeout=999)), "v1")
        self.assertEqual(a, b)

    def test_any_semantic_flag_changes_the_hash(self):
        a = run.spec_hash(toy, run.run_spec(_toy_args(step_size=0.1)), "v1")
        b = run.spec_hash(toy, run.run_spec(_toy_args(step_size=0.2)), "v1")
        self.assertNotEqual(a, b)

    def test_a_changed_solver_changes_the_hash(self):
        spec = run.run_spec(_toy_args())
        self.assertNotEqual(run.spec_hash(toy, spec, "v1"), run.spec_hash(toy, spec, "v2"))

    def test_derived_seed_is_stable_and_differs_per_replicate(self):
        spec = run.run_spec(_toy_args())
        self.assertEqual(run.derive_seed(toy, spec, 0), run.derive_seed(toy, spec, 0))
        self.assertNotEqual(run.derive_seed(toy, spec, 0), run.derive_seed(toy, spec, 1))

    def test_derived_seed_ignores_execution_flags(self):
        a = run.derive_seed(toy, run.run_spec(_toy_args(timeout=5)), 0)
        b = run.derive_seed(toy, run.run_spec(_toy_args(timeout=50)), 0)
        self.assertEqual(a, b)

    def test_explicit_seed_is_kept(self):
        self.assertEqual(run.with_seed(toy, _toy_args(seed=42)).seed, 42)

    def test_unset_seed_is_filled_with_the_derived_seed(self):
        args = _toy_args(replicate=3)
        self.assertEqual(run.with_seed(toy, args).seed, run.derive_seed(toy, run.run_spec(args), 3))

    def test_seedless_adapter_is_left_alone(self):
        seedless = types.SimpleNamespace(SEED_FLAG=None)
        args = _toy_args()
        self.assertIs(run.with_seed(seedless, args), args)


class TestBuildRecord(unittest.TestCase):
    """A record carries identity, spec, metrics, provenance, and evidence."""

    def _record(self, outcome: contract.ExperimentOutcome, **arg_overrides) -> dict:
        return run._build_record(
            toy, _toy_args(problem="rastrigin", seed=7, **arg_overrides), outcome, 0.0,
            run_id="r1", digest="h1", solver_identity="v1", evidence={"log": {"sha256": "x", "bytes": 1}},
        )

    def test_identity_fields(self):
        record = self._record(contract.ExperimentOutcome("pass", "ok", parent_run_id="p0"))
        self.assertEqual(
            {k: record[k] for k in ("id", "adapter", "target", "spec_hash", "replicate", "seed", "parent_run_id")},
            {"id": "r1", "adapter": "toy", "target": "rastrigin", "spec_hash": "h1",
             "replicate": 0, "seed": 7, "parent_run_id": "p0"},
        )

    def test_column_metric_projected_and_unknown_metric_kept_in_overflow(self):
        record = self._record(contract.ExperimentOutcome(
            "pass", "ok", metrics={"objective_J": 0.5, "distance_to_optimum": 0.1}
        ))
        self.assertEqual(record["metrics"], {"objective_J": 0.5, "distance_to_optimum": 0.1})
        self.assertNotIn("objective_J", record, "metrics live only in the metrics field")

    def test_nan_metric_and_nan_param_are_cleaned_to_none(self):
        outcome = contract.ExperimentOutcome("fail", "incomplete_metrics", metrics={"objective_J": float("nan")})
        record = self._record(outcome, noise=float("nan"))
        self.assertIsNone(record["metrics"]["objective_J"])
        self.assertIsNone(record["params"]["noise"])

    def test_provenance_combines_solver_adapter_harness_and_platform(self):
        record = self._record(contract.ExperimentOutcome("pass", "ok", provenance={"command": ["x"]}))
        provenance = record["provenance"]
        self.assertEqual((provenance["solver_identity"], provenance["adapter"]), ("v1", {"command": ["x"]}))
        self.assertEqual(set(provenance["harness"]), {"commit", "dirty"})
        self.assertEqual(set(provenance["platform"]), {"os", "release", "machine", "python"})

    def test_summary_is_compact(self):
        record = self._record(contract.ExperimentOutcome("pass", "ok", metrics={"objective_J": 0.5}))
        printed = run.summary(record, on_front=True)
        self.assertFalse({"provenance", "evidence", "params", "spec_hash"} & set(printed))
        self.assertEqual((printed["metrics"], printed["on_front"]), ({"objective_J": 0.5}, True))

    def test_summary_carries_crash_signature_and_omits_on_front_for_non_pass(self):
        record = run._build_record(
            toy, _toy_args(seed=1), contract.ExperimentOutcome("crash", "exit_1"), 0.0,
            run_id="r", digest="h", solver_identity="v", evidence={}, crash_signature="ValueError: bad",
        )
        printed = run.summary(record, on_front=None)
        self.assertEqual(printed["crash_signature"], "ValueError: bad")
        self.assertNotIn("on_front", printed)


class TestEvidenceAndRecords(_ScratchDirTest):
    """Evidence is stored by content hash; run records are written atomically."""

    def test_evidence_is_content_addressed_and_shared(self):
        a, b = self.root / "a.log", self.root / "b.log"
        a.write_text("same"), b.write_text("same")
        stored = run.store_evidence(self.root / "blobs", {"first": a, "second": b, "gone": self.root / "nope"})
        self.assertEqual(set(stored), {"first", "second"}, "a missing file is skipped")
        self.assertEqual(stored["first"], stored["second"])
        digest = stored["first"]["sha256"]
        self.assertEqual((self.root / "blobs" / digest[:2] / digest).read_text(), "same")

    def test_run_record_write_leaves_no_temp_files(self):
        run.write_run_record(self.root / "runs", {"id": "r1", "status": "pass"})
        self.assertEqual([p.name for p in (self.root / "runs").iterdir()], ["r1.json"])

    def test_records_read_in_creation_order(self):
        for rid, created in (("b", "2026-01-02"), ("a", "2026-01-03"), ("c", "2026-01-01")):
            run.write_run_record(self.root / "runs", {"id": rid, "created_at": created})
        self.assertEqual([r["id"] for r in run.read_run_records(self.root / "runs")], ["c", "b", "a"])


class TestRebuild(_ScratchDirTest):
    """results.db and results.jsonl are regenerated from the run files."""

    def _record(self, rid: str, created: str) -> dict:
        return {
            "id": rid, "adapter": "toy", "mode": "optimize", "target": "sphere",
            "status": "pass", "status_reason": "ok", "created_at": created,
            "spec_hash": "h", "replicate": 0,
            "params": {"dim": 2}, "metrics": {"objective_J": 1.5}, "provenance": {}, "evidence": {},
        }

    def test_rebuild_indexes_every_run_and_writes_jsonl(self):
        layout = self._layout()
        for rid, created in (("r2", "2026-01-02"), ("r1", "2026-01-01")):
            run.write_run_record(layout.runs_dir, self._record(rid, created))
        self.assertEqual(run.rebuild(layout), 2)
        with contextlib.closing(sqlite3.connect(layout.db_path)) as db:
            rows = db.execute("SELECT id, json_extract(metrics, '$.objective_J') FROM runs ORDER BY id").fetchall()
        self.assertEqual(rows, [("r1", 1.5), ("r2", 1.5)])
        jsonl_ids = [json.loads(line)["id"] for line in layout.jsonl_path.read_text().splitlines()]
        self.assertEqual(jsonl_ids, ["r1", "r2"])

    def test_rebuild_keeps_a_backup_of_the_old_db(self):
        layout = self._layout()
        with contextlib.closing(sqlite3.connect(layout.db_path)) as db:
            db.execute("CREATE TABLE runs (id TEXT)")
        run.rebuild(layout)
        self.assertEqual(len(list(self.root.glob("results.db.bak-*"))), 1)

    def test_legacy_jsonl_import_is_idempotent(self):
        layout = self._layout()
        legacy = self.root / "old.jsonl"
        legacy.write_text(json.dumps({**self._record("old1", "2025-01-01"), "spec_hash": None}) + "\n")
        self.assertEqual(run.import_jsonl(layout, legacy), 1)
        self.assertEqual(run.import_jsonl(layout, legacy), 0)
        record = json.loads((layout.runs_dir / "old1.json").read_text())
        self.assertEqual(record["provenance"], {"imported_from": str(legacy)})

    def test_outdated_db_is_refused_with_rebuild_instructions(self):
        layout = self._layout()
        with contextlib.closing(sqlite3.connect(layout.db_path)) as db:
            db.execute("CREATE TABLE runs (id TEXT)")
        with self.assertRaisesRegex(run.HarnessError, "schema version 0.*python run.py rebuild"):
            run.open_db(layout)


class TestCompareRuns(unittest.TestCase):
    """A replay matches when status agrees and every metric is within the relative tolerance."""

    def _rec(self, status="pass", **metrics) -> dict:
        return {"status": status, "metrics": {"objective_J": metrics.pop("objective_J", 1.0), **metrics}}

    def test_identical_runs_match(self):
        self.assertEqual(run.compare_runs(self._rec(final_step=0.5), self._rec(final_step=0.5), 0.0), [])

    def test_difference_within_tolerance_matches(self):
        self.assertEqual(run.compare_runs(self._rec(objective_J=1.0), self._rec(objective_J=1.0 + 1e-9), 1e-6), [])

    def test_difference_beyond_tolerance_is_reported(self):
        mismatches = run.compare_runs(self._rec(objective_J=1.0), self._rec(objective_J=1.1), 1e-6)
        self.assertEqual(mismatches, [{"field": "objective_J", "original": 1.0, "replay": 1.1}])

    def test_status_change_and_missing_metric_are_reported(self):
        mismatches = run.compare_runs(self._rec(final_step=0.5), self._rec(status="fail"), 1e-6)
        self.assertEqual([m["field"] for m in mismatches], ["status", "final_step"])


class TestExecute(_ScratchDirTest):
    """execute records crashes from a raising adapter and applies artifact retention."""

    def test_unexpected_adapter_error_records_full_spec(self):
        broken = types.SimpleNamespace(
            NAME="broken", TARGET_FLAG="problem", SEED_FLAG=None, EXECUTION_FLAGS=(),
            run_experiment=lambda _a, _r: 1 / 0,
        )
        args = argparse.Namespace(campaign="c", replicate=0, mode="optimize", problem="sphere", dim=3)
        record = run.execute(broken, self._layout(), args, "v0")
        self.assertEqual(record["status"], "crash")
        self.assertTrue(record["status_reason"].startswith("adapter_error"), record["status_reason"])
        self.assertEqual(record["params"], {"mode": "optimize", "problem": "sphere", "dim": 3})
        self.assertTrue((self.root / "runs" / f"{record['id']}.json").exists())

    def test_pass_policy_keeps_passing_run(self):
        src = self.root / "scratch" / "run_1"
        src.mkdir(parents=True)
        (src / "results.json").write_text("{}")
        run._finalize_run_dir(self._layout("pass"), src, "pass", "id-pass-1")
        self.assertTrue((self.root / "artifacts" / "id-pass-1" / "results.json").exists())
        self.assertFalse(src.exists(), "run dir should be moved, not copied")

    def test_none_policy_discards_run_dir(self):
        src = self.root / "scratch" / "run_2"
        src.mkdir(parents=True)
        run._finalize_run_dir(self._layout("none"), src, "pass", "id-pass-2")
        self.assertFalse(src.exists())
        self.assertFalse((self.root / "artifacts").exists(), "no artifacts dir under 'none'")


class _CliTest(_ScratchDirTest):
    """Runs run.py as a subprocess against a scratch campaigns directory."""

    def setUp(self):
        super().setUp()
        self.campaigns = self.root / "campaigns"
        self.demo = _make_campaign(self.campaigns, "demo", {"adapter": "toy"})

    def _run(self, *flags: str) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            run.CAMPAIGNS_DIR_ENV: str(self.campaigns),
            run.SLOTS_DIR_ENV: str(self.root / "slots"),
            run.MAX_PARALLEL_ENV: "4",
            run.MACHINE_DIR_ENV: str(self.root / "machine"),
        }
        for inherited in (run.CAMPAIGN_ENV, "KEEP_ARTIFACTS", "ARTIFACTS_DIR", "OUTPUT_BASE", run.BLOBS_DIR_ENV):
            env.pop(inherited, None)
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "run.py"), *flags],
            capture_output=True, text=True, env=env, cwd=REPO_ROOT, timeout=60,
        )

    def _json(self, *flags: str) -> dict:
        proc = self._run(*flags)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def _run_files(self, campaign: Path | None = None) -> list[Path]:
        return sorted(((campaign or self.demo) / "runs").glob("*.json"))


class TestEndToEnd(_CliTest):
    """run.py as the agent calls it: real toy runs recorded in the campaign."""

    def test_passing_run_is_recorded_as_file_and_db_row(self):
        printed = self._json("--problem", "sphere", "--dim", "3", "--maxiter", "500")
        self.assertEqual((printed["status"], printed["target"]), ("pass", "sphere"))
        self.assertNotIn("provenance", printed, "stdout should stay compact")
        record = json.loads((self.demo / "runs" / f"{printed['id']}.json").read_text())
        self.assertEqual(record["provenance"]["adapter"]["command"][2:4], ["--problem", "sphere"])
        log_hash = record["evidence"]["log"]["sha256"]
        self.assertTrue((self.demo / "blobs" / log_hash[:2] / log_hash).exists())
        self.assertTrue(printed["on_front"], "the only passing run is on its front")
        with contextlib.closing(sqlite3.connect(self.demo / "results.db")) as db:
            row = db.execute("SELECT id, seed, spec_hash FROM runs").fetchone()
        self.assertEqual(row, (printed["id"], printed["seed"], record["spec_hash"]))
        self.assertFalse((self.demo / "scratch" / printed["id"]).exists(), "scratch cleaned under 'none'")

    def test_identical_spec_is_not_run_twice(self):
        first = self._json("--problem", "sphere", "--maxiter", "50")
        second = self._json("--problem", "sphere", "--maxiter", "50")
        self.assertEqual(second["duplicate_of"], first["id"])
        self.assertEqual(len(self._run_files()), 1)

    def test_replicate_draws_a_new_seed(self):
        first = self._json("--problem", "rastrigin", "--maxiter", "50")
        second = self._json("--problem", "rastrigin", "--maxiter", "50", "--replicate", "1")
        self.assertNotEqual(first["seed"], second["seed"])
        self.assertEqual(len(self._run_files()), 2)

    def test_crash_does_not_block_a_retry(self):
        self._json("--inject", "crash")
        retry = self._json("--inject", "crash")
        self.assertNotIn("duplicate_of", retry)
        self.assertEqual(retry["status_reason"], "exit_3")

    def test_hung_solver_is_recorded_as_timeout(self):
        self.assertEqual(self._json("--inject", "hang", "--timeout", "1")["status_reason"], "timeout")

    def test_nan_objective_is_recorded_as_fail(self):
        printed = self._json("--inject", "nan")
        self.assertEqual((printed["status"], printed["status_reason"]), ("fail", "incomplete_metrics"))

    def test_replay_reproduces_a_run(self):
        original = self._json("--problem", "rastrigin", "--dim", "3", "--maxiter", "300")
        verdict = self._json("replay", original["id"])
        self.assertTrue(verdict["match"], verdict)
        replayed = json.loads((self.demo / "runs" / f"{verdict['run_id']}.json").read_text())
        self.assertEqual((replayed["replay_of"], replayed["seed"]), (original["id"], original["seed"]))

    def test_replay_reports_a_mismatch_with_exit_code_2(self):
        original = self._json("--problem", "sphere", "--maxiter", "100")
        path = self.demo / "runs" / f"{original['id']}.json"
        tampered = json.loads(path.read_text())
        tampered["metrics"]["objective_J"] = 123.0
        path.write_text(json.dumps(tampered))
        proc = self._run("replay", original["id"])
        self.assertEqual(proc.returncode, run.REPLAY_MISMATCH_EXIT, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["mismatches"][0]["field"], "objective_J")

    def test_rebuild_regenerates_the_db_from_run_files(self):
        self._json("--problem", "sphere", "--maxiter", "50")
        (self.demo / "results.db").unlink()
        self.assertEqual(self._json("rebuild")["rows"], 1)
        with contextlib.closing(sqlite3.connect(self.demo / "results.db")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 1)

    def test_second_campaign_requires_explicit_selection(self):
        other = _make_campaign(self.campaigns, "other", {"adapter": "toy"})
        refused = self._run("--problem", "sphere")
        self.assertEqual(refused.returncode, 1)
        self.assertIn("2 campaigns exist", refused.stderr)
        self._json("--campaign", "other", "--problem", "sphere", "--maxiter", "50")
        self.assertEqual((len(self._run_files(other)), len(self._run_files())), (1, 0))

    def test_config_env_is_applied_before_the_run(self):
        kept = _make_campaign(self.campaigns, "keeper", {"adapter": "toy", "env": {"KEEP_ARTIFACTS": "all"}})
        run_id = self._json("--campaign", "keeper", "--problem", "sphere", "--maxiter", "50")["id"]
        self.assertTrue((kept / "artifacts" / run_id / "results.json").exists())

    def test_missing_required_env_stops_before_running(self):
        needy = types.ModuleType("needy")
        needy.__dict__.update({k: getattr(toy, k) for k in adapter.CONTRACT_MEMBERS})
        needy.NAME, needy.REQUIRED_ENV = "needy", ("NEEDY_SOLVER_ROOT",)
        self.addCleanup(adapter.REGISTRY.pop, "needy", None)
        adapter.REGISTRY["needy"] = needy
        campaign = _make_campaign(self.campaigns, "needy", {"adapter": "needy"})
        environ = {**os.environ, run.CAMPAIGNS_DIR_ENV: str(self.campaigns), run.MACHINE_DIR_ENV: str(self.root / "machine")}
        environ.pop("NEEDY_SOLVER_ROOT", None)
        original = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(original)))
        os.environ.clear()
        os.environ.update(environ)
        with self.assertRaisesRegex(run.HarnessError, "needs NEEDY_SOLVER_ROOT"):
            run._dispatch("run", ["--campaign", "needy"])
        self.assertFalse((campaign / "runs").exists())

    def test_unknown_adapter_exits_with_message(self):
        _make_campaign(self.campaigns, "broken", {"adapter": "nope"})
        proc = self._run("--campaign", "broken")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no adapter 'nope'", proc.stderr)



class TestSchemaUpgrade(_ScratchDirTest):
    """A DB from a record-backed older schema is rebuilt from runs/ automatically."""

    def test_v2_db_is_rebuilt_on_open(self):
        layout = self._layout()
        run.write_run_record(layout.runs_dir, {
            "id": "r1", "coil_type": "toy", "solver": "optimize", "equilibrium": "sphere",
            "status": "crash", "status_reason": "exit_1", "created_at": "2026-01-01",
        })
        with contextlib.closing(sqlite3.connect(layout.db_path)) as db:
            db.execute("CREATE TABLE runs (id TEXT)")
            db.execute("PRAGMA user_version = 2")
        with contextlib.closing(run.open_db(layout)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], run.SCHEMA_VERSION)
            self.assertEqual(
                db.execute("SELECT id, adapter, mode, target FROM runs").fetchall(),
                [("r1", "toy", "optimize", "sphere")],
                "old run files are converted to the current names on rebuild",
            )


class TestSpecBase(unittest.TestCase):
    """Replicates of one spec share a spec_base whatever their seeds."""

    def test_seed_and_execution_flags_do_not_change_it(self):
        a = run.spec_base(toy, run.run_spec(_toy_args(seed=1, timeout=5)))
        b = run.spec_base(toy, run.run_spec(_toy_args(seed=2, timeout=50)))
        self.assertEqual(a, b)
        self.assertNotEqual(a, run.spec_base(toy, run.run_spec(_toy_args(dim=9))))


class TestLoaderMetrics(unittest.TestCase):
    """METRICS goals must be min, max or None."""

    def test_bad_goal_is_rejected(self):
        bad = types.SimpleNamespace(**{m: None for m in adapter.CONTRACT_MEMBERS})
        bad.METRICS = {"error": "lower"}
        with self.assertRaisesRegex(adapter.AdapterError, "METRICS goals"):
            adapter.load_adapter("bad", {"bad": bad})


class TestQueryAndLessons(_ScratchDirTest):
    """query is read-only and capped; lessons import as marked priors."""

    def _layout_with_runs(self, n: int) -> run.Layout:
        layout = self._layout()
        for i in range(n):
            run.write_run_record(layout.runs_dir, {
                "id": f"r{i}", "adapter": "toy", "mode": "optimize", "target": "sphere",
                "status": "pass", "status_reason": "ok", "created_at": f"2026-01-{i + 1:02d}",
                "metrics": {"objective_J": float(i)},
            })
        run.rebuild(layout)
        return layout

    def test_query_returns_header_and_rows(self):
        out = run.query(self._layout_with_runs(2), toy, "SELECT id, objective_J FROM results ORDER BY id", 10)
        self.assertEqual(out.splitlines(), ["id\tobjective_J", "r0\t0", "r1\t1"])

    def test_query_is_capped(self):
        out = run.query(self._layout_with_runs(5), toy, "SELECT id FROM runs", 2)
        self.assertEqual(len(out.splitlines()), 4)
        self.assertIn("more than 2 rows", out)

    def test_query_cannot_write(self):
        layout = self._layout_with_runs(1)
        with self.assertRaisesRegex(run.HarnessError, "readonly"):
            run.query(layout, toy, "DELETE FROM runs", 10)

    def test_import_lessons_demotes_headings_under_one_marked_entry(self):
        source, target = self.root / "src", self.root / "dst"
        source.mkdir(), target.mkdir()
        (source / run.LESSONS_NAME).write_text("# L\n\n## 2026-01-01 — use restarts\n- kind: recipe\n")
        (target / run.LESSONS_NAME).write_text("# L\n")
        self.assertEqual(run.import_lessons(source, target), 1)
        text = (target / run.LESSONS_NAME).read_text()
        self.assertIn("### 2026-01-01 — use restarts", text)
        self.assertEqual(len(analysis.lesson_titles(text)), 1, "imported entries are not this campaign's own")
        self.assertIn("Imported 1 lessons from campaign src", analysis.lesson_titles(text)[0])


class TestCommandsEndToEnd(_CliTest):
    """brief, query, campaigns and crash signatures through the real CLI."""

    def test_crash_carries_its_signature(self):
        self.assertEqual(self._json("--inject", "crash")["crash_signature"], "injected crash")

    def test_brief_query_and_campaigns(self):
        self._json("--problem", "sphere", "--maxiter", "50")
        brief = self._run("brief")
        self.assertIn("campaign demo · adapter toy · 1 runs: 1 pass", brief.stdout, brief.stderr)
        rows = self._run("query", "SELECT status, COUNT(*) FROM runs GROUP BY status").stdout.splitlines()
        self.assertEqual(rows, ["status\tCOUNT(*)", "pass\t1"])
        table = self._run("campaigns").stdout.splitlines()
        self.assertEqual(table[1].split("\t")[:4], ["demo", "toy", "1", "1"])



class TestLocksAndSlots(_ScratchDirTest):
    """Locks are exclusive until released; a held slot sends the next run to another."""

    def test_lock_is_exclusive_until_released(self):
        path = self.root / "x.lock"
        held = locks.try_lock(path)
        self.assertIsNotNone(held)
        self.assertIsNone(locks.try_lock(path))
        locks.release(held)
        again = locks.try_lock(path)
        self.assertIsNotNone(again)
        locks.release(again)

    def test_busy_slot_is_skipped(self):
        first = locks.acquire_slot(self.root, 2)
        second = locks.acquire_slot(self.root, 2)
        self.assertIsNone(locks.try_lock(self.root / "slot-0.lock"))
        self.assertIsNone(locks.try_lock(self.root / "slot-1.lock"))
        locks.release(first), locks.release(second)

    def test_slot_capacity_comes_from_the_environment(self):
        slots = run.resolve_slots({run.MAX_PARALLEL_ENV: "3", run.SLOTS_DIR_ENV: "/s", run.MACHINE_DIR_ENV: str(self.root)})
        self.assertEqual((slots.dir, slots.capacity), (Path("/s"), 3))
        with self.assertRaisesRegex(run.HarnessError, "must be an integer >= 1"):
            run.resolve_slots({run.MAX_PARALLEL_ENV: "0", run.MACHINE_DIR_ENV: str(self.root)})

    def test_machine_json_sets_capacity_and_env_overrides_it(self):
        machine.write_settings(self.root, {"max_parallel": 5})
        environ = {run.MACHINE_DIR_ENV: str(self.root)}
        self.assertEqual(run.resolve_slots(environ).capacity, 5)
        self.assertEqual(run.resolve_slots({**environ, run.MAX_PARALLEL_ENV: "2"}).capacity, 2)
        self.assertEqual(run.resolve_slots(environ).dir, self.root / "slots")
        self.assertEqual(run.resolve_slots({run.MACHINE_DIR_ENV: str(self.root / "empty")}).capacity, 1)

    def test_busy_slots_counts_held_locks(self):
        slots = run.Slots(self.root / "slots", 3)
        held = locks.acquire_slot(slots.dir, slots.capacity)
        self.addCleanup(locks.release, held)
        self.assertEqual(run.busy_slots(slots), 1)

    def test_plan_minutes_is_validated(self):
        d = _make_campaign(self.root, "c", {"adapter": "toy", "plan_minutes": 0})
        with self.assertRaisesRegex(run.CampaignError, "plan_minutes"):
            run.load_config(d)

    def test_campaign_max_parallel_is_validated(self):
        d = _make_campaign(self.root, "c", {"adapter": "toy", "max_parallel": 0})
        with self.assertRaisesRegex(run.CampaignError, "max_parallel"):
            run.load_config(d)

    def test_claimed_spec_is_reported_in_progress(self):
        layout = self._layout()
        args = run.with_seed(toy, _toy_args(problem="sphere"))
        digest = run.spec_hash(toy, run.run_spec(args), toy.solver_identity(args))
        claim = locks.try_lock(layout.claims_dir / f"{digest}-0.lock")
        self.addCleanup(locks.release, claim)
        printed = run.run_once(toy, layout, run.Slots(self.root / "slots", 1), args)
        self.assertEqual(printed, {"in_progress": True, "spec_hash": digest, "replicate": 0})
        self.assertFalse(layout.runs_dir.exists(), "nothing ran")

    def test_shared_blob_store_from_environment(self):
        layout = run.resolve_layout(Path("/c/demo"), {run.BLOBS_DIR_ENV: "/machine/blobs"})
        self.assertEqual(layout.blobs_dir, Path("/machine/blobs"))


class TestBatchEndToEnd(_CliTest):
    """run.py batch: validated up front, parallel children, promotion, reuse, early stop."""

    def _write(self, name: str, content: dict) -> Path:
        path = self.root / name
        path.write_text(json.dumps(content))
        return path

    def _plan(self, *stages, **extra) -> Path:
        return self._write("plan.json", {
            "hypothesis": "h", "lessons": {"applies": [], "tests": [], "rejects": []},
            "stages": list(stages), **extra,
        })

    def _rows(self, sql: str) -> list:
        with contextlib.closing(sqlite3.connect(self.demo / "results.db")) as db:
            return db.execute(sql).fetchall()

    def test_two_stage_batch_records_batch_and_lineage(self):
        plan = self._plan(
            {"name": "screen", "base": {"problem": "rastrigin", "maxiter": 200}, "grid": {"dim": [2, 3, 4]}},
            {"name": "confirm", "from": "screen", "select": {"top": 2, "by": "objective_J"},
             "base": {"problem": "rastrigin", "maxiter": 400}, "carry": ["dim"]},
        )
        proc = self._run("batch", str(plan))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("screen: 3 runs (0 already recorded)", proc.stdout)
        self.assertIn("confirm: 2 runs", proc.stdout)
        batch_ids = self._rows("SELECT DISTINCT batch_id FROM runs")
        self.assertEqual(len(batch_ids), 1)
        promoted = self._rows("SELECT r.parent_run_id IN (SELECT id FROM runs WHERE json_extract(params,'$.maxiter') = 200) "
                              "FROM runs r WHERE json_extract(r.params,'$.maxiter') = 400")
        self.assertEqual(promoted, [(1,), (1,)], "each promoted run names its screen parent")
        record = json.loads((self.demo / "batches" / f"{batch_ids[0][0]}.json").read_text())
        self.assertEqual((record["status"], len(record["stages"]["screen"])), ("done", 3))

    def test_rerun_reuses_recorded_runs(self):
        plan = self._plan({"base": {"problem": "sphere", "maxiter": 50}, "grid": {"dim": [2, 3]}})
        self._run("batch", str(plan))
        again = self._run("batch", str(plan))
        self.assertIn("2 runs (2 already recorded)", again.stdout)
        self.assertEqual(self._rows("SELECT COUNT(*) FROM runs"), [(2,)])

    def test_repeated_crash_stops_the_batch(self):
        plan = self._plan({"base": {"inject": "crash"}, "grid": {"dim": [2, 3, 4, 5, 6]}}, early_stop={"same_crash": 2})
        proc = self._run("batch", str(plan), "--parallel", "1")
        self.assertIn("stopped: last 2 runs crashed the same way: injected crash", proc.stdout)
        self.assertEqual(self._rows("SELECT COUNT(*) FROM runs"), [(2,)])

    def test_invalid_batch_launches_nothing(self):
        plan = self._plan({"runs": [{"dimm": 3}, {"dim": "x"}]})
        proc = self._run("batch", str(plan))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("unrecognized arguments: --dimm 3", proc.stderr)
        self.assertIn("invalid int value: 'x'", proc.stderr)
        self.assertFalse((self.demo / "runs").exists())

    def test_dry_run_reports_the_plan(self):
        plan = self._plan({"name": "s", "grid": {"dim": [2, 3]}, "replicates": 2})
        proc = self._run("batch", str(plan), "--dry-run")
        self.assertIn("s: 4 to run, 0 already recorded", proc.stdout)
        self.assertFalse((self.demo / "runs").exists())

    @unittest.skipIf(os.name == "nt", "SIGTERM delivery is POSIX-only")
    def test_sigterm_records_a_cancelled_run(self):
        env = {**os.environ, run.CAMPAIGNS_DIR_ENV: str(self.campaigns),
               run.SLOTS_DIR_ENV: str(self.root / "slots"), run.MAX_PARALLEL_ENV: "4"}
        env.pop(run.CAMPAIGN_ENV, None)
        proc = subprocess.Popen(
            [sys.executable, str(REPO_ROOT / "run.py"), "--inject", "hang", "--timeout", "600"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=REPO_ROOT,
        )
        deadline = time.monotonic() + 30
        while not list((self.demo / "scratch").glob("*/run.log")) and time.monotonic() < deadline:
            time.sleep(0.1)
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=30)
        printed = json.loads(out)
        self.assertEqual((printed["status"], printed["status_reason"]), ("crash", "cancelled"))



class TestMachineEndToEnd(_CliTest):
    """machine and schema commands, peak memory, and sizing in the brief."""

    def test_machine_saves_settings_and_reports_campaign_cost(self):
        self._json("--problem", "sphere", "--maxiter", "100")
        proc = self._run("machine", "--max-parallel", "3", "--usable-cores", "12")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(machine.read_settings(self.root / "machine"), {"max_parallel": 3, "usable_cores": 12})
        self.assertIn("usable_cpus:", proc.stdout)
        self.assertIn("campaign demo:", proc.stdout)
        self.assertIn("optimize: 1 runs", proc.stdout)

    def test_invalid_machine_setting_is_refused(self):
        proc = self._run("machine", "--max-parallel", "0")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("must be > 0", proc.stderr)

    def test_schema_lists_columns_and_goals(self):
        out = self._run("schema").stdout
        self.assertIn("peak_rss_mb", out)
        self.assertIn("goals: objective_J↓  distance_to_optimum↓", out)

    @unittest.skipIf(os.name == "nt", "peak memory is POSIX-only")
    def test_peak_memory_is_recorded_and_brief_suggests_a_batch_size(self):
        (self.demo / "config.json").write_text(json.dumps({"adapter": "toy", "plan_minutes": 10}))
        printed = self._json("--problem", "sphere", "--maxiter", "100")
        record = json.loads((self.demo / "runs" / f"{printed['id']}.json").read_text())
        self.assertGreater(record["peak_rss_mb"], 0)
        brief = self._run("brief").stdout
        self.assertIn("machine: 4 run slots", brief)
        self.assertIn("batch ≤", brief)



class TestGenericSchema(_ScratchDirTest):
    """Metrics live in JSON; the results view gives each declared metric a column."""

    def test_legacy_record_is_upgraded(self):
        old = {"id": "r", "coil_type": "banana", "solver": "stage2", "equilibrium": "nfp5",
               "field_error": 0.01, "objective_J": None, "metrics": {"lead_end_curvature": 3.0},
               "params": {"solver": "stage2", "cc_weight": 10}, "status": "pass"}
        new = run.upgrade_record(old)
        self.assertEqual((new["adapter"], new["mode"], new["target"]), ("banana", "stage2", "nfp5"))
        self.assertEqual(new["metrics"], {"field_error": 0.01, "lead_end_curvature": 3.0})
        self.assertEqual(new["params"], {"mode": "stage2", "cc_weight": 10})
        self.assertNotIn("field_error", new)
        self.assertEqual(run.upgrade_record(new), new, "current records pass through unchanged")

    def test_results_view_has_one_column_per_metric(self):
        layout = self._layout()
        run.write_run_record(layout.runs_dir, {
            "id": "r1", "adapter": "toy", "mode": "optimize", "target": "sphere", "status": "pass",
            "created_at": "2026-01-01", "metrics": {"objective_J": 0.25, "optimizer_success": True},
        })
        run.rebuild(layout)
        out = run.query(layout, toy, "SELECT objective_J, optimizer_success, final_step FROM results", 5)
        self.assertEqual(out.splitlines(), ["objective_J\toptimizer_success\tfinal_step", "0.25\t1\t"])
        with contextlib.closing(sqlite3.connect(layout.db_path)) as db:
            indexes = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertTrue({"idx_metric_objective_J", "idx_metric_distance_to_optimum"} <= indexes)

    def test_metric_names_must_be_identifiers(self):
        bad = types.SimpleNamespace(**{m: getattr(toy, m) for m in adapter.CONTRACT_MEMBERS})
        bad.METRICS = {"field error": "min"}
        with self.assertRaisesRegex(adapter.AdapterError, "snake_case identifiers"):
            adapter.load_adapter("bad", {"bad": bad})

    def test_metric_named_like_a_run_column_is_refused(self):
        layout = self._layout()
        clash = types.SimpleNamespace(NAME="clash", METRICS={"status": None})
        with self.assertRaisesRegex(run.HarnessError, "reuse run column names"):
            run.ensure_results_view(layout, clash)


if __name__ == "__main__":
    unittest.main()
