#!/usr/bin/env python3
"""Run optimization experiments for a campaign and record them.

The harness core is solver-agnostic: it owns campaign selection, the run
records, the scratch/evidence lifecycle, and the agent-facing CLI skeleton.
The campaign's solver adapter (named in its config.json; see adapter.py /
contract.py) owns everything solver-specific.

Usage (experiment flags come from the campaign's adapter; this shows the toy):
    python run.py --campaign demo --problem rastrigin --dim 4      # run one experiment
    python run.py --campaign demo --problem rastrigin --replicate 1  # another seed of it
    python run.py brief --campaign demo                            # fixed-size campaign digest
    python run.py query "SELECT ..." --campaign demo               # read-only SQL, compact output
    python run.py replay <run-id> --campaign demo                  # re-run and compare
    python run.py rebuild --campaign demo [--from-jsonl FILE]      # regenerate results.db/.jsonl
    python run.py campaigns                                        # every campaign at a glance
    python run.py import-lessons --from other --campaign demo      # another campaign's lessons as priors
    python run.py batch plan.json --campaign demo [--dry-run]      # run a planned batch of experiments
    python run.py machine [--max-parallel N ...]                   # hardware, run slots, run cost, sizing
    python run.py schema --campaign demo                           # columns + metric goals for program.md

`--campaign` (or $AUTORESEARCH_CAMPAIGN) may be omitted when exactly one
campaign exists. Each run is written atomically to the campaign's
runs/<run-id>.json — the source of truth — then indexed in results.db; a
single-line JSON summary goes to stdout.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import io
import hashlib
import json
import os
import platform
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Mapping, MutableMapping

import analysis
import batch
import machine
from adapter import AdapterError, load_adapter
from contract import Cancelled, ExperimentOutcome, RunContext, clean, git_output, sha256_file
from locks import acquire_slot, release, report_waiting, try_lock

REPO_ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = REPO_ROOT / "schema.sql"
SCHEMA_VERSION = 6
# From this version on every DB row has a run file, so an older DB can be
# rebuilt from runs/ without losing anything.
FIRST_RECORD_BACKED_VERSION = 2
CONFIG_NAME = "config.json"
CAMPAIGN_ENV = "AUTORESEARCH_CAMPAIGN"
CAMPAIGNS_DIR_ENV = "AUTORESEARCH_CAMPAIGNS_DIR"
KEEP_ARTIFACTS_CHOICES = ("none", "pass", "all")
COMMANDS = ("replay", "rebuild", "brief", "query", "campaigns", "import-lessons", "batch", "machine", "schema")
SLOTS_DIR_ENV = "AUTORESEARCH_SLOTS_DIR"
MAX_PARALLEL_ENV = "AUTORESEARCH_MAX_PARALLEL"
BLOBS_DIR_ENV = "AUTORESEARCH_BLOBS_DIR"
MACHINE_DIR_ENV = "AUTORESEARCH_MACHINE_DIR"
CANCELLED_EXIT = 143
BATCH_POLL_SECONDS = 0.2
BATCH_SUMMARY_ROWS = 10
LESSONS_NAME = "LESSONS.md"
QUERY_DEFAULT_LIMIT = 50
QUERY_CELL_CHARS = 120
LOG_TAIL_BYTES = 64 * 1024
# Core flags that select how the harness runs, not what the solver computes.
CORE_FLAGS = ("campaign", "replicate", "batch_id", "parent_run_id")
# Prior results that make a repeat redundant; a crash can always be retried.
DEDUPE_STATUSES = ("pass", "fail")
REPLAY_MISMATCH_EXIT = 2

IDENTITY_COLUMNS = (
    "id", "adapter", "mode", "target", "experiment_group",
    "spec_hash", "replicate", "seed", "parent_run_id", "replay_of", "batch_id",
    "status", "status_reason", "crash_signature", "validated", "elapsed", "peak_rss_mb", "created_at",
)
JSON_COLUMNS = ("metrics", "params", "provenance", "evidence")
DB_COLUMNS = IDENTITY_COLUMNS + JSON_COLUMNS
# The view that turns each of the adapter's METRICS into a column.
RESULTS_VIEW = "results"
# Run records written before schema v6 used stellarator-specific names and
# kept some metrics as top-level fields; upgrade_record converts them.
LEGACY_RENAMES = {"coil_type": "adapter", "solver": "mode", "equilibrium": "target"}
LEGACY_METRIC_FIELDS = (
    "iterations", "optimizer_success", "termination_message", "field_error", "qs_error",
    "boozer_residual", "iota_actual", "volume_actual", "max_curvature", "coil_length",
    "coil_coil_dist", "coil_surface_dist", "surface_vessel_dist", "max_force",
    "self_intersecting", "objective_J",
)
# Identity fields shown in the stdout summary when set; metrics follow.
SUMMARY_FIELDS = (
    "id", "mode", "target", "status", "status_reason", "crash_signature",
    "validated", "parent_run_id", "replay_of", "batch_id", "replicate", "seed", "elapsed",
)
# Run fields the analysis views carry besides metric values.
VIEW_FIELDS = (
    "id", "mode", "target", "status", "status_reason", "crash_signature",
    "validated", "created_at", "replicate", "seed", "elapsed", "peak_rss_mb",
)


class HarnessError(Exception):
    """A user-correctable problem that stops run.py with a message (exit 1)."""


class CampaignError(HarnessError):
    """The campaign cannot be selected or its config.json is invalid."""


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CampaignConfig:
    """A campaign's config.json: which adapter it runs, plus default env vars.

    `env` supplies adapter configuration (solver paths, interpreters) without
    shell-specific export syntax; a variable already set in the environment
    takes precedence over the config value. `max_parallel` caps how many of
    the machine's run slots one batch of this campaign uses at once.
    `plan_minutes` is how often the agent plans a new batch; `brief` and
    `machine` turn it into a suggested batch size.
    """

    adapter: str
    env: Mapping[str, str]
    max_parallel: int | None = None
    plan_minutes: float | None = None


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
    max_parallel = raw.get("max_parallel")
    if max_parallel is not None and (isinstance(max_parallel, bool) or not isinstance(max_parallel, int) or max_parallel < 1):
        raise CampaignError(f"{path}: \"max_parallel\" must be an integer >= 1")
    plan_minutes = raw.get("plan_minutes")
    if plan_minutes is not None and (isinstance(plan_minutes, bool) or not isinstance(plan_minutes, (int, float)) or plan_minutes <= 0):
        raise CampaignError(f"{path}: \"plan_minutes\" must be a number > 0")
    return CampaignConfig(adapter=adapter_name, env=env, max_parallel=max_parallel, plan_minutes=plan_minutes)


@dataclass(frozen=True)
class Slots:
    """The machine-wide run-slot pool: `capacity` lock files under `dir`.

    Every run, from any campaign, holds one slot while it executes, so the
    machine never runs more than `capacity` experiments at once.
    """

    dir: Path
    capacity: int


def machine_dir(environ: Mapping[str, str]) -> Path:
    """Where machine-wide state lives: machine.json and the slot locks (~/.autoresearch)."""
    return Path(environ.get(MACHINE_DIR_ENV, str(Path.home() / ".autoresearch")))


def resolve_slots(environ: Mapping[str, str]) -> Slots:
    """Slot capacity: $AUTORESEARCH_MAX_PARALLEL, else machine.json's max_parallel, else 1."""
    configured = machine.read_settings(machine_dir(environ)).get("max_parallel", 1)
    raw = environ.get(MAX_PARALLEL_ENV, str(configured))
    if not raw.isdigit() or int(raw) < 1:
        raise HarnessError(f"run slots must be an integer >= 1, got {raw!r} (${MAX_PARALLEL_ENV} or machine.json)")
    return Slots(Path(environ.get(SLOTS_DIR_ENV, str(machine_dir(environ) / "slots"))), int(raw))


