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
run.py                    ← generic runner: selects the campaign, runs its adapter, records results
adapter.py                ← looks up the adapter a campaign's config.json names
contract.py               ← the harness↔adapter contract
adapters/__init__.py      ← registry of installed adapters (static imports)
adapters/<solver>.py      ← solver-specific glue (the only file that knows your solver)
schema.sql                ← database schema (applied automatically)
campaigns/<name>/
  config.json             ← which adapter + its settings (solver paths, interpreter)
  program.md              ← agent instructions for this campaign
  LESSONS.md              ← append-only research memory (agent + human)
  runs/<run-id>.json      ← one record per run: the source of truth
  blobs/                  ← evidence files (logs, results, solver patches) by content hash
  results.db              ← query index of runs/ (query with SQL)
  results.jsonl           ← flat export of runs/, written by `run.py rebuild`
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

- `adapter` — a key in `adapters/__init__.py` `REGISTRY` (`"toy"`,
  `"simsopt_banana"`).
- `env` *(optional)* — settings the adapter reads (solver paths, the
  interpreter that has your solver installed). A variable already set in your
  shell overrides the config value. This avoids shell-specific `export`
  syntax.

`config.json` and the run data (`runs/`, `blobs/`, `results.*`, `scratch/`,
`artifacts/`) are gitignored; `program.md` and `LESSONS.md` are yours to commit
or not.

## Architecture: core + adapter

The core (`run.py`) owns campaign selection, the database, the scratch/artifact
lifecycle, and the agent-facing CLI skeleton — and nothing solver-specific. One
experiment is:

```
run.py  →  adapter.run_experiment(args, run)  →  ExperimentOutcome  →  runs/<id>.json  →  results.db
```

The adapter (see `contract.py` for the interface) owns everything about your
solver: which flags exist (`add_arguments`), which modes it has
(`SOLVER_MODES`), which flag names the target configuration (`TARGET_FLAG`),
which flag is its RNG seed (`SEED_FLAG`), what fingerprints its code
(`solver_identity`), and how to run one experiment end-to-end
(`run_experiment`) — whether that's a single subprocess or a chained pipeline.
It returns metrics as canonical keys plus what the run used (provenance) and
which files to keep (evidence). The core records the full parsed command line
of every run, so no flag is ever lost.

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
  in `blobs/` by content hash, whatever `KEEP_ARTIFACTS` says.
- **Replay.** `python run.py replay <run-id>` re-runs a recorded experiment
  from its spec and compares status and metrics within the adapter's
  `REPLAY_TOLERANCE`; it exits 2 on a mismatch and says whether the solver
  changed since.
- **Rebuild.** `python run.py rebuild` regenerates `results.db` and
  `results.jsonl` from `runs/`. `--from-jsonl FILE` first imports records
  from an older harness's `results.jsonl`.

## Environment variables

| Variable | Description |
|----------|-------------|
| `AUTORESEARCH_CAMPAIGN` | *(optional)* campaign to use when `--campaign` is not given. |
| `AUTORESEARCH_CAMPAIGNS_DIR` | *(optional)* where campaigns live (default `<repo>/campaigns`). |
| `OUTPUT_BASE` | *(optional)* scratch dir for live runs (default `campaigns/<name>/scratch`). |
| `KEEP_ARTIFACTS` | *(optional)* retention for completed runs' outputs: `none` (default) / `pass` / `all`. Kept dirs move to `ARTIFACTS_DIR/<run-id>`. |
| `ARTIFACTS_DIR` | *(optional)* where kept run dirs land, named by run id (default `campaigns/<name>/artifacts`). |

Any of these can also go in a campaign's `config.json` `env` map.

## Results

Every run is written to the campaign's `runs/<run-id>.json` and indexed in
`results.db` (SQLite). The columns are the same for every solver;
solver-specific metrics live in the `metrics` JSON column, every flag of the
run in `params`, and provenance/evidence in their own JSON columns.

```bash
DB=campaigns/<name>/results.db

# SQLite (recommended for agents)
sqlite3 $DB -header -column "SELECT * FROM runs WHERE status='pass' ORDER BY objective_J LIMIT 10"

# a solver-specific metric or parameter from the JSON columns
sqlite3 $DB "SELECT id, json_extract(metrics,'\$.<your_metric>'), json_extract(params,'\$.<your_flag>') FROM runs"

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
2. Write `adapters/<your-solver>.py` implementing the `contract.py` interface,
   using `adapters/toy.py` as the template and
   `examples/banana/simsopt_banana.py` for a multi-mode physics example;
   register it in `adapters/__init__.py` (one import + one `REGISTRY` entry);
   then create `campaigns/<name>/config.json` with `"adapter": "<your-solver>"`.

`run_experiment` can run a single subprocess or a multi-step pipeline (e.g. a
DESC chain: bumped surface → fixed-boundary equilibrium → coil optimization →
free-boundary equilibrium). Solver-specific metrics go to the `metrics` JSON
column.

## Project structure

```
contract.py                     ← harness↔adapter contract (ExperimentOutcome, helpers)
run.py                          ← generic experiment runner (solver-agnostic)
adapter.py                      ← adapter lookup + contract check
adapters/__init__.py            ← adapter registry (static imports)
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
