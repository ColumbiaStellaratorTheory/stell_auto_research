-- results.db is a query index rebuilt from the campaign's runs/*.json records
-- (`python run.py rebuild`). Bump SCHEMA_VERSION in run.py with any change here.
-- run.py applies this script inside one transaction together with the
-- schema version, so keep it to CREATE statements (no PRAGMAs).
--
-- Metrics are adapter-defined, so they live in the `metrics` JSON column.
-- run.py maintains a `results` view over this table with one column per
-- metric the adapter declares, plus expression indexes on its goal metrics.

CREATE TABLE IF NOT EXISTS runs (
    -- identity
    id                      TEXT PRIMARY KEY,
    adapter                 TEXT NOT NULL,   -- adapter NAME
    mode                    TEXT NOT NULL,   -- adapter mode (--mode)
    target                  TEXT NOT NULL,   -- value of the adapter's TARGET_FLAG
    experiment_group        TEXT,            -- groups rows of one multi-step experiment; NULL = one row per experiment
    spec_hash               TEXT,            -- hash of what was asked (adapter, flags, solver identity); NULL for imported legacy rows
    replicate               INTEGER,         -- --replicate index; with spec_hash, the dedupe key
    seed                    INTEGER,         -- value of the adapter's SEED_FLAG (given or derived); NULL if the solver has none
    parent_run_id           TEXT,            -- run this one built on (warm start, batch promotion)
    replay_of               TEXT,            -- run this one replays (`run.py replay`)
    batch_id                TEXT,            -- batch this run belongs to (`run.py batch`; file under batches/)
    -- outcome
    status                  TEXT NOT NULL,   -- pass | fail | crash
    status_reason           TEXT,
    crash_signature         TEXT,            -- normalized line from the log naming why a crash died
    validated               TEXT,            -- pass | fail | error | NULL (not attempted)
    elapsed                 REAL,            -- seconds, including validation and chained steps
    peak_rss_mb             REAL,            -- peak resident memory of the solver processes (NULL on Windows)
    created_at              TEXT,
    -- JSON
    metrics                 TEXT,            -- every metric the adapter emitted
    params                  TEXT,            -- every flag of the run, defaults included
    provenance              TEXT,            -- solver identity/commit, command, input hashes, harness commit, platform
    evidence                TEXT             -- kept files: name -> {sha256, bytes}, stored under blobs/
);

CREATE INDEX IF NOT EXISTS idx_runs_target   ON runs(mode, target);
CREATE INDEX IF NOT EXISTS idx_runs_group    ON runs(experiment_group);
CREATE INDEX IF NOT EXISTS idx_runs_spec     ON runs(spec_hash, replicate);
CREATE INDEX IF NOT EXISTS idx_runs_parent   ON runs(parent_run_id);
CREATE INDEX IF NOT EXISTS idx_runs_batch    ON runs(batch_id);
CREATE INDEX IF NOT EXISTS idx_runs_status   ON runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_crash    ON runs(crash_signature);
CREATE INDEX IF NOT EXISTS idx_runs_created  ON runs(created_at);
