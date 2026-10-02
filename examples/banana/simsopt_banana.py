"""simsopt banana-coil solver adapter — a real-world example adapter.

Implements the harness↔adapter contract (see contract.py) for the banana-coil
campaign on a simsopt fork. Two solver modes:

  - "stage2"       optimize coil geometry against a fixed plasma surface to
                   minimize field error (fast; also archives a seed).
  - "single-stage" jointly optimize coils + a Boozer surface for quasi-symmetry
                   (slow; warm-starts from an archived stage2 seed, then runs
                   Poincaré validation).

Each mode is one subprocess against the fork's solver scripts. Unlike the toy
reference adapter (adapters/toy.py) it shows multiple modes, a warm-start seed
store shared between them, and independent validation. A campaign selects it
with `"adapter": "simsopt_banana"` in its config.json.

Configuration is read from the environment when a run starts (the core checks
REQUIRED_ENV first). Script paths default to the fork's standard layout and may
be overridden per fork via STAGE2_SCRIPT / SINGLE_STAGE_SCRIPT / POINCARE_SCRIPT.

Stage 2 runs archive their coils into a seed store under a directory named by
their run id; a single-stage run records the seed it warm-started from (path +
content hash) and that stage2 run as its parent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from contract import ExperimentOutcome, RunContext, git_output, run_solver, sha256_file, thread_env

# --- Contract surface -------------------------------------------------------

NAME = "banana"
SOLVER_MODES = ("stage2", "single-stage")
TARGET_FLAG = "equilibrium"
REQUIRED_ENV = ("SIMSOPT_ROOT", "SIMSOPT_PYTHON", "EQUILIBRIA_DIR")
OPTIONAL_ENV = ("STAGE2_SCRIPT", "SINGLE_STAGE_SCRIPT", "POINCARE_SCRIPT", "STAGE2_SEED_DIR")
EXECUTION_FLAGS = ("timeout", "omp_threads", "solver_root", "solver_python")
SEED_FLAG = "basin_seed"
THREADS_FLAG = "omp_threads"
# Starting value, not measured: OpenMP reductions make L-BFGS runs differ in the
# last digits between repeats. Calibrate it by replaying a few runs.
REPLAY_TOLERANCE = 1e-6
# Goals follow program.md's "Physics Goals" (lower is better); the rest are recorded.
METRICS = {
    "field_error": "min",
    "qs_error": "min",
    "boozer_residual": "min",
    "max_curvature": "min",
    "iterations": None,
    "optimizer_success": None,
    "termination_message": None,
    "iota_actual": None,
    "volume_actual": None,
    "coil_length": None,
    "coil_coil_dist": None,
    "coil_surface_dist": None,
    "surface_vessel_dist": None,
    "max_force": None,
    "self_intersecting": None,
    "objective_J": None,
    "lead_end_curvature": None,
    "non_lead_end_curvature": None,
    "poincare_uniformity": None,
}

# Solver script defaults, relative to the solver root (the fork's standard layout).
DEFAULT_SCRIPTS = {
    "stage2": "examples/single_stage_optimization/STAGE_2/banana_coil_solver.py",
    "single-stage": "examples/single_stage_optimization/SINGLE_STAGE/single_stage_banana_example.py",
    "poincare": "examples/single_stage_optimization/POINCARE_PLOTTING/poincare_surfaces.py",
}
SCRIPT_ENV = {"stage2": "STAGE2_SCRIPT", "single-stage": "SINGLE_STAGE_SCRIPT", "poincare": "POINCARE_SCRIPT"}
DEFAULT_SEED_STORE = Path(__file__).resolve().parents[2] / "stage2_seeds"
SEED_ORIGIN_FILE = "origin.json"


@dataclass(frozen=True)
class BananaConfig:
    """Adapter configuration, read from the environment when a run starts.

    scripts: mode ("stage2" / "single-stage" / "poincare") → script path
        relative to the solver root, overridable per fork via SCRIPT_ENV.
    seed_store: Stage 2 seed archive that single-stage warm-starts from.
    """

    equilibria_dir: Path
    solver_root: Path
    solver_python: str
    scripts: Mapping[str, str]
    seed_store: Path


def config_from_env(environ: Mapping[str, str]) -> BananaConfig:
    return BananaConfig(
        equilibria_dir=Path(environ["EQUILIBRIA_DIR"]),
        solver_root=Path(environ["SIMSOPT_ROOT"]),
        solver_python=environ["SIMSOPT_PYTHON"],
        scripts={mode: environ.get(SCRIPT_ENV[mode], path) for mode, path in DEFAULT_SCRIPTS.items()},
        seed_store=Path(environ.get("STAGE2_SEED_DIR", str(DEFAULT_SEED_STORE))),
    )

# Single-stage runs Poincaré validation only when the field error clears this
# bar; tighter than the survival bar so validation isn't wasted on bad fits.
POINCARE_FIELD_ERROR_THRESHOLD = 0.1
POINCARE_SURVIVAL_THRESHOLD = 0.9
POINCARE_TIMEOUT_SECONDS = 600
POINCARE_LOG = "poincare.log"

# --- Equilibrium registry: nfp{N}_iota{XX} -> wout filename -----------------

EQUILIBRIUM_FILES: dict[str, str] = {}
for _nfp in (5, 10, 15):
    for _iota_int in range(10, 51):
        _key = f"nfp{_nfp}_iota{_iota_int}"
        EQUILIBRIUM_FILES[_key] = f"wout_nfp{_nfp}ginsburg_desc_iota{_iota_int:02d}.nc"

# Legacy aliases (NFP=5 only)
EQUILIBRIUM_FILES.update({
    "iota15": "wout_nfp5ginsburg_000_014417_iota15.nc",
    "iota20": "wout_nfp5ginsburg_000_002084_iota20.nc",
    "iota15p": "wout_nfp5ginsburg_desc_iota15.nc",
    "iota20p": "wout_nfp5ginsburg_desc_iota20.nc",
    "001490": "wout_nfp5ginsburg_000_001490.nc",
})
for _i in range(15, 31):
    _legacy = f"iota{_i}"
    if _legacy not in EQUILIBRIUM_FILES:
        _mapped = EQUILIBRIUM_FILES.get(f"nfp5_iota{_i}")
        if _mapped:
            EQUILIBRIUM_FILES[_legacy] = _mapped


# --- CLI flags --------------------------------------------------------------

def add_arguments(p: argparse.ArgumentParser) -> None:
    """Register the banana solver's CLI flags on the harness parser."""
    p.add_argument("--equilibrium", default="nfp5_iota15")

    # Shared across modes
    p.add_argument("--cc-weight", type=float, default=100.0)
    p.add_argument("--cc-threshold", type=float, default=0.05)
    p.add_argument("--curvature-weight", type=float, default=0.1)
    p.add_argument("--curvature-threshold", type=float, default=40.0)
    p.add_argument("--banana-surf-radius", type=float, default=0.22)
    p.add_argument("--major-radius", type=float, default=0.915)
    p.add_argument("--toroidal-flux", type=float, default=0.215)
    p.add_argument("--order", type=int, default=2)
    p.add_argument("--maxiter", type=int, default=400)
    p.add_argument("--nphi", type=int, default=255)
    p.add_argument("--ntheta", type=int, default=64)

    # Stage 2 only
    p.add_argument("--length-weight", type=float, default=1.0)
    p.add_argument("--length-target", type=float, default=1.75)
    p.add_argument("--squared-flux-weight", type=float, default=1.0)
    p.add_argument("--curvature-p-norm", type=int, default=4)
    p.add_argument("--num-quadpoints", type=int, default=128)
    p.add_argument("--basin-hops", type=int, default=0)
    p.add_argument("--basin-stepsize", type=float, default=0.01)
    p.add_argument("--basin-seed", type=int, default=None, help="basin-hopping RNG seed (default: derived by the harness)")

    # Single-stage only
    p.add_argument("--iota-target", type=float, default=0.15)
    p.add_argument("--vol-target", type=float, default=0.10)
    p.add_argument("--mpol", type=int, default=8)
    p.add_argument("--ntor", type=int, default=6)
    p.add_argument("--constraint-weight", type=float, default=1.0)
    p.add_argument("--cc-dist", type=float, default=0.05)
    p.add_argument("--res-weight", type=float, default=1000.0)
    p.add_argument("--iotas-weight", type=float, default=100.0)
    p.add_argument("--cs-weight", type=float, default=1.0)
    p.add_argument("--cs-dist", type=float, default=0.02)
    p.add_argument("--surf-dist-weight", type=float, default=1000.0)
    p.add_argument("--ss-dist", type=float, default=0.04)
    p.add_argument("--ss-length-weight", type=float, default=1.0)
    p.add_argument("--num-tf-coils", type=int, default=20)
    p.add_argument("--maxcor", type=int, default=300)
    p.add_argument("--boozer-stage", choices=["initial", "final"], default="initial")
    p.add_argument("--stage2-bs-path", type=str, default=None)

    # Solver location overrides (per fork)
    p.add_argument("--solver-root", type=str, default=None)
    p.add_argument("--solver-python", type=str, default=None)

    # Execution
    p.add_argument("--omp-threads", type=int, default=10)
    p.add_argument("--timeout", type=int, default=600)


