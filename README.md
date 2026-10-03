# autoresearch — a runner and logbook for optimization campaigns

> **Start with `/setup-harness`, run with `/research <campaign>`** (Claude
> Code). Setup wires your solver and writes the campaign; the research skill
> runs the loop. You never write a system prompt.

Autonomous AI agent harness for optimization campaigns. Fork of
[karpathy/autoresearch](https://github.com/karpathy/autoresearch) by
[Andrej Karpathy](https://github.com/karpathy), adapted from LLM training to
solver-driven optimization.

It does **not** do the science — that's yours. It does the work *around* it:
you describe one experiment as a command, and it runs your parameter
scans, records every result (and every failure) in a queryable SQLite database,
and keeps an append-only `LESSONS.md` so nothing learned is lost between
sessions. You decide what to try; it handles the loop and the bookkeeping.

The harness core is **solver-agnostic**. You bring your optimizer — anything
that runs from a command — behind a small *adapter*, and the core never
changes. A stdlib-only toy adapter ships as the reference.

## How it works

```
run.py                    ← generic runner: selects the campaign, runs its adapter, records results
adapter.py                ← looks up the adapter a campaign's config.json names
contract.py               ← the harness↔adapter contract
adapters/__init__.py      ← registry of installed adapters (static imports)
adapters/<solver>.py      ← solver-specific glue (the only file that knows your solver)
campaigns/<name>/
  config.json             ← experiment design: adapter, parameter constraints, budget (commit it)
  local.json              ← this machine only: solver paths and other env, run cap (gitignored, optional)
  program.md              ← mission, goals + stopping criteria, rules code can't check
  LESSONS.md              ← append-only campaign memory (agent + human)
  runs/<run-id>.json      ← one record per run: the source of truth
  blobs/                  ← evidence files (logs, results, solver patches) by content hash
  results.db              ← query index of runs/ (`runs` table + `results` view)
  batches/<batch-id>.json ← each batch: its file, hash, hypothesis, cited lessons, limits, run ids
  claims/                 ← per-spec locks (a spec runs in one process at a time)
  scratch/                ← live run directories
lessons/<adapter>.md      ← append-only solver memory, shared by every campaign on that adapter
.claude/skills/research/  ← the research method: one copy for all campaigns
```

A **campaign** is one research goal: one solver, one objective, one set of hard
limits. Each campaign keeps its own database, lessons and program file, so
several campaigns can live side by side without mixing. Start a new campaign
when the goal changes; new parameters for the same goal are just more runs.

The agent (via `/research <campaign>`) reads the campaign's `program.md` and
lessons, gets a digest of what's been tried from `run.py brief`, plans a batch,
runs it through `run.py`, records what it learned, and loops.

Rules a computer can check are enforced in code — metric directions and
solver validity by the adapter, parameter constraints and budget by `run.py`
from `config.json`. Prose (`program.md`, lessons, the research skill) holds
only judgment.

## Quick start

If you use Claude Code, the fastest path is the bundled setup skill:

```
/setup-harness
```

It finds your solver and the interpreter it runs in, **installs any missing
dependencies**, detects your hardware, interviews you about your campaign
(goals, parameter limits, budget), generates a solver adapter and a campaign
folder, and ends with a real smoke run. For a new goal on a solver that is
already wired up, it skips straight to the campaign questions. Then:

```
/research <campaign>
```

starts the experiment loop, or resumes it in a later session. You do not need
to read anything below first.

To try the harness without any solver, use the toy adapter:

```bash
mkdir -p campaigns/demo
echo '{"adapter": "toy"}' > campaigns/demo/config.json
python run.py --problem rastrigin --dim 4 --seed 3
python run.py query "SELECT status, target, seed, objective_J FROM results"
```

## Campaigns

`run.py` works on one campaign per call:

- `--campaign <name>` (or `$AUTORESEARCH_CAMPAIGN`) selects it.
- With exactly one campaign under `campaigns/`, it is selected automatically.
- With several and none named, `run.py` refuses rather than guess.

A campaign's settings are split by who they belong to. `config.json` is the
experiment design — part of the research record, committed with
`program.md` and `LESSONS.md`. `local.json` is what depends on this machine
(paths, possibly secrets) — gitignored and optional. A key in the wrong file
stops every command for the campaign with an error naming the file it
belongs in; `SETTING_FILES` in `campaign.py` is the one list of which key
lives where.

`campaigns/<name>/config.json`:

```json
{
  "adapter": "toy",
  "plan_minutes": 30,
  "fixed":  {"dim": 4},
  "bounds": {"step_size": [0.01, 1.0], "maxiter": [100, 5000]},
  "budget": {"runs": 500, "hours": 24}
}
```

`campaigns/<name>/local.json` *(optional)*:

```json
{
  "env": {"SOLVER_ROOT": "/path/to/solver"},
  "max_parallel": 4
}
```

- `adapter` — a key in `adapters/__init__.py` `REGISTRY` (e.g. `"toy"`).
- `plan_minutes` *(optional)* — how often the agent plans a new batch (see
  [Machine budget](#machine-budget)).
- `env` *(optional, local.json)* — settings the adapter reads (solver paths,
  the interpreter that has your solver installed, credentials). A variable
  already set in your shell overrides the local.json value. This avoids
  shell-specific `export` syntax. Never put these in `config.json`.
- `max_parallel` *(optional, local.json)* — this campaign's cap on runs at
  once on this machine (see [Machine budget](#machine-budget)).
- `fixed` *(optional)* — parameters every run must use: each run's parsed
  value (default included) must equal the given value, parsed by the
  adapter's own flag.
- `bounds` *(optional)* — inclusive `[min, max]` per numeric parameter; a
  non-numeric or missing (`None`) value for a bounded parameter is a
  violation. A parameter may appear in `fixed` or `bounds`, not both.
- `budget` *(optional)* — `runs`: the number of run records in the campaign
  (all statuses); `hours`: the sum of their recorded `elapsed` time. Usage is
  counted from recorded runs, so runs already in flight when the budget is
  reached can overshoot it by at most the number running at once.

Keys in `fixed` / `bounds` are adapter parameter names exactly as used in
batch specs (argparse dests, e.g. `step_size`). Unknown names, the adapter's
execution flags, core flags, or malformed values are a config error when the
campaign loads, and stop every command for that campaign until fixed. Absent keys mean no constraint. Enforcement:

- A single run that breaks `fixed` / `bounds`, or starts after either budget
  is exhausted, is refused before it executes (non-zero exit, nothing
  recorded).
- `batch` checks every spec up front, together with its other spec
  validation, and refuses the whole batch on any violation, or when its new
  (not yet recorded) runs exceed the remaining run budget (a promotion stage
  counts at its maximum, top × replicates). While it runs, it stops launching
  once either budget is spent (runs already going finish). `--dry-run` reports violations, remaining budget and the planned
  new runs.
- `replay` is exempt — it re-checks an existing record — but its run counts
  toward the budget afterwards.
- `brief` and `status` show the active `fixed` / `bounds` and the budget used
  and remaining.

What to commit: `config.json`, `program.md` and `LESSONS.md` — the
experiment's design, goals and memory. `local.json` and the run data
(`runs/`, `blobs/`, `results.db`, `scratch/`, `artifacts/`, `batches/`,
`claims/`) are gitignored. Each batch record stores the `fixed`, `bounds`
and `budget` in effect when it ran, so a later change to `config.json` does
not rewrite what earlier batches were allowed to do.

## Architecture: core + adapter

The core (`run.py`) owns campaign selection, the database, the scratch/artifact
lifecycle, and the agent-facing CLI skeleton — and nothing solver-specific. One
experiment is:

```
run.py  →  adapter.run_experiment(args, run)  →  ExperimentOutcome  →  runs/<id>.json  →  results.db
```

The adapter owns everything about your solver: its flags, modes and metrics,
and how to run one experiment end-to-end — a single subprocess or a chained
pipeline. It returns metrics plus what the run used (provenance) and which
files to keep (evidence). The full interface is described in one place, the
docstring of `contract.py`. The core records the full parsed command line of
every run, so no flag is ever lost.

## Reproducibility

- **Spec hash and dedupe.** Each run's `spec_hash` covers the adapter, every
  flag except execution-only ones (timeout, threads, solver location) and the
  solver identity (commit + uncommitted-diff hash). If a pass/fail run with
  the same hash and `--replicate` index exists, `run.py` prints
  `{"duplicate_of": ...}` instead of running it again. Crashes can always be
  retried.
- **Seeds.** If the solver has a seed flag and you leave it unset, the
  harness derives one from the spec and `--replicate` (default 0). Repeats
  are deliberate: `--replicate 1`, `2`, … draw new seeds.
- **Provenance.** Every record keeps the solver identity, the exact solver
  command, input-file hashes, the harness commit, and the platform.
- **Evidence.** Files the adapter names (log, results, solver patch) are kept
  in `blobs/` by content hash, whatever `AUTORESEARCH_KEEP_ARTIFACTS` says.
- **Replay.** `python run.py replay <run-id>` re-runs a recorded experiment
  from its spec and compares status and metrics within the adapter's
  `REPLAY_TOLERANCE`; it exits 2 on a mismatch and says whether the solver
  changed since. A replay skips dedupe but still waits for a run slot.
- **Rebuild.** `python run.py rebuild` regenerates `results.db` from
  `runs/`.

## Commands

| Command | What it does |
|---------|--------------|
| `python run.py [--campaign C] <adapter flags>` | run one experiment; prints a compact JSON summary (set fields, metrics, `on_front`, `crash_signature`) |
| `python run.py brief [--campaign C]` | fixed-size digest: counts per mode/target, Pareto fronts over the adapter's goal metrics, recent runs, crash causes, replicate spread, runs since the front last moved, latest campaign and solver lessons, active constraints and budget, run slots and batch sizing |
| `python run.py query "SQL" [--limit N]` | one read-only SQL statement, tab-separated, capped (default 50 rows) |
| `python run.py replay <run-id>` | re-run a recorded experiment and compare |
| `python run.py rebuild` | regenerate `results.db` from `runs/` |
| `python run.py batch FILE [--parallel N] [--dry-run]` | run a planned batch of experiments (below) |
| `python run.py status [--max-parallel N] [--usable-cores C] [--usable-memory-gb M]` | hardware, machine settings and run slots, then every campaign: adapter, run counts, last run, runs since its front moved, constraints and budget, measured run cost and sizing per mode; flags save machine settings |

A crashed run's `crash_signature` is the line in its log that names the failure
(the last `...Error:` line, else the last line), with paths and numbers
normalized so the same failure groups together across runs.

## Batches

Instead of thinking before every single run, the agent can plan a batch: write
one JSON file, run it in the background, and analyze the summary when it
finishes. The format is documented at the top of `batch.py`:

```json
{
  "hypothesis": "what this batch should show",
  "lessons": {"applies": [], "tests": [], "rejects": []},
  "early_stop": {"same_crash": 3},
  "stages": [
    {"name": "screen", "base": {"problem": "rastrigin", "dim": 4},
     "halton": {"n": 16, "ranges": {"step_size": [0.01, 1.0, "log"], "maxiter": [200, 2000, "int"]}}},
    {"name": "confirm", "from": "screen", "select": {"top": 3, "by": "objective_J"},
     "base": {"problem": "rastrigin", "dim": 4, "maxiter": 5000}, "carry": ["step_size"], "replicates": 3}
  ]
}
```

- **Points:** `runs` (explicit), `grid` (cartesian product), `halton`
  (low-discrepancy sequence) and `lhs` (seeded Latin hypercube); ranges are
  `[min, max]` plus optional `"log"` / `"int"`. Each point is merged into
  `base` and run `replicates` times.
- **Promotion:** a stage with `from` takes the best passing runs of an earlier
  stage (`"by"`: a goal metric, or `"front"` for the Pareto front), carries
  the named params, and records the source run as each new run's parent.
- **Before anything runs** every spec is parsed against the adapter's flags
  and checked against the campaign's `fixed` / `bounds` and run budget; any
  error stops the whole batch. `--dry-run` shows the plan, how many runs are
  already recorded, and the remaining budget.
- **While it runs:** each distinct spec is its own `run.py` process (specs
  repeated in the file run once); specs already recorded are reused, not
  re-run; a spec another agent is running right now is waited for and then
  reused; launching stops once the last
  `same_crash` runs crashed the same way (0 disables), or once the hours
  budget is spent. Children's stderr goes
  to `batches/<id>.log`, so the summary stays short.
- **Waiting is free:** run it in the background and read the summary when it
  ends; SIGTERM (or Ctrl-C) cancels it, and each in-flight run records itself
  as `cancelled` after its solver's whole process tree is killed.

## Machine budget

Every run, from every campaign, holds one of the machine's run slots while it
executes, so the machine is never oversubscribed however many agents and
batches run at once. Slots are lock files under `~/.autoresearch/slots`
(`$AUTORESEARCH_MACHINE_DIR/slots` when set), released by the OS if a run dies.

`python run.py status` shows what the harness sees and what fits:

- **hardware** — OS, usable CPUs (affinity and SLURM aware), performance
  cores on Apple Silicon, memory, GPUs (nvidia-smi; Apple's shared-memory GPU),
  scheduler;
- **settings** — `~/.autoresearch/machine.json` (`max_parallel`,
  `usable_cores`, `usable_memory_gb`), written by the same command:
  `python run.py status --max-parallel 6 --usable-cores 60 --usable-memory-gb 100`;
- **per campaign** — adapter, run counts (pass/fail/crash), last run, runs
  since its front moved, and per mode the median run time, peak memory and
  threads of the recorded runs, how many fit at once (`usable cores ÷ threads`, capped by
  `usable memory ÷ peak memory`), and, with `"plan_minutes"` in the
  campaign's `config.json`, the batch size that fills one planning interval
  (`runs at once × planning interval ÷ run time`).

Slot capacity: `$AUTORESEARCH_MAX_PARALLEL`, else `machine.json`, else 1. A
campaign's `local.json` may cap its own batches with `"max_parallel": N`.
`run.py brief` repeats the per-mode sizing for its campaign. `/setup-harness`
asks where runs execute, how much of the machine to use and how often to
plan, and fills all of this in; the run budget is the campaign's `budget`.

## Environment variables

| Variable | Description |
|----------|-------------|
| `AUTORESEARCH_CAMPAIGN` | *(optional)* campaign to use when `--campaign` is not given. |
| `AUTORESEARCH_CAMPAIGNS_DIR` | *(optional)* where campaigns live (default `<repo>/campaigns`). |
| `AUTORESEARCH_MAX_PARALLEL` | *(optional)* machine-wide run slots (default 1). |
| `AUTORESEARCH_MACHINE_DIR` | *(optional)* machine-wide state: `machine.json` and the `slots/` lock files (default `~/.autoresearch`). |
| `AUTORESEARCH_SCRATCH_DIR` | *(optional)* scratch dir for live runs (default `campaigns/<name>/scratch`). |
| `AUTORESEARCH_KEEP_ARTIFACTS` | *(optional)* retention for completed runs' outputs: `none` (default) / `pass` / `all`. Kept dirs move to `AUTORESEARCH_ARTIFACTS_DIR/<run-id>`. |
| `AUTORESEARCH_ARTIFACTS_DIR` | *(optional)* where kept run dirs land, named by run id (default `campaigns/<name>/artifacts`). |

Any of these can also go in a campaign's `local.json` `env` map.

## Results

Every run is written to the campaign's `runs/<run-id>.json` and indexed in
`results.db` (SQLite). The `runs` table is the same for every solver:
identity, status, timing, and JSON columns for `metrics` (whatever the adapter
emits), `params` (every flag), `provenance` and `evidence`. The `results` view
adds one column per metric the adapter declares in `METRICS`, and its goal
metrics get indexes.

```bash
# read-only SQL through the harness (recommended for agents; works without the sqlite3 CLI)
python run.py query --campaign <name> "SELECT id, target, objective_J FROM results WHERE status='pass' ORDER BY objective_J LIMIT 10"

# a parameter from the JSON columns
python run.py query --campaign <name> "SELECT id, json_extract(params,'$.<your_flag>') FROM runs"

# flat files (for scripts, jq, grep)
jq 'select(.status=="pass")' campaigns/<name>/runs/*.json
```

## Autonomous agent usage

In Claude Code:

```
/research <name>
```

The research skill (`.claude/skills/research/SKILL.md`) is the method shared by
every campaign: start or resume from `program.md`, the lesson files and
`run.py brief` (never from chat history); then brief → plan a batch →
`--dry-run` → run → record lessons and distill → repeat, until the campaign's
stopping criteria or budget end it. Each campaign's `program.md` holds only
its mission, goals and stopping criteria, and the rules code cannot check
(`templates/program_template.md`, about 30 lines). Other agents can be pointed
at the skill file and the campaign's `program.md`.

## Lessons

Memory lives at three levels, all append-only (corrections are new entries):

| Level | File | Changed by |
|---|---|---|
| Campaign | `campaigns/<name>/LESSONS.md` | the agent, after any finding that generalizes |
| Solver | `lessons/<adapter>.md` (repo root, tracked) | the agent, by promotion from campaigns |
| Method | `.claude/skills/research/SKILL.md` | the user only |

Both lesson files use the entry format in `templates/LESSONS.md`; solver
entries add a required `source:` field (campaign, campaign-lesson title, run
ids). A campaign lesson is promoted to the solver file when it is `confirmed`
and its scope does not depend on the campaign's goal; it is `confirmed` at
solver level only once it holds in at least two campaigns, otherwise
`hypothesis`. A confirmed parameter limit may become a `config.json` `bounds`
entry, only after the user agrees. `run.py brief` lists the latest titles of
both files (solver lessons in their own section; none when the adapter has
no lessons file). `/setup-harness` creates `lessons/<adapter>.md` when it is
missing.

## Adding a solver

You never touch `run.py`. Either:

1. Run `/setup-harness` — it detects your solver, installs deps, and generates
   the adapter and campaign for you; **or**
2. Write `adapters/<your-solver>.py` implementing the interface described in
   `contract.py`'s docstring,
   using `adapters/toy.py` as the template; register it in
   `adapters/__init__.py` (one import + one `REGISTRY` entry);
   then create `campaigns/<name>/config.json` with `"adapter": "<your-solver>"`.

`run_experiment` can run a single subprocess or a multi-step pipeline (each
step's output feeding the next, several `run_solver` calls in one run). An
adapter with a cheap and a costly stage can expose both as `MODES`, and a
batch can promote the best cheap runs to the costly mode.

## Project structure

```
contract.py                     ← harness↔adapter contract: its docstring is the full interface
run.py                          ← CLI: argument parsing and dispatch
runner.py                       ← spec/seed/hash, run, replay, batch execution, brief and status
records.py                      ← run files, evidence store, results.db (columns defined once), query
campaign.py                     ← campaign selection, config.json + local.json, layout, env vars, run slots
analysis.py                     ← derived views: crash signatures, Pareto fronts, the brief
batch.py                        ← batch files: validation, spec expansion, promotion, early stop
locks.py                        ← OS-released file locks for run slots and spec claims
machine.py                      ← hardware detection, peak memory, run-slot and batch sizing
adapter.py                      ← adapter lookup + contract check
adapters/__init__.py            ← adapter registry (static imports)
adapters/toy.py                 ← reference adapter (stdlib-only test functions)
examples/toy_solver.py          ← the toy adapter's solver
templates/program_template.md   ← skeleton for a campaign's program.md (~30 lines)
templates/LESSONS.md            ← entry format for campaign and solver lessons
campaigns/<name>/               ← one folder per campaign (created by /setup-harness)
lessons/<adapter>.md            ← solver lessons, shared across campaigns (created by /setup-harness)
.claude/skills/setup-harness/   ← interactive setup: new solver, or new campaign for an existing adapter
.claude/skills/research/        ← the research loop, run with /research <campaign>
```

## Tests

```bash
python3 -m unittest discover -s tests -t .      # core, with the toy adapter
```