def busy_slots(slots: Slots) -> int:
    """How many of the machine's slots are held right now."""
    busy = 0
    for index in range(slots.capacity):
        fd = try_lock(slots.dir / f"slot-{index}.lock")
        if fd is None:
            busy += 1
        else:
            release(fd)
    return busy


def apply_env(defaults: Mapping[str, str], environ: MutableMapping[str, str]) -> None:
    """Set each config env var that the environment does not already define."""
    for key, value in defaults.items():
        environ.setdefault(key, value)


def check_required_env(active: ModuleType, environ: Mapping[str, str], campaign_dir: Path) -> None:
    """Refuse to start a run when the adapter's REQUIRED_ENV is incomplete."""
    missing = [name for name in active.REQUIRED_ENV if not environ.get(name)]
    if missing:
        raise HarnessError(
            f"adapter '{active.NAME}' needs {', '.join(missing)}: add to the \"env\" map in "
            f"{campaign_dir / CONFIG_NAME} or set in the shell."
        )


# ---------------------------------------------------------------------------
# Directory layout
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Layout:
    """Where one campaign's records, scratch, evidence, and kept artifacts live.

    scratch_dir: live run dirs, one per run id ($OUTPUT_BASE, default
        <campaign>/scratch).
    artifacts_dir: where kept run dirs land, named by run id ($ARTIFACTS_DIR,
        default <campaign>/artifacts).
    keep_artifacts: retention for completed runs ($KEEP_ARTIFACTS) — "none"
        discards the run dir after recording, "pass" keeps passing runs, "all"
        keeps every run. Evidence files are kept regardless.
    """

    campaign_dir: Path
    scratch_dir: Path
    artifacts_dir: Path
    keep_artifacts: str
    shared_blobs_dir: Path | None = None

    @property
    def runs_dir(self) -> Path:
        return self.campaign_dir / "runs"

    @property
    def blobs_dir(self) -> Path:
        """Evidence store: machine-wide when $AUTORESEARCH_BLOBS_DIR is set, else per campaign."""
        return self.shared_blobs_dir or self.campaign_dir / "blobs"

    @property
    def claims_dir(self) -> Path:
        return self.campaign_dir / "claims"

    @property
    def batches_dir(self) -> Path:
        return self.campaign_dir / "batches"

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
        scratch_dir=Path(environ.get("OUTPUT_BASE", str(campaign_dir / "scratch"))),
        artifacts_dir=Path(environ.get("ARTIFACTS_DIR", str(campaign_dir / "artifacts"))),
        keep_artifacts=keep,
        shared_blobs_dir=Path(environ[BLOBS_DIR_ENV]) if environ.get(BLOBS_DIR_ENV) else None,
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


def _canonical_hash(payload: object) -> str:
    """SHA-256 of `payload` as canonical JSON (sorted keys, no whitespace)."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _write_atomic(path: Path, data: bytes) -> None:
    """Write `data` to `path` so readers see either nothing or the whole file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Run specification: spec, seed, spec hash
# ---------------------------------------------------------------------------

def run_spec(args: argparse.Namespace) -> dict:
    """Every parsed CLI value except core flags, NaN-cleaned.

    This is the full experiment specification: the mode, the target, and every
    flag the adapter registered (defaults included).
    """
    return {k: clean(v) for k, v in vars(args).items() if k not in CORE_FLAGS}


def _hashed_fields(active: ModuleType, spec: Mapping[str, object]) -> dict:
    return {k: v for k, v in spec.items() if k not in active.EXECUTION_FLAGS}


def _seedless_fields(active: ModuleType, spec: Mapping[str, object]) -> dict:
    return {k: v for k, v in _hashed_fields(active, spec).items() if k != active.SEED_FLAG}


def spec_base(active: ModuleType, spec: Mapping[str, object]) -> str:
    """Hash shared by every replicate of a spec: no seed, no execution flags, no solver."""
    return _canonical_hash({"adapter": active.NAME, "spec": _seedless_fields(active, spec)})


def derive_seed(active: ModuleType, spec: Mapping[str, object], replicate: int) -> int:
    """Seed for an unset SEED_FLAG: a function of the spec (minus the seed) and replicate.

    Independent of the solver identity, so the same experiment keeps its seed
    across solver versions and their results stay comparable.
    """
    digest = _canonical_hash(
        {"adapter": active.NAME, "spec": _seedless_fields(active, spec), "replicate": replicate}
    )
    return int(digest[:8], 16) % 2**31


def with_seed(active: ModuleType, args: argparse.Namespace) -> argparse.Namespace:
    """args with the adapter's seed filled in when the agent left it unset."""
    if active.SEED_FLAG is None or getattr(args, active.SEED_FLAG) is not None:
        return args
    seed = derive_seed(active, run_spec(args), args.replicate)
    return argparse.Namespace(**{**vars(args), active.SEED_FLAG: seed})


def spec_hash(active: ModuleType, spec: Mapping[str, object], solver_identity: str) -> str:
    """Hash of what was asked: adapter, non-execution flags, and the solver code."""
    return _canonical_hash(
        {"adapter": active.NAME, "spec": _hashed_fields(active, spec), "solver": solver_identity}
    )


# ---------------------------------------------------------------------------
# Provenance and evidence
# ---------------------------------------------------------------------------

def harness_provenance() -> dict:
    commit = git_output(REPO_ROOT, "rev-parse", "HEAD")
    status = git_output(REPO_ROOT, "status", "--porcelain", "--untracked-files=no")
    return {
        "commit": commit.strip() if commit else None,
        "dirty": bool(status) if status is not None else None,
    }


def platform_provenance() -> dict:
    return {
        "os": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
    }