# --- Equilibrium + seed resolution ------------------------------------------

def _resolve_equilibrium(eq_key: str, equilibria_dir: Path) -> str:
    """Map an equilibrium key (registry alias or raw wout filename) to a wout
    filename present in equilibria_dir. Raises KeyError if neither resolves."""
    filename = EQUILIBRIUM_FILES.get(eq_key)
    if filename:
        return filename
    if (equilibria_dir / eq_key).exists():
        return eq_key
    raise KeyError(eq_key)


def _seed_parent_run(bs_file: Path) -> str | None:
    """Run id of the stage2 run that archived this seed, if it recorded one."""
    origin = bs_file.parent / SEED_ORIGIN_FILE
    try:
        return json.loads(origin.read_text()).get("run_id")
    except (OSError, json.JSONDecodeError):
        return None


def _resolve_stage2_seed(args: argparse.Namespace, plasma_surf: str, seed_store: Path) -> str | None:
    """Find the best Stage 2 seed matching equilibrium + geometry, or None.

    Ties on field error go to the first seed directory by name, so the choice
    does not depend on filesystem listing order.
    """
    if args.stage2_bs_path:
        if Path(args.stage2_bs_path).is_file():
            return args.stage2_bs_path
        print(f"ERROR: seed not found: {args.stage2_bs_path}", file=sys.stderr)
        return None

    seeds_parent = seed_store / f"outputs-{plasma_surf}"
    if not seeds_parent.is_dir():
        print(f"No seeds for {args.equilibrium}. Run Stage 2 first.", file=sys.stderr)
        return None

    best_seed = None
    best_fe = float("inf")
    for seed_dir in sorted(seeds_parent.iterdir()):
        bs_file = seed_dir / "biot_savart_opt.json"
        results_file = seed_dir / "results.json"
        if not bs_file.is_file():
            continue
        if results_file.is_file():
            try:
                with open(results_file) as f:
                    meta = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            if (
                abs(meta.get("MAJOR_RADIUS", 0) - args.major_radius) < 0.001
                and meta.get("order", 0) == args.order
                and not meta.get("SELF_INTERSECTING", False)
            ):
                fe = meta.get("FIELD_ERROR", 999.0)
                if isinstance(fe, float) and math.isnan(fe):
                    fe = 999.0
                if fe < best_fe:
                    best_fe = fe
                    best_seed = str(bs_file)

    if best_seed:
        print(f"Seed: FE={best_fe:.6f} {best_seed}", file=sys.stderr)
        return best_seed

    print(
        f"No Stage 2 seed for eq={args.equilibrium} R={args.major_radius} order={args.order}. "
        f"Run Stage 2 first.",
        file=sys.stderr,
    )
    return None


