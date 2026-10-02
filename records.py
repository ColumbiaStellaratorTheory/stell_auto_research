"""A campaign's records: run files, evidence, and the results.db index over them.

Each run is one JSON file, runs/<run-id>.json, written atomically and never
rewritten: the source of truth. Evidence files are stored once each, by
content hash, under blobs/. results.db is a query index regenerated from the
run files by `rebuild`. Its `runs` table is defined once, by COLUMNS; the
`results` view adds one column per metric the campaign's adapter declares.
Every open, write and rebuild of results.db holds the campaign DB lock.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import sqlite3
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Mapping

import analysis
from campaign import HarnessError, Layout
from contract import sha256_file
from locks import acquire, release

# Bump with any change to COLUMNS. A DB from a record-backed older version is
# rebuilt from runs/ when opened.
SCHEMA_VERSION = 8
# From this version on every DB row has a run file, so an older DB can be
# rebuilt from runs/ without losing anything.
FIRST_RECORD_BACKED_VERSION = 2
# Prior results that make a repeat redundant; a crash can always be retried.
DEDUPE_STATUSES = ("pass", "fail")
# The view that turns each of the adapter's METRICS into a column.
RESULTS_VIEW = "results"
QUERY_CELL_CHARS = 120


@dataclass(frozen=True)
class Column:
    """One column of the `runs` table.

    sql: its SQLite type and constraints. is_json: holds a JSON object.
    summary: shown, when set, in the one-line stdout summary of a run.
    view: read into the analysis views (see analysis.py).
    index: name of the index it belongs to; columns sharing a name form one
        composite index, in column order.
    """

    name: str
    sql: str
    doc: str
    is_json: bool = False
    summary: bool = False
    view: bool = False
    index: str | None = None


# The single definition of the runs table; its DDL, indexes and every field
# list below derive from it. Run records carry the same fields.
COLUMNS = (
    Column("id", "TEXT PRIMARY KEY", "run id (UUIDv7); the record is runs/<id>.json", summary=True, view=True),
    Column("adapter", "TEXT NOT NULL", "adapter NAME"),
    Column("mode", "TEXT NOT NULL", "adapter mode (--mode)", summary=True, view=True, index="target"),
    Column("target", "TEXT NOT NULL", "value of the adapter's TARGET_FLAG", summary=True, view=True, index="target"),
    Column("status", "TEXT NOT NULL", "pass | fail | crash", summary=True, view=True, index="status"),
    Column("status_reason", "TEXT", "short machine-readable tag, e.g. ok, timeout", summary=True, view=True),
    Column("crash_signature", "TEXT", "normalized log line naming why a crash died",
           summary=True, view=True, index="crash"),
    Column("validated", "TEXT", "pass | fail | error | NULL (not attempted)", summary=True, view=True),
    Column("parent_run_id", "TEXT", "run this one built on (warm start, batch promotion)", summary=True, index="parent"),
    Column("replay_of", "TEXT", "run this one replays (`run.py replay`)", summary=True),
    Column("batch_id", "TEXT", "batch this run belongs to (`run.py batch`; file under batches/)",
           summary=True, index="batch"),
    Column("spec_hash", "TEXT", "hash of what was asked: adapter, flags, solver identity", index="spec"),
    Column("replicate", "INTEGER", "--replicate index; with spec_hash, the dedupe key",
           summary=True, view=True, index="spec"),
    Column("seed", "INTEGER", "value of the adapter's SEED_FLAG (given or derived); NULL if the solver has none",
           summary=True, view=True),
    Column("elapsed", "REAL", "seconds, including validation and chained steps", summary=True, view=True),
    Column("peak_rss_mb", "REAL", "peak resident memory of the solver processes (NULL on Windows)", view=True),
    Column("created_at", "TEXT", "UTC ISO-8601 time the run was recorded", view=True, index="created"),
    Column("metrics", "TEXT", "every metric the adapter emitted", is_json=True, view=True),
    Column("params", "TEXT", "every flag of the run, defaults included", is_json=True, view=True),
    Column("provenance", "TEXT", "solver identity/commit, command, input hashes, harness commit, platform", is_json=True),
    Column("evidence", "TEXT", "kept files: name -> {sha256, bytes}, stored under blobs/", is_json=True),
)
COLUMN_NAMES = tuple(c.name for c in COLUMNS)
JSON_COLUMNS = tuple(c.name for c in COLUMNS if c.is_json)
SCALAR_COLUMNS = tuple(c.name for c in COLUMNS if not c.is_json)
SUMMARY_FIELDS = tuple(c.name for c in COLUMNS if c.summary)
VIEW_FIELDS = tuple(c.name for c in COLUMNS if c.view)


def _schema_sql() -> str:
    """CREATE statements for the runs table and its indexes (no PRAGMAs: they run in one transaction)."""
    last = len(COLUMNS) - 1
    lines = [f"    {c.name} {c.sql}{',' if i < last else ''}  -- {c.doc}" for i, c in enumerate(COLUMNS)]
    indexes: dict[str, list[str]] = {}
    for c in COLUMNS:
        if c.index:
            indexes.setdefault(c.index, []).append(c.name)
    return "\n".join([
        "CREATE TABLE IF NOT EXISTS runs (", *lines, ");",
        *(f"CREATE INDEX IF NOT EXISTS idx_runs_{name} ON runs({', '.join(cols)});" for name, cols in indexes.items()),
    ])


SCHEMA_SQL = _schema_sql()
_INSERT_SQL = f"INSERT INTO runs ({', '.join(COLUMN_NAMES)}) VALUES ({', '.join(':' + c for c in COLUMN_NAMES)})"


# ---------------------------------------------------------------------------
# Run files and evidence
# ---------------------------------------------------------------------------

def write_atomic(path: Path, data: bytes) -> None:
    """Write `data` to `path` so readers see either nothing or the whole file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _record_path(runs_dir: Path, run_id: str) -> Path:
    return runs_dir / f"{run_id}.json"


