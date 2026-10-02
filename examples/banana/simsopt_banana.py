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
with `"adapter": "examples.banana.simsopt_banana"` in its config.json.

Configuration is read from the environment at import (fail-fast); see
ENV_REQUIREMENTS. Script paths default to the fork's standard layout and may be
overridden per fork via STAGE2_SCRIPT / SINGLE_STAGE_SCRIPT / POINCARE_SCRIPT.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from contract import ExperimentOutcome, require_env

# --- Contract surface -------------------------------------------------------

NAME = "banana"
SOLVER_MODES = ("stage2", "single-stage")
TARGET_FLAG = "equilibrium"
ENV_REQUIREMENTS = (
    "SIMSOPT_ROOT",
    "SIMSOPT_PYTHON",
    "EQUILIBRIA_DIR",
    "STAGE2_SCRIPT",
    "SINGLE_STAGE_SCRIPT",
    "POINCARE_SCRIPT",
    "STAGE2_SEED_DIR",
)

# --- Configuration (env-driven; resolved once at import) --------------------

EQUILIBRIA_DIR = Path(require_env("EQUILIBRIA_DIR"))
DEFAULT_SOLVER_ROOT = Path(require_env("SIMSOPT_ROOT"))
DEFAULT_SOLVER_PYTHON = require_env("SIMSOPT_PYTHON")

# Solver script paths are relative to the solver root. Override via env when a
# fork keeps these scripts elsewhere.
SCRIPTS = {
    "stage2": os.environ.get(
        "STAGE2_SCRIPT",
        "examples/single_stage_optimization/STAGE_2/banana_coil_solver.py",
    ),
    "single-stage": os.environ.get(
        "SINGLE_STAGE_SCRIPT",
        "examples/single_stage_optimization/SINGLE_STAGE/single_stage_banana_example.py",
    ),
}

# Stage 2 seed archive that single-stage warm-starts from.
STAGE2_SEED_STORE = Path(
    os.environ.get("STAGE2_SEED_DIR", str(Path(__file__).resolve().parents[2] / "stage2_seeds"))
)

# Single-stage runs Poincaré validation only when the field error clears this
# bar; tighter than the survival bar so validation isn't wasted on bad fits.
POINCARE_FIELD_ERROR_THRESHOLD = 0.1
POINCARE_SURVIVAL_THRESHOLD = 0.9

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

def _resolve_equilibrium(eq_key: str) -> str:
    """Map an equilibrium key (registry alias or raw wout filename) to a wout
    filename present in EQUILIBRIA_DIR. Raises KeyError if neither resolves."""
    filename = EQUILIBRIUM_FILES.get(eq_key)
    if filename:
        return filename
    if (EQUILIBRIA_DIR / eq_key).exists():
        return eq_key
    raise KeyError(eq_key)


def _resolve_stage2_seed(args: argparse.Namespace, plasma_surf: str) -> str | None:
    """Find the best Stage 2 seed matching equilibrium + geometry, or None."""
    if args.stage2_bs_path:
        if Path(args.stage2_bs_path).is_file():
            return args.stage2_bs_path
        print(f"ERROR: seed not found: {args.stage2_bs_path}", file=sys.stderr)
        return None

    seeds_parent = STAGE2_SEED_STORE / f"outputs-{plasma_surf}"
    if not seeds_parent.is_dir():
        print(f"No seeds for {args.equilibrium}. Run Stage 2 first.", file=sys.stderr)
        return None

    best_seed = None
    best_fe = float("inf")
    for seed_dir in seeds_parent.iterdir():
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

def _build_cli(args: argparse.Namespace, plasma_surf: str, run_dir: Path) -> list[str] | None:
    """Build the solver subprocess args (incl. --output-root). None on seed miss."""
    common = [
        "--plasma-surf-filename", plasma_surf,
        "--equilibria-dir", str(EQUILIBRIA_DIR),
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
        ]
        return common + mode_args + ["--output-root", str(run_dir)]

    # Single-stage: resolve a warm-start seed first.
    seed = _resolve_stage2_seed(args, plasma_surf)
    if seed is None:
        return None

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

