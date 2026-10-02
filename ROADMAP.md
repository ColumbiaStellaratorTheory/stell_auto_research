# Roadmap — general-harness

Goal: a solver-agnostic autoresearch harness that is deterministic, replayable,
analysis-rich and token-efficient, and that installs on any user's hardware
(macOS, Linux, Windows via WSL2; CPU or GPU) for any optimizer.

Design rule: the core stores and organizes runs and never interprets a metric
or parameter name. Domain knowledge lives in the adapter (declared data), the
campaign program file, and LESSONS.md.

```
campaign  (one research goal)
 └─ batch   (one agent plan)
     └─ run   (one solver call)
```

## Step 1 — generic core (low risk)

- [x] 1. Toy stdlib adapter (test function with seed flag, optional noise, deliberate crash case) as the reference adapter, test fixture, and `/setup-harness` template.
- [x] 2. Move banana to `examples/banana/` (adapter, program file, `PLAN.md`, tests; the simsopt `.env.sample` became `config.example.json`); the loader accepts dotted module paths.
- [x] 3. Campaign folders `campaigns/<slug>/`: `program.md`, `LESSONS.md`, `results.db` + run records, `config.json` (adapter, interpreter, settings). `/setup-harness` creates them.
- [x] 4. Explicit campaign selection (`--campaign <slug>` or env var); refuse when more than one campaign exists and none is named.
- [x] 5. Contract: `TARGET_FLAG` replaces the required `--equilibrium` flag; `ExperimentOutcome.params` removed. (The other contract fields moved to the steps that first use them: `SEED_FLAG`, `STOCHASTIC_MODES`, `provenance()` → step 2; `METRICS` → step 3; deleting `COLUMN_METRIC_KEYS` → step 6.)
- [x] 6. Core records the full parsed CLI (`vars(args)`) for every run (fixes dropped flags such as basin-hopping and `num_tf_coils`).
- [x] 7. Banana adapter adopts the step-1 contract (`TARGET_FLAG`, no curated params). Seed and lineage parts moved to item 10.

## Step 2 — recording and replay

- [x] 8. Contract: `RunContext`, `REQUIRED_ENV`/`OPTIONAL_ENV` (checked before a run; adapters no longer read env at import), `EXECUTION_FLAGS`, `SEED_FLAG`, `REPLAY_TOLERANCE`, `solver_identity()`; outcomes carry `provenance`, `evidence`, `parent_run_id`. Static adapter registry (`adapters/__init__.py`) replaces runtime imports. Per-run provenance: solver identity, exact command(s), input-file hashes, solver commit + uncommitted patch, harness commit, OS/arch/Python. (`STOCHASTIC_MODES` dropped: every run gets a seed. Package versions and BLAS vendor are left to adapters.)
- [x] 9. `spec_hash` (adapter + non-execution flags + solver identity) vs `run_id`; a pass/fail run with the same hash and `--replicate` is not repeated; crashes can be retried.
- [x] 10. Seeds derived from the spec (minus seed and execution flags) + replicate, independent of solver identity; explicit seeds win. Lineage via `parent_run_id`. Banana: `--basin-seed` flag, stage2 seeds archived under their run id with an origin file, single-stage records warm-start seed path + sha256 and its parent run; seed choice ties broken by name. (A machine-wide content-addressed seed store stays in item 28.)
- [x] 11. One record file per run (`runs/<id>.json`, atomic `os.replace`) as source of truth; `run.py rebuild [--from-jsonl FILE]` regenerates `results.db` and `results.jsonl` and imports legacy records; outdated DB schemas are refused with the rebuild command. `fcntl` removed.
- [x] 12. Run folder named by run id; scratch defaults to `<campaign>/scratch`; evidence files named by the adapter (log, results, solver patch, coils) always kept in `<campaign>/blobs/` by content hash. Stdout leaves out provenance/evidence.
- [x] 13. `run.py replay <id>` re-runs from the recorded spec, compares status + metrics within the adapter's `REPLAY_TOLERANCE`, reports solver changes, exits 2 on mismatch. Session-start drift check = replaying a known run (documented in the program template).