def write_run_record(runs_dir: Path, record: Mapping[str, object]) -> Path:
    path = _record_path(runs_dir, str(record["id"]))
    write_atomic(path, (json.dumps(record, indent=1) + "\n").encode())
    return path


def read_run_record(runs_dir: Path, run_id: str) -> dict:
    path = _record_path(runs_dir, run_id)
    if not path.exists():
        raise HarnessError(f"no run record {path}")
    return json.loads(path.read_text())


def read_run_records(runs_dir: Path) -> list[dict]:
    """Every run record, ordered by (created_at, id)."""
    records = [json.loads(p.read_text()) for p in runs_dir.glob("*.json")]
    return sorted(records, key=lambda r: (r.get("created_at") or "", r["id"]))


def store_evidence(blobs_dir: Path, files: Mapping[str, Path]) -> dict:
    """Store each existing evidence file by content hash; return name → {sha256, bytes}."""
    stored = {}
    for name, path in sorted(files.items()):
        if not path.is_file():
            continue
        digest = sha256_file(path)
        blob = blobs_dir / digest[:2] / digest
        if not blob.exists():
            write_atomic(blob, path.read_bytes())
        stored[name] = {"sha256": digest, "bytes": path.stat().st_size}
    return stored


# ---------------------------------------------------------------------------
# The DB index
# ---------------------------------------------------------------------------

# Campaign DB locks held here: (canonical lock path, thread) -> [fd, depth].
# Re-entrant per thread, because a rebuild can start inside an insert that
# opened an outdated DB; each thread takes its own OS lock, so threads exclude
# each other like processes do.
_DB_LOCKS: dict[tuple[Path, int], list] = {}


@contextlib.contextmanager
def campaign_db_lock(layout: Layout):
    """Serialize every open, write and rebuild of results.db across processes and threads."""
    path = (layout.campaign_dir / ".db.lock").resolve()
    key = (path, threading.get_ident())
    held = _DB_LOCKS.get(key)
    if held is None:
        held = _DB_LOCKS[key] = [acquire(path), 0]
    held[1] += 1
    try:
        yield
    finally:
        held[1] -= 1
        if held[1] == 0:
            release(held[0])
            del _DB_LOCKS[key]


def _db_row(record: Mapping[str, object]) -> dict:
    row = {}
    for column in COLUMN_NAMES:
        value = record.get(column)
        if column in JSON_COLUMNS:
            value = json.dumps(value if value is not None else {})
        row[column] = value
    return row