def _run_poincare(run_dir: Path, solver_python: str, solver_root: Path) -> str | None:
    """Trace field lines and judge confinement. Returns 'pass'/'fail' or None.

    The Poincaré script prints phi hit counts to stdout. Field lines that exit
    the surface produce fewer hits; uniformity across phi slices (min/max)
    indicates confinement quality.
    """
    poincare_script = solver_root / os.environ.get(
        "POINCARE_SCRIPT",
        "examples/single_stage_optimization/POINCARE_PLOTTING/poincare_surfaces.py",
    )
    if not poincare_script.exists():
        print(f"Poincare script not found: {poincare_script}", file=sys.stderr)
        return None

    bs_files = list(run_dir.rglob("biot_savart_opt.json"))
    if not bs_files:
        print("No biot_savart_opt.json for Poincare", file=sys.stderr)
        return None
    out_dir = str(bs_files[0].parent)

    env = os.environ.copy()
    env["POINCARE_OUT_DIR"] = out_dir

    try:
        result = subprocess.run(
            [solver_python, str(poincare_script)],
            capture_output=True, text=True, env=env, timeout=600,
        )
    except subprocess.TimeoutExpired:
        print("Poincare timed out after 600s", file=sys.stderr)
        return None

    if result.returncode != 0:
        print(f"Poincare failed (exit {result.returncode})", file=sys.stderr)
        return None

    for line in result.stdout.splitlines():
        if "phi hit counts=" not in line:
            continue
        try:
            counts_str = line.split("phi hit counts=")[1].strip()
            counts = json.loads(counts_str)
            if not isinstance(counts, list) or not counts:
                continue
            counts = [c for c in counts if isinstance(c, (int, float)) and c is not None]
            if not counts or max(counts) == 0:
                return "fail"
            uniformity = min(counts) / max(counts)
            return "pass" if uniformity > POINCARE_SURVIVAL_THRESHOLD else "fail"
        except Exception:
            continue

    print("Could not parse Poincare output", file=sys.stderr)
    return None


def _archive_stage2_seed(run_dir: Path, plasma_surf: str) -> None:
    """Copy a Stage 2 biot_savart_opt.json (+ results.json) into the seed store."""
    bs_files = list(run_dir.rglob("biot_savart_opt.json"))
    if not bs_files:
        return
    ts = int(time.time() * 1000)
    seed_dir = STAGE2_SEED_STORE / f"outputs-{plasma_surf}" / f"{bs_files[0].parent.name}-{ts}"
    seed_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(bs_files[0], seed_dir / "biot_savart_opt.json")
    results_files = list(run_dir.rglob("results.json"))
    if results_files:
        shutil.copy2(results_files[0], seed_dir / "results.json")


# --- Experiment entry point -------------------------------------------------

def run_experiment(args: argparse.Namespace, run_dir: Path) -> ExperimentOutcome:
    """Run one banana experiment end-to-end in run_dir; return its outcome."""
    solver_root = Path(args.solver_root) if args.solver_root else DEFAULT_SOLVER_ROOT
    solver_python = args.solver_python or DEFAULT_SOLVER_PYTHON
    solver_script = solver_root / SCRIPTS[args.solver]

    try:
        plasma_surf = _resolve_equilibrium(args.equilibrium)
    except KeyError:
        return ExperimentOutcome("crash", "unknown_equilibrium")

    cli_args = _build_cli(args, plasma_surf, run_dir)
    if cli_args is None:
        return ExperimentOutcome("crash", "no_seed")

    env = os.environ.copy()
    env["OMP_NUM_THREADS"] = str(args.omp_threads)
    env["MKL_NUM_THREADS"] = str(args.omp_threads)

    cmd = [solver_python, str(solver_script)] + cli_args
    log_path = run_dir / "run.log"
    try:
        with open(log_path, "w") as lf:
            result = subprocess.run(
                cmd, stdout=lf, stderr=subprocess.STDOUT, env=env, timeout=args.timeout,
            )
    except subprocess.TimeoutExpired:
        return ExperimentOutcome("crash", "timeout")
    if result.returncode != 0:
        return ExperimentOutcome("crash", f"exit_{result.returncode}")

    results_files = list(run_dir.rglob("results.json"))
    if not results_files:
        return ExperimentOutcome("crash", "no_results_json")
    try:
        with open(results_files[0]) as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return ExperimentOutcome("crash", f"bad_results_json: {e}")

    metrics = _map_metrics(raw)
    status, status_reason = _classify(metrics, args.solver)

    validated = None
    if args.solver == "single-stage" and status == "pass":
        fe = metrics.get("field_error")
        if fe is not None and not _is_missing(fe) and fe < POINCARE_FIELD_ERROR_THRESHOLD:
            try:
                validated = _run_poincare(run_dir, solver_python, solver_root)
            except Exception as e:
                print(f"WARNING: Poincare validation failed: {e}", file=sys.stderr)

    if args.solver == "stage2":
        try:
            _archive_stage2_seed(run_dir, plasma_surf)
        except Exception as e:
            print(f"WARNING: seed archival failed: {e}", file=sys.stderr)

    return ExperimentOutcome(
        status=status,
        status_reason=status_reason,
        metrics=metrics,
        validated=validated,
    )
