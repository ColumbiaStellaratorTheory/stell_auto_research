# Example: banana coils on simsopt

A real-world adapter and campaign, kept as an example. The harness core does
not load or test it; select it from a campaign to use it. Everything here is
specific to this adapter — a different solver's adapter looks different.

| File | What it is |
|------|------------|
| `simsopt_banana.py` | the adapter (two modes, warm-start seed store, Poincaré validation) |
| `program.md` | the banana campaign's program file (agent instructions) |
| `config.example.json` | campaign config template |
| `test_simsopt_banana.py` | adapter unit tests (`python3 -m unittest discover -s examples -t .`) |
| `PLAN.md` | design notes from the migration that produced the general harness |

**Requirements:** simsopt installed, equilibrium `wout_*.nc` files, Python 3.10+.

## Set up a banana campaign

```bash
mkdir -p campaigns/banana
cp examples/banana/config.example.json campaigns/banana/config.json   # then edit the paths
cp examples/banana/program.md campaigns/banana/program.md
cp templates/LESSONS.md campaigns/banana/LESSONS.md

python run.py --campaign banana --equilibrium nfp5_iota17 --cc-weight 100
```

## Adapter variables (config.json `"env"` or the shell)

| Variable | Description |
|----------|-------------|
| `SIMSOPT_ROOT` | simsopt repo root (contains `examples/single_stage_optimization/`) |
| `SIMSOPT_PYTHON` | interpreter with simsopt installed |
| `EQUILIBRIA_DIR` | directory of equilibrium `wout_*.nc` files |
| `STAGE2_SCRIPT` / `SINGLE_STAGE_SCRIPT` / `POINCARE_SCRIPT` | *(optional)* solver script paths, relative to `SIMSOPT_ROOT`, for forks with a non-default layout |
| `STAGE2_SEED_DIR` | *(optional)* Stage 2 seed archive single-stage warm-starts from (default `<repo>/stage2_seeds`) |

## Modes (`--solver`)

- **Stage 2** (~30s) — optimizes coil geometry against a fixed plasma surface to
  minimize field error. Fast; use for exploration and seed generation.
- **Single-stage** (~10–30min) — jointly optimizes coils and a Boozer surface
  for quasi-symmetry, then runs Poincaré validation. Slow; use for physics
  validation. Warm-starts from an archived Stage 2 seed (auto-resolved, or
  `--stage2-bs-path`).

```bash
python run.py --campaign banana --solver single-stage --equilibrium nfp5_iota20 \
    --iota-target 0.20 --vol-target 0.10 --mpol 8 --timeout 1200
```