def store_evidence(blobs_dir: Path, files: Mapping[str, Path]) -> dict:
    """Store each existing evidence file by content hash; return name → {sha256, bytes}."""
    stored = {}
    for name, path in sorted(files.items()):
        if not path.is_file():
            continue
        digest = sha256_file(path)
        blob = blobs_dir / digest[:2] / digest
        if not blob.exists():
            _write_atomic(blob, path.read_bytes())
        stored[name] = {"sha256": digest, "bytes": path.stat().st_size}
    return stored


# ---------------------------------------------------------------------------
# Run records (source of truth) and the DB index
# ---------------------------------------------------------------------------

def write_run_record(runs_dir: Path, record: Mapping[str, object]) -> Path:
    path = runs_dir / f"{record['id']}.json"
    _write_atomic(path, (json.dumps(record, indent=1) + "\n").encode())
    return path


def read_run_records(runs_dir: Path) -> list[dict]:
    """Every run record, ordered by (created_at, id)."""
    records = [upgrade_record(json.loads(p.read_text())) for p in runs_dir.glob("*.json")]
    return sorted(records, key=lambda r: (r.get("created_at") or "", r["id"]))


def upgrade_record(record: Mapping[str, object]) -> dict:
    """A run record in the current shape; records from before schema v6 are converted.

    Old records named the adapter, mode and target `coil_type`, `solver` and
    `equilibrium`, and kept some metrics as top-level fields. Run files are
    never rewritten; they are converted whenever they are read.
    """
    if "coil_type" not in record:
        return dict(record)
    renamed = {new: record.get(old) for old, new in LEGACY_RENAMES.items()}
    metrics = {k: record[k] for k in LEGACY_METRIC_FIELDS if record.get(k) is not None}
    metrics.update(record.get("metrics") or {})
    params = dict(record.get("params") or {})
    if "solver" in params and "mode" not in params:
        params["mode"] = params.pop("solver")
    rest = {
        k: v for k, v in record.items()
        if k not in LEGACY_RENAMES and k not in LEGACY_METRIC_FIELDS and k not in ("metrics", "params")
    }
    return {**rest, **renamed, "metrics": metrics, "params": params}


def _db_row(record: Mapping[str, object]) -> dict:
    row = {}
    for column in DB_COLUMNS:
        value = record.get(column)
        if column in JSON_COLUMNS:
            value = json.dumps(value if value is not None else {})
        row[column] = value
    return row


_INSERT_SQL = (
    f"INSERT INTO runs ({', '.join(DB_COLUMNS)}) "
    f"VALUES ({', '.join(':' + c for c in DB_COLUMNS)})"
)


def _create_schema(db: sqlite3.Connection) -> None:
    """Create the tables and stamp the schema version in one transaction.

    Concurrent first runs must never see the table without its version, so
    both happen atomically; IF NOT EXISTS makes a second creator a no-op.
    """
    db.execute("PRAGMA journal_mode = WAL")
    db.executescript(
        f"BEGIN IMMEDIATE;\n{SCHEMA_PATH.read_text()}\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;"
    )


def _schema_version(db: sqlite3.Connection) -> int | None:
    """The DB's schema version, or None when it has no runs table yet."""
    has_runs = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'"
    ).fetchone()
    return db.execute("PRAGMA user_version").fetchone()[0] if has_runs else None


def open_db(layout: Layout) -> sqlite3.Connection:
    """Open the campaign's DB, creating it if absent.

    A DB from an older record-backed schema is rebuilt from runs/; one from
    before run files existed is refused with the command that imports it.
    """
    db = sqlite3.connect(str(layout.db_path))
    version = _schema_version(db)
    if version is None:
        _create_schema(db)
        return db
    if version != SCHEMA_VERSION:
        db.close()
        if version >= FIRST_RECORD_BACKED_VERSION:
            print(f"results.db schema {version} -> {SCHEMA_VERSION}: rebuilding from runs/", file=sys.stderr)
            rebuild(layout)
            return sqlite3.connect(str(layout.db_path))
        jsonl_hint = f" --from-jsonl {layout.jsonl_path}" if layout.jsonl_path.exists() else ""
        raise HarnessError(
            f"{layout.db_path} has schema version {version}, this harness needs "
            f"{SCHEMA_VERSION}. Regenerate it: python run.py rebuild "
            f"--campaign {layout.campaign_dir.name}{jsonl_hint}"
        )
    return db


def find_duplicate(layout: Layout, digest: str, replicate: int) -> dict | None:
    """The latest pass/fail run with this spec hash and replicate, if any."""
    if not layout.db_path.exists():
        return None
    with contextlib.closing(open_db(layout)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            f"SELECT id, status, status_reason FROM runs WHERE spec_hash = ? AND replicate = ? "
            f"AND status IN ({', '.join('?' for _ in DEDUPE_STATUSES)}) "
            f"ORDER BY created_at DESC LIMIT 1",
            (digest, replicate, *DEDUPE_STATUSES),
        ).fetchone()
    return dict(row) if row else None


def index_record(layout: Layout, record: Mapping[str, object]) -> None:
    """Add one run record to results.db. A failure leaves the run file intact."""
    try:
        with contextlib.closing(open_db(layout)) as db:
            db.execute(_INSERT_SQL, _db_row(record))
            db.commit()
    except (sqlite3.Error, HarnessError) as e:
        print(f"WARNING: results.db not updated ({e}); run `python run.py rebuild`", file=sys.stderr)


def _legacy_record(raw: dict, source: Path) -> dict:
    """A results.jsonl record from an older harness, in the current record shape."""
    return {**upgrade_record(raw), "provenance": {"imported_from": str(source)}, "evidence": {}}


def import_jsonl(layout: Layout, source: Path) -> int:
    """Write a run file for each record in `source` that has none yet; return the count."""
    imported = 0
    for line in source.read_text().splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        if (layout.runs_dir / f"{raw['id']}.json").exists():
            continue
        write_run_record(layout.runs_dir, _legacy_record(raw, source))
        imported += 1
    return imported


def rebuild(layout: Layout) -> int:
    """Regenerate results.db and results.jsonl from runs/*.json; return the row count.

    The previous DB, if any, is kept as results.db.bak-<timestamp>.
    """
    records = read_run_records(layout.runs_dir)
    tmp_db = layout.db_path.with_name("results.db.rebuild")
    tmp_db.unlink(missing_ok=True)
    with contextlib.closing(sqlite3.connect(str(tmp_db))) as db:
        _create_schema(db)
        db.executemany(_INSERT_SQL, [_db_row(r) for r in records])
        db.commit()
    if layout.db_path.exists():
        with contextlib.closing(sqlite3.connect(str(layout.db_path))) as old:
            old.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        layout.db_path.rename(layout.db_path.with_name(f"results.db.bak-{stamp}"))
    for sidecar in ("results.db-wal", "results.db-shm"):
        layout.db_path.with_name(sidecar).unlink(missing_ok=True)
    os.replace(tmp_db, layout.db_path)
    lines = "".join(json.dumps(r) + "\n" for r in records)
    _write_atomic(layout.jsonl_path, lines.encode())
    return len(records)


