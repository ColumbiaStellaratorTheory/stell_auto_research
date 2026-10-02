"""Unit tests for the banana example adapter (no simsopt needed).

Run from the repo root with:  python3 -m unittest discover -s examples -t .

The adapter reads its configuration at import time, so the required env vars
and the import-time override cases are set once in setUpModule before the
single import.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

_IMPORT_ENV = {
    # Required by the banana adapter at import.
    "SIMSOPT_ROOT": tempfile.gettempdir(),
    "SIMSOPT_PYTHON": "/usr/bin/python3",
    "EQUILIBRIA_DIR": tempfile.gettempdir(),
    # Import-time override cases under test.
    "STAGE2_SCRIPT": "custom/stage2.py",
    "STAGE2_SEED_DIR": "/tmp/test_seed_dir",
}


def setUpModule() -> None:
    os.environ.update(_IMPORT_ENV)
    global banana
    from examples.banana import simsopt_banana as banana  # noqa: PLC0415 — import must follow env setup


class TestContractSurface(unittest.TestCase):
    """The adapter loads through the harness loader and names its target flag."""

    def test_loads_through_the_harness_loader(self):
        import adapter  # noqa: PLC0415

        self.assertIs(adapter.load_adapter("examples.banana.simsopt_banana"), banana)
        self.assertEqual(banana.TARGET_FLAG, "equilibrium")


class TestAdapterEnvOverrides(unittest.TestCase):
    """Env vars override the adapter's script/seed paths at import time."""

    def test_stage2_script_override(self):
        self.assertEqual(banana.SCRIPTS["stage2"], "custom/stage2.py")

    def test_single_stage_script_default(self):
        self.assertEqual(
            banana.SCRIPTS["single-stage"],
            "examples/single_stage_optimization/SINGLE_STAGE/single_stage_banana_example.py",
        )

    def test_seed_store_override(self):
        self.assertEqual(banana.STAGE2_SEED_STORE, Path("/tmp/test_seed_dir"))


class TestClassify(unittest.TestCase):
    """The adapter classifies canonical metrics into pass/fail."""

    def test_self_intersecting_fails(self):
        status, reason = banana._classify({"self_intersecting": True}, "stage2")
        self.assertEqual((status, reason), ("fail", "self_intersecting"))

    def test_missing_required_metric_fails(self):
        status, reason = banana._classify({"field_error": 0.01}, "stage2")
        self.assertEqual((status, reason), ("fail", "incomplete_metrics"))

    def test_complete_stage2_passes(self):
        status, reason = banana._classify({"field_error": 0.01, "max_curvature": 40.0}, "stage2")
        self.assertEqual((status, reason), ("pass", "ok"))

    def test_single_stage_requires_iota_and_volume(self):
        status, _ = banana._classify({"field_error": 0.01, "max_curvature": 40.0}, "single-stage")
        self.assertEqual(status, "fail")


if __name__ == "__main__":
    unittest.main()
