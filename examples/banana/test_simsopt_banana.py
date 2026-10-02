"""Unit tests for the banana example adapter (no simsopt needed).

Run from the repo root with:  python3 -m unittest discover -s examples -t .
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import adapter
from contract import RunContext
from examples.banana import simsopt_banana as banana

_REQUIRED = {"SIMSOPT_ROOT": "/solver", "SIMSOPT_PYTHON": "/py", "EQUILIBRIA_DIR": "/eq"}


class TestContractSurface(unittest.TestCase):
    """The adapter is registered and imports without any environment."""

    def test_registered_and_complete(self):
        self.assertIs(adapter.load_adapter("simsopt_banana"), banana)
        self.assertEqual((banana.TARGET_FLAG, banana.SEED_FLAG), ("equilibrium", "basin_seed"))

    def test_required_env_is_declared_not_read_at_import(self):
        self.assertEqual(set(banana.REQUIRED_ENV), set(_REQUIRED))


class TestConfigFromEnv(unittest.TestCase):
    """Configuration is read from the environment when a run starts."""

    def test_required_values(self):
        config = banana.config_from_env(_REQUIRED)
        self.assertEqual(
            (config.solver_root, config.solver_python, config.equilibria_dir),
            (Path("/solver"), "/py", Path("/eq")),
        )

    def test_script_override_and_defaults(self):
        config = banana.config_from_env({**_REQUIRED, "STAGE2_SCRIPT": "custom/stage2.py"})
        self.assertEqual(config.scripts["stage2"], "custom/stage2.py")
        self.assertEqual(config.scripts["single-stage"], banana.DEFAULT_SCRIPTS["single-stage"])

    def test_seed_store_override_and_default(self):
        self.assertEqual(banana.config_from_env({**_REQUIRED, "STAGE2_SEED_DIR": "/seeds"}).seed_store, Path("/seeds"))
        self.assertEqual(banana.config_from_env(_REQUIRED).seed_store, banana.DEFAULT_SEED_STORE)


class TestSeedLineage(unittest.TestCase):
    """Stage 2 archives seeds under its run id; single-stage finds them and their parent."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.store = self.root / "seeds"

    def _stage2_run(self, run_id: str, field_error: float) -> None:
        run_dir = self.root / "scratch" / run_id / "out"
        run_dir.mkdir(parents=True)
        (run_dir / "biot_savart_opt.json").write_text("{}")
        (run_dir / "results.json").write_text(json.dumps(
            {"MAJOR_RADIUS": 0.915, "order": 2, "SELF_INTERSECTING": False, "FIELD_ERROR": field_error}
        ))
        banana._archive_stage2_seed(RunContext(run_id, run_dir.parent), "wout_x.nc", self.store)

    def _args(self) -> argparse.Namespace:
        return argparse.Namespace(stage2_bs_path=None, equilibrium="x", major_radius=0.915, order=2)

    def test_best_seed_is_chosen_and_names_its_parent_run(self):
        self._stage2_run("run-a", 0.05)
        self._stage2_run("run-b", 0.01)
        seed = banana._resolve_stage2_seed(self._args(), "wout_x.nc", self.store)
        self.assertEqual(Path(seed).parent.name, "run-b")
        self.assertEqual(banana._seed_parent_run(Path(seed)), "run-b")

    def test_tie_goes_to_the_first_seed_by_name(self):
        self._stage2_run("run-z", 0.02)
        self._stage2_run("run-a", 0.02)
        seed = banana._resolve_stage2_seed(self._args(), "wout_x.nc", self.store)
        self.assertEqual(Path(seed).parent.name, "run-a")

    def test_legacy_seed_without_origin_has_no_parent(self):
        legacy = self.root / "legacy"
        legacy.mkdir()
        (legacy / "biot_savart_opt.json").write_text("{}")
        self.assertIsNone(banana._seed_parent_run(legacy / "biot_savart_opt.json"))


class TestPoincare(unittest.TestCase):
    """A check that cannot run reports "error", never a pass/fail verdict."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_missing_script_is_an_error(self):
        self.assertEqual(banana._run_poincare(self.root, "/py", self.root / "absent.py"), ("error", None))

    def test_missing_coils_is_an_error(self):
        script = self.root / "poincare.py"
        script.write_text("")
        self.assertEqual(banana._run_poincare(self.root, "/py", script), ("error", None))


class TestClassify(unittest.TestCase):
    """The adapter classifies canonical metrics into pass/fail."""

    def test_self_intersecting_fails(self):
        self.assertEqual(banana._classify({"self_intersecting": True}, "stage2"), ("fail", "self_intersecting"))

    def test_missing_required_metric_fails(self):
        self.assertEqual(banana._classify({"field_error": 0.01}, "stage2"), ("fail", "incomplete_metrics"))

    def test_complete_stage2_passes(self):
        self.assertEqual(
            banana._classify({"field_error": 0.01, "max_curvature": 40.0}, "stage2"), ("pass", "ok")
        )

    def test_single_stage_requires_iota_and_volume(self):
        status, _ = banana._classify({"field_error": 0.01, "max_curvature": 40.0}, "single-stage")
        self.assertEqual(status, "fail")


if __name__ == "__main__":
    unittest.main()