# ---------------------------------------------------------------------------
# Record construction (single place where NaN cleaning happens)
# ---------------------------------------------------------------------------

def _build_record(
    active: ModuleType,
    args: argparse.Namespace,
    outcome: ExperimentOutcome,
    elapsed: float,
    *,
    run_id: str,
    digest: str,
    solver_identity: str,
    evidence: Mapping[str, object],
    crash_signature: str | None = None,
    replay_of: str | None = None,
) -> dict:
    """Assemble a run record from an adapter outcome. NaN cleaning happens here."""
    metrics = outcome.metrics
    record = {
        "id": run_id,
        "adapter": active.NAME,
        "mode": args.mode,
        "target": getattr(args, active.TARGET_FLAG),
        "experiment_group": outcome.experiment_group,
        "spec_hash": digest,
        "replicate": args.replicate,
        "seed": getattr(args, active.SEED_FLAG) if active.SEED_FLAG else None,
        "parent_run_id": outcome.parent_run_id or getattr(args, "parent_run_id", None),
        "replay_of": replay_of,
        "batch_id": getattr(args, "batch_id", None),
        "status": outcome.status,
        "status_reason": outcome.status_reason,
        "crash_signature": crash_signature,
        "validated": outcome.validated,
        "elapsed": round(elapsed, 1),
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "params": run_spec(args),
    }
    record["metrics"] = {k: clean(v) for k, v in metrics.items()}
    record["provenance"] = {
        "solver_identity": solver_identity,
        "adapter": dict(outcome.provenance),
        "harness": harness_provenance(),
        "platform": platform_provenance(),
    }
    record["evidence"] = dict(evidence)
    return record


def metric_values(record: Mapping[str, object]) -> dict:
    """Every non-null metric of a record."""
    return {k: v for k, v in (record.get("metrics") or {}).items() if v is not None}


def summary(record: Mapping[str, object], on_front: bool | None) -> dict:
    """The compact stdout view of a record: set identity fields, metrics, front membership.

    Provenance, evidence, params and empty fields stay in the run file.
    """
    out = {k: record[k] for k in SUMMARY_FIELDS if record.get(k) is not None}
    out["metrics"] = metric_values(record)
    if on_front is not None:
        out["on_front"] = on_front
    return out


# ---------------------------------------------------------------------------
# Analysis views (see analysis.py)
# ---------------------------------------------------------------------------

def _read_views(db: sqlite3.Connection, active: ModuleType) -> list[dict]:
    db.row_factory = sqlite3.Row
    columns = ", ".join(VIEW_FIELDS + ("metrics", "params"))
    views = []
    for row in db.execute(f"SELECT {columns} FROM runs"):
        record = dict(row)
        record["metrics"] = json.loads(record["metrics"] or "{}")
        views.append({
            **{k: record[k] for k in VIEW_FIELDS},
            "values": metric_values(record),
            "params": json.loads(record["params"] or "{}"),
        })
        views[-1]["spec_base"] = spec_base(active, views[-1]["params"])
    return views


def load_views(layout: Layout, active: ModuleType) -> list[dict]:
    if not layout.db_path.exists():
        return []
    with contextlib.closing(open_db(layout)) as db:
        return _read_views(db, active)


def _tail_text(path: Path, max_bytes: int = LOG_TAIL_BYTES) -> str:
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - max_bytes))
        return f.read().decode(errors="replace")


def _log_signature(outcome: ExperimentOutcome) -> str | None:
    log = outcome.evidence.get("log")
    if outcome.status != "crash" or log is None or not log.is_file():
        return None
    return analysis.crash_signature(_tail_text(log))


# ---------------------------------------------------------------------------
# Core experiment
# ---------------------------------------------------------------------------

def execute(
    active: ModuleType,
    layout: Layout,
    args: argparse.Namespace,
    identity: str,
    *,
    replay_of: str | None = None,
) -> dict:
    """Run one experiment via the campaign's adapter; record it; return the record.

    `identity` is the adapter's solver_identity(args), computed once by the caller.
    """
    digest = spec_hash(active, run_spec(args), identity)
    run_id = _uuid7()
    run_dir = layout.scratch_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.monotonic()
    try:
        outcome = active.run_experiment(args, RunContext(run_id=run_id, dir=run_dir))
    except Cancelled:
        print("Run cancelled; recording it.", file=sys.stderr)
        outcome = ExperimentOutcome("crash", "cancelled")
    except Exception as e:
        # Truly-unexpected adapter failure (the adapter never returned an
        # outcome). The record still carries the full run spec, so the
        # experiment stays reproducible.
        print(f"WARNING: adapter raised: {e}", file=sys.stderr)
        outcome = ExperimentOutcome("crash", f"adapter_error: {e}")
    # Total experiment wall time: includes any validation or
    # chained sub-steps the adapter runs internally, not just one solver call.
    elapsed = time.monotonic() - t0
    peak_rss_mb = machine.children_peak_rss_mb()

    evidence = store_evidence(layout.blobs_dir, outcome.evidence)
    record = _build_record(
        active, args, outcome, elapsed,
        run_id=run_id, digest=digest, solver_identity=identity, evidence=evidence,
        crash_signature=_log_signature(outcome), replay_of=replay_of,
    )
    record["peak_rss_mb"] = round(peak_rss_mb, 1) if peak_rss_mb is not None else None
    write_run_record(layout.runs_dir, record)
    index_record(layout, record)
    _finalize_run_dir(layout, run_dir, outcome.status, run_id)
    return record


def run_once(active: ModuleType, layout: Layout, slots: Slots, args: argparse.Namespace) -> dict:
    """Run the experiment unless it is already recorded or running; return the stdout object.

    Order: claim the spec (so concurrent agents never run it twice), check for
    an earlier pass/fail run, then wait for a machine-wide slot and execute.
    """
    identity = active.solver_identity(args)
    digest = spec_hash(active, run_spec(args), identity)
    claim = try_lock(layout.claims_dir / f"{digest}-{args.replicate}.lock")
    if claim is None:
        print("This spec is running in another process right now.", file=sys.stderr)
        return {"in_progress": True, "spec_hash": digest, "replicate": args.replicate}
    try:
        duplicate = find_duplicate(layout, digest, args.replicate)
        if duplicate:
            print(
                f"Already run as {duplicate['id']}; pass --replicate N for another sample.",
                file=sys.stderr,
            )
            return {"duplicate_of": duplicate["id"], "status": duplicate["status"],
                    "status_reason": duplicate["status_reason"], "replicate": args.replicate}
        slot = acquire_slot(slots.dir, slots.capacity, report_waiting(slots.capacity))
        try:
            record = execute(active, layout, args, identity)
        finally:
            release(slot)
    finally:
        release(claim)
    on_front = None
    if record["status"] == "pass":
        on_front = record["id"] in analysis.front_ids(load_views(layout, active), active.METRICS)
    return summary(record, on_front)


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
# Replay
# ---------------------------------------------------------------------------

