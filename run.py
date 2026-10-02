#!/usr/bin/env python3
"""Run a single optimization experiment for a campaign and record it.

The harness core is solver-agnostic: it owns campaign selection, the experiment
database, the scratch/artifact lifecycle, and the agent-facing CLI skeleton.
The campaign's solver adapter (named in its config.json; see adapter.py /
contract.py) owns everything solver-specific — which flags exist, how to invoke
the solver, what its outputs mean.

Usage (flags come from the campaign's adapter; this shows the toy adapter):
    python run.py --campaign demo --problem rastrigin --dim 4 --seed 3

`--campaign` (or $AUTORESEARCH_CAMPAIGN) may be omitted when exactly one
campaign exists. Writes to the campaign's results.jsonl (append-only) and
results.db (queryable), and prints a single-line JSON summary to stdout.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import json
import os
import shutil
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Mapping, MutableMapping

from adapter import AdapterError, load_adapter
from contract import ExperimentOutcome, clean

REPO_ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = REPO_ROOT / "schema.sql"
CONFIG_NAME = "config.json"
CAMPAIGN_ENV = "AUTORESEARCH_CAMPAIGN"
CAMPAIGNS_DIR_ENV = "AUTORESEARCH_CAMPAIGNS_DIR"
KEEP_ARTIFACTS_CHOICES = ("none", "pass", "all")

# Canonical metric keys with a dedicated DB column (must match schema.sql and
# the INSERT in _insert_db). Any other key an adapter emits is preserved in the
# row's `metrics` JSON blob.
COLUMN_METRIC_KEYS = (
    "iterations",
    "optimizer_success",
    "termination_message",
    "field_error",
    "qs_error",
    "boozer_residual",
    "iota_actual",
    "volume_actual",
    "max_curvature",
    "coil_length",
    "coil_coil_dist",
    "coil_surface_dist",
    "surface_vessel_dist",
    "max_force",
    "self_intersecting",
    "objective_J",
)


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------

class CampaignError(Exception):
    """The campaign cannot be selected or its config.json is invalid."""


@dataclass(frozen=True)
class CampaignConfig:
    """A campaign's config.json: which adapter it runs, plus default env vars.

    `env` supplies adapter configuration (solver paths, interpreters) without
    shell-specific export syntax; a variable already set in the environment
    takes precedence over the config value.
    """

    adapter: str
    env: Mapping[str, str]


def campaigns_root(environ: Mapping[str, str]) -> Path:
    return Path(environ.get(CAMPAIGNS_DIR_ENV, str(REPO_ROOT / "campaigns")))


def list_campaigns(root: Path) -> list[str]:
    """Names of the campaigns under `root` (directories holding a config.json), sorted."""
    return sorted(p.parent.name for p in root.glob(f"*/{CONFIG_NAME}"))


def resolve_campaign(name: str | None, root: Path) -> Path:
    """Return the campaign directory for `name`, or the only campaign when unnamed.

    Refuses to guess: an unnamed selection with zero or several campaigns raises
    CampaignError naming the way out.
    """
    existing = list_campaigns(root)
    if name:
        if name not in existing:
            listing = ", ".join(existing) or "none"
            raise CampaignError(
                f"no campaign '{name}' (expected {root / name / CONFIG_NAME}); "
                f"existing campaigns: {listing}"
            )
        return root / name
    if len(existing) == 1:
        return root / existing[0]
    if not existing:
        raise CampaignError(
            f"no campaign found under {root}. Run /setup-harness, or create "
            f"{root}/<name>/{CONFIG_NAME} with {{\"adapter\": \"toy\"}}."
        )
    raise CampaignError(
        f"{len(existing)} campaigns exist ({', '.join(existing)}); "
        f"name one with --campaign or ${CAMPAIGN_ENV}."
    )


def load_config(campaign_dir: Path) -> CampaignConfig:
    """Parse and validate a campaign's config.json."""
    path = campaign_dir / CONFIG_NAME
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise CampaignError(f"cannot read {path}: {e}") from e
    if not isinstance(raw, dict):
        raise CampaignError(f"{path}: top level must be a JSON object")
    adapter_name = raw.get("adapter")
    if not isinstance(adapter_name, str) or not adapter_name:
        raise CampaignError(f"{path}: \"adapter\" must be a non-empty string")
    env = raw.get("env", {})
    if not isinstance(env, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in env.items()
    ):
        raise CampaignError(f"{path}: \"env\" must map names to string values")
    return CampaignConfig(adapter=adapter_name, env=env)


def apply_env(defaults: Mapping[str, str], environ: MutableMapping[str, str]) -> None:
    """Set each config env var that the environment does not already define."""
    for key, value in defaults.items():
        environ.setdefault(key, value)


