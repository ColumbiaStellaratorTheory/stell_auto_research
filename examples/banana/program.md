# Stellarator Banana Coil Optimization

You are an autonomous researcher optimizing stellarator banana coil configurations. Your job: run experiments, analyze results, push the frontier. Loop forever.

## Setup

1. Read this file completely.
2. Query the database to understand what has been tried.
3. Start the loop.

## Two Solvers

**Stage 2** optimizes coil geometry to minimize field error (~30s). **Single-stage** validates quasi-symmetric fields with Boozer surfaces (~10-30min). A coil that looks great in Stage 2 may fail in single-stage. Use Stage 2 for fast exploration, single-stage for physics validation.

### Stage 2: Pure Field Accuracy
```
J = SQUARED_FLUX_WEIGHT * SquaredFlux
  + LENGTH_WEIGHT * QuadraticPenalty(CurveLength, LENGTH_TARGET)
  + CC_WEIGHT * CurveCurveDistance(CC_THRESHOLD)
  + CURVATURE_WEIGHT * LpCurveCurvature(p=CURVATURE_P_NORM, threshold=CURVATURE_THRESHOLD)
```

### Single-Stage: Full Quasi-Symmetry
```
J = NonQSRatio
  + RES_WEIGHT * BoozerResidual
  + IOTAS_WEIGHT * QuadraticPenalty(Iota, IOTA_TARGET)
  + LENGTH_WEIGHT * QuadraticPenalty(CurveLength)
  + CC_WEIGHT * CurveCurveDistance(CC_DIST)
  + CS_WEIGHT * CurveSurfaceDistance(CS_DIST)
  + SURF_DIST_WEIGHT * SurfaceSurfaceDistance(SS_DIST)
  + CURVATURE_WEIGHT * LpCurveCurvature(p=2, threshold=CURVATURE_THRESHOLD)
```

Single-stage needs a Stage 2 `biot_savart_opt.json` as starting coil. `run.py` auto-resolves the best matching seed from `stage2_seeds/`.

## Running an Experiment

**Always use `run.py`. Never call the solver directly.**

```bash
# Stage 2 (default)
python run.py --campaign banana --equilibrium nfp5_iota17 --cc-weight 100 --curvature-threshold 40

# Single-stage
python run.py --campaign banana --solver single-stage --equilibrium nfp5_iota20 \
    --iota-target 0.20 --vol-target 0.10 --mpol 8 --timeout 1200
```

Output: one JSON line to stdout. The run is recorded in `campaigns/banana/runs/` and indexed in `campaigns/banana/results.db`. An identical earlier pass/fail run is not repeated (`--replicate N` draws another seed); `python run.py replay <id>` re-runs one and compares.

### Parameters

**Both solvers:**
`--cc-weight` (100), `--curvature-weight` (0.1), `--curvature-threshold` (40), `--banana-surf-radius` (0.22), `--major-radius` (0.915), `--toroidal-flux` (0.215), `--order` (2), `--maxiter` (400), `--nphi` (255), `--ntheta` (64)

**Stage 2 only:**
`--cc-threshold` (0.05), `--length-weight` (1.0), `--length-target` (1.75), `--squared-flux-weight` (1.0), `--curvature-p-norm` (4), `--num-quadpoints` (128), `--basin-hops` (0), `--basin-stepsize` (0.01)

**Single-stage only:**
`--iota-target` (0.15), `--vol-target` (0.10), `--mpol` (8), `--ntor` (6), `--res-weight` (1000), `--iotas-weight` (100), `--cc-dist` (0.05), `--cs-weight` (1), `--cs-dist` (0.02), `--surf-dist-weight` (1000), `--ss-dist` (0.04), `--ss-length-weight` (1.0), `--num-tf-coils` (20), `--maxcor` (300), `--boozer-stage` (initial), `--constraint-weight` (1.0)

**Execution:** `--timeout` (600, use 1200+ for single-stage), `--omp-threads` (10)

Every parameter is yours to set. Defaults are starting points, not constraints.

## Constraint Floors

These are enforced by the solver. Do not go below:
- `curvature_threshold` >= 40
- `cc_threshold` / `cc_dist` >= 0.05m
- `length_target` >= 1.75m
- `cs_dist` >= 0.02m (single-stage)
- `ss_dist` >= 0.04m (single-stage)

## Equilibria

126 equilibrium files available across NFP=5/10/15, iota=0.10-0.50.

Format: `--equilibrium nfp{N}_iota{XX}` (e.g., `nfp5_iota17`, `nfp10_iota25`).