def _differs(a: object, b: object, tolerance: float) -> bool:
    numeric = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (a, b))
    if not numeric:
        return a != b
    scale = max(abs(a), abs(b))
    return scale > 0 and abs(a - b) / scale > tolerance


def compare_runs(original: Mapping[str, object], replay: Mapping[str, object], tolerance: float) -> list[dict]:
    """Differences that break a replay: status, or a metric beyond `tolerance` (relative)."""
    mismatches = []
    if original.get("status") != replay.get("status"):
        mismatches.append({"field": "status", "original": original.get("status"), "replay": replay.get("status")})
    before, after = metric_values(original), metric_values(replay)
    for key in sorted(before.keys() | after.keys()):
        a, b = before.get(key), after.get(key)
        if a is None or b is None or _differs(a, b, tolerance):
            mismatches.append({"field": key, "original": a, "replay": b})
    return mismatches


def replay(active: ModuleType, layout: Layout, parser: argparse.ArgumentParser, run_id: str) -> bool:
    """Re-run a recorded experiment from its spec, compare, and print the verdict."""
    path = layout.runs_dir / f"{run_id}.json"
    if not path.exists():
        raise HarnessError(f"no run record {path}")
    original = upgrade_record(json.loads(path.read_text()))
    defaults = vars(parser.parse_args([]))
    values = {**defaults, **original["params"], "replicate": original.get("replicate") or 0}
    args = with_seed(active, argparse.Namespace(**values))
    record = execute(active, layout, args, active.solver_identity(args), replay_of=run_id)
    mismatches = compare_runs(original, record, active.REPLAY_TOLERANCE)
    before = (original.get("provenance") or {}).get("solver_identity")
    print(json.dumps({
        "replay_of": run_id,
        "run_id": record["id"],
        "match": not mismatches,
        "tolerance": active.REPLAY_TOLERANCE,
        "solver_changed": before is not None and before != record["provenance"]["solver_identity"],
        "mismatches": mismatches,
    }))
    return not mismatches


# ---------------------------------------------------------------------------
# Brief, query, campaigns, lessons
# ---------------------------------------------------------------------------

def read_lessons(campaign_dir: Path) -> str:
    path = campaign_dir / LESSONS_NAME
    return path.read_text() if path.exists() else ""


def capacity_lines(
    active: ModuleType, views: list[dict], config: CampaignConfig, environ: Mapping[str, str],
) -> list[str]:
    """Measured cost per mode and what fits: runs at once and, with plan_minutes, batch size."""
    settings = machine.read_settings(machine_dir(environ))
    cores = settings.get("usable_cores") or machine.usable_cpus(environ)
    memory = settings.get("usable_memory_gb") or machine.total_memory_gb()
    slots = resolve_slots(environ)
    lines = []
    for cost in machine.mode_costs(views, active.THREADS_FLAG):
        fit = machine.runs_at_once(cores, cost.threads, memory, cost.peak_memory_gb)
        parallel = min(fit, slots.capacity, config.max_parallel or slots.capacity)
        peak = f"{cost.peak_memory_gb:.2f} GB" if cost.peak_memory_gb is not None else "peak memory unknown"
        line = (
            f"  {cost.mode}: {cost.runs} runs · median {cost.median_seconds:.3g}s · {peak} · "
            f"{cost.threads} threads → {fit} fit at once, {parallel} with current slots"
        )
        if config.plan_minutes:
            size = machine.batch_size(parallel, config.plan_minutes, cost.median_seconds)
            line += f" · batch ≤ {size} per {config.plan_minutes:g} min"
        lines.append(line)
    return lines


def brief(active: ModuleType, layout: Layout, config: CampaignConfig) -> str:
    views = load_views(layout, active)
    slots = resolve_slots(os.environ)
    machine_lines = [f"machine: {slots.capacity} run slots, {busy_slots(slots)} busy",
                     *capacity_lines(active, views, config, os.environ)]
    return analysis.render_brief(
        layout.campaign_dir.name,
        active.NAME,
        views,
        active.METRICS,
        analysis.lesson_titles(read_lessons(layout.campaign_dir)),
        machine_lines,
    )


def machine_report(environ: Mapping[str, str], root: Path) -> str:
    """Hardware, machine settings, and each campaign's measured run cost with sizing."""
    hw = machine.detect(environ)
    slots = resolve_slots(environ)
    settings = machine.read_settings(machine_dir(environ))
    lines = ["hardware:", *(f"  {line}" for line in machine.describe_hardware(hw))]
    shown = ", ".join(f"{k}={v}" for k, v in settings.items()) or "none (defaults: 1 slot, detected cores/memory)"
    lines.append(f"settings ({machine_dir(environ) / machine.MACHINE_FILE}): {shown}")
    lines.append(f"run slots: {slots.capacity}, {busy_slots(slots)} busy")
    for name in list_campaigns(root):
        campaign_dir = root / name
        try:
            config = load_config(campaign_dir)
            active = load_adapter(config.adapter)
        except (HarnessError, AdapterError) as e:
            lines.append(f"campaign {name}: {e}")
            continue
        views = _read_views_readonly(campaign_dir, active)
        plan = f" (plans every {config.plan_minutes:g} min)" if config.plan_minutes else ""
        lines.append(f"campaign {name}{plan}:")
        if views is None:
            lines.append(f"  results.db needs `python run.py rebuild --campaign {name}`")
            continue
        lines.extend(capacity_lines(active, views, config, environ) or ["  no runs yet"])
    return "\n".join(lines)


def schema_report(active: ModuleType, campaign: str) -> str:
    """What the program template's schema and evaluation sections need."""
    goals = "  ".join(f"{m}{'↓' if g == 'min' else '↑'}" for m, g in active.METRICS.items() if g)
    recorded = ", ".join(m for m, g in active.METRICS.items() if not g)
    return "\n".join([
        f"runs({', '.join(DB_COLUMNS)})",
        f"{RESULTS_VIEW}({', '.join(IDENTITY_COLUMNS + tuple(active.METRICS))}): runs with one column per metric",
        f"goals: {goals or 'none'}",
        f"recorded only: {recorded or 'none'}",
        f"flags: python run.py --campaign {campaign} --help",
    ])


def _cell(value: object) -> str:
    text = analysis.fmt(value) if value is not None else ""
    text = text.replace("\t", " ").replace("\n", " ")
    return text if len(text) <= QUERY_CELL_CHARS else text[: QUERY_CELL_CHARS - 1] + "…"


