#!/usr/bin/env python3
"""Toy optimizer: (1+1) evolution strategy on a classic test function.

Stands in for a real solver so the harness can be exercised on any machine with
plain Python. It is deliberately shaped like one: run as a subprocess with CLI
flags, it writes a results.json, it is reproducible for a fixed --seed, and it
can fail on demand (--inject) so crash and timeout handling can be tested.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from pathlib import Path


def sphere(x: list[float]) -> float:
    return sum(xi * xi for xi in x)


def rosenbrock(x: list[float]) -> float:
    return sum(
        100.0 * (x[i + 1] - x[i] ** 2) ** 2 + (1.0 - x[i]) ** 2 for i in range(len(x) - 1)
    )


def rastrigin(x: list[float]) -> float:
    return 10.0 * len(x) + sum(xi * xi - 10.0 * math.cos(2.0 * math.pi * xi) for xi in x)


PROBLEMS = {"sphere": sphere, "rosenbrock": rosenbrock, "rastrigin": rastrigin}
OPTIMUM = {"sphere": 0.0, "rosenbrock": 1.0, "rastrigin": 0.0}
CONVERGED_BELOW = 1e-6


def optimize(problem: str, dim: int, maxiter: int, step_size: float, seed: int, noise: float) -> dict:
    """Minimize `problem` from a seeded random start; noise perturbs only what the search sees."""
    f = PROBLEMS[problem]
    rng = random.Random(seed)
    x = [rng.uniform(-2.0, 2.0) for _ in range(dim)]
    seen = f(x) + rng.gauss(0.0, noise)
    step = step_size
    for _ in range(maxiter):
        candidate = [xi + rng.gauss(0.0, step) for xi in x]
        candidate_seen = f(candidate) + rng.gauss(0.0, noise)
        if candidate_seen < seen:
            x, seen = candidate, candidate_seen
            step *= 1.5
        else:
            step *= 0.95
        step = max(step, 1e-12)
    objective = f(x)
    return {
        "objective": objective,
        "distance_to_optimum": math.dist(x, [OPTIMUM[problem]] * dim),
        "evaluations": maxiter + 1,
        "converged": objective < CONVERGED_BELOW,
        "final_step": step,
        "x": x,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--problem", choices=sorted(PROBLEMS), required=True)
    p.add_argument("--dim", type=int, required=True)
    p.add_argument("--maxiter", type=int, required=True)
    p.add_argument("--step-size", type=float, required=True)
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--noise", type=float, required=True)
    p.add_argument("--inject", choices=("none", "crash", "hang", "nan"), required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()

    if args.inject == "crash":
        print("injected crash", file=sys.stderr)
        sys.exit(3)
    if args.inject == "hang":
        time.sleep(3600)

    result = optimize(args.problem, args.dim, args.maxiter, args.step_size, args.seed, args.noise)
    if args.inject == "nan":
        result["objective"] = math.nan
    args.output.write_text(json.dumps(result))


if __name__ == "__main__":
    main()