# --- CLI building -----------------------------------------------------------

def _build_cli(
    args: argparse.Namespace, plasma_surf: str, seed: str | None, config: BananaConfig, run_dir: Path
) -> list[str]:
    """Build the solver subprocess args (incl. --output-root)."""
    common = [
        "--plasma-surf-filename", plasma_surf,
        "--equilibria-dir", str(config.equilibria_dir),
        "--nphi", str(args.nphi),
        "--ntheta", str(args.ntheta),
        "--maxiter", str(args.maxiter),
        "--cc-weight", str(args.cc_weight),
        "--curvature-weight", str(args.curvature_weight),
        "--curvature-threshold", str(args.curvature_threshold),
        "--banana-surf-radius", str(args.banana_surf_radius),
    ]

    if args.solver == "stage2":
        mode_args = [
            "--major-radius", str(args.major_radius),
            "--toroidal-flux", str(args.toroidal_flux),
            "--order", str(args.order),
            "--cc-threshold", str(args.cc_threshold),
            "--length-weight", str(args.length_weight),
            "--length-target", str(args.length_target),
            "--squared-flux-weight", str(args.squared_flux_weight),
            "--curvature-p-norm", str(args.curvature_p_norm),
            "--num-quadpoints", str(args.num_quadpoints),
            "--basin-hops", str(args.basin_hops),
            "--basin-stepsize", str(args.basin_stepsize),
            "--basin-seed", str(args.basin_seed),
        ]
        return common + mode_args + ["--output-root", str(run_dir)]

    mode_args = [
        "--stage2-bs-path", seed,
        "--cc-dist", str(args.cc_dist),
        "--iota-target", str(args.iota_target),
        "--vol-target", str(args.vol_target),
        "--mpol", str(args.mpol),
        "--ntor", str(args.ntor),
        "--constraint-weight", str(args.constraint_weight),
        "--boozer-stage", args.boozer_stage,
        "--num-tf-coils", str(args.num_tf_coils),
        "--length-weight", str(args.ss_length_weight),
        "--res-weight", str(args.res_weight),
        "--iotas-weight", str(args.iotas_weight),
        "--cs-weight", str(args.cs_weight),
        "--cs-dist", str(args.cs_dist),
        "--surf-dist-weight", str(args.surf_dist_weight),
        "--ss-dist", str(args.ss_dist),
        "--maxcor", str(args.maxcor),
    ]
    return common + mode_args + ["--output-root", str(run_dir)]


