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
                                     `adapter` column.
    MODES             tuple[str]   — the `--mode` choices; MODES[0] is the
                                     default (e.g. a cheap screen and a costly
                                     full solve).
    TARGET_FLAG       str          — argparse dest of the flag naming the target
                                     configuration (e.g. "problem", "case");
                                     stored in the `target` column. Give it a
                                     default so it always has a value.
    REQUIRED_ENV      tuple[str]   — env vars that must be set before a run; the
                                     core checks them and refuses to start
                                     without them.
    OPTIONAL_ENV      tuple[str]   — env vars read when present (informational).
    EXECUTION_FLAGS   tuple[str]   — argparse dests that change how a run
                                     executes but not what it computes (timeout,
                                     thread count, solver location). Recorded,
                                     but excluded from the spec hash.
    THREADS_FLAG      str | None   — argparse dest of the flag setting threads
                                     per run (e.g. "omp_threads"), or None for
                                     a single-threaded solver. Used to size how
                                     many runs fit on the machine at once.
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
                                     is never mistaken for an earlier run. It
                                     may depend only on EXECUTION_FLAGS values
                                     and the environment (the core caches it).
    run_experiment(args, run: RunContext) -> ExperimentOutcome
                                   — run ONE experiment end-to-end in run.dir.
                                     Must not raise for solver failures — return
                                     an outcome with status "crash"/"fail".
                                     Launch solvers with `run_solver`, so a
                                     timeout or cancellation kills the whole
                                     process tree.

Adapters read environment variables when a run starts, never at import:
every registered adapter is imported on every run, including campaigns that
use a different one.

Metric keys are snake_case names declared in METRICS. The core stores every
emitted metric in the run's `metrics` JSON; the `results` view gives each
declared key its own column for SQL.
"""

from __future__ import annotations

import hashlib
import math
import os
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

METRIC_GOALS = ("min", "max", None)

# Thread-count variables read by OpenMP and the common math libraries; set all
# of them, or parallel runs oversubscribe the CPU through whichever one is unset.
THREAD_ENV_VARS = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "BLIS_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "NUMBA_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
)


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


class Cancelled(BaseException):
    """The run was asked to stop (SIGTERM). A BaseException so broad `except Exception` handlers let it through."""


def thread_env(threads: int) -> dict[str, str]:
    """Environment entries limiting every common math library to `threads` threads."""
    return {name: str(threads) for name in THREAD_ENV_VARS}


def _new_process_group() -> dict:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _kill_process_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    proc.wait()


def run_solver(
    cmd: list[str],
    log_path: Path,
    timeout: float | None,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> int | None:
    """Run `cmd` with stdout+stderr to `log_path`; its exit code, or None on timeout.

    The command runs in its own process group. On timeout, or if the run is
    interrupted (Cancelled, KeyboardInterrupt), the whole group — the solver
    and anything it spawned — is killed before returning or re-raising.
    """
    with open(log_path, "w") as log:
        proc = subprocess.Popen(
            cmd, stdout=log, stderr=subprocess.STDOUT, env=env, cwd=cwd, **_new_process_group()
        )
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_tree(proc)
            return None
        except BaseException:
            _kill_process_tree(proc)
            raise
