-- results.db is a query index rebuilt from the campaign's runs/*.json records
-- (`python run.py rebuild`). Bump SCHEMA_VERSION in run.py with any change here.
-- run.py applies this script inside one transaction together with the
-- schema version, so keep it to CREATE statements (no PRAGMAs).

CREATE TABLE IF NOT EXISTS runs (
    -- identity
    id                      TEXT PRIMARY KEY,
    coil_type               TEXT NOT NULL,   -- solver family (adapter NAME)
    solver                  TEXT NOT NULL,   -- solver mode (--solver)
    equilibrium             TEXT NOT NULL,   -- target configuration (the adapter's TARGET_FLAG value)
    experiment_group        TEXT,            -- groups rows of one multi-step experiment; NULL = one row per experiment
    spec_hash               TEXT,            -- hash of what was asked (adapter, flags, solver identity); NULL for imported legacy rows
    replicate               INTEGER,         -- --replicate index; with spec_hash, the dedupe key
    seed                    INTEGER,         -- value of the adapter's SEED_FLAG (given or derived); NULL if the solver has none
    parent_run_id           TEXT,            -- run this one built on (e.g. its warm-start seed's run)
    replay_of               TEXT,            -- run this one replays (`run.py replay`)
    batch_id                TEXT,            -- batch this run belongs to (`run.py batch`; file under batches/)
    -- outcome
    status                  TEXT NOT NULL,
    status_reason           TEXT,
    crash_signature         TEXT,            -- normalized line from the log naming why a crash died
    validated               TEXT,
    iterations              INTEGER,
    elapsed                 REAL,
    peak_rss_mb             REAL,            -- peak resident memory of the solver processes (NULL on Windows)
    created_at              TEXT DEFAULT (datetime('now')),
    optimizer_success       INTEGER,
    termination_message     TEXT,
    -- physics outputs (canonical metric keys with a dedicated column)
    field_error             REAL,
    qs_error                REAL,
    boozer_residual         REAL,
    iota_actual             REAL,
    volume_actual           REAL,
    max_curvature           REAL,
    coil_length             REAL,
    coil_coil_dist          REAL,
    coil_surface_dist       REAL,
    surface_vessel_dist     REAL,
    max_force               REAL,
    self_intersecting       INTEGER,
    objective_J             REAL,
    -- solver-specific metrics with no dedicated column (JSON, query with json_extract)
    metrics                 TEXT,
    -- every flag of the run, defaults included (JSON)
    params                  TEXT,
    -- what the run used: solver identity/commit, command, input hashes, harness commit, platform (JSON)
    provenance              TEXT,
    -- kept evidence files: name -> {sha256, bytes}, stored under blobs/ (JSON)
    evidence                TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_coil_type    ON runs(coil_type);
CREATE INDEX IF NOT EXISTS idx_runs_equilibrium  ON runs(equilibrium);
CREATE INDEX IF NOT EXISTS idx_runs_group        ON runs(experiment_group);
CREATE INDEX IF NOT EXISTS idx_runs_spec         ON runs(spec_hash, replicate);
CREATE INDEX IF NOT EXISTS idx_runs_parent       ON runs(parent_run_id);
CREATE INDEX IF NOT EXISTS idx_runs_batch        ON runs(batch_id);
CREATE INDEX IF NOT EXISTS idx_runs_status       ON runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_crash        ON runs(crash_signature);
CREATE INDEX IF NOT EXISTS idx_runs_validated    ON runs(validated);
CREATE INDEX IF NOT EXISTS idx_runs_fe           ON runs(field_error);
CREATE INDEX IF NOT EXISTS idx_runs_qs           ON runs(qs_error);
