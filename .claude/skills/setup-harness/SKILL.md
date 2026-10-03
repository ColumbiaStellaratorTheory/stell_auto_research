---
name: setup-harness
description: Interactive setup of the autoresearch harness for the user's own optimizer (any program that runs from a command). Finds their solver and its interpreter, installs missing dependencies, generates a solver adapter + campaign folder (config with constraints and budget, program file, lessons), and ends with a real run. Use when a collaborator clones this repo, when wiring the harness to a new solver, or to start a new campaign for an already-installed adapter.
---

# setup-harness

Get a first-time user from a fresh clone to a running experiment loop against
**their own optimizer**. The harness does not do the physics — the user owns
that. It runs their parameter scans, records every run (and every failure) in a
queryable SQLite DB, and keeps an append-only `LESSONS.md`. Your job here is to
wire it to their solver and prove it runs.

The end state is these pieces, all verified by a real run:
**adapter** (`adapters/<solver>.py`, the solver-specific glue) + **campaign
folder** `campaigns/<slug>/` holding `config.json` (the experiment design:
which adapter, parameter constraints, budget; committed), `local.json` (this
machine's adapter env and run cap; gitignored), `program.md` (~30 lines: mission,
goals and stopping criteria, rules no code can check) and `LESSONS.md`
(campaign memory) + **solver lessons** `lessons/<adapter>.md` (shared by every
campaign on that adapter) + **run.py** (generic runner, untouched) + the
campaign's **results.db** (experiment DB, created by the first run). The
research method itself lives once in `.claude/skills/research/SKILL.md`; the
user then runs `/research <slug>` and never writes a system prompt.

**Two paths.** Ask first whether this is a new solver or a new campaign for
an adapter already registered in `adapters/__init__.py` (list them, and the
existing campaigns from `python run.py status`).
- **New solver** — run every phase.
- **New campaign for an existing adapter** — skip Phases 2–4, Phase 5's
  hardware questions 1–2, device probe and threads/timeout questions, and
  Phase 6 steps 1 and 3 (adapter and machine settings already exist). Take
  `env` from an existing campaign's `local.json` on that adapter (show it and confirm), then
  ask Phase 5's campaign questions (mission, goals, limits, budget, autonomy,
  planning interval, how it shares the machine; metric goals belong to the
  adapter and are not asked again), do Phase 6 steps 2, 4, 5 and 6, a smoke
  run (Phase 7 step 1) and Phase 8.

Run the phases in order. Be concrete, verify every step, and do not declare
done on a red smoke run. The user may be new to autoresearch and skeptical that
an LLM helps with their optimization — keep the walkthrough practical and
honest; the value you deliver is a working runner + bookkeeping, not physics.

## Phase 1 — Preflight: learn the contract

Read these before asking anything:
1. `contract.py` — the harness↔adapter contract. Its module docstring is the
   one full description of the interface the generated adapter must implement,
   including the `RunContext` / `ExperimentOutcome` types it uses.
2. `README.md` — how the generic core works: campaign selection
   (`--campaign`, one folder per campaign under `campaigns/`), `config.json`
   (`adapter`, `fixed` / `bounds` / `budget`, `plan_minutes`) and `local.json`
   (`env`, `max_parallel`), how it calls the adapter, the run records (`runs/`, `blobs/`, `results.db`), spec
   hash / dedupe / derived seeds / replay, the artifact layout
   (`AUTORESEARCH_SCRATCH_DIR` / `AUTORESEARCH_KEEP_ARTIFACTS` /
   `AUTORESEARCH_ARTIFACTS_DIR`). Metrics are stored as JSON; the
   `results` view gives each of the adapter's `METRICS` a column. The core
   records every parsed flag.
   `adapters/__init__.py` is the registry of installed adapters.
3. `adapters/toy.py` — the **reference adapter**: a complete, stdlib-only
   example of the contract. You will model the generated adapter on this.
4. `templates/program_template.md` and `templates/LESSONS.md` — the skeletons
   you fill or copy into the campaign folder later.
