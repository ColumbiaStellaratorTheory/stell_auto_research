#!/usr/bin/env python3
"""Run optimization experiments for a campaign and record them.

The harness core is solver-agnostic: it owns campaign selection, the run
records, the scratch/evidence lifecycle, and the agent-facing CLI skeleton.
The campaign's solver adapter (named in its config.json; see adapter.py /
contract.py) owns everything solver-specific.

Usage (experiment flags come from the campaign's adapter; this shows the toy):
    python run.py --campaign demo --problem rastrigin --dim 4      # run one experiment
    python run.py --campaign demo --problem rastrigin --replicate 1  # another seed of it
    python run.py replay <run-id> --campaign demo                  # re-run and compare
    python run.py rebuild --campaign demo [--from-jsonl FILE]      # regenerate results.db/.jsonl

`--campaign` (or $AUTORESEARCH_CAMPAIGN) may be omitted when exactly one
campaign exists. Each run is written atomically to the campaign's
runs/<run-id>.json — the source of truth — then indexed in results.db; a
single-line JSON summary goes to stdout.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import hashlib
import json
import os
import platform
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
from contract import ExperimentOutcome, RunContext, clean, git_output, sha256_file

REPO_ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = REPO_ROOT / "schema.sql"
SCHEMA_VERSION = 2
CONFIG_NAME = "config.json"
CAMPAIGN_ENV = "AUTORESEARCH_CAMPAIGN"
CAMPAIGNS_DIR_ENV = "AUTORESEARCH_CAMPAIGNS_DIR"
KEEP_ARTIFACTS_CHOICES = ("none", "pass", "all")
COMMANDS = ("replay", "rebuild")
# Core flags that select how the harness runs, not what the solver computes.
CORE_FLAGS = ("campaign", "replicate")
# Prior results that make a repeat redundant; a crash can always be retried.
DEDUPE_STATUSES = ("pass", "fail")
REPLAY_MISMATCH_EXIT = 2

# Canonical metric keys with a dedicated DB column (must match schema.sql). Any
# other key an adapter emits is preserved in the row's `metrics` JSON blob.
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
IDENTITY_COLUMNS = (
    "id", "coil_type", "solver", "equilibrium", "experiment_group",
    "spec_hash", "replicate", "seed", "parent_run_id", "replay_of",
    "status", "status_reason", "validated", "elapsed", "created_at",
)
JSON_COLUMNS = ("metrics", "params", "provenance", "evidence")
BOOL_COLUMNS = ("optimizer_success", "self_intersecting")
DB_COLUMNS = IDENTITY_COLUMNS + COLUMN_METRIC_KEYS + JSON_COLUMNS
# Record fields kept out of the stdout summary (they stay in the run file).
DETAIL_FIELDS = ("provenance", "evidence")


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

    @property
    def runs_dir(self) -> Path:
        return self.campaign_dir / "runs"

    @property
    def blobs_dir(self) -> Path:
        return self.campaign_dir / "blobs"

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


def derive_seed(active: ModuleType, spec: Mapping[str, object], replicate: int) -> int:
    """Seed for an unset SEED_FLAG: a function of the spec (minus the seed) and replicate.

    Independent of the solver identity, so the same experiment keeps its seed
    across solver versions and their results stay comparable.
    """
    fields = {k: v for k, v in _hashed_fields(active, spec).items() if k != active.SEED_FLAG}
    digest = _canonical_hash({"adapter": active.NAME, "spec": fields, "replicate": replicate})
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
    records = [json.loads(p.read_text()) for p in runs_dir.glob("*.json")]
    return sorted(records, key=lambda r: (r.get("created_at") or "", r["id"]))


def _db_row(record: Mapping[str, object]) -> dict:
    row = {}
    for column in DB_COLUMNS:
        value = record.get(column)
        if column in BOOL_COLUMNS:
            value = _bool_to_int(value)
        elif column in JSON_COLUMNS:
            value = json.dumps(value if value is not None else {})
        row[column] = value
    return row


_INSERT_SQL = (
    f"INSERT INTO runs ({', '.join(DB_COLUMNS)}) "
    f"VALUES ({', '.join(':' + c for c in DB_COLUMNS)})"
)


def _create_schema(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA_PATH.read_text())
    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def open_db(layout: Layout) -> sqlite3.Connection:
    """Open the campaign's DB, creating it if absent; refuse an outdated schema."""
    db = sqlite3.connect(str(layout.db_path))
    has_runs = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'"
    ).fetchone()
    if not has_runs:
        _create_schema(db)
        return db
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        db.close()
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
    """A results.jsonl record from an older harness, given the current record fields."""
    record = {column: raw.get(column) for column in IDENTITY_COLUMNS + COLUMN_METRIC_KEYS}
    record.update({
        "metrics": raw.get("metrics") or {},
        "params": raw.get("params") or {},
        "provenance": {"imported_from": str(source)},
        "evidence": {},
    })
    return record


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
    replay_of: str | None = None,
) -> dict:
    """Assemble a run record from an adapter outcome. NaN cleaning happens here.

    Canonical metric keys with a column are projected to top-level fields; every
    other emitted key is preserved (cleaned) in `metrics` for JSON storage.
    """
    metrics = outcome.metrics
    record = {
        "id": run_id,
        "coil_type": active.NAME,
        "solver": args.solver,
        "equilibrium": getattr(args, active.TARGET_FLAG),
        "experiment_group": outcome.experiment_group,
        "spec_hash": digest,
        "replicate": args.replicate,
        "seed": getattr(args, active.SEED_FLAG) if active.SEED_FLAG else None,
        "parent_run_id": outcome.parent_run_id,
        "replay_of": replay_of,
        "status": outcome.status,
        "status_reason": outcome.status_reason,
        "validated": outcome.validated,
        "elapsed": round(elapsed, 1),
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "params": run_spec(args),
    }
    for key in COLUMN_METRIC_KEYS:
        record[key] = clean(metrics.get(key))
    record["metrics"] = {
        k: clean(v) for k, v in metrics.items() if k not in COLUMN_METRIC_KEYS
    }
    record["provenance"] = {
        "solver_identity": solver_identity,
        "adapter": dict(outcome.provenance),
        "harness": harness_provenance(),
        "platform": platform_provenance(),
    }
    record["evidence"] = dict(evidence)
    return record


