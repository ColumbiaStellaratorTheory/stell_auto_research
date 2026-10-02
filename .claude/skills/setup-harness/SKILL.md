---
name: setup-harness
description: Interactive first-time setup of the autoresearch harness for the user's own optimizer (simsopt, DESC, or any other). Detects their solver stack, installs missing dependencies, generates a solver adapter + campaign folder (config, program file, lessons), and ends with a real run. Use when a collaborator clones this repo, when wiring the harness to a new solver, or to regenerate the program file for a new campaign.
---

# setup-harness

Get a first-time user from a fresh clone to a running experiment loop against
**their own optimizer**. The harness does not do the physics — the user owns
that. It runs their parameter scans, records every run (and every failure) in a
queryable SQLite DB, and keeps an append-only `LESSONS.md`. Your job here is to
wire it to their solver and prove it runs.

The end state is these pieces, all verified by a real run:
**adapter** (`adapters/<solver>.py`, the solver-specific glue) + **campaign
folder** `campaigns/<slug>/` holding `config.json` (which adapter + its
settings), `program.md` (the agent's instructions) and `LESSONS.md` (research
memory) + **run.py** (generic runner, untouched) + the campaign's
**results.db/jsonl** (experiment DB, created by the first run).

Run the phases in order. Be concrete, verify every step, and do not declare
done on a red smoke run. The user may be new to autoresearch and skeptical that
an LLM helps with their optimization — keep the walkthrough practical and
honest; the value you deliver is a working runner + bookkeeping, not physics.

## Phase 1 — Preflight: learn the contract

Read these before asking anything:
1. `contract.py` — the harness↔adapter contract. This is the interface the
   generated adapter must implement (its module docstring lists every member)
   and the `RunContext` / `ExperimentOutcome` types it uses.
2. `run.py` — the generic core: campaign selection (`--campaign`, one folder
   per campaign under `campaigns/`), `config.json` (`adapter` + `env`), how it
   calls the adapter, the run records (`runs/`, `blobs/`, `results.db`), spec
   hash / dedupe / derived seeds / replay, the artifact layout (`OUTPUT_BASE` /
   `KEEP_ARTIFACTS` / `ARTIFACTS_DIR`), and `COLUMN_METRIC_KEYS` (the canonical
   metric keys that get their own DB column; everything else an adapter emits
   lands in the `metrics` JSON overflow). The core records every parsed flag.
   `adapters/__init__.py` is the registry of installed adapters.
3. `adapters/toy.py` — the **reference adapter**: a complete, stdlib-only
   example of the contract. You will model the generated adapter on this.
   `examples/banana/simsopt_banana.py` is a real physics adapter showing
   multiple modes, a warm-start seed store and independent validation.
4. `templates/program_template.md` and `templates/LESSONS.md` — the skeletons
   you fill or copy into the campaign folder later.

Do not read or write dotenv files; adapter settings go in the campaign's
`config.json` `"env"` map (a variable set in the shell overrides it).

## Phase 2 — Identify the solver stack