5. `.claude/skills/research/SKILL.md` — the shared research method the
   campaign will run under; do not repeat any of it in `program.md`.

Do not read or write dotenv files; adapter settings go in the campaign's
`local.json` `"env"` map (a variable set in the shell overrides it). Never put
paths, credentials or other secrets in `config.json`: it is committed as part
of the research record, and `run.py` refuses an `env` key there.

## Phase 2 — Identify the solver stack

Ask the user (AskUserQuestion, with detected defaults where possible):
- **Which optimizer** do they run, and how is it launched (a script, a
  module, a binary, a chain of steps)?
- **Where it lives**: the solver repo root, and the interpreter or runtime that
  has it installed (often a conda/venv distinct from this repo's env).
- **Target configurations**: where the inputs each experiment optimizes
  against live (input files, named cases, generated configurations).

Verify each path immediately (dir exists; interpreter runs). If an adapter for
their solver is already registered in `adapters/__init__.py`, skip Phase 6's
adapter step and just do dependency check + campaign identity + campaign
folder + smoke. Otherwise you will generate a new adapter for their solver.

## Phase 3 — Dependencies (hard gate before introspection)

A solver whose imports fail produces confusing crashes later, so resolve this
first:
1. Probe the chosen interpreter for the framework and its key deps:
   `<python> -c "import <framework>"` for each package the solver imports.
2. If anything is missing, **guide the install** — do not silently run a heavy
   install:
   - Detect the user's package manager (uv / pip / conda) from their env.
   - For framework install commands, fetch **current** instructions rather than
     guessing — use the `find-docs` skill or `ctx7` (versions and CPU/GPU
     extras drift; a wrong accelerator build, e.g. of jax or torch, is a
     classic silent failure).
   - Show the exact command, get confirmation, run it, then **re-verify the
     import**. No silent fallback.
3. Do not proceed to introspection until every required import succeeds.

## Phase 4 — Understand the solver & sketch the adapter

You are about to write an adapter; learn the solver's shape first.
1. **How is one experiment invoked?** A single script/subprocess, or a
   **chained pipeline** of several steps where each step's output feeds the
   next (e.g. build an input → solve → optimize → re-solve seeded from the
   first solve)? Is there a cheap stage and a costly one? Those become
   separate `MODES`, and batches can promote the best cheap runs.
2. **Inputs/params** the agent should be able to set (grep the solver's argparse
   or function signatures). These become `add_arguments` flags.
3. **Outputs**: what does a run produce, and where are the metrics — a
   `results.json`, a returned object, stdout? This becomes `run_experiment`'s
   parsing.
4. **Metrics → keys**: list the solver's native metric names and map each
   onto a snake_case key; these become `METRICS` (no schema change is ever
   needed — metrics are stored as JSON and the `results` view gives each a
   column).
5. **Target-configuration resolution**: how the target flag (an input file, a named case, …) maps
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
- **Goals and stopping criteria**: the one measurable claim that defines
  success and how it is verified, how to rank conflicting goals, and when the
  agent should stop (criteria met, a plateau, …).
- **Metric goals**: for each metric the solver reports, should it go down,
  up, or is it only recorded? These become the adapter's `METRICS` and decide
  which runs `run.py brief` shows on the Pareto front. Keep the goal set to
  what the user actually optimizes; constraints are not goals.
- **Limits**: hardware limits, buildability floors, resolution ceilings,
  sign/convention contracts. Offer "none yet" as valid. Remind: every entry
  removes agent freedom — keep to real physical/hardware limits. Sort each
  limit by whether code can check it:
  - a parameter that must take one value → `config.json` `fixed`
    (`{"<dest>": value}`);
  - a numeric parameter range → `bounds` (`{"<dest>": [min, max]}`,
    inclusive);
  - anything else (conventions, judgment, limits on outputs rather than
    inputs) → a hard rule in `program.md`.
  Keys are the adapter's parameter dests as used in batch specs
  (`python run.py --campaign <slug> --help`, dashes → underscores), never
  core flags or the adapter's `EXECUTION_FLAGS`; `run.py` rejects unknown
  keys and malformed values when it loads the config.
- **Budget**: the campaign's total runs and/or hours (sum of recorded run
  time) → `config.json` `budget` (`{"runs": N, "hours": H}`, either optional).
  It counts every run record of the campaign, all statuses, across sessions;
  `run.py` refuses runs and batches beyond it. To extend a campaign, the user
  raises it.
