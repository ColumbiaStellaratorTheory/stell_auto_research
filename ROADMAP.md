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

- [x] 14. Crash signatures: the core reads the tail of the adapter's `log` evidence and records `crash_signature` (last `...Error:` line, else last line; paths/numbers normalized) next to the machine `status_reason` (`exit_N` kept). Schema v3; record-backed v2 DBs are rebuilt automatically.
- [x] 15. `validated` is pass / fail / error / None (not attempted); banana records `poincare_uniformity` and turns a broken check into `error` instead of losing the run.
- [x] 16. Contract field `METRICS` (name → "min" / "max" / None, validated by the loader). `run.py brief`: fixed-size digest — counts per mode/target, Pareto fronts, recent runs, crash causes, replicate spread (noise floor), runs since the front last moved, latest lesson titles. (Machine settings move to step 5.)
- [x] 17. Compact stdout: set identity fields, metrics, `crash_signature`, and `on_front` for passing runs (front membership instead of a numeric delta); duplicates print `duplicate_of`.
- [x] 18. `run.py query "SQL"`: one read-only statement (`mode=ro`), tab-separated, capped at 50 rows (`--limit`), long cells truncated; no `sqlite3` CLI needed.
- [x] 19. Typed `LESSONS.md` template (kind / scope / claim / evidence / action / status, closed vocabularies); the program template's loop asks the agent to name the lessons each experiment applies, tests or rejects. (Enforcing it in batch files is item 22.)
- [x] 20. `run.py campaigns`: every campaign with adapter, run counts, last run, runs since its front moved; read-only. (Slots in use come with item 26.)
- [x] 21. `run.py import-lessons --from OTHER`: appends the other campaign's entries under one dated `kind: import` entry with headings demoted, marked as hypotheses.

## Step 4 — batches

- [x] 22. `run.py batch FILE [--parallel N] [--dry-run]`: every spec parsed against the adapter's flags before anything launches; recorded specs reused; each spec its own `run.py` process; early stop after N identical crashes (default 3, `0` disables); rows tagged `batch_id` (schema v4); `batches/<id>.json` keeps the file, its sha256, hypothesis, cited lessons (required fields), run ids and stop reason; children's stderr in `batches/<id>.log`; capped summary.
- [x] 23. Generators: explicit `runs`, `grid`, `halton` (low-discrepancy) and seeded `lhs`, ranges with `log`/`int` tags, `replicates`; promotion stages (`from` + `select` by goal metric or `front` + `carry`) with the source run recorded as parent (`--parent-run-id`). (Halton instead of Sobol: Sobol needs a direction-number table the stdlib does not ship.)
- [x] 24. `contract.run_solver`: solver in its own process group; timeout or cancellation kills the whole group (POSIX killpg, Windows taskkill /T). SIGTERM → `Cancelled` → the run is recorded as `cancelled`; a cancelled batch SIGTERMs its children.
- [x] 25. `contract.thread_env(n)` sets all seven thread variables; banana uses it.
- [x] 26. Machine-wide slot pool (`AUTORESEARCH_MAX_PARALLEL`, lock files in `~/.autoresearch/slots`, OS-released on death, `locks.py`); every run holds a slot; per-campaign cap `max_parallel` in config.json (caps rather than weights).
- [x] 27. Each run takes a lock on `claims/<spec_hash>-<replicate>.lock` before the duplicate check, so concurrent agents never run the same spec twice (the loser prints `in_progress`). First-time DB creation made atomic (tables + version in one transaction).
- [x] 28. `AUTORESEARCH_BLOBS_DIR` makes the evidence store machine-wide (default stays per campaign so a campaign folder is self-contained). Seed stores remain adapter settings (banana: `STAGE2_SEED_DIR`).

## Step 5 — hardware-aware setup

- [x] 29. `machine.py` + `run.py machine`: OS, usable CPUs (affinity, SLURM), Apple performance cores, memory, GPUs (nvidia-smi, Apple), scheduler; every run records `peak_rss_mb` (schema v5; POSIX `resource`, None on Windows). `/setup-harness` runs it first, fills gaps with OS commands, and probes each candidate device with the solver's framework (float64 where needed) before offering it.
- [x] 30. Four questions (where runs execute, share of the machine, planning interval, session budget) plus campaign shares when several exist. Sizing in `machine.py`: runs at once = min(cores ÷ threads, memory ÷ peak memory); batch = runs at once × interval ÷ median run time; new contract field `THREADS_FLAG`. Shown by `run.py machine` and at the end of `run.py brief`. (GPU slots are left to the interview: the probe decides whether a device counts.)
- [x] 31. Machine-wide settings in `~/.autoresearch/machine.json` (`run.py machine --max-parallel/--usable-cores/--usable-memory-gb`; `$AUTORESEARCH_MAX_PARALLEL` overrides); per campaign `plan_minutes`, `max_parallel` and `env` (JAX `XLA_PYTHON_CLIENT_PREALLOCATE` / `XLA_CLIENT_MEM_FRACTION`) in config.json.
- [x] 32. Metric goals in the interview → `METRICS`; `run.py schema` prints columns, goals and recorded-only metrics for the program template.

## Step 6 — generic schema (needs sign-off: changes the DB format)

- [ ] 33. Delete `COLUMN_METRIC_KEYS`. Generic `runs` table (identity, status, timing, `spec` / `metrics` / `provenance` JSON) plus per-adapter views and expression indexes generated from `METRICS`.
- [ ] 34. Migration: backup → dry run → convert → verify (or rebuild from JSONL).

## Ongoing

- [ ] 35. GitHub Actions on Ubuntu, macOS, Windows with the toy adapter.
- [ ] 36. Update README, setup skill, and program template to match.