# --- Result interpretation --------------------------------------------------

def _map_metrics(raw: dict) -> dict:
    """Translate the solver's results.json (UPPERCASE keys) to canonical keys.

    Canonical keys that `run.py` backs with a column become columns; the rest
    (lead/non-lead curvature here) land in the row's `metrics` JSON blob.
    """
    return {
        "iterations": raw.get("iterations"),
        "optimizer_success": raw.get("OPTIMIZER_SUCCESS"),
        "termination_message": raw.get("TERMINATION_MESSAGE"),
        "field_error": raw.get("FIELD_ERROR"),
        "qs_error": raw.get("NONQS_RATIO"),
        "boozer_residual": raw.get("BOOZER_RESIDUAL"),
        "iota_actual": raw.get("FINAL_IOTA"),
        "volume_actual": raw.get("FINAL_VOLUME"),
        "max_curvature": raw.get("MAX_CURVATURE"),
        "coil_length": raw.get("COIL_LENGTH"),
        "coil_coil_dist": raw.get("CURVE_CURVE_MIN_DIST"),
        "coil_surface_dist": raw.get("CURVE_SURFACE_MIN_DIST"),
        "surface_vessel_dist": raw.get("SURFACE_VESSEL_MIN_DIST"),
        "max_force": raw.get("MAX_FORCE"),
        "self_intersecting": raw.get("SELF_INTERSECTING", False),
        "objective_J": raw.get("OBJECTIVE_J"),
        # banana-specific (no column → preserved in metrics JSON overflow)
        "lead_end_curvature": raw.get("LEAD_END_CURVATURE"),
        "non_lead_end_curvature": raw.get("NON_LEAD_END_CURVATURE"),
    }