def summary(record: Mapping[str, object]) -> dict:
    """The stdout view of a record: everything except the bulky provenance/evidence."""
    return {k: v for k, v in record.items() if k not in DETAIL_FIELDS}


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
    except Exception as e:
        # Truly-unexpected adapter failure (the adapter never returned an
        # outcome). The record still carries the full run spec, so the
        # experiment stays reproducible.
        print(f"WARNING: adapter raised: {e}", file=sys.stderr)
        outcome = ExperimentOutcome("crash", f"adapter_error: {e}")
    # Total experiment wall time: includes any validation (e.g. Poincaré) or
    # chained sub-steps the adapter runs internally, not just one solver call.
    elapsed = time.monotonic() - t0

    evidence = store_evidence(layout.blobs_dir, outcome.evidence)
    record = _build_record(
        active, args, outcome, elapsed,
        run_id=run_id, digest=digest, solver_identity=identity, evidence=evidence,
        replay_of=replay_of,
    )
    write_run_record(layout.runs_dir, record)
    index_record(layout, record)
    _finalize_run_dir(layout, run_dir, outcome.status, run_id)
    return record


def run_once(active: ModuleType, layout: Layout, args: argparse.Namespace) -> None:
    """Run the experiment unless an identical pass/fail run is already recorded."""
    identity = active.solver_identity(args)
    digest = spec_hash(active, run_spec(args), identity)
    duplicate = find_duplicate(layout, digest, args.replicate)
    if duplicate:
        print(
            f"Already run as {duplicate['id']}; pass --replicate N for another sample.",
            file=sys.stderr,
        )
        print(json.dumps({"duplicate_of": duplicate["id"], "status": duplicate["status"],
                          "status_reason": duplicate["status_reason"],
                          "spec_hash": digest, "replicate": args.replicate}))
        return
    print(json.dumps(summary(execute(active, layout, args, identity))))


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

def _metric_values(record: Mapping[str, object]) -> dict:
    values = {k: record.get(k) for k in COLUMN_METRIC_KEYS}
    values.update(record.get("metrics") or {})
    return {k: v for k, v in values.items() if v is not None}


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
    before, after = _metric_values(original), _metric_values(replay)
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
    original = json.loads(path.read_text())
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
    p.add_argument(
        "--solver",
        choices=list(active.SOLVER_MODES),
        default=active.SOLVER_MODES[0],
        help="solver mode exposed by the campaign's adapter",
    )
    active.add_arguments(p)
    return p


def _dispatch(command: str, argv: list[str]) -> int:
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

    active = load_adapter(config.adapter)
    parser = build_parser(active, selection.campaign)
    if command == "replay":
        p = argparse.ArgumentParser(description="Re-run a recorded experiment and compare")
        p.add_argument("run_id")
        p.add_argument("--campaign", default=selection.campaign)
        args = p.parse_args(argv)
        check_required_env(active, os.environ, campaign_dir)
        return 0 if replay(active, layout, parser, args.run_id) else REPLAY_MISMATCH_EXIT

    args = parser.parse_args(argv)
    check_required_env(active, os.environ, campaign_dir)
    run_once(active, layout, with_seed(active, args))
    return 0


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    command = argv.pop(0) if argv and argv[0] in COMMANDS else "run"
    try:
        code = _dispatch(command, argv)
    except (HarnessError, AdapterError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    sys.exit(code)


if __name__ == "__main__":
    main()