def _create_schema(db: sqlite3.Connection) -> None:
    """Create the tables and stamp the schema version in one transaction.

    Concurrent first runs must never see the table without its version, so
    both happen atomically; IF NOT EXISTS makes a second creator a no-op.
    """
    db.execute("PRAGMA journal_mode = WAL")
    db.executescript(f"BEGIN IMMEDIATE;\n{SCHEMA_SQL}\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;")


def _schema_version(db: sqlite3.Connection) -> int | None:
    """The DB's schema version, or None when it has no runs table yet."""
    has_runs = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'"
    ).fetchone()
    return db.execute("PRAGMA user_version").fetchone()[0] if has_runs else None


def open_db(layout: Layout, active: ModuleType) -> sqlite3.Connection:
    """Open the campaign's DB, creating it if absent.

    Connecting, creating and migrating all happen under the campaign DB lock,
    so no process opens (or creates) the file while a rebuild replaces it,
    and concurrent openers of an outdated DB rebuild it once. A DB from an
    older record-backed schema is rebuilt from runs/; one from before run
    files existed is refused.
    """
    with campaign_db_lock(layout):
        db = sqlite3.connect(str(layout.db_path))
        version = _schema_version(db)
        if version is None:
            _create_schema(db)
            return db
        if version == SCHEMA_VERSION:
            return db
        db.close()
        if version >= FIRST_RECORD_BACKED_VERSION:
            print(f"results.db schema {version} -> {SCHEMA_VERSION}: rebuilding from runs/", file=sys.stderr)
            rebuild(layout, active)
            return sqlite3.connect(str(layout.db_path))
        raise HarnessError(
            f"{layout.db_path} has schema version {version}, this harness needs "
            f"{SCHEMA_VERSION}. Regenerate it: python run.py rebuild "
            f"--campaign {layout.campaign_dir.name}"
        )


def find_duplicate(layout: Layout, active: ModuleType, digest: str, replicate: int) -> dict | None:
    """The latest pass/fail run with this spec hash and replicate, if any."""
    if not layout.db_path.exists():
        return None
    with contextlib.closing(open_db(layout, active)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            f"SELECT id, status, status_reason FROM runs WHERE spec_hash = ? AND replicate = ? "
            f"AND status IN ({', '.join('?' for _ in DEDUPE_STATUSES)}) "
            f"ORDER BY created_at DESC LIMIT 1",
            (digest, replicate, *DEDUPE_STATUSES),
        ).fetchone()
    return dict(row) if row else None


def index_record(layout: Layout, active: ModuleType, record: Mapping[str, object]) -> None:
    """Add one run record to results.db. A failure leaves the run file intact.

    Held under the campaign DB lock, so a concurrent rebuild cannot swap the
    DB file between this insert's open and commit.
    """
    try:
        with campaign_db_lock(layout), contextlib.closing(open_db(layout, active)) as db:
            db.execute(_INSERT_SQL, _db_row(record))
            db.commit()
    except (sqlite3.Error, HarnessError) as e:
        print(f"WARNING: results.db not updated ({e}); run `python run.py rebuild`", file=sys.stderr)


def rebuild(layout: Layout, active: ModuleType) -> int:
    """Regenerate results.db from runs/*.json; return the row count.

    Runs under the campaign DB lock, so no insert lands in a DB that is about
    to be replaced and two rebuilds never interleave. The previous DB, if
    any, is kept as results.db.bak-<timestamp>.
    """
    db_path = layout.db_path
    with campaign_db_lock(layout):
        records = read_run_records(layout.runs_dir)
        tmp_db = db_path.with_name(f"{db_path.name}.rebuild-{os.getpid()}")
        tmp_db.unlink(missing_ok=True)
        with contextlib.closing(sqlite3.connect(str(tmp_db))) as db:
            _create_schema(db)
            db.executemany(_INSERT_SQL, [_db_row(r) for r in records])
            db.commit()
            _create_results_view(db, active)
        if db_path.exists():
            with contextlib.closing(sqlite3.connect(str(db_path))) as old:
                old.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            db_path.rename(db_path.with_name(f"{db_path.name}.bak-{stamp}"))
        for sidecar in ("-wal", "-shm"):
            db_path.with_name(db_path.name + sidecar).unlink(missing_ok=True)
        os.replace(tmp_db, db_path)
        return len(records)


