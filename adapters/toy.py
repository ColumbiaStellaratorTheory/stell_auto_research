"""Toy solver adapter — the reference implementation of the harness contract.

Implements contract.py for `examples/toy_solver.py`, a stdlib-only optimizer
on classic test functions (sphere, rosenbrock, rastrigin). It needs no physics
stack, so it runs on any machine and serves as the test fixture and as the
template `/setup-harness` copies when generating an adapter for a real solver.

The shape to copy: register the solver's flags, fingerprint the solver code,
run it with `contract.run_solver` in the run directory with a timeout, parse its results
file, map native keys onto canonical metric keys, report what the run used
(provenance) and what to keep (evidence), and classify the run — never raising
for a solver failure. `examples/banana/simsopt_banana.py` shows the same
contract for a real two-mode physics solver with a warm-start seed store and
validation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

from contract import ExperimentOutcome, RunContext, run_solver

# --- Contract surface -------------------------------------------------------

NAME = "toy"
SOLVER_MODES = ("optimize",)
TARGET_FLAG = "problem"
REQUIRED_ENV = ()
OPTIONAL_ENV = ()
EXECUTION_FLAGS = ("timeout",)
SEED_FLAG = "seed"
# The toy solver is pure Python on one thread: a replay must reproduce the
# original exactly, up to float printing.
REPLAY_TOLERANCE = 1e-12
METRICS = {
    "objective_J": "min",
    "distance_to_optimum": "min",
    "iterations": None,
    "optimizer_success": None,
    "final_step": None,
}

SOLVER_SCRIPT = Path(__file__).resolve().parents[1] / "examples" / "toy_solver.py"


# --- CLI flags --------------------------------------------------------------

def add_arguments(p: argparse.ArgumentParser) -> None:
    """Register the toy solver's CLI flags on the harness parser."""
    p.add_argument("--problem", choices=("sphere", "rosenbrock", "rastrigin"), default="rosenbrock")
    p.add_argument("--dim", type=int, default=2)
    p.add_argument("--maxiter", type=int, default=2000)
    p.add_argument("--step-size", type=float, default=0.1)
    p.add_argument("--seed", type=int, default=None, help="RNG seed (default: derived by the harness)")
    p.add_argument("--noise", type=float, default=0.0, help="std dev of evaluation noise the search sees")
    p.add_argument(
        "--inject",
        choices=("none", "crash", "hang", "nan"),
        default="none",
        help="deliberate failure, for exercising the harness",
    )
    p.add_argument("--timeout", type=int, default=60)


def solver_identity(args: argparse.Namespace) -> str:
    """Content hash of the toy solver script."""
    return hashlib.sha256(SOLVER_SCRIPT.read_bytes()).hexdigest()[:16]


# --- Experiment entry point -------------------------------------------------

def run_experiment(args: argparse.Namespace, run: RunContext) -> ExperimentOutcome:
    """Run one toy optimization in run.dir; return its outcome."""
    results_path = run.dir / "results.json"
    log_path = run.dir / "run.log"
    cmd = [
        sys.executable, str(SOLVER_SCRIPT),
        "--problem", args.problem,
        "--dim", str(args.dim),
        "--maxiter", str(args.maxiter),
        "--step-size", str(args.step_size),
        "--seed", str(args.seed),
        "--noise", str(args.noise),
        "--inject", args.inject,
        "--output", str(results_path),
    ]
    provenance = {"command": cmd, "solver_identity": solver_identity(args)}
    evidence = {"log": log_path}
    exit_code = run_solver(cmd, log_path, args.timeout)
    if exit_code is None:
        return ExperimentOutcome("crash", "timeout", provenance=provenance, evidence=evidence)
    if exit_code != 0:
        return ExperimentOutcome("crash", f"exit_{exit_code}", provenance=provenance, evidence=evidence)
    try:
        raw = json.loads(results_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return ExperimentOutcome(
            "crash", f"bad_results_json: {e}", provenance=provenance, evidence=evidence
        )

    evidence = {**evidence, "results": results_path}
    metrics = {
        "iterations": raw.get("evaluations"),
        "optimizer_success": raw.get("converged"),
        "objective_J": raw.get("objective"),
        # no dedicated column → preserved in the metrics JSON overflow
        "distance_to_optimum": raw.get("distance_to_optimum"),
        "final_step": raw.get("final_step"),
    }
    objective = metrics["objective_J"]
    if not isinstance(objective, float) or not math.isfinite(objective):
        return ExperimentOutcome(
            "fail", "incomplete_metrics", metrics=metrics, provenance=provenance, evidence=evidence
        )
    return ExperimentOutcome("pass", "ok", metrics=metrics, provenance=provenance, evidence=evidence)