def ensure_results_view(layout: Layout, active: ModuleType) -> None:
    """(Re)create the `results` view — runs plus one column per METRICS key — and goal indexes.

    Rebuilt each time it is needed, so it always matches the adapter's
    current METRICS. Goal metrics get expression indexes for fast ORDER BY.
    """
    clashes = sorted(set(active.METRICS) & set(DB_COLUMNS))
    if clashes:
        raise HarnessError(f"adapter '{active.NAME}' METRICS reuse run column names: {clashes}")
    columns = ", ".join(
        [*IDENTITY_COLUMNS, *(f"json_extract(metrics, '$.{m}') AS {m}" for m in active.METRICS)]
    )
    indexes = "".join(
        f"CREATE INDEX IF NOT EXISTS idx_metric_{m} ON runs(json_extract(metrics, '$.{m}'));\n"
        for m, goal in active.METRICS.items() if goal
    )
    with contextlib.closing(open_db(layout)) as db:
        db.executescript(
            f"BEGIN IMMEDIATE;\nDROP VIEW IF EXISTS {RESULTS_VIEW};\n"
            f"CREATE VIEW {RESULTS_VIEW} AS SELECT {columns} FROM runs;\n{indexes}COMMIT;"
        )


def query(layout: Layout, active: ModuleType, sql: str, limit: int) -> str:
    """Run one read-only SQL statement; return tab-separated rows (header first), capped.

    The `results` view (runs plus one column per metric) is refreshed first.
    """
    if not layout.db_path.exists():
        raise HarnessError(f"no {layout.db_path} yet: run an experiment first")
    ensure_results_view(layout, active)
    uri = f"{layout.db_path.resolve().as_uri()}?mode=ro"
    try:
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as db:
            cursor = db.execute(sql)
            columns = [d[0] for d in cursor.description or ()]
            rows = cursor.fetchmany(limit + 1)
    except sqlite3.Error as e:
        raise HarnessError(f"query failed: {e}") from e
    lines = ["\t".join(columns)] + ["\t".join(_cell(v) for v in row) for row in rows[:limit]]
    if len(rows) > limit:
        lines.append(f"… more than {limit} rows; aggregate, or raise --limit")
    return "\n".join(lines)


def _read_views_readonly(campaign_dir: Path, active: ModuleType) -> list[dict] | None:
    """A campaign's run views without modifying anything; None if its DB needs a rebuild."""
    db_path = campaign_dir / "results.db"
    if not db_path.exists():
        return []
    with contextlib.closing(sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)) as db:
        if _schema_version(db) != SCHEMA_VERSION:
            return None
        return _read_views(db, active)


def campaign_status(campaign_dir: Path) -> list[str]:
    """One row of `run.py campaigns` for this campaign, without modifying anything."""
    name = campaign_dir.name
    try:
        active = load_adapter(load_config(campaign_dir).adapter)
    except (HarnessError, AdapterError) as e:
        return [name, f"error: {e}", "", "", "", "", "", ""]
    views = _read_views_readonly(campaign_dir, active)
    if views is None:
        return [name, active.NAME, "outdated schema: run.py rebuild", "", "", "", "", ""]
    if not views:
        return [name, active.NAME, "0", "0", "0", "0", "-", "-"]
    counts = Counter(v["status"] for v in views)
    last = max((v["created_at"] for v in views), default="-")[:19]
    stall = analysis.runs_since_front_change(views, active.METRICS)
    return [
        name, active.NAME, str(len(views)), str(counts["pass"]), str(counts["fail"]),
        str(counts["crash"]), last, "-" if stall is None else str(stall),
    ]


def campaigns_table(root: Path) -> str:
    header = "campaign\tadapter\truns\tpass\tfail\tcrash\tlast_run\truns_since_front"
    rows = ["\t".join(campaign_status(root / name)) for name in list_campaigns(root)]
    return "\n".join([header, *rows]) if rows else f"no campaigns under {root}"


def import_lessons(source_dir: Path, target_dir: Path) -> int:
    """Append the source campaign's lesson entries to the target's LESSONS.md as priors.

    They go under one dated import entry, with their headings demoted so they
    are not counted as the target campaign's own lessons. Returns the count.
    """
    entries = analysis.lesson_entries(read_lessons(source_dir))
    if not entries:
        raise HarnessError(f"no dated entries in {source_dir / LESSONS_NAME}")
    today = datetime.date.today().isoformat()
    block = [
        f"## {today} — Imported {len(entries)} lessons from campaign {source_dir.name}",
        "",
        "- kind: import",
        f"- source: {source_dir / LESSONS_NAME}",
        "- status: hypothesis — priors from another campaign until this campaign's runs confirm them",
        "",
        *[entry.replace("## ", "### ", 1) + "\n" for entry in entries],
    ]
    target = target_dir / LESSONS_NAME
    existing = target.read_text() if target.exists() else ""
    separator = "" if not existing or existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    with open(target, "a") as f:
        f.write(separator + "\n".join(block))
    return len(entries)


# ---------------------------------------------------------------------------
# Batches (planning in batch.py; execution here)
# ---------------------------------------------------------------------------

def _flag(dest: str) -> str:
    return "--" + dest.replace("_", "-")


def planned_argv(planned: batch.PlannedRun, campaign: str, batch_id: str) -> list[str]:
    """The run.py command-line arguments that execute one planned run."""
    argv = ["--campaign", campaign, "--batch-id", batch_id, "--replicate", str(planned.replicate)]
    if planned.parent_run_id:
        argv += ["--parent-run-id", planned.parent_run_id]
    for dest, value in sorted(planned.spec.items()):
        argv += [_flag(dest), str(value)]
    return argv


def _parse_planned(parser: argparse.ArgumentParser, argv: list[str]) -> argparse.Namespace | str:
    """The parsed args for `argv`, or argparse's error message."""
    errors = io.StringIO()
    try:
        with contextlib.redirect_stderr(errors):
            return parser.parse_args(argv)
    except SystemExit:
        return errors.getvalue().strip().splitlines()[-1]


@dataclass
class _IdentityCache:
    """solver_identity per distinct execution-flag values (contract: it depends on nothing else)."""

    active: ModuleType
    known: dict = field(default_factory=dict)

    def __call__(self, args: argparse.Namespace) -> str:
        key = json.dumps({k: getattr(args, k, None) for k in self.active.EXECUTION_FLAGS}, sort_keys=True)
        if key not in self.known:
            self.known[key] = self.active.solver_identity(args)
        return self.known[key]