def read_runs(db: sqlite3.Connection) -> list[dict]:
    """Every run's VIEW_FIELDS, JSON columns decoded."""
    db.row_factory = sqlite3.Row
    return [
        {k: json.loads(row[k] or "{}") if k in JSON_COLUMNS else row[k] for k in VIEW_FIELDS}
        for row in db.execute(f"SELECT {', '.join(VIEW_FIELDS)} FROM runs")
    ]


def load_runs(layout: Layout, active: ModuleType) -> list[dict]:
    """read_runs of the campaign's DB (none if it does not exist yet)."""
    if not layout.db_path.exists():
        return []
    with contextlib.closing(open_db(layout, active)) as db:
        return read_runs(db)


def load_runs_readonly(db_path: Path) -> list[dict] | None:
    """read_runs without modifying anything; None if the DB needs a rebuild."""
    if not db_path.exists():
        return []
    with contextlib.closing(sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)) as db:
        if _schema_version(db) != SCHEMA_VERSION:
            return None
        return read_runs(db)


# ---------------------------------------------------------------------------
# The results view and query
# ---------------------------------------------------------------------------

def _results_view_sql(active: ModuleType, temporary: bool) -> str:
    """DDL for the `results` view (runs plus one column per METRICS key)."""
    clashes = sorted(set(active.METRICS) & set(COLUMN_NAMES))
    if clashes:
        raise HarnessError(f"adapter '{active.NAME}' METRICS reuse run column names: {clashes}")
    columns = ", ".join(
        [*SCALAR_COLUMNS, *(f"json_extract(metrics, '$.{m}') AS {m}" for m in active.METRICS)]
    )
    kind = "TEMP VIEW" if temporary else "VIEW"
    return f"CREATE {kind} {RESULTS_VIEW} AS SELECT {columns} FROM runs;"


def _create_results_view(db: sqlite3.Connection, active: ModuleType) -> None:
    """(Re)create the persistent `results` view and the goal-metric expression indexes."""
    indexes = "".join(
        f"CREATE INDEX IF NOT EXISTS idx_metric_{m} ON runs(json_extract(metrics, '$.{m}'));\n"
        for m, goal in active.METRICS.items() if goal
    )
    db.executescript(
        f"BEGIN IMMEDIATE;\nDROP VIEW IF EXISTS {RESULTS_VIEW};\n"
        f"{_results_view_sql(active, temporary=False)}\n{indexes}COMMIT;"
    )


def ensure_results_view(layout: Layout, active: ModuleType) -> None:
    """Refresh the persistent `results` view (for sqlite3 and other tools) and its indexes.

    Rebuilt each time it is needed, so it always matches the adapter's
    current METRICS; `rebuild` also creates it in the new DB.
    """
    with campaign_db_lock(layout), contextlib.closing(open_db(layout, active)) as db:
        _create_results_view(db, active)


def _cell(value: object) -> str:
    text = analysis.fmt(value) if value is not None else ""
    text = text.replace("\t", " ").replace("\n", " ")
    return text if len(text) <= QUERY_CELL_CHARS else text[: QUERY_CELL_CHARS - 1] + "…"


def query(layout: Layout, active: ModuleType, sql: str, limit: int) -> str:
    """Run one read-only SQL statement; return tab-separated rows (header first), capped.

    The `results` view (runs plus one column per metric) is created as a
    temporary view on the query's own read-only connection, so a concurrent
    rebuild cannot remove it between refresh and use; the persistent view and
    indexes are refreshed too, for other tools.
    """
    if not layout.db_path.exists():
        raise HarnessError(f"no {layout.db_path} yet: run an experiment first")
    ensure_results_view(layout, active)
    uri = f"{layout.db_path.resolve().as_uri()}?mode=ro"
    try:
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as db:
            db.execute(_results_view_sql(active, temporary=True))
            cursor = db.execute(sql)
            columns = [d[0] for d in cursor.description or ()]
            rows = cursor.fetchmany(limit + 1)
    except sqlite3.Error as e:
        raise HarnessError(f"query failed: {e}") from e
    lines = ["\t".join(columns)] + ["\t".join(_cell(v) for v in row) for row in rows[:limit]]
    if len(rows) > limit:
        lines.append(f"… more than {limit} rows; aggregate, or raise --limit")
    return "\n".join(lines)
