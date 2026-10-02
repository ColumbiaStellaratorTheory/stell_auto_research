# autoresearch — a runner and logbook for optimization campaigns

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
  config.json             ← which adapter + its settings (solver paths, interpreter)
  program.md              ← agent instructions for this campaign
  LESSONS.md              ← append-only research memory (agent + human)
  runs/<run-id>.json      ← one record per run: the source of truth
  blobs/                  ← evidence files (logs, results, solver patches) by content hash
  results.db              ← query index of runs/ (`runs` table + `results` view)
  batches/<batch-id>.json ← each batch: its file, hash, hypothesis, cited lessons, run ids
  claims/                 ← per-spec locks (a spec runs in one process at a time)
  scratch/                ← live run directories
```

A **campaign** is one research goal: one solver, one objective, one set of hard
limits. Each campaign keeps its own database, lessons and program file, so
several campaigns can live side by side without mixing. Start a new campaign
when the goal changes; new parameters for the same goal are just more runs.

The agent reads the campaign's `program.md`, queries its `results.db` to see
what's been tried, picks parameters, calls `run.py`, evaluates the result, and
loops.

## Quick start

If you use Claude Code, the fastest path is the bundled setup skill:

```
/setup-harness
```

It finds your solver and the interpreter it runs in, **installs any missing
dependencies**, detects your hardware, interviews you about your campaign,
generates a solver adapter and a campaign folder, and ends with a real smoke
run. You do not need to read anything below first.

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

`campaigns/<name>/config.json`:

```json
{
  "adapter": "toy",
  "env": {"SOLVER_ROOT": "/path/to/solver"}
}
```

- `adapter` — a key in `adapters/__init__.py` `REGISTRY` (e.g. `"toy"`).
- `env` *(optional)* — settings the adapter reads (solver paths, the
  interpreter that has your solver installed). A variable already set in your
  shell overrides the config value. This avoids shell-specific `export`
  syntax.

`config.json` and the run data (`runs/`, `blobs/`, `results.db`, `scratch/`,
`artifacts/`) are gitignored; `program.md` and `LESSONS.md` are yours to commit
or not.

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
| `python run.py brief [--campaign C]` | fixed-size digest: counts per mode/target, Pareto fronts over the adapter's goal metrics, recent runs, crash causes, replicate spread, runs since the front last moved, latest lessons |
| `python run.py query "SQL" [--limit N]` | one read-only SQL statement, tab-separated, capped (default 50 rows) |
| `python run.py replay <run-id>` | re-run a recorded experiment and compare |
| `python run.py rebuild` | regenerate `results.db` from `runs/` |
| `python run.py batch FILE [--parallel N] [--dry-run]` | run a planned batch of experiments (below) |
| `python run.py status [--max-parallel N] [--usable-cores C] [--usable-memory-gb M]` | hardware, machine settings and run slots, then every campaign: adapter, run counts, last run, runs since its front moved, measured run cost and sizing per mode; flags save machine settings |

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
- **Before anything runs** every spec is parsed against the adapter's flags;
  any error stops the whole batch. `--dry-run` shows the plan and how many
  runs are already recorded.
- **While it runs:** each distinct spec is its own `run.py` process (specs
  repeated in the file run once); specs already recorded are reused, not
  re-run; a spec another agent is running right now is waited for and then
  reused; launching stops once the last
  `same_crash` runs crashed the same way (0 disables). Children's stderr goes
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
  campaign's config, the batch size that fills one planning interval
  (`runs at once × planning interval ÷ run time`).

Slot capacity: `$AUTORESEARCH_MAX_PARALLEL`, else `machine.json`, else 1. A
campaign's `config.json` may cap its own batches with `"max_parallel": N`.
`run.py brief` repeats the per-mode sizing for its campaign. `/setup-harness`
asks four questions (where runs execute, how much of the machine to use, how
often to plan, session budget) and fills all of this in.

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

Any of these can also go in a campaign's `config.json` `env` map.

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

Point your AI agent at the campaign's program file and let it go:

```
Read campaigns/<name>/program.md, then start the optimization loop.
```

The agent queries the database, picks experiments, runs them, evaluates results,
records lessons, and repeats.

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
campaign.py                     ← campaign selection, config.json, layout, env vars, run slots
analysis.py                     ← derived views: crash signatures, Pareto fronts, the brief
batch.py                        ← batch files: validation, spec expansion, promotion, early stop
locks.py                        ← OS-released file locks for run slots and spec claims
machine.py                      ← hardware detection, peak memory, run-slot and batch sizing
adapter.py                      ← adapter lookup + contract check
adapters/__init__.py            ← adapter registry (static imports)
adapters/toy.py                 ← reference adapter (stdlib-only test functions)
examples/toy_solver.py          ← the toy adapter's solver
templates/program_template.md   ← skeleton for a campaign's program.md
templates/LESSONS.md            ← scaffold for a campaign's LESSONS.md
campaigns/<name>/               ← one folder per campaign (created by /setup-harness)
.claude/skills/setup-harness/   ← interactive first-time setup skill
```

## Tests

```bash
python3 -m unittest discover -s tests -t .      # core, with the toy adapter
```