# ---------------------------------------------------------------------------
# Directory layout
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Layout:
    """Where one campaign's database, logs, scratch, and kept artifacts live.

    output_base: scratch dir for live runs ($OUTPUT_BASE). Crashed runs always
        leave their dir + run.log here for debugging.
    artifacts_dir: where kept run dirs land, named by run id ($ARTIFACTS_DIR,
        default <campaign>/artifacts).
    keep_artifacts: retention for completed runs ($KEEP_ARTIFACTS) — "none"
        discards the run dir after ingest, "pass" keeps passing runs, "all"
        keeps every completed run.
    """

    campaign_dir: Path
    output_base: Path
    artifacts_dir: Path
    keep_artifacts: str

    @property
    def db_path(self) -> Path:
        return self.campaign_dir / "results.db"

    @property
    def jsonl_path(self) -> Path:
        return self.campaign_dir / "results.jsonl"


def resolve_layout(campaign_dir: Path, environ: Mapping[str, str]) -> Layout:
    keep = environ.get("KEEP_ARTIFACTS", "none")
    if keep not in KEEP_ARTIFACTS_CHOICES:
        print(f"WARNING: unknown KEEP_ARTIFACTS '{keep}', using 'none'", file=sys.stderr)
        keep = "none"
    return Layout(
        campaign_dir=campaign_dir,
        output_base=Path(environ.get("OUTPUT_BASE", "/tmp/stellarator_harness")),
        artifacts_dir=Path(environ.get("ARTIFACTS_DIR", str(campaign_dir / "artifacts"))),
        keep_artifacts=keep,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _uuid7() -> str:
    ts_ms = int(time.time() * 1000)
    rand_bits = uuid.uuid4().int & ((1 << 74) - 1)
    u = (ts_ms << 80) | (0x7 << 76) | rand_bits
    u = (u & ~(0x3 << 62)) | (0x2 << 62)
    return str(uuid.UUID(int=u))


def _bool_to_int(v: object) -> int | None:
    if v is True:
        return 1
    if v is False:
        return 0
    return None


# ---------------------------------------------------------------------------
# DB operations
# ---------------------------------------------------------------------------

def _ensure_db(db_path: Path) -> sqlite3.Connection:
    """Open DB, create schema if needed. Safe for concurrent first-run."""
    db = sqlite3.connect(str(db_path))
    db.executescript(SCHEMA_PATH.read_text())  # IF NOT EXISTS + PRAGMA WAL in schema.sql
    return db


def _insert_db(db: sqlite3.Connection, record: dict) -> None:
    """Insert one run record into the DB."""
    db.execute(
        """INSERT INTO runs (
            id, coil_type, solver, equilibrium, experiment_group,
            status, status_reason, validated,
            iterations, elapsed, created_at,
            optimizer_success, termination_message,
            field_error, qs_error, boozer_residual,
            iota_actual, volume_actual,
            max_curvature,
            coil_length, coil_coil_dist, coil_surface_dist, surface_vessel_dist,
            max_force, self_intersecting, objective_J,
            metrics, params
        ) VALUES (
            :id, :coil_type, :solver, :equilibrium, :experiment_group,
            :status, :status_reason, :validated,
            :iterations, :elapsed, :created_at,
            :optimizer_success, :termination_message,
            :field_error, :qs_error, :boozer_residual,
            :iota_actual, :volume_actual,
            :max_curvature,
            :coil_length, :coil_coil_dist, :coil_surface_dist, :surface_vessel_dist,
            :max_force, :self_intersecting, :objective_J,
            :metrics, :params
        )""",
        {
            "id": record["id"],
            "coil_type": record["coil_type"],
            "solver": record["solver"],
            "equilibrium": record["equilibrium"],
            "experiment_group": record.get("experiment_group"),
            "status": record["status"],
            "status_reason": record.get("status_reason"),
            "validated": record.get("validated"),
            "iterations": record.get("iterations"),
            "elapsed": record.get("elapsed"),
            "created_at": record.get("created_at"),
            "optimizer_success": _bool_to_int(record.get("optimizer_success")),
            "termination_message": record.get("termination_message"),
            "field_error": record.get("field_error"),
            "qs_error": record.get("qs_error"),
            "boozer_residual": record.get("boozer_residual"),
            "iota_actual": record.get("iota_actual"),
            "volume_actual": record.get("volume_actual"),
            "max_curvature": record.get("max_curvature"),
            "coil_length": record.get("coil_length"),
            "coil_coil_dist": record.get("coil_coil_dist"),
            "coil_surface_dist": record.get("coil_surface_dist"),
            "surface_vessel_dist": record.get("surface_vessel_dist"),
            "max_force": record.get("max_force"),
            "self_intersecting": _bool_to_int(record.get("self_intersecting")),
            "objective_J": record.get("objective_J"),
            "metrics": json.dumps(record.get("metrics", {})),
            "params": json.dumps(record.get("params", {})),
        },
    )
    db.commit()


def _append_jsonl(jsonl_path: Path, record: dict) -> None:
    """Append one JSON record to results.jsonl with file locking."""
    line = json.dumps(record) + "\n"
    with open(jsonl_path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(line)
        f.flush()


def _emit_result(layout: Layout, record: dict) -> None:
    """Write to JSONL + DB + stdout. Persistence failures don't block stdout."""
    try:
        _append_jsonl(layout.jsonl_path, record)
    except Exception as e:
        print(f"WARNING: JSONL write failed: {e}", file=sys.stderr)
    try:
        with contextlib.closing(_ensure_db(layout.db_path)) as db:
            _insert_db(db, record)
    except Exception as e:
        print(f"WARNING: DB write failed: {e}", file=sys.stderr)
    print(json.dumps(record))


# ---------------------------------------------------------------------------
# Record construction (single place where NaN cleaning happens)
# ---------------------------------------------------------------------------

def _run_spec(args: argparse.Namespace) -> dict:
    """Every parsed CLI value except campaign selection, NaN-cleaned.

    This is the full experiment specification: the mode, the target, and every
    flag the adapter registered (defaults included).
    """
    return {k: clean(v) for k, v in vars(args).items() if k != "campaign"}


def _build_record(
    active: ModuleType, args: argparse.Namespace, outcome: ExperimentOutcome, elapsed: float
) -> dict:
    """Assemble a result record from an adapter outcome. NaN cleaning happens here.

    Canonical metric keys with a column are projected to top-level fields; every
    other emitted key is preserved (cleaned) in `metrics` for JSON storage.
    """
    metrics = outcome.metrics
    record = {
        "id": _uuid7(),
        "coil_type": active.NAME,
        "solver": args.solver,
        "equilibrium": getattr(args, active.TARGET_FLAG),
        "experiment_group": outcome.experiment_group,
        "status": outcome.status,
        "status_reason": outcome.status_reason,
        "validated": outcome.validated,
        "elapsed": round(elapsed, 1),
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "params": _run_spec(args),
    }
    for key in COLUMN_METRIC_KEYS:
        record[key] = clean(metrics.get(key))
    record["metrics"] = {
        k: clean(v) for k, v in metrics.items() if k not in COLUMN_METRIC_KEYS
    }
    return record


# ---------------------------------------------------------------------------
# Core experiment
# ---------------------------------------------------------------------------

def _run_experiment(active: ModuleType, layout: Layout, args: argparse.Namespace) -> None:
    """Run one experiment via the campaign's adapter and record the outcome."""
    run_dir = layout.output_base / f"run_{int(time.time() * 1000)}"
    run_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.monotonic()
    try:
        outcome = active.run_experiment(args, run_dir)
    except Exception as e:
        # Truly-unexpected adapter failure (the adapter never returned an
        # outcome). The record still carries the full run spec, so the
        # experiment stays reproducible.
        print(f"WARNING: adapter raised: {e}", file=sys.stderr)
        outcome = ExperimentOutcome("crash", f"adapter_error: {e}")
    # Total experiment wall time: includes any validation (e.g. Poincaré) or
    # chained sub-steps the adapter runs internally, not just one solver call.
    elapsed = time.monotonic() - t0

    record = _build_record(active, args, outcome, elapsed)
    _emit_result(layout, record)
    _finalize_run_dir(layout, run_dir, outcome.status, record["id"])


def _finalize_run_dir(layout: Layout, run_dir: Path, status: str, run_id: str) -> None:
    """Apply keep_artifacts: move the run dir to artifacts_dir/<run-id> or discard it."""
    keep = layout.keep_artifacts == "all" or (
        layout.keep_artifacts == "pass" and status == "pass"
    )
    if not keep:
        shutil.rmtree(run_dir, ignore_errors=True)
        return
    layout.artifacts_dir.mkdir(parents=True, exist_ok=True)
    dest = layout.artifacts_dir / run_id
    shutil.move(str(run_dir), str(dest))
    print(f"Artifacts kept: {dest}", file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    # Campaign selection comes first: the campaign's adapter defines every
    # other flag, so the full parser can only be built once it is loaded.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--campaign", default=os.environ.get(CAMPAIGN_ENV))
    selection, _ = pre.parse_known_args(argv)
    try:
        campaign_dir = resolve_campaign(selection.campaign, campaigns_root(os.environ))
        config = load_config(campaign_dir)
        apply_env(config.env, os.environ)
        active = load_adapter(config.adapter)
    except (CampaignError, AdapterError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    p = argparse.ArgumentParser(description="Run one optimization experiment")
    p.add_argument(
        "--campaign",
        default=selection.campaign,
        help=f"campaign under campaigns/ (default: ${CAMPAIGN_ENV}, or the only campaign)",
    )
    p.add_argument(
        "--solver",
        choices=list(active.SOLVER_MODES),
        default=active.SOLVER_MODES[0],
        help="solver mode exposed by the campaign's adapter",
    )
    active.add_arguments(p)
    args = p.parse_args(argv)

    layout = resolve_layout(campaign_dir, os.environ)
    layout.output_base.mkdir(parents=True, exist_ok=True)
    _run_experiment(active, layout, args)


if __name__ == "__main__":
    main()
