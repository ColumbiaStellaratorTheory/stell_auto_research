---
name: research
description: Start or resume an autoresearch optimization campaign and run its experiment loop — brief, plan a batch, dry-run, run, record lessons, repeat — until the campaign's stopping criteria or budget are met. Use when the user says "/research <campaign>", "run research", "start the campaign", "resume the campaign", "continue the optimization loop", or points at a campaigns/<name>/program.md. Run /setup-harness first if no campaign exists.
---

# research

Drive one campaign under `campaigns/<name>/`. This file is the research method
shared by every campaign; the campaign's `program.md` holds only what is
specific to it (mission, goals and stopping criteria, rules no code can check).

**You drive the search.** Strategy, parameters, which mode and target to spend
runs on, when to pivot, branch, kill or run a side experiment — all yours.
Everything here is facts and tools, not a preset queue. The binding limits are
few and mostly enforced in code: the adapter's metric directions and
validation, `config.json` `fixed` / `bounds` / `budget`, plus the hard rules in
`program.md`.

## Start or resume

A session is restartable from files alone; never rely on chat history.

1. **Pick the campaign.** Use the name given (`/research <campaign>`). With
   none given, `python run.py status` lists the campaigns; use the only one,
   or ask. Pass `--campaign <name>` on every command below.
2. **Read**, in order:
   - `campaigns/<name>/program.md` — mission, goals, stopping criteria, hard rules.
   - `campaigns/<name>/LESSONS.md` — this campaign's memory.
   - `lessons/<adapter>.md` — solver lessons from every campaign on this adapter
     (`<adapter>` is the adapter's `NAME`, shown in the brief's first line;
     skip if the file does not exist).
   - `python run.py brief --campaign <name>` — what has been tried, the Pareto
     fronts, recent runs, crash causes, noise floor, latest lesson titles,
     solver-lesson titles, active `fixed` / `bounds`, budget used / remaining,
     and the run slots and batch sizing.
   - `python run.py --campaign <name> --help` — the modes and every parameter
     with its default. Read the adapter (`adapters/<adapter>.py`) when a mode,
     flag or metric is unclear.
3. **Resuming:** the newest files in `campaigns/<name>/batches/` are the last
   batches (hypothesis, cited lessons, run ids). Optionally replay one known
   run (`python run.py replay <run-id> --campaign <name>`) to detect
   environment drift: it exits 2 on a mismatch and says whether the solver
   changed.
4. Start the loop.

## The loop

A default shape, not a prescription — restructure it when evidence says a
different use of the next run is better.

1. **Brief**: `python run.py brief --campaign <name>`.
2. **Think**: what is the objective rewarding? Why did that config fail?
   Which lessons (campaign and solver) does the next experiment apply, test
   or reject? Name them.
3. **Plan**: write a batch file (format at the top of `batch.py`): a
   hypothesis, the lessons it applies / tests / rejects, and stages —
   explicit `runs`, a `grid`, `halton` / `lhs` samples, `replicates`, and
   promotion (`from` + `select` + `carry`) of the best runs to a costlier
   stage. Spec keys are the adapter's argparse dests. The batch record in
   `batches/<id>.json` keeps a full copy of the file.
4. **Dry-run**: `python run.py batch plan.json --campaign <name> --dry-run`
   validates every spec against the adapter's flags and the campaign's
   `fixed` / `bounds`, shows how many runs are new vs already recorded, and
   the remaining budget. Fix the plan until it is clean.
5. **Run**: `python run.py batch plan.json --campaign <name>` in the
   background; wait for it to finish — do not poll. Exact repeats are reused,
   not re-run; a spec another agent is running is waited for and reused;
   launching stops after `early_stop.same_crash` identical crashes (default
   3, 0 disables) or once the run or hours budget is spent. SIGTERM cancels it and
   each in-flight run records itself as `cancelled`. A single experiment is
   `python run.py --campaign <name> [--mode M] [params]`.
6. **Evaluate**: read the batch summary (per-stage counts, front members,
   crash causes; details in `batches/<id>.json`, children's stderr in
   `batches/<id>.log`), then `brief` / `query` for anything deeper.
   `on_front` marks runs that joined their Pareto front.
7. **Record**: append campaign lessons, then distill (below).
8. **Stop or repeat** (see Stopping).

## Running experiments

- **`run.py` is the recording boundary.** A run that bypasses it never
  happened: no record, no lesson, no frontier credit. Reading solver source
  is encouraged when a metric or crash is ambiguous.
- One run prints one JSON line (set fields, metrics, `on_front`,
  `crash_signature`) and is written to `campaigns/<name>/runs/<run-id>.json`
  and indexed in `results.db`.
- **No accidental repeats.** An identical pass/fail run prints
  `{"duplicate_of": "<id>", ...}` instead of running. `--replicate N` draws
  another sample of the same spec (a new derived seed). Crashes can be
  retried as-is.
- **Constraints and budget are enforced, not advisory.** A run or batch that
  breaks `fixed` / `bounds`, or exceeds the remaining `budget`, is refused
  before anything executes and nothing is recorded. Do not work around a
  refusal. If a limit looks wrong, tell the user and let them change
  `config.json`. `replay` is exempt (it re-checks an existing record), but
  its run counts toward the budget.