Legacy aliases `iota15`-`iota30` still work (NFP=5 only).

Match `--iota-target` to the equilibrium's iota value.

## Querying Results

Start each iteration with `python run.py brief --campaign banana`: counts per mode/equilibrium, Pareto fronts over field_error / qs_error / boozer_residual / max_curvature, recent runs, crash causes, replicate spread.

For anything else, run read-only SQL (tab-separated, capped at 50 rows):

```bash
python run.py query --campaign banana "YOUR QUERY"
```

### Schema

```
runs(
  id, coil_type, solver, equilibrium, experiment_group,
  spec_hash, replicate, seed, parent_run_id, replay_of,
  status, status_reason, crash_signature, validated,
  iterations, elapsed, created_at, optimizer_success, termination_message,
  field_error, qs_error, boozer_residual,
  iota_actual, volume_actual,
  max_curvature,
  coil_length, coil_coil_dist, coil_surface_dist, surface_vessel_dist,
  max_force, self_intersecting, objective_J,
  metrics,  -- JSON: solver-specific metrics, e.g. json_extract(metrics, '$.lead_end_curvature')
  params,   -- JSON: every flag of the run, e.g. json_extract(params, '$.cc_weight')
  provenance, evidence  -- JSON: what the run used / kept files
)
```

### Query Discipline

- **Before every run**, check it hasn't been done:
  ```sql
  SELECT COUNT(*) FROM runs
  WHERE equilibrium = 'nfp5_iota17' AND solver = 'stage2'
  AND json_extract(params, '$.cc_weight') = 100;
  ```
- **Always aggregate first.** Use COUNT, GROUP BY, MIN, MAX. Never SELECT * without LIMIT 20.
- **Only `validated = 'pass'` results are confirmed.** Metrics can lie. Until Poincare validation confirms good flux surfaces, treat results with caution.

### Useful Queries

```sql
-- What's been explored?
SELECT equilibrium, solver, COUNT(*) as n, SUM(status='pass') as passes,
       MIN(field_error) as best_fe
FROM runs GROUP BY equilibrium, solver ORDER BY best_fe;

-- Best single-stage results
SELECT equilibrium, field_error, qs_error, iota_actual, volume_actual, max_curvature
FROM runs WHERE solver = 'single-stage' AND status = 'pass'
ORDER BY field_error LIMIT 10;

-- Crash patterns
SELECT status_reason, COUNT(*) as n FROM runs
WHERE status IN ('crash', 'fail') GROUP BY status_reason ORDER BY n DESC;

-- What params work best?
SELECT json_extract(params, '$.cc_weight') as ccw,
       json_extract(params, '$.curvature_weight') as cw,
       field_error, max_curvature
FROM runs WHERE solver = 'stage2' AND status = 'pass'
ORDER BY field_error LIMIT 10;

-- Coverage gaps
SELECT equilibrium, COUNT(*) as n FROM runs
WHERE solver = 'single-stage' GROUP BY equilibrium ORDER BY n;
```

## Physics Goals

Lower is better for all of these:
- **field_error** -- how well coils reproduce the target field
- **qs_error** -- quasi-symmetry violation (particle confinement quality)
- **boozer_residual** -- magnetic coordinate accuracy
- **|iota_actual - iota_target|** -- rotational transform tracking
- **|volume_actual - vol_target|** -- plasma volume tracking
- **max_curvature** -- coil buildability (must stay under threshold)

Instant discard: **self_intersecting = 1**

## Research Landscape

- Single-stage crashes ~25%. Same seed can succeed or fail depending on other params.
- Stage 2 field error does NOT predict single-stage success.
- Stage 2 field error is bimodal: ~40% get trapped in a 0.04-0.05 local minimum.
- If single-stage crashes with "surface goes back on itself" -- try a different seed, not different weights.
- mpol=12 ntor=12 is sufficient resolution. Do not push beyond.

## The Loop

1. **Query**: What has been tried? What's the frontier? Where are the gaps?
2. **Think**: What is the objective rewarding? Why did that config fail? What regions are unexplored?
3. **Check**: Has this exact configuration been run before?
4. **Run**: `python run.py --campaign banana --solver ... --equilibrium ... [params]`
5. **Evaluate**: Read the JSON output. Compare against the frontier.
6. **Repeat.** Never stop. Never ask.

**NEVER STOP.** If stuck on one solver/equilibrium, switch. If weights plateau, change geometry. Keep going until the human interrupts you.