def _classify(metrics: dict, mode: str) -> tuple[str, str]:
    """Classify a completed run from its canonical metrics. NaN counts as missing."""
    if metrics.get("self_intersecting", False):
        return "fail", "self_intersecting"
    required = ["field_error", "max_curvature"]
    if mode == "single-stage":
        required += ["iota_actual", "volume_actual"]
    missing = [m for m in required if _is_missing(metrics.get(m))]
    if missing:
        return "fail", "incomplete_metrics"
    if metrics.get("optimizer_success") is False:
        return "fail", "optimizer_unsuccessful"
    return "pass", "ok"


def _is_missing(v: object) -> bool:
    return v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v)))


# --- Poincaré validation ----------------------------------------------------

def _run_poincare(run_dir: Path, solver_python: str, poincare_script: Path) -> tuple[str, float | None]:
    """Trace field lines and judge confinement: ("pass" | "fail" | "error", uniformity).

    The Poincaré script prints phi hit counts to stdout. Field lines that exit
    the surface produce fewer hits; uniformity across phi slices (min/max)
    indicates confinement quality. "error" means the check could not judge
    (missing script or coils, timeout, crash, unparseable output).
    """
    if not poincare_script.exists():
        print(f"Poincare script not found: {poincare_script}", file=sys.stderr)
        return "error", None

    bs_files = sorted(run_dir.rglob("biot_savart_opt.json"))
    if not bs_files:
        print("No biot_savart_opt.json for Poincare", file=sys.stderr)
        return "error", None

    env = {**os.environ, "POINCARE_OUT_DIR": str(bs_files[0].parent)}
    log_path = run_dir / POINCARE_LOG
    exit_code = run_solver([solver_python, str(poincare_script)], log_path, POINCARE_TIMEOUT_SECONDS, env=env)
    if exit_code is None:
        print(f"Poincare timed out after {POINCARE_TIMEOUT_SECONDS}s", file=sys.stderr)
        return "error", None
    if exit_code != 0:
        print(f"Poincare failed (exit {exit_code})", file=sys.stderr)
        return "error", None

    for line in log_path.read_text(errors="replace").splitlines():
        if "phi hit counts=" not in line:
            continue
        try:
            counts = json.loads(line.split("phi hit counts=")[1].strip())
        except json.JSONDecodeError:
            continue
        if not isinstance(counts, list):
            continue
        counts = [c for c in counts if isinstance(c, (int, float))]
        if not counts:
            continue
        if max(counts) == 0:
            return "fail", 0.0
        uniformity = min(counts) / max(counts)
        return ("pass" if uniformity > POINCARE_SURVIVAL_THRESHOLD else "fail"), uniformity

    print("Could not parse Poincare output", file=sys.stderr)
    return "error", None


def _archive_stage2_seed(run: RunContext, plasma_surf: str, seed_store: Path) -> None:
    """Copy a Stage 2 biot_savart_opt.json (+ results.json) into the seed store.

    The seed directory is named by the run id and carries an origin file, so a
    single-stage run that warm-starts from it can record this run as its parent.
    """
    bs_files = sorted(run.dir.rglob("biot_savart_opt.json"))
    if not bs_files:
        return
    seed_dir = seed_store / f"outputs-{plasma_surf}" / run.run_id
    seed_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(bs_files[0], seed_dir / "biot_savart_opt.json")
    results_files = sorted(run.dir.rglob("results.json"))
    if results_files:
        shutil.copy2(results_files[0], seed_dir / "results.json")
    (seed_dir / SEED_ORIGIN_FILE).write_text(json.dumps({"run_id": run.run_id}))


# --- Provenance -------------------------------------------------------------

def _solver_root(args: argparse.Namespace, config: BananaConfig) -> Path:
    return Path(args.solver_root) if args.solver_root else config.solver_root


def _solver_state(root: Path) -> tuple[str | None, str]:
    """(commit, uncommitted diff against HEAD) of the solver checkout."""
    commit = git_output(root, "rev-parse", "HEAD")
    diff = git_output(root, "diff", "HEAD") or ""
    return (commit.strip() if commit else None), diff


def solver_identity(args: argparse.Namespace) -> str:
    """Solver commit, plus a hash of any uncommitted changes."""
    root = _solver_root(args, config_from_env(os.environ))
    commit, diff = _solver_state(root)
    if commit is None:
        return f"unversioned:{root}"
    if not diff:
        return commit
    return f"{commit}+dirty:{hashlib.sha256(diff.encode()).hexdigest()[:16]}"


