"""Tests for machine.py: sizing formulas, measured run cost, settings, detection.

Run from the repo root with:  python3 -m unittest discover -s tests -t .
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import machine


def _run(mode: str, elapsed: float, peak: float | None, status: str = "pass", threads: int | None = None) -> dict:
    params = {"omp_threads": threads} if threads is not None else {}
    return {"mode": mode, "status": status, "elapsed": elapsed, "peak_rss_mb": peak, "params": params}


class TestSizing(unittest.TestCase):
    """Runs at once are limited by cores per run and memory per run; batches by the planning interval."""

    def test_cores_limit(self):
        self.assertEqual(machine.runs_at_once(64, 10, None, None), 6)

    def test_memory_limit(self):
        self.assertEqual(machine.runs_at_once(64, 1, 100.0, 30.0), 3)

    def test_at_least_one(self):
        self.assertEqual(machine.runs_at_once(4, 16, 8.0, 20.0), 1)

    def test_batch_size_fills_the_planning_interval(self):
        self.assertEqual(machine.batch_size(6, 30, 30.0), 360)
        self.assertEqual(machine.batch_size(2, 120, 1200.0), 12)


class TestModeCosts(unittest.TestCase):
    """Per-mode cost uses finished runs; crashes count only when nothing else exists."""

    def test_median_time_max_memory_common_threads(self):
        runs = [
            _run("screen", 30, 1000, threads=10), _run("screen", 40, 2048, threads=10),
            _run("screen", 2, 50, status="crash", threads=10), _run("full", 900, 4096, threads=4),
        ]
        costs = {c.mode: c for c in machine.mode_costs(runs, "omp_threads")}
        self.assertEqual((costs["screen"].runs, costs["screen"].median_seconds, costs["screen"].threads), (2, 35, 10))
        self.assertAlmostEqual(costs["screen"].peak_memory_gb, 2.0)
        self.assertEqual(costs["full"].threads, 4)

    def test_single_threaded_adapter_and_unknown_memory(self):
        cost = machine.mode_costs([_run("m", 5, None)], None)[0]
        self.assertEqual((cost.threads, cost.peak_memory_gb), (1, None))

    def test_only_crashes_still_give_a_cost(self):
        self.assertEqual(machine.mode_costs([_run("m", 3, 10, status="crash")], None)[0].runs, 1)


class TestSettingsAndDetection(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_settings_merge_and_ignore_unset(self):
        machine.write_settings(self.root, {"max_parallel": 4, "usable_cores": None})
        machine.write_settings(self.root, {"usable_cores": 32})
        self.assertEqual(machine.read_settings(self.root), {"max_parallel": 4, "usable_cores": 32})

    def test_missing_settings_are_empty(self):
        self.assertEqual(machine.read_settings(self.root / "absent"), {})

    def test_slurm_allocation_caps_usable_cpus(self):
        self.assertEqual(machine.usable_cpus({"SLURM_CPUS_PER_TASK": "1"}), 1)

    def test_detection_reports_basics(self):
        hw = machine.detect({})
        self.assertGreaterEqual(hw.usable_cpus, 1)
        self.assertTrue(all(":" in line for line in machine.describe_hardware(hw)))

    @unittest.skipIf(os.name == "nt", "resource module is POSIX-only")
    def test_children_peak_memory_is_measured(self):
        subprocess.run([sys.executable, "-c", "x = bytearray(50 * 2**20)"], check=True)
        self.assertGreater(machine.children_peak_rss_mb(), 40)


if __name__ == "__main__":
    unittest.main()
