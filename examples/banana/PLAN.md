# General Stellarator Harness — Plan

## Goal

Replace the current banana-coil-specific harness (run_one.py 1236L, lab.py 2100L, registry/ 539L, 6 program_hbt*.md files, 4 analysis scripts) with a general stellarator optimization harness following Karpathy's pattern: three files that matter, everything else derived.

## Target architecture

```
program_optimization.md  ← agent instructions + DB schema + goals (per coil type)
run.py              ← run solver, auto-Poincare, write to both JSONL + DB
results.jsonl       ← append-only log (source of truth, portable, human-readable)
results.db          ← derived query index (agent queries via sqlite3 CLI)
```

No lab.py. No registry/. No pre-built analysis scripts. The agent (Opus 4.6) writes its own SQL.

### Dual storage

run.py writes every result to both:
- **results.jsonl** — append one JSON line. Source of truth. Durable, human-readable, `cat | jq | grep` friendly. Physicists who prefer flat files use this.
- **results.db** — INSERT one row. Derived index. Agent queries with `sqlite3`. Poincare UPDATE lives here only.

If the DB gets corrupted, rebuild from JSONL. JSONL never stores Poincare validation — that's DB-only mutable state.

## Schema

One `schema.sql`, applied once on init. No migration system — if a column is needed later, `ALTER TABLE ADD COLUMN` by hand.

Columns derived from real stellarator solver output fields. Nothing invented.

One table. One row per experiment (or per sub-step, grouped by `experiment_group`). Params and solver-specific metrics as JSON columns on the same row.

See `schema.sql` (SSOT). Summary: 28 columns — 5 identity, 8 outcome, 13 physics outputs, 2 JSON (metrics + params).

### No scoring

No pre-computed score. The agent has all raw metrics and writes its own `ORDER BY` for whatever question it's asking. Pre-computed scores are lossy — they hide which metric is actually bad.

## run.py design

Lives at top level (like Karpathy's train.py), not in scripts/.

### Responsibilities

1. Accept `--solver {stage2|single-stage}` + solver-specific params (banana only for now)
2. Resolve equilibrium file from `--equilibrium` key
3. Build solver CLI from params
4. Run solver in isolated tmpdir with `--timeout`
5. Parse solver's results.json output
6. Auto-run Poincare for single-stage runs that pass basic checks:
   - not self_intersecting
   - field_error < 0.1 (configurable)
   - Parses phi hit count uniformity from Poincare output
   - UPDATE runs SET validated = 'pass'/'fail' (DB only — JSONL stays append-only)
7. Append JSON line to results.jsonl (source of truth)
8. INSERT into results.db (query index)
9. Print JSON summary to stdout

### DB init

On startup, run.py checks if results.db exists. If not, reads schema.sql and applies it. One `if not exists` check, not a migration system.

### Solver dispatch

Simple dict, not a plugin system:

See `run.py` SOLVERS dict (SSOT). Each coil type entry maps solver names to script paths + default Python/root.

### Poincare integration

Poincare runs automatically after single-stage solver completes, gated on:
- solver != "stage2"
- self_intersecting == false
- field_error is not None and < threshold

Uses the existing poincare_surfaces.py script. Parses survival_fraction from output. Stores result in `runs.validated` (DB only — JSONL stays append-only).

### Params handling

All solver params passed as CLI flags (same as current run_one.py). Stored as JSON in the `params` column. Agent queries with `json_extract(params, '$.cc_weight')`.

## program.md design

One file per coil type. Each answers 7 questions:

1. **What can I change?** — parameter list with defaults and ranges
2. **How do I run?** — exact command template
3. **How do I read results?** — DB schema + example queries
4. **How do I compare?** — metric names and directions (lower/higher = better)
5. **What are the constraints?** — floors/ceilings the solver enforces
6. **What equilibria exist?** — available configs
7. **When do I stop?** — never

Includes query discipline:
- "Always check before running: `sqlite3 results.db \"SELECT COUNT(*) ...\"`"
- "Always aggregate first. Never SELECT * without LIMIT 20."
- "Only validated='pass' results count toward the frontier."

## Execution plan

### Phase 1: Build the new harness (this branch)

1. Write `schema.sql` — the schema above, verbatim
2. Write `run.py` — extract from current run_one.py:
   - DB init (apply schema.sql if results.db missing)
   - Solver dispatch (banana only)
   - Equilibrium resolution
   - Result parsing
   - Dual write: JSONL append + DB insert
   - Poincare integration
   - JSON stdout contract
3. Write `program_optimization.md` — banana coils version, incorporating:
   - DB schema
   - Query examples
   - Goals/constraints from current program_hbt.md
   - Available equilibria

### Phase 2: Validate

4. Run the agent with the new harness on banana coils
5. Confirm it can:
   - Query the DB effectively
   - Pick good experiments
   - Interpret Poincare results
   - Not re-run existing experiments

### Phase 3: Extend (when needed, not now)

6. Add umbilic coil support — new program_umbilic.md, new solver entry in run.py
7. Add dipole support — same pattern

## What gets deleted

```
scripts/lab.py                      2100 lines → killed (agent writes SQL)
scripts/build_results_annotated.py   371 lines → killed (agent can derive)
scripts/plot_campaign_analysis.py    679 lines → killed (agent can plot)
scripts/render_analysis_plots.py     676 lines → killed
scripts/compute_metrics.py           172 lines → killed (metrics in DB)
registry/                            539 lines → killed (results.db replaces)
program_hbt.md                                 → replaced by program_optimization.md
program_hbt_alm.md                             → folded into program_optimization.md
program_hbt_aws.md                             → separate concern (infra)
program_hbt_live.md                            → folded into program_optimization.md
program_hbt_topology.md                        → folded into program_optimization.md
program_hbt_topology_surrogate.md              → folded into program_optimization.md
```

Total deleted: ~4500 lines of domain-coupled code.

## What stays

```
run.py (new, ~670 lines)            ← replaces scripts/run_one.py + scripts/_solver_launcher.py
schema.sql (new, ~40 lines)         ← one table, applied once
program_optimization.md (new)         ← replaces 6 program_hbt*.md files
results.jsonl                       ← append-only source of truth (created by run.py)
results.db                          ← derived query index (created by run.py)
scripts/aws_run.py                  ← stays (infra, not harness)
scripts/generate_equilibria.py      ← stays (run once, not part of loop)
```

## Risks

1. **Agent SQL quality.** If the agent writes bad queries (SELECT * with no LIMIT), it burns context. Mitigated by prompt discipline in program_optimization.md.

2. **Poincare auto-run failures.** Poincare script may crash on edge cases. run.py catches exceptions, sets validated=null, logs error, continues. Never blocks the loop.

3. **JSON params performance.** json_extract() is slower than column lookup. At typical run counts, irrelevant. At 10K+, promote hot params to columns via ALTER TABLE.

4. **Loss of lab.py suggest logic.** The 4-strategy search (exploit/transfer/boundary/explore) was valuable. Now it's the agent's job. If the agent tunnel-visions, the prompt needs strengthening. Monitor in Phase 2.

5. **Dual write divergence.** If run.py crashes between JSONL append and DB insert, they diverge. Mitigated: write JSONL first (crash-safe append), then DB.

## Success criteria

- Agent can run a banana coil experiment end-to-end with `python run.py --solver single-stage ...`
- Agent can query results.db and make informed decisions about next experiments
- Physicists can `cat results.jsonl | jq` for the same data
- Agent never re-runs an experiment that's already in the DB
- Poincare validation runs automatically on viable single-stage results
- Total harness code < 1000 lines (run.py + schema.sql + program_optimization.md)