# --- Experiment entry point -------------------------------------------------

def run_experiment(args: argparse.Namespace, run: RunContext) -> ExperimentOutcome:
    """Run one banana experiment end-to-end in run.dir; return its outcome."""
    config = config_from_env(os.environ)
    solver_root = _solver_root(args, config)
    solver_python = args.solver_python or config.solver_python
    solver_script = solver_root / config.scripts[args.solver]

    commit, diff = _solver_state(solver_root)
    provenance: dict[str, object] = {
        "solver_root": str(solver_root),
        "solver_commit": commit,
        "solver_dirty": bool(diff),
        "solver_python": solver_python,
    }
    log_path = run.dir / "run.log"
    evidence: dict[str, Path] = {"log": log_path}
    if diff:
        patch_path = run.dir / "solver.patch"
        patch_path.write_text(diff)
        evidence["solver_patch"] = patch_path

    def outcome(status: str, reason: str, **fields: object) -> ExperimentOutcome:
        return ExperimentOutcome(status, reason, provenance=provenance, evidence=evidence, **fields)

    try:
        plasma_surf = _resolve_equilibrium(args.equilibrium, config.equilibria_dir)
    except KeyError:
        return outcome("crash", "unknown_equilibrium")
    equilibrium_path = config.equilibria_dir / plasma_surf
    provenance["equilibrium_file"] = str(equilibrium_path)
    if equilibrium_path.is_file():
        provenance["equilibrium_sha256"] = sha256_file(equilibrium_path)

    seed = None
    parent_run_id = None
    if args.solver == "single-stage":
        seed = _resolve_stage2_seed(args, plasma_surf, config.seed_store)
        if seed is None:
            return outcome("crash", "no_seed")
        parent_run_id = _seed_parent_run(Path(seed))
        provenance["warm_start_seed"] = seed
        provenance["warm_start_seed_sha256"] = sha256_file(Path(seed))

    env = {**os.environ, **thread_env(args.omp_threads)}

    cmd = [solver_python, str(solver_script)] + _build_cli(args, plasma_surf, seed, config, run.dir)
    provenance["command"] = cmd
    exit_code = run_solver(cmd, log_path, args.timeout, env=env)
    if exit_code is None:
        return outcome("crash", "timeout", parent_run_id=parent_run_id)
    if exit_code != 0:
        return outcome("crash", f"exit_{exit_code}", parent_run_id=parent_run_id)

    results_files = sorted(run.dir.rglob("results.json"))
    if not results_files:
        return outcome("crash", "no_results_json", parent_run_id=parent_run_id)
    try:
        with open(results_files[0]) as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return outcome("crash", f"bad_results_json: {e}", parent_run_id=parent_run_id)
    evidence["results"] = results_files[0]
    coil_files = sorted(run.dir.rglob("biot_savart_opt.json"))
    if coil_files:
        evidence["coils"] = coil_files[0]

    metrics = _map_metrics(raw)
    status, status_reason = _classify(metrics, args.solver)

    validated = None
    if args.solver == "single-stage" and status == "pass":
        fe = metrics.get("field_error")
        if fe is not None and not _is_missing(fe) and fe < POINCARE_FIELD_ERROR_THRESHOLD:
            evidence["poincare_log"] = run.dir / POINCARE_LOG
            try:
                validated, metrics["poincare_uniformity"] = _run_poincare(
                    run.dir, solver_python, solver_root / config.scripts["poincare"]
                )
            except Exception as e:
                # Keep the solver result: a broken check is a validation error, not a crash.
                print(f"WARNING: Poincare validation failed: {e}", file=sys.stderr)
                validated = "error"

    if args.solver == "stage2":
        try:
            _archive_stage2_seed(run, plasma_surf, config.seed_store)
        except Exception as e:
            print(f"WARNING: seed archival failed: {e}", file=sys.stderr)

    return outcome(
        status, status_reason, metrics=metrics, validated=validated, parent_run_id=parent_run_id
    )