def check_planned(
    active: ModuleType, layout: Layout, parser: argparse.ArgumentParser, planned: list[batch.PlannedRun],
    campaign: str, identity: _IdentityCache,
) -> tuple[list[str], list[str], list[str]]:
    """(argv per run to launch, already-recorded duplicates, errors), without running anything."""
    launch, duplicates, errors, seen = [], [], [], set()
    for i, run_plan in enumerate(planned):
        argv = planned_argv(run_plan, campaign, "check")
        parsed = _parse_planned(parser, argv)
        if isinstance(parsed, str):
            errors.append(f"{run_plan.stage} run {i} {dict(run_plan.spec)}: {parsed}")
            continue
        args = with_seed(active, parsed)
        digest = spec_hash(active, run_spec(args), identity(args))
        key = (digest, args.replicate)
        if key in seen:
            continue
        seen.add(key)
        duplicate = find_duplicate(layout, digest, args.replicate)
        if duplicate:
            duplicates.append(duplicate["id"])
        else:
            launch.append(argv)
    return launch, duplicates, errors


def _result_view(record: Mapping[str, object]) -> dict:
    """A run's record as an analysis view (enough for promotion and the summary)."""
    return {
        "id": record["id"], "status": record["status"], "status_reason": record.get("status_reason"),
        "crash_signature": record.get("crash_signature"), "target": record.get("target"),
        "mode": record.get("mode"), "values": metric_values(record),
    }


def _read_record(layout: Layout, run_id: str) -> dict:
    return upgrade_record(json.loads((layout.runs_dir / f"{run_id}.json").read_text()))


def launch_runs(
    layout: Layout, argvs: list[list[str]], parallel: int, same_crash: int,
    completed: list[dict], log: Path,
) -> tuple[list[dict], str | None, int]:
    """Run each argv as its own `run.py` process, `parallel` at a time.

    Stops launching (in-flight runs finish) once the early-stop rule fires.
    Returns (result views of this call's runs, stop reason or None, number of
    children that exited without a result — their stderr is in `log`). On
    cancellation, children are sent SIGTERM so each records itself as cancelled.
    """
    queue, running, results, stop, failed = list(argvs), [], [], None, 0
    with open(log, "a") as stderr:
        try:
            while running or (queue and stop is None):
                while queue and len(running) < parallel and stop is None:
                    argv = queue.pop(0)
                    running.append(subprocess.Popen(
                        [sys.executable, str(REPO_ROOT / "run.py"), *argv],
                        stdout=subprocess.PIPE, stderr=stderr, text=True,
                    ))
                finished = [proc for proc in running if proc.poll() is not None]
                if not finished:
                    time.sleep(BATCH_POLL_SECONDS)
                    continue
                for proc in finished:
                    running.remove(proc)
                    out = proc.communicate()[0].strip().splitlines()
                    printed = json.loads(out[-1]) if out else {}
                    run_id = printed.get("id") or printed.get("duplicate_of")
                    if run_id is None:
                        failed += proc.returncode != 0
                        continue
                    view = {**_result_view(_read_record(layout, run_id)), "reused": "duplicate_of" in printed}
                    results.append(view)
                    completed.append(view)
                    stop = stop or batch.should_stop(completed, same_crash)
        except BaseException:
            for proc in running:
                proc.terminate()
            for proc in running:
                proc.wait()
            raise
    return results, stop, failed


def _summary_lines(stage: str, results: list[dict], metric_goals: Mapping[str, str | None]) -> list[str]:
    counts = Counter(r["status"] for r in results)
    reused = sum(1 for r in results if r.get("reused"))
    goals = analysis.active_goals(results, metric_goals)
    lines = [
        f"{stage}: {len(results)} runs ({reused} already recorded) — "
        f"{counts['pass']} pass, {counts['fail']} fail, {counts['crash']} crash"
    ]
    front = analysis.pareto_front(results, goals)
    for r in front[:BATCH_SUMMARY_ROWS]:
        values = " ".join(f"{m}={analysis.fmt(r['values'][m])}" for m, _ in goals)
        lines.append(f"  front {r['id']} {r['mode']}/{r['target']} {values}")
    causes = Counter(batch.crash_key(r) for r in results if r["status"] == "crash")
    for cause, n in causes.most_common(3):
        lines.append(f"  crash {n}× {cause}")
    return lines


