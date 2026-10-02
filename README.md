# autoresearch — a runner and logbook for optimization campaigns

Autonomous AI agent harness for optimization campaigns. Fork of
[karpathy/autoresearch](https://github.com/karpathy/autoresearch) by
[Andrej Karpathy](https://github.com/karpathy), adapted from LLM training to
solver-driven optimization.

It does **not** do the physics — that's yours. It does the work *around* the
physics: you describe one experiment as a command, and it runs your parameter
scans, records every result (and every failure) in a queryable SQLite database,
and keeps an append-only `LESSONS.md` so nothing learned is lost between
sessions. You decide what to try; it handles the loop and the bookkeeping.

The harness core is **solver-agnostic**. You bring your optimizer — simsopt,
DESC, anything that runs from a command — behind a small *adapter*, and the core
never changes. A stdlib-only toy adapter ships as the reference; a real physics
adapter (banana coils on simsopt) lives in [`examples/banana/`](examples/banana/).

## How it works

```
run.py                    ← generic runner: selects the campaign, runs its adapter, stores results
adapter.py                ← loads the adapter a campaign's config.json names
contract.py               ← the harness↔adapter contract
adapters/<solver>.py      ← solver-specific glue (the only file that knows your solver)
schema.sql                ← database schema (applied automatically)
campaigns/<name>/
  config.json             ← which adapter + its settings (solver paths, interpreter)
  program.md              ← agent instructions for this campaign
  LESSONS.md              ← append-only research memory (agent + human)
  results.db              ← experiment database (query with SQL)
  results.jsonl           ← append-only log (human-readable backup)
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

It detects your solver stack (simsopt / DESC / other), **installs any missing
dependencies**, interviews you about your campaign, generates a solver adapter
and a campaign folder, and ends with a real smoke run. You do not need to read
anything below first.

To try the harness without any solver, use the toy adapter:

```bash
mkdir -p campaigns/demo
echo '{"adapter": "toy"}' > campaigns/demo/config.json
python run.py --problem rastrigin --dim 4 --seed 3
sqlite3 campaigns/demo/results.db -header -column \
  "SELECT status, equilibrium, objective_J, json_extract(params,'$.seed') AS seed FROM runs"
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

- `adapter` — a module in `adapters/` (`"toy"`), or a dotted module path
  (`"examples.banana.simsopt_banana"`).
- `env` *(optional)* — settings the adapter reads (solver paths, the
  interpreter that has your solver installed). A variable already set in your
  shell overrides the config value. This avoids shell-specific `export`
  syntax.

`config.json`, `results.*` and `artifacts/` are gitignored (machine-specific
paths and run data); `program.md` and `LESSONS.md` are yours to commit or not.

## Architecture: core + adapter

The core (`run.py`) owns campaign selection, the database, the scratch/artifact
lifecycle, and the agent-facing CLI skeleton — and nothing solver-specific. One
experiment is:

```
run.py  →  adapter.run_experiment(args, run_dir)  →  ExperimentOutcome  →  results.db / results.jsonl
```

The adapter (see `contract.py` for the interface) owns everything about your
solver: which flags exist (`add_arguments`), which modes it has
(`SOLVER_MODES`), which flag names the target configuration (`TARGET_FLAG`),
and how to run one experiment end-to-end (`run_experiment`) — whether that's a
single subprocess or a chained pipeline. It returns metrics as canonical keys;
the core stores the ones with a dedicated column and preserves the rest in a
`metrics` JSON blob, so every solver shares one schema. The core also records
the full parsed command line of every run, so no flag is ever lost.

## Environment variables

| Variable | Description |
|----------|-------------|
| `AUTORESEARCH_CAMPAIGN` | *(optional)* campaign to use when `--campaign` is not given. |
| `AUTORESEARCH_CAMPAIGNS_DIR` | *(optional)* where campaigns live (default `<repo>/campaigns`). |
| `OUTPUT_BASE` | *(optional)* scratch dir for live runs (default `/tmp/stellarator_harness`). Crashed runs leave their dir + `run.log` here for debugging. |
| `KEEP_ARTIFACTS` | *(optional)* retention for completed runs' outputs: `none` (default) / `pass` / `all`. Kept dirs move to `ARTIFACTS_DIR/<run-id>`. |
| `ARTIFACTS_DIR` | *(optional)* where kept run dirs land, named by run id (default `campaigns/<name>/artifacts`). |

Any of these can also go in a campaign's `config.json` `env` map.

## Results

Every run writes to both the campaign's `results.jsonl` (flat file) and
`results.db` (SQLite). The columns are the same for every solver;
solver-specific metrics live in the `metrics` JSON column, and every flag of
the run in the `params` JSON column.

```bash
DB=campaigns/<name>/results.db

# SQLite (recommended for agents)
sqlite3 $DB -header -column "SELECT * FROM runs WHERE status='pass' ORDER BY objective_J LIMIT 10"

# a solver-specific metric or parameter from the JSON columns
sqlite3 $DB "SELECT id, json_extract(metrics,'\$.<your_metric>'), json_extract(params,'\$.<your_flag>') FROM runs"

# JSONL (for scripts, jq, grep)
jq 'select(.status=="pass")' campaigns/<name>/results.jsonl
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
2. Write `adapters/<your-solver>.py` implementing the `contract.py` interface
   (`NAME`, `SOLVER_MODES`, `TARGET_FLAG`, `ENV_REQUIREMENTS`, `add_arguments`,
   `run_experiment`), using `adapters/toy.py` as the template and
   `examples/banana/simsopt_banana.py` for a multi-mode physics example, then
   create `campaigns/<name>/config.json` with `"adapter": "<your-solver>"`.

`run_experiment` can run a single subprocess or a multi-step pipeline (e.g. a
DESC chain: bumped surface → fixed-boundary equilibrium → coil optimization →
free-boundary equilibrium). Solver-specific metrics go to the `metrics` JSON
column.

## Project structure

```
contract.py                     ← harness↔adapter contract (ExperimentOutcome, helpers)
run.py                          ← generic experiment runner (solver-agnostic)
adapter.py                      ← adapter loader
adapters/toy.py                 ← reference adapter (stdlib-only test functions)
examples/toy_solver.py          ← the toy adapter's solver
examples/banana/                ← real-world example: banana coils on simsopt
schema.sql                      ← database schema
templates/program_template.md   ← skeleton for a campaign's program.md
templates/LESSONS.md            ← scaffold for a campaign's LESSONS.md
campaigns/<name>/               ← one folder per campaign (created by /setup-harness)
.claude/skills/setup-harness/   ← interactive first-time setup skill
ROADMAP.md                      ← planned work
```

## Tests

```bash
python3 -m unittest discover -s tests -t .      # core, with the toy adapter
python3 -m unittest discover -s examples -t .   # example adapters
```