- **Hardware.** First run `python run.py status` and show the
  user what it detected (OS, usable CPUs, performance cores on Apple Silicon,
  memory, GPUs, SLURM). Fill gaps with OS-native commands only when needed
  (Windows: PowerShell `Get-CimInstance Win32_ComputerSystem`; AMD:
  `rocm-smi`). Then ask, with the detected values as defaults:
  1. **Where do runs execute?** this machine / a SLURM cluster / cloud GPUs.
     Native Windows: recommend WSL2 (no native JAX GPU; most compiled solvers
     target Linux/macOS).
  2. **How much of the machine may the harness use?** all of it / leave
     headroom on a shared box / a fixed number of cores and GB.
  3. **How often should the agent look at results and plan the next batch?**
     e.g. every 30 min / 2 h / overnight. Becomes `plan_minutes`.
  If other campaigns already exist (`python run.py status` lists them), also ask
  **how this campaign shares the machine** — becomes its `max_parallel` cap
  (in `local.json`).
  Also ask the solver's threads per run and timeout per mode.
- **Devices the solver can actually use.** Do not count a GPU because it
  exists: run a tiny probe of the solver's framework on each candidate device
  in the solver's interpreter (JAX: `python -c "import jax, jax.numpy as jnp;
  jax.config.update('jax_enable_x64', True); print(jax.devices(),
  jnp.ones(3).sum())"`; check float64 if the solver needs it).
  Only devices that pass are offered. Several JAX runs per GPU need
  `XLA_PYTHON_CLIENT_PREALLOCATE=false` (or a smaller
  `XLA_CLIENT_MEM_FRACTION`) in the campaign's `local.json` `env`, since JAX takes 75% of
  GPU memory at its first operation. On Apple Silicon the GPU shares RAM
  with the CPU: one memory budget, not two.
- **Autonomy**: "never stop" until the budget or the goals end it, or bounded
  sessions (e.g. N runs per session, then report) — goes into `program.md`'s
  stopping criteria.
- **Artifact layout**: `AUTORESEARCH_SCRATCH_DIR` (scratch, default
  `campaigns/<slug>/scratch`), `AUTORESEARCH_KEEP_ARTIFACTS`
  (`none`/`pass`/`all` — whole run dirs; the adapter's evidence files are
  kept regardless), `AUTORESEARCH_ARTIFACTS_DIR`, and any solver-specific
  seed/intermediate store the adapter needs.