Ask the user (AskUserQuestion, with detected defaults where possible):
- **Which optimizer** do they run — simsopt, DESC, or something else?
- **Where it lives**: the solver repo root, and the Python interpreter that has
  it installed (often a conda/venv distinct from this repo's env).
- **Target configurations**: the directory holding the inputs each experiment
  optimizes against (simsopt: `wout_*.nc` equilibria; DESC: base cases / `.h5`).

Verify each path immediately (dir exists; interpreter runs). If the user's
optimizer **is** the banana/simsopt example, no adapter generation is needed —
use `"adapter": "simsopt_banana"` (see
`examples/banana/config.example.json` and `examples/banana/README.md`), skip
Phase 6's adapter step, and just do dependency check + campaign identity +
campaign folder + smoke. Otherwise you will generate a new adapter for their
solver.

## Phase 3 — Dependencies (hard gate before introspection)

A solver whose imports fail produces confusing crashes later, so resolve this
first:
1. Probe the chosen interpreter for the framework and its key deps:
   `<python> -c "import simsopt"` / `"import desc"` (DESC also pulls `jax`).
2. If anything is missing, **guide the install** — do not silently run a heavy
   install:
   - Detect the user's package manager (uv / pip / conda) from their env.
   - For framework install commands, fetch **current** instructions rather than
     guessing — use the `find-docs` skill or `ctx7` (DESC + jax versions and
     CPU/GPU extras drift, and a wrong jax build is a classic silent failure).
   - Show the exact command, get confirmation, run it, then **re-verify the
     import**. No silent fallback.
3. Do not proceed to introspection until every required import succeeds.

## Phase 4 — Understand the solver & sketch the adapter

You are about to write an adapter; learn the solver's shape first.
1. **How is one experiment invoked?** A single script/subprocess (like banana's
   stage2), or a **chained pipeline** of several steps where each step's output
   feeds the next? (DESC umbilic, e.g., is a 4-step chain per parameter set:
   build a bumped plasma surface → solve the fixed-boundary equilibrium → stage-2
   coil optimization → free-boundary equilibrium seeded from the earlier solve.)
2. **Inputs/params** the agent should be able to set (grep the solver's argparse
   or function signatures). These become `add_arguments` flags.
3. **Outputs**: what does a run produce, and where are the metrics — a
   `results.json`, a returned object, stdout? This becomes `run_experiment`'s
   parsing.
4. **Metrics → canonical keys**: list the solver's native metric names and map
   each onto a canonical snake_case key. Keys in `run.py`'s `COLUMN_METRIC_KEYS`
   get a dedicated column; everything else is preserved in the `metrics` JSON
   blob (so nothing is lost — no schema change needed).
5. **Target-configuration resolution**: how the target flag (`--equilibrium`, a base-case key, …) maps
   to an actual input file or case.
6. **Validation / intermediates**: any independent check (field-line tracing,
   convergence) that should set `validated`, and any expensive intermediate a
   later step reuses.

## Phase 5 — Campaign identity & setup decisions (interview)

Free-text, the user's physics, not yours. Keep it lean — guardrails remove agent
freedom; physics findings belong in `LESSONS.md`, not here.
- **Campaign title** + 2–5 sentence **mission**, and a short **slug** for the
  campaign folder (`campaigns/<slug>/`). If campaigns already exist, confirm
  this is a new goal rather than more runs for an existing campaign.
- **Success metric**: the one measurable claim that defines success, and how it
  is verified.
- **Metric goals**: for each metric the solver reports, should it go down,
  up, or is it only recorded? These become the adapter's `METRICS` and decide
  which runs `run.py brief` shows on the Pareto front. Keep the goal set to
  what the user actually optimizes; constraints are not goals.
- **Hard invariants**: hardware limits, sign/convention contracts, ceilings.
  Offer "none yet" as valid. Remind: every entry removes agent freedom — keep to
  real physical/hardware limits.
- **Constraint floors**: the buildability limits (offer the adapter's defaults
  as baseline).
- **Execution policy**: timeout and thread count for this machine; autonomy
  ("never stop" loop vs bounded sessions).
- **Artifact layout**: `OUTPUT_BASE` (scratch, default `campaigns/<slug>/scratch`),
  `KEEP_ARTIFACTS` (`none`/`pass`/`all` — whole run dirs; the adapter's evidence
  files are kept regardless), `ARTIFACTS_DIR`, and any solver-specific
  seed/intermediate store the adapter needs.
- **Experiment granularity (multi-step pipelines only)**: ask whether one
  experiment should be **one DB row** (simplest — one row per full pipeline run)
  or **one row per sub-step** (more plumbing, but lets the agent reuse an
  expensive intermediate — e.g. a solved equilibrium — across downstream scans).
  Per-step uses the `experiment_group` column to tie a parameter set's rows back
  together, plus an archival hook to thread the intermediate forward. Default to
  one-row-per-chain unless they want the reuse.

## Phase 6 — Generate

1. **Adapter** (`adapters/<solver-slug>.py`) — implement the `contract.py`
   interface, modeling `adapters/toy.py`:
   - `NAME`, `SOLVER_MODES` (the `--solver` choices; a single-shot solver has
     one mode, a pipeline may expose several), `TARGET_FLAG` (the argparse
     dest of the flag naming the target configuration, e.g. `"equilibrium"`),
     `REQUIRED_ENV` / `OPTIONAL_ENV`, `EXECUTION_FLAGS` (dests that change how
     a run executes but not its result: timeout, threads, solver location),
     `SEED_FLAG` (the solver's RNG seed flag dest with default `None`, or
     `None` if the solver is deterministic), `REPLAY_TOLERANCE` (relative
     per-metric tolerance for `run.py replay`; say in a comment whether it was
     measured or is a starting value), `METRICS` (every canonical metric key
     the adapter emits → `"min"`, `"max"` or `None`; the goals come from the
     interview's evaluation answers and drive the Pareto front in
     `run.py brief`).
   - `add_arguments(parser)` — register the target flag (with a default) plus
     every solver param the agent may set, with the solver's real defaults.
     Only flags the solver actually supports. The core records every flag, so
     there is no list of "kept" params to maintain.
   - Launch every solver process with `contract.run_solver` (timeouts and
     cancellation kill the whole process tree) and pass thread counts through
     `contract.thread_env(n)` (sets OpenMP, OpenBLAS, MKL, BLIS, Accelerate,
     Numba and NumExpr limits together).
   - Name the solver's combined output `"log"` in `evidence`: the core reads
     its tail to give crashes a `crash_signature`.
   - `solver_identity(args)` — fingerprint of the solver code (commit + hash
     of uncommitted changes via `contract.git_output`, or a file hash).
   - `run_experiment(args, run)` — run one experiment end-to-end in `run.dir`
     (one subprocess, or the full chain), parse outputs, map metrics →
     canonical keys, classify pass/fail, run any validation, archive any
     reusable intermediate under `run.run_id` (and set `parent_run_id` when a
     run reuses one), and return an `ExperimentOutcome` with `provenance`
     (exact command(s), input-file hashes) and `evidence` (log, results file,
     solver patch). Must not raise on solver failure — return a `crash`/`fail`
     outcome (the core catches truly-unexpected exceptions).
   - Read env vars inside `run_experiment` / `solver_identity`, never at
     import: every registered adapter is imported on every run.
   - **Register it** in `adapters/__init__.py`: one import line and one
     `REGISTRY` entry. Do **not** edit `adapter.py` or `run.py`.
2. **`campaigns/<slug>/config.json`** — `{"adapter": "<key>", "env": {...}}`
   from the interview: the adapter's `REGISTRY` key, plus the solver root,
   interpreter, config dir, and any non-default artifact-layout values in
   `env`. Omit anything left at default. No shell exports are needed.
3. **`campaigns/<slug>/program.md`** — fill every `{{PLACEHOLDER}}` in
   `templates/program_template.md` from the interview + introspection:
   parameter table (only real flags), target-configuration table, artifact
   layout, success metric, hard invariants, constraint floors. Leave deferred
   sections as short "TODO — fill in as lessons accumulate" notes; do not invent
   physics. Remove all template HTML comments from the generated file.
4. **`campaigns/<slug>/LESSONS.md`** — copy `templates/LESSONS.md`. Never
   overwrite an existing campaign's `LESSONS.md`.

## Phase 7 — Verify (must end green)

1. Run one tiny experiment with `python run.py --campaign <slug> ...` and the
   smallest meaningful settings (low iterations/resolution, short timeout).
   Expect a single JSON line with a `"status"`, a record in
   `campaigns/<slug>/runs/`, and a row in `campaigns/<slug>/results.db`. Then
   run `python run.py replay <run-id> --campaign <slug>` and confirm it matches.
2. If it crashes, read the run log (the record's `evidence.log.sha256` names
   the file under `campaigns/<slug>/blobs/`), diagnose (usually a missing dep,
   an adapter↔solver flag
   mismatch, target-config resolution, or a broken pipeline step), fix, re-run.
   Do not declare setup done with a failing smoke run.
3. For a chained pipeline, run one reduced-resolution full parameter set to prove
   every step and the artifact hand-offs work end-to-end.
4. Leave smoke rows in the DB by default — they are honest history; clean up only
   if the user asks.

## Phase 8 — Report & how to run

Print a short, practical summary:
- The adapter written (`adapters/<slug>.py`) and the campaign folder
  (`campaigns/<slug>/`: `config.json`, `program.md`, `LESSONS.md`).
- Any adapter↔solver flag drift found and how it was resolved.
- **The first real launch command**, e.g.
  `python run.py --campaign <slug> --solver <mode> --<target-flag> <case> [params]`.
- **How to start the loop**: "Read `campaigns/<slug>/program.md`, then start
  the optimization loop."
- Reminders: `LESSONS.md` is append-only memory; the program file is the
  contract; query results with `sqlite3 campaigns/<slug>/results.db`; re-run
  `/setup-harness` for a new campaign or solver (it creates a new campaign
  folder / adapter rather than overwriting).
