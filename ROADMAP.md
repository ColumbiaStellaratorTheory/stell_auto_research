# Roadmap — general-harness

Goal: a solver-agnostic autoresearch harness that is deterministic, replayable,
analysis-rich and token-efficient, and that installs on any user's hardware
(macOS, Linux, Windows via WSL2; CPU or GPU) for any optimizer.

Design rule: the core stores and organizes runs and never interprets a metric
or parameter name. Domain knowledge lives in the adapter (declared data), the
campaign program file, and LESSONS.md. This branch ships no domain adapter:
only the stdlib toy reference.

```
campaign  (one research goal)
 └─ batch   (one agent plan)
     └─ run   (one solver call)
```

## Step 1 — generic core

- [x] 1. Toy stdlib adapter (test functions, seed flag, optional noise, deliberate crash/hang/NaN) as the reference adapter, test fixture, and `/setup-harness` template.
- [x] 2. Domain-specific code removed from this branch (the original physics adapter and its campaign files live on `master`).
- [x] 3. Campaign folders `campaigns/<slug>/`: `config.json`, `program.md`, `LESSONS.md`, run records, DB. `/setup-harness` creates them.
- [x] 4. Explicit campaign selection (`--campaign` or `$AUTORESEARCH_CAMPAIGN`); refuses when several campaigns exist and none is named.
- [x] 5. Contract: `TARGET_FLAG`; the core records the full parsed CLI of every run (no adapter-curated params, so no flag is ever dropped).

## Step 2 — recording and replay

- [x] 6. Contract: `RunContext`, `REQUIRED_ENV`/`OPTIONAL_ENV` (checked before a run; adapters never read env at import), `EXECUTION_FLAGS`, `SEED_FLAG`, `REPLAY_TOLERANCE`, `solver_identity()`; outcomes carry `provenance`, `evidence`, `parent_run_id`. Static adapter registry (`adapters/__init__.py`), no runtime imports.
- [x] 7. Per-run provenance: solver identity, exact command(s), input-file hashes, solver commit + uncommitted patch, harness commit, OS/arch/Python.
- [x] 8. `spec_hash` vs `run_id`; a pass/fail run with the same hash and `--replicate` is not repeated; crashes can be retried.
- [x] 9. Seeds derived from the spec (minus seed and execution flags) + replicate, independent of solver identity; explicit seeds win; lineage via `parent_run_id`.
- [x] 10. One record file per run (`runs/<id>.json`, atomic) as source of truth; `run.py rebuild [--from-jsonl FILE]` regenerates `results.db` and `results.jsonl`.
- [x] 11. Run folder named by run id; scratch inside the campaign; adapter-named evidence files kept in `blobs/` by content hash.
- [x] 12. `run.py replay <id>`: re-run from the recorded spec, compare within `REPLAY_TOLERANCE`, report solver changes, exit 2 on mismatch.

## Step 3 — analysis and token efficiency

- [x] 13. Crash signatures from the tail of the adapter's `log` evidence.
- [x] 14. `validated` is pass / fail / error / None (not attempted).
- [x] 15. Contract field `METRICS` (name → "min" / "max" / None). `run.py brief`: fixed-size digest (counts, Pareto fronts, recent runs, crash causes, replicate spread, runs since the front moved, lessons, machine sizing).
- [x] 16. Compact stdout (set fields, metrics, `crash_signature`, `on_front`).
- [x] 17. `run.py query "SQL"`: one read-only statement, tab-separated, capped.
- [x] 18. Typed `LESSONS.md` template; experiments and batches name the lessons they apply, test or reject.
- [x] 19. `run.py campaigns`; `run.py import-lessons --from OTHER`.

## Step 4 — batches

- [x] 20. `run.py batch FILE [--parallel N] [--dry-run]`: specs validated before launch, recorded specs reused, one `run.py` process per spec, early stop on repeated identical crashes, `batch_id` on every row, `batches/<id>.json` record, capped summary.
- [x] 21. Generators: explicit runs, grid, Halton, seeded Latin hypercube, log/int ranges, replicates; promotion stages with parent lineage. (Halton rather than Sobol: no direction-number table in the stdlib.)
- [x] 22. `contract.run_solver` kills the whole process tree on timeout or cancellation; SIGTERM records a run as `cancelled`; `contract.thread_env(n)` sets all common math-library thread variables.
- [x] 23. Machine-wide run slots and per-spec claims (`locks.py`, OS-released locks); per-campaign `max_parallel` cap; optional shared evidence store (`AUTORESEARCH_BLOBS_DIR`); atomic first-time DB creation.

## Step 5 — hardware-aware setup

- [x] 24. `machine.py` + `run.py machine`: hardware detection, per-run `peak_rss_mb`, measured cost per mode, runs at once and batch size; `~/.autoresearch/machine.json`; contract field `THREADS_FLAG`; campaign `plan_minutes`.
- [x] 25. `/setup-harness`: four questions (where runs execute, share of the machine, planning interval, session budget) plus campaign shares; device probes with the solver's framework; JAX GPU memory variables.
- [x] 26. `run.py schema` for the program template.

## Step 6 — generic schema

- [x] 27. Schema v6: one `runs` table (identity, status, timing, `metrics` / `params` / `provenance` / `evidence` JSON) with `adapter` / `mode` / `target` instead of solver-specific names; `--mode` and `MODES` replace `--solver` and `SOLVER_MODES`; the `results` view gives each declared metric a column and goal metrics get expression indexes; metric names validated.
- [x] 28. Migration: run files are never rewritten; `upgrade_record` converts pre-v6 records (old names, top-level metrics) whenever they are read, so `rebuild` (automatic for record-backed DBs) is the whole migration.

## Remaining

- [ ] 29. GitHub Actions on Ubuntu, macOS, Windows with the toy adapter.
- [ ] 30. A real second adapter through `/setup-harness` against a user's solver, to test the walkthrough end to end.