def run_batch(
    active: ModuleType, layout: Layout, parser: argparse.ArgumentParser, campaign: str,
    path: Path, parallel: int, dry_run: bool,
) -> int:
    """Validate a batch file, then run its stages in order; print a capped summary."""
    plan = batch.load_batch(path, active.METRICS)
    identity = _IdentityCache(active)
    dests = set(vars(parser.parse_args([])))
    errors, previews = [], []
    for stage in plan.stages:
        if stage.source is None:
            launch, duplicates, stage_errors = check_planned(
                active, layout, parser, batch.plan_stage(stage), campaign, identity
            )
            errors += stage_errors
            previews.append(f"{stage.name}: {len(launch)} to run, {len(duplicates)} already recorded")
        else:
            unknown = sorted(k for k in [*stage.base, *stage.carry] if k not in dests)
            if unknown:
                errors.append(f"{stage.name}: unknown params {unknown}")
            previews.append(
                f"{stage.name}: top {stage.top} of {stage.source} by {stage.rank_by} × {stage.replicates} replicates"
            )
    if errors:
        raise HarnessError("batch has invalid runs; nothing was launched:\n" + "\n".join(f"- {e}" for e in errors))
    if dry_run:
        print(f"batch {path} (valid) · parallel {parallel}\n" + "\n".join(previews))
        return 0

    batch_id = _uuid7()
    started = time.monotonic()
    record_path = layout.batches_dir / f"{batch_id}.json"
    batch_record = {
        "id": batch_id, "source": str(path), "sha256": sha256_file(path),
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "hypothesis": plan.hypothesis, "lessons": plan.lessons, "parallel": parallel,
        "batch": plan.raw, "status": "running", "stages": {},
    }
    _write_atomic(record_path, json.dumps(batch_record, indent=1).encode())
    log = layout.batches_dir / f"{batch_id}.log"
    results_by_stage: dict[str, list[dict]] = {}
    completed: list[dict] = []
    stop = None
    lines = []
    try:
        for stage in plan.stages:
            if stop:
                break
            if stage.source is None:
                planned = batch.plan_stage(stage)
            else:
                selected = batch.select_runs(stage, results_by_stage.get(stage.source, []), active.METRICS)
                params = {r["id"]: _read_record(layout, r["id"])["params"] for r in selected}
                planned = batch.plan_promotion(stage, selected, params)
            _, _, stage_errors = check_planned(active, layout, parser, planned, campaign, identity)
            if stage_errors:
                lines.append(f"{stage.name}: skipped, invalid promoted runs: {stage_errors[0]}")
                continue
            argvs = [planned_argv(p, campaign, batch_id) for p in planned]
            results, stop, failed = launch_runs(layout, argvs, parallel, plan.same_crash_stop, completed, log)
            results_by_stage[stage.name] = results
            batch_record["stages"][stage.name] = [r["id"] for r in results]
            lines += _summary_lines(stage.name, results, active.METRICS)
            if failed:
                lines.append(f"  {failed} runs exited without a result (see the stderr log)")
        batch_record["status"] = "stopped" if stop else "done"
    except BaseException:
        batch_record["status"] = "cancelled"
        raise
    finally:
        batch_record["stop_reason"] = stop
        batch_record["finished_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        _write_atomic(record_path, json.dumps(batch_record, indent=1).encode())
    head = (
        f"batch {batch_id} · {len(completed)} runs · {round(time.monotonic() - started)}s · "
        f"{'stopped: ' + stop if stop else 'done'}"
    )
    print("\n".join([head, *lines, f"details: {record_path} · children's stderr: {log}"]))
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser(active: ModuleType, campaign: str | None) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run one optimization experiment")
    p.add_argument(
        "--campaign",
        default=campaign,
        help=f"campaign under campaigns/ (default: ${CAMPAIGN_ENV}, or the only campaign)",
    )
    p.add_argument(
        "--replicate", type=int, default=0,
        help="sample index of this spec; a new index draws a new derived seed (default 0)",
    )
    p.add_argument("--batch-id", default=None, help="set by `run.py batch`: the batch this run belongs to")
    p.add_argument("--parent-run-id", default=None, help="the run this one builds on (set by batch promotion)")
    p.add_argument(
        "--mode",
        choices=list(active.MODES),
        default=active.MODES[0],
        help="mode exposed by the campaign's adapter",
    )
    active.add_arguments(p)
    return p


def _dispatch(command: str, argv: list[str]) -> int:
    if command == "campaigns":
        argparse.ArgumentParser(description="List every campaign").parse_args(argv)
        print(campaigns_table(campaigns_root(os.environ)))
        return 0
    if command == "machine":
        p = argparse.ArgumentParser(description="Hardware, run slots, run cost and sizing; flags save settings")
        p.add_argument("--max-parallel", type=int, help="machine-wide run slots")
        p.add_argument("--usable-cores", type=int, help="cores the harness may use")
        p.add_argument("--usable-memory-gb", type=float, help="memory the harness may use")
        args = p.parse_args(argv)
        updates = {k: getattr(args, k) for k in machine.MACHINE_KEYS}
        if any(v is not None and v <= 0 for v in updates.values()):
            raise HarnessError("machine settings must be > 0")
        if any(v is not None for v in updates.values()):
            machine.write_settings(machine_dir(os.environ), updates)
        print(machine_report(os.environ, campaigns_root(os.environ)))
        return 0

    # Campaign selection comes first: the campaign's adapter defines every
    # other flag, so the full parser can only be built once it is loaded.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--campaign", default=os.environ.get(CAMPAIGN_ENV))
    selection, _ = pre.parse_known_args(argv)
    campaign_dir = resolve_campaign(selection.campaign, campaigns_root(os.environ))
    config = load_config(campaign_dir)
    apply_env(config.env, os.environ)
    layout = resolve_layout(campaign_dir, os.environ)

    if command == "rebuild":
        p = argparse.ArgumentParser(description="Regenerate results.db/.jsonl from runs/*.json")
        p.add_argument("--campaign", default=selection.campaign)
        p.add_argument("--from-jsonl", type=Path, help="first import records from an older results.jsonl")
        args = p.parse_args(argv)
        imported = import_jsonl(layout, args.from_jsonl) if args.from_jsonl else 0
        rows = rebuild(layout)
        print(json.dumps({"campaign": campaign_dir.name, "imported": imported, "rows": rows}))
        return 0

    if command == "import-lessons":
        p = argparse.ArgumentParser(description="Append another campaign's lessons as priors")
        p.add_argument("--from", dest="source", required=True, help="campaign to import from")
        p.add_argument("--campaign", default=selection.campaign)
        args = p.parse_args(argv)
        source_dir = resolve_campaign(args.source, campaigns_root(os.environ))
        count = import_lessons(source_dir, campaign_dir)
        print(json.dumps({"campaign": campaign_dir.name, "imported_from": source_dir.name, "entries": count}))
        return 0

    active = load_adapter(config.adapter)
    if command == "brief":
        p = argparse.ArgumentParser(description="Fixed-size digest of the campaign")
        p.add_argument("--campaign", default=selection.campaign)
        p.parse_args(argv)
        print(brief(active, layout, config))
        return 0
    if command == "schema":
        p = argparse.ArgumentParser(description="Columns and metric goals for the program file")
        p.add_argument("--campaign", default=selection.campaign)
        p.parse_args(argv)
        print(schema_report(active, campaign_dir.name))
        return 0
    if command == "query":
        p = argparse.ArgumentParser(description="Run one read-only SQL statement on results.db")
        p.add_argument("sql")
        p.add_argument("--limit", type=int, default=QUERY_DEFAULT_LIMIT)
        p.add_argument("--campaign", default=selection.campaign)
        args = p.parse_args(argv)
        print(query(layout, active, args.sql, args.limit))
        return 0

    parser = build_parser(active, selection.campaign)
    if command == "replay":
        p = argparse.ArgumentParser(description="Re-run a recorded experiment and compare")
        p.add_argument("run_id")
        p.add_argument("--campaign", default=selection.campaign)
        args = p.parse_args(argv)
        check_required_env(active, os.environ, campaign_dir)
        return 0 if replay(active, layout, parser, args.run_id) else REPLAY_MISMATCH_EXIT

    if command == "batch":
        p = argparse.ArgumentParser(description="Run a planned batch of experiments (see batch.py)")
        p.add_argument("file", type=Path)
        p.add_argument("--campaign", default=selection.campaign)
        p.add_argument("--parallel", type=int, default=None, help="runs at once (capped by the campaign and machine)")
        p.add_argument("--dry-run", action="store_true", help="validate and show the plan without running")
        args = p.parse_args(argv)
        if args.parallel is not None and args.parallel < 1:
            raise HarnessError("--parallel must be >= 1")
        check_required_env(active, os.environ, campaign_dir)
        slots = resolve_slots(os.environ)
        parallel = min(args.parallel or config.max_parallel or slots.capacity, config.max_parallel or slots.capacity, slots.capacity)
        return run_batch(active, layout, parser, campaign_dir.name, args.file, parallel, args.dry_run)

    args = parser.parse_args(argv)
    check_required_env(active, os.environ, campaign_dir)
    print(json.dumps(run_once(active, layout, resolve_slots(os.environ), with_seed(active, args))))
    return 0


def _raise_cancelled(_signum: int, _frame: object) -> None:
    raise Cancelled()


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    command = argv.pop(0) if argv and argv[0] in COMMANDS else "run"
    signal.signal(signal.SIGTERM, _raise_cancelled)
    try:
        code = _dispatch(command, argv)
    except (HarnessError, AdapterError, batch.BatchError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    except Cancelled:
        print("Cancelled.", file=sys.stderr)
        sys.exit(CANCELLED_EXIT)
    sys.exit(code)


if __name__ == "__main__":
    main()