- A crashed run's log is kept as evidence: the record's
  `evidence.log.sha256` names the file under `campaigns/<name>/blobs/`.
  When `AUTORESEARCH_KEEP_ARTIFACTS` keeps run dirs, they sit under
  `AUTORESEARCH_ARTIFACTS_DIR/<run-id>` (default
  `campaigns/<name>/artifacts/`). Cite champions by run id so they stay
  reproducible.

## Querying results

Start from `brief` rather than re-deriving its aggregates. For anything else,
one read-only SQL statement:

```bash
python run.py query --campaign <name> "SELECT id, target, objective_J FROM results WHERE status='pass' ORDER BY objective_J LIMIT 10"
```

Output is tab-separated, capped at 50 rows (`--limit N`), with long cells
truncated; it needs no `sqlite3` CLI and cannot modify the database. Schema:
`python run.py query --campaign <name> "PRAGMA table_info(results)"`.

- The `results` view is the `runs` columns plus one column per metric the
  adapter declares in `METRICS`. The `runs` table holds the same rows with
  metrics as JSON, plus `params` (every flag of the run;
  `json_extract(params, '$.<dest>')`), `provenance` and `evidence`.
- `adapter` is the solver family; `target` is the value of the adapter's
  target flag; `parent_run_id` links a run to the one it built on;
  `batch_id` to its batch.
- Replicates of one spec differ only in `replicate` and the derived `seed`;
  their spread (the brief's noise-floor section) is the yardstick for
  comparing configs.
- Aggregates (`COUNT`, `GROUP BY`, `MIN`, `MAX`) keep context lean; an
  unbounded `SELECT *` floods it.

## Evaluation

- Metric directions are the adapter's `METRICS` goals; the brief's `goals:`
  line shows them (↓ lower is better, ↑ higher), and the Pareto fronts are
  computed over them. Metrics without a goal are recorded only.
- The adapter marks a run `fail` with a reason, or sets `validated`, when a
  solver invariant is violated. **Only `validated = 'pass'` results are
  confirmed** where the adapter validates; scalar metrics can lie until
  independent validation agrees.
- Success and the ranking between conflicting goals are defined by
  `program.md`'s goals.

## Lessons

Three levels of memory, each with one home:

| Level | File | Who changes it |
|---|---|---|
| Campaign | `campaigns/<name>/LESSONS.md` | you, after any finding |
| Solver | `lessons/<adapter>.md` (repo root, tracked) | you, by promotion only |
| Method | this skill | the user only |

Both lesson files use the entry format at the top of `templates/LESSONS.md`
(kind, scope, claim, evidence, action, status; solver entries add `source:`).
`brief` lists the latest titles of each.

- Read both before your first launch of a session.
- **Append** a campaign lesson whenever a finding generalizes beyond one run:
  a recipe that works, a dead region, a crash with a known cause, a metric
  that does not predict what you assumed.
- Cite run ids or a reproducing `run.py query` as evidence. Record
  negatives — they save more compute than champions.
- **Append-only.** Never edit or delete an entry; a correction is a new entry
  of kind `correction` that names the one it corrects.
- A crash that changes your next experiment was a successful experiment.

### Distill (after recording)

1. **Promote to solver lessons.** A campaign lesson whose status is
   `confirmed` and whose scope does not depend on this campaign's goal (it is
   about the solver: a crash cause, a stable/unstable parameter region, a
   metric caveat) is appended to `lessons/<adapter>.md` with
   `source: <campaign> · <campaign-lesson title> · <run ids>`. It is
   `confirmed` at solver level only when it holds in at least two campaigns
   (cite both sources); otherwise `hypothesis`. If an earlier solver entry
   already states it, append a `correction` / confirming entry instead of a
   duplicate. Create the file from `templates/LESSONS.md` if it is missing.
2. **Promote limits to code — with the user.** A confirmed lesson that states
   a parameter limit (e.g. "maxiter above 5000 never converges") may become a
   `config.json` `bounds` entry. Ask the user first; do not edit
   `config.json` on your own.

## Machine and parallelism

`python run.py status` and the last lines of `brief` show the run slots, each
mode's measured run time, memory and threads, how many runs fit at once, and
the batch size that fills one planning interval (`plan_minutes`). Size batches
from those numbers. Every run from every campaign takes one machine-wide run
slot, so the machine is never oversubscribed; `--parallel N` on a batch is
capped by the campaign's `max_parallel` (its `local.json`) and the slots. Changing machine
settings (`run.py status --max-parallel …`) affects every campaign — leave it
to the user.

## Stopping

- Stop when `program.md`'s stopping criteria are met, when the budget is
  exhausted (`run.py` refuses new runs), or when the user interrupts.
  Otherwise keep going: if one mode, target or parameter family plateaus,
  switch. The brief's "runs since the newest front member" is the plateau
  signal.
- Before stopping, append any unrecorded lessons and distill, then give the
  user a short report: best runs by id with their metrics, what was learned,
  budget used, and what you would run next.