## Step 3 — analysis and token efficiency

- [ ] 14. Crash signatures parsed from the run log (replaces `exit_N`).
- [ ] 15. Validation as a number (e.g. Poincaré uniformity) with not-run / error / pass / fail distinguished.
- [ ] 16. Contract field `METRICS` (name → goal). `run.py brief`: fixed-size digest — Pareto front from `METRICS` goals, latest runs, crash causes, coverage, runs since last improvement, noise floor, machine settings.
- [ ] 17. Compact stdout (id, status, key metrics, delta vs front, duplicate flag).
- [ ] 18. `run.py query "SQL"`: read-only, compact output, works on every OS (no `sqlite3` CLI needed).
- [ ] 19. Typed `LESSONS.md` entries; batch proposals must cite or reject relevant lessons.
- [ ] 20. `run.py campaigns`: every campaign with status, slots in use, latest result, stall state.
- [ ] 21. Explicit lesson import from another campaign as priors.

## Step 4 — batches

- [ ] 22. `run.py batch <file>`: validate every spec before launch, skip duplicates, run in parallel within budget, early-stop on repeated crashes, tag rows with batch id + file hash, one summary at the end. Agent launches in background and waits for the completion notification (no polling).
- [ ] 23. Sobol / grid / replicate generators; two stages in one batch (cheap screen, then promote top-k by an agent-chosen rule).
- [ ] 24. Kill the whole process tree on timeout or early stop (process group on POSIX, job object on Windows).
- [ ] 25. Thread limits for every math library: `OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, `MKL_NUM_THREADS`, `BLIS_NUM_THREADS`, `VECLIB_MAXIMUM_THREADS`, `NUMBA_NUM_THREADS`, `NUMEXPR_NUM_THREADS`.
- [ ] 26. Machine-wide slot pool (`~/.autoresearch/slots`, cross-platform file locks); `run.py batch` takes a slot per run; campaigns share the machine by configured weights.
- [ ] 27. Claim `spec_hash` before running so concurrent agents on one campaign never run the same spec twice.
- [ ] 28. Machine-wide content-addressed store for seeds and evidence packs.

## Step 5 — hardware-aware setup

- [ ] 29. `/setup-harness` detects cores (performance cores on Apple Silicon; SLURM/affinity allocations), RAM, GPUs, scheduler on macOS / Linux / Windows (WSL2); measures run time and peak memory from the smoke run; runs a tiny solver probe per candidate device and only counts devices that work.
- [ ] 30. Questions: where runs execute, how much of the machine to use, how often to plan the next batch, session budget, campaign shares. Derive: runs at once = min(cores ÷ threads per run, RAM ÷ peak memory, GPU slots); largest batch = runs at once × (planning interval ÷ run time).
- [ ] 31. Settings in the campaign's gitignored `config.json`; environment variables override. Sets JAX GPU memory variables (`XLA_PYTHON_CLIENT_PREALLOCATE=false` / `XLA_CLIENT_MEM_FRACTION`) when needed.
- [ ] 32. Metrics and their goals collected in the interview → `METRICS`; `run.py schema` prints the active adapter's view for the program template.

## Step 6 — generic schema (needs sign-off: changes the DB format)

- [ ] 33. Delete `COLUMN_METRIC_KEYS`. Generic `runs` table (identity, status, timing, `spec` / `metrics` / `provenance` JSON) plus per-adapter views and expression indexes generated from `METRICS`.
- [ ] 34. Migration: backup → dry run → convert → verify (or rebuild from JSONL).

## Ongoing

- [ ] 35. GitHub Actions on Ubuntu, macOS, Windows with the toy adapter.
- [ ] 36. Update README, setup skill, and program template to match.