- **Reusing expensive intermediates (multi-step pipelines only)**: one run is
  one full pipeline. If a costly first step (e.g. a solve) should be reused
  across downstream scans, make it its own mode that archives its output under
  its run id, and let the later mode pick it up and set `parent_run_id` (the
  toy adapter's docstring describes the pattern).

## Phase 6 — Generate

1. **Adapter** (`adapters/<solver-slug>.py`) — implement every member that
   `contract.py`'s docstring describes, modeling `adapters/toy.py`. Checklist:
   `NAME`, `MODES`, `TARGET_FLAG`, `REQUIRED_ENV`, `EXECUTION_FLAGS`,
   `SEED_FLAG`, `THREADS_FLAG`, `REPLAY_TOLERANCE`, `METRICS`,
   `add_arguments`, `solver_identity`, `run_experiment`. Fill them from
   Phase 4 and the interview (`METRICS` goals come from the metric-goal
   answers). In a comment, say whether `REPLAY_TOLERANCE` was measured or is a
   starting value.
   - **Register it** in `adapters/__init__.py`: one import line and one
     `REGISTRY` entry. Do **not** edit `adapter.py`, `run.py` or any other
     core module.
2. **Campaign settings**, two files from the interview; omit anything left at
   default or unconstrained. A key in the wrong file is a load error naming
   the right one.
   - **`campaigns/<slug>/config.json`** (experiment design, committed) —
     `{"adapter": "<key>", "plan_minutes": P, "fixed": {...}, "bounds": {...},
     "budget": {"runs": R, "hours": H}}`: the adapter's `REGISTRY` key;
     `plan_minutes` from question 3; `fixed` / `bounds` / `budget` from the
     limits and budget answers. No paths or secrets.
   - **`campaigns/<slug>/local.json`** (this machine, gitignored; skip it
     when empty) — `{"env": {...}, "max_parallel": N}`: in `env` the solver
     root, interpreter, config dir, device settings (e.g. the JAX memory
     variables), any credentials and any non-default artifact-layout values;
     `max_parallel` only when the machine is shared between campaigns.
   No shell exports are needed. Check they load:
   `python run.py brief --campaign <slug>` shows the constraints and budget.
3. **Machine settings** — `python run.py status --usable-cores C
   --usable-memory-gb M --max-parallel N` from question 2. Start
   `--max-parallel` at `usable cores ÷ threads per run`; Phase 7 refines it
   with measured memory. These are machine-wide (`~/.autoresearch/machine.json`),
   shared by every campaign.
4. **`campaigns/<slug>/program.md`** — fill every `{{PLACEHOLDER}}` in
   `templates/program_template.md` from the interview: title, slug, mission,
   goals and stopping criteria, hard rules (only the ones code cannot
   check). Keep it near 30 lines. Parameters, modes, schema, machine numbers
   and the research loop are not copied in — the agent reads them live
   (`--help`, `brief`, `status`) and from the `/research` skill. Do not invent
   physics. Remove all template HTML comments from the generated file.
5. **`campaigns/<slug>/LESSONS.md`** — copy `templates/LESSONS.md`. Never
   overwrite an existing campaign's `LESSONS.md`.
6. **`lessons/<adapter>.md`** (repo root; `<adapter>` is the adapter's
   `NAME`) — if missing, copy `templates/LESSONS.md` with the title
   `# Solver lessons — <adapter>`. Never overwrite an existing one: it holds
   lessons from every campaign on that solver.

## Phase 7 — Verify (must end green)

1. Run one tiny experiment with `python run.py --campaign <slug> ...` and the
   smallest meaningful settings (low iterations/resolution, short timeout)
   that respect the campaign's `fixed` / `bounds` (a violating run is refused
   — that refusal is also a check that the constraints load). Smoke runs
   count toward the budget.
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
4. **Size from measurement.** Run one realistic (not reduced) experiment per
   mode, then `python run.py status`: it shows each mode's median run time,
   peak memory and threads, how many runs fit at once, and the batch size per
   planning interval. If memory, not cores, is the limit, lower
   `--max-parallel` accordingly; show the user the numbers and confirm.
5. Leave smoke rows in the DB by default — they are honest history; clean up only
   if the user asks.

## Phase 8 — Report & how to run

Print a short, practical summary:
- The adapter written (`adapters/<slug>.py`), the campaign folder
  (`campaigns/<slug>/`: `config.json`, `local.json`, `program.md`, `LESSONS.md`) and the
  solver lessons file (`lessons/<adapter>.md`, new or existing).
- The active constraints (`fixed`, `bounds`) and budget.
- Any adapter↔solver flag drift found and how it was resolved.
- The machine budget: run slots, runs that fit at once per mode, the
  suggested batch size per planning interval, and which devices passed the
  probe.
- **The first real launch command**, e.g.
  `python run.py --campaign <slug> --mode <mode> --<target-flag> <case> [params]`.
- **How to start the loop**: `/research <slug>` (it also resumes a stopped
  campaign).
- Reminders: lesson files are append-only memory; `program.md` holds the
  goals, `config.json` the enforced limits (commit it with `program.md` and
  `LESSONS.md`; `local.json` stays on this machine); query results with
  `python run.py query --campaign <slug> "SQL"`; re-run `/setup-harness` for a
  new campaign or solver (it creates a new campaign folder / adapter rather
  than overwriting).
