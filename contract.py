"""Shared harness↔adapter contract.

The harness core (`run.py`) is solver-agnostic: it owns campaign selection, the
run records, the scratch/evidence lifecycle, and the agent-facing CLI skeleton.
Everything solver-specific — how to invoke a solver, what its outputs mean,
how to validate a result — lives behind a *solver adapter*, registered in
`adapters/__init__.py` and named by the campaign's `config.json`. This module
is the contract both sides import; it has no project dependencies so neither
side imports the other.

An adapter is a module exposing:

    NAME              str          — identifies the solver family; stored in the
                                     `coil_type` column.
    SOLVER_MODES      tuple[str]   — the `--solver` choices; SOLVER_MODES[0] is
                                     the default.
    TARGET_FLAG       str          — argparse dest of the flag naming the target
                                     configuration (e.g. "equilibrium"); stored
                                     in the `equilibrium` column. Give it a
                                     default so it always has a value.
    REQUIRED_ENV      tuple[str]   — env vars that must be set before a run; the
                                     core checks them and refuses to start
                                     without them.
    OPTIONAL_ENV      tuple[str]   — env vars read when present (informational).
    EXECUTION_FLAGS   tuple[str]   — argparse dests that change how a run
                                     executes but not what it computes (timeout,
                                     thread count, solver location). Recorded,
                                     but excluded from the spec hash.
    SEED_FLAG         str | None   — argparse dest of the solver's RNG seed
                                     flag (default None), or None for a
                                     deterministic solver. When the agent leaves
                                     it unset, the core derives a seed from the
                                     spec and `--replicate`.
    REPLAY_TOLERANCE  float        — largest relative difference per numeric
                                     metric for which `run.py replay` reports a
                                     match.
    METRICS           Mapping[str, str | None]
                                   — every canonical metric key the adapter
                                     emits → its goal: "min", "max", or None
                                     (recorded, not optimized). Goals drive the
                                     Pareto front in `run.py brief`.
    add_arguments(parser) -> None  — register the solver's CLI flags.
    solver_identity(args) -> str   — fingerprint of the solver code that will
                                     run (e.g. commit + uncommitted-diff hash).
                                     Part of the spec hash, so a changed solver
                                     is never mistaken for an earlier run.
    run_experiment(args, run: RunContext) -> ExperimentOutcome
                                   — run ONE experiment end-to-end in run.dir.
                                     Must not raise for solver failures — return
                                     an outcome with status "crash"/"fail".

Adapters read environment variables when a run starts, never at import:
every registered adapter is imported on every run, including campaigns that
use a different one.

A *canonical metric key* is a snake_case name an adapter emits in
`ExperimentOutcome.metrics`. Keys that `run.py` projects into dedicated DB
columns get their own column; every other key is preserved in the row's
`metrics` JSON blob.
"""

from __future__ import annotations

import hashlib
import math
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

METRIC_GOALS = ("min", "max", None)


@dataclass(frozen=True)
class RunContext:
    """Identity and scratch directory of the run an adapter is executing.

    run_id: the run's id, final before the solver starts; use it to name
        anything this run produces for later runs (e.g. an archived seed), so
        their records can point back to this one as their parent.
    dir: empty scratch directory for this run's solver outputs.
    """

    run_id: str
    dir: Path


@dataclass(frozen=True)
class ExperimentOutcome:
    """The result of one experiment, in solver-agnostic terms.

    status: "pass" | "fail" | "crash". "crash" — no evaluable metrics produced
        (timeout, solver error, missing output). "fail" — ran but violated a
        gate (self-intersection, missing required metric, optimizer reported
        failure). "pass" — produced a complete, gate-passing result.
    status_reason: short machine-readable tag, e.g. "ok", "timeout".
    metrics: canonical-key → value. NaN/Inf are cleaned by the core, so adapters
        may pass raw solver floats.
    validated: independent-validation verdict — "pass"/"fail" when the check
        ran and judged, "error" when it was attempted but could not judge,
        None when it was not attempted. Put the measured quantity behind the
        verdict (e.g. a survival fraction) in `metrics`.
    experiment_group: groups DB rows of one logical multi-step experiment;
        None when one experiment is one row.
    provenance: what the run actually used, as JSON-able values — the exact
        solver command(s), solver commit, interpreter, hashes of input files.
    evidence: name → file under run.dir worth keeping whatever the artifact
        policy (results file, solver patch, log). The core stores each one by
        content hash in the campaign and records the hashes. Name the solver's
        combined output "log": the core derives a crash signature from it.
    parent_run_id: the earlier run this one built on (e.g. the run that
        produced its warm-start seed), when known.
    """

    status: str
    status_reason: str
    metrics: Mapping[str, object] = field(default_factory=dict)
    validated: str | None = None
    experiment_group: str | None = None
    provenance: Mapping[str, object] = field(default_factory=dict)
    evidence: Mapping[str, Path] = field(default_factory=dict)
    parent_run_id: str | None = None


def clean(v: object) -> object:
    """Map NaN/Inf floats to None for JSON and SQLite safety; pass else through."""
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def sha256_file(path: Path) -> str:
    """Hex SHA-256 of a file's contents, read in chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_output(root: Path, *git_args: str) -> str | None:
    """Stdout of `git -C root <git_args>`, or None when git fails (not a checkout, no git)."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *git_args], capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None
