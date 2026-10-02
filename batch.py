"""Batch files: a planned set of experiments, expanded into concrete run specs.

A batch file is JSON written by the agent once per planning step:

    {
      "hypothesis": "what this batch should show",
      "lessons": {"applies": [...], "tests": [...], "rejects": [...]},
      "early_stop": {"same_crash": 3},
      "stages": [
        {"name": "screen",
         "base": {"mode": "fast", "problem": "rastrigin"},
         "runs": [{"step_size": 0.05}],                       # explicit specs
         "grid": {"dim": [2, 3]},                             # cartesian product
         "halton": {"n": 16, "ranges": {"step_size": [0.01, 1.0, "log"]}},
         "lhs": {"n": 8, "seed": 0, "ranges": {"maxiter": [100, 400, "int"]}},
         "replicates": 2},
        {"name": "confirm", "from": "screen",
         "select": {"top": 3, "by": "objective_J"},           # or "by": "front"
         "base": {"mode": "full"}, "carry": ["step_size", "dim"]}
      ]
    }

Spec keys are the adapter's argparse dests (as stored in `params`); values are
strings or numbers. A stage's specs are `base` merged with each point from
`runs`, `grid` and the samplers (a stage with none of them runs `base` once),
each repeated for `replicates` indices. A stage with `from` instead selects
passing runs of an earlier stage and carries the named params into `base`.
"""

from __future__ import annotations

import itertools
import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

import analysis

DEFAULT_SAME_CRASH_STOP = 3
_PRIMES = (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 53, 59, 61, 67, 71, 73, 79, 83, 89, 97)
_RANGE_TAGS = ("log", "int")
_STAGE_KEYS = {"name", "base", "runs", "grid", "halton", "lhs", "replicates", "from", "select", "carry"}
_LESSON_KEYS = ("applies", "tests", "rejects")
RESERVED_KEYS = ("campaign", "replicate", "batch_id", "parent_run_id")


class BatchError(Exception):
    """The batch file is malformed; the message lists every problem found."""


@dataclass(frozen=True)
class Stage:
    name: str
    base: Mapping[str, object]
    points: Sequence[Mapping[str, object]]
    replicates: int
    source: str | None = None
    top: int = 0
    rank_by: str = ""
    carry: Sequence[str] = ()


@dataclass(frozen=True)
class Batch:
    hypothesis: str
    lessons: Mapping[str, Sequence[str]]
    same_crash_stop: int
    stages: Sequence[Stage]
    raw: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class PlannedRun:
    """One concrete run of a stage: its spec (dest → value) and lineage."""

    stage: str
    spec: Mapping[str, object]
    replicate: int
    parent_run_id: str | None = None


# ---------------------------------------------------------------------------
# Low-discrepancy and Latin-hypercube points in [0, 1)^d
# ---------------------------------------------------------------------------

def _radical_inverse(index: int, base: int) -> float:
    result, fraction = 0.0, 1.0
    while index > 0:
        fraction /= base
        result += fraction * (index % base)
        index //= base
    return result


def halton_points(n: int, dims: int) -> list[list[float]]:
    """The first n points (indices 1..n) of the Halton sequence in `dims` dimensions."""
    return [[_radical_inverse(i, _PRIMES[d]) for d in range(dims)] for i in range(1, n + 1)]


def lhs_points(n: int, dims: int, seed: int) -> list[list[float]]:
    """A seeded Latin hypercube: each dimension's n strata used exactly once."""
    rng = random.Random(seed)
    columns = []
    for _ in range(dims):
        strata = list(range(n))
        rng.shuffle(strata)
        columns.append([(s + rng.random()) / n for s in strata])
    return [[columns[d][i] for d in range(dims)] for i in range(n)]


def _scale(u: float, lo: float, hi: float, tags: Sequence[str]) -> float | int:
    if "log" in tags:
        value = math.exp(math.log(lo) + u * (math.log(hi) - math.log(lo)))
    else:
        value = lo + u * (hi - lo)
    if "int" in tags and "log" in tags:
        return min(int(hi), max(int(lo), round(value)))
    if "int" in tags:
        return min(int(hi), int(math.floor(lo + u * (hi - lo + 1))))
    return value


# ---------------------------------------------------------------------------
# Parsing and validation
# ---------------------------------------------------------------------------

def _check_value(where: str, key: str, value: object, errors: list[str]) -> None:
    if key in RESERVED_KEYS:
        errors.append(f"{where}: '{key}' is set by the harness, not the batch file")
    elif isinstance(value, bool) or not isinstance(value, (str, int, float)):
        errors.append(f"{where}: '{key}' must be a string or number, got {value!r}")


def _parse_ranges(where: str, ranges: object, errors: list[str]) -> list[tuple[str, float, float, tuple[str, ...]]]:
    if not isinstance(ranges, dict) or not ranges:
        errors.append(f"{where}: 'ranges' must map params to [min, max, tags...]")
        return []
    parsed = []
    for key, spec in ranges.items():
        _check_value(where, key, 0, errors)
        ok = (
            isinstance(spec, list) and len(spec) >= 2
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in spec[:2])
            and all(t in _RANGE_TAGS for t in spec[2:])
        )
        if not ok or spec[0] >= spec[1]:
            errors.append(f"{where}: range for '{key}' must be [min, max] with min < max, plus optional tags {_RANGE_TAGS}")
            continue
        tags = tuple(spec[2:])
        if "log" in tags and spec[0] <= 0:
            errors.append(f"{where}: log range for '{key}' needs min > 0")
            continue
        parsed.append((key, float(spec[0]), float(spec[1]), tags))
    return parsed


def _sampled(where: str, kind: str, config: object, errors: list[str]) -> list[dict]:
    if not isinstance(config, dict) or not isinstance(config.get("n"), int) or config["n"] < 1:
        errors.append(f"{where}.{kind}: needs an integer 'n' >= 1 and 'ranges'")
        return []
    ranges = _parse_ranges(f"{where}.{kind}", config.get("ranges"), errors)
    if not ranges:
        return []
    if kind == "halton":
        if len(ranges) > len(_PRIMES):
            errors.append(f"{where}.halton: at most {len(_PRIMES)} dimensions")
            return []
        unit = halton_points(config["n"], len(ranges))
    else:
        seed = config.get("seed", 0)
        if not isinstance(seed, int):
            errors.append(f"{where}.lhs: 'seed' must be an integer")
            return []
        unit = lhs_points(config["n"], len(ranges), seed)
    return [
        {key: _scale(u, lo, hi, tags) for u, (key, lo, hi, tags) in zip(point, ranges)}
        for point in unit
    ]


def _points(where: str, raw: Mapping, errors: list[str]) -> list[dict]:
    points: list[dict] = []
    runs = raw.get("runs", [])
    if not isinstance(runs, list) or not all(isinstance(r, dict) for r in runs):
        errors.append(f"{where}: 'runs' must be a list of objects")
    else:
        for i, spec in enumerate(runs):
            for key, value in spec.items():
                _check_value(f"{where}.runs[{i}]", key, value, errors)
            points.append(dict(spec))
    grid = raw.get("grid")
    if grid is not None:
        if not isinstance(grid, dict) or not all(isinstance(v, list) and v for v in grid.values()):
            errors.append(f"{where}: 'grid' must map params to non-empty lists")
        else:
            keys = sorted(grid)
            for key in keys:
                for value in grid[key]:
                    _check_value(f"{where}.grid", key, value, errors)
            points.extend(dict(zip(keys, combo)) for combo in itertools.product(*(grid[k] for k in keys)))
    for kind in ("halton", "lhs"):
        if kind in raw:
            points.extend(_sampled(where, kind, raw[kind], errors))
    return points


def _stage(index: int, raw: object, earlier: Sequence[str], metric_goals: Mapping[str, str | None], errors: list[str]) -> Stage | None:
    where = f"stages[{index}]"
    if not isinstance(raw, dict):
        errors.append(f"{where}: must be an object")
        return None
    unknown = set(raw) - _STAGE_KEYS
    if unknown:
        errors.append(f"{where}: unknown keys {sorted(unknown)}")
    name = raw.get("name", f"stage{index}")
    if not isinstance(name, str) or name in earlier:
        errors.append(f"{where}: 'name' must be a unique string")
    base = raw.get("base", {})
    if not isinstance(base, dict):
        errors.append(f"{where}: 'base' must be an object")
        base = {}
    for key, value in base.items():
        _check_value(f"{where}.base", key, value, errors)
    replicates = raw.get("replicates", 1)
    if not isinstance(replicates, int) or replicates < 1:
        errors.append(f"{where}: 'replicates' must be an integer >= 1")
        replicates = 1

    if "from" not in raw:
        if "select" in raw or "carry" in raw:
            errors.append(f"{where}: 'select'/'carry' need 'from'")
        return Stage(name, base, _points(where, raw, errors) or [{}], replicates)

    source = raw["from"]
    if source not in earlier:
        errors.append(f"{where}: 'from' must name an earlier stage, got {source!r}")
    if any(k in raw for k in ("runs", "grid", "halton", "lhs")):
        errors.append(f"{where}: a 'from' stage takes its points from the selected runs")
    select = raw.get("select", {})
    top = select.get("top") if isinstance(select, dict) else None
    rank_by = select.get("by") if isinstance(select, dict) else None
    if not isinstance(top, int) or top < 1:
        errors.append(f"{where}: 'select.top' must be an integer >= 1")
        top = 1
    if rank_by != "front" and metric_goals.get(rank_by) is None:
        goals = sorted(m for m, g in metric_goals.items() if g)
        errors.append(f"{where}: 'select.by' must be \"front\" or a goal metric {goals}, got {rank_by!r}")
    carry = raw.get("carry", [])
    if not isinstance(carry, list) or not all(isinstance(c, str) for c in carry):
        errors.append(f"{where}: 'carry' must be a list of param names")
        carry = []
    return Stage(name, base, [], replicates, source, top, str(rank_by), tuple(carry))


def parse_batch(raw: object, metric_goals: Mapping[str, str | None]) -> Batch:
    """Validate a decoded batch file; raise BatchError listing every problem."""
    errors: list[str] = []
    if not isinstance(raw, dict):
        raise BatchError("batch file: top level must be a JSON object")
    hypothesis = raw.get("hypothesis")
    if not isinstance(hypothesis, str) or not hypothesis.strip():
        errors.append("'hypothesis' must be a non-empty string: what this batch should show")
    lessons = raw.get("lessons")
    if not isinstance(lessons, dict) or set(lessons) - set(_LESSON_KEYS) or not all(
        isinstance(v, list) and all(isinstance(t, str) for t in v) for v in lessons.values()
    ):
        errors.append(f"'lessons' must map {list(_LESSON_KEYS)} to lists of lesson titles (empty lists are fine)")
        lessons = {}
    early = raw.get("early_stop", {})
    same_crash = early.get("same_crash", DEFAULT_SAME_CRASH_STOP) if isinstance(early, dict) else None
    if not isinstance(same_crash, int) or same_crash < 0:
        errors.append("'early_stop.same_crash' must be an integer >= 0 (0 disables)")
        same_crash = DEFAULT_SAME_CRASH_STOP
    stages_raw = raw.get("stages")
    stages: list[Stage] = []
    if not isinstance(stages_raw, list) or not stages_raw:
        errors.append("'stages' must be a non-empty list")
    else:
        for i, stage_raw in enumerate(stages_raw):
            stage = _stage(i, stage_raw, [s.name for s in stages], metric_goals, errors)
            if stage is not None:
                stages.append(stage)
    if errors:
        raise BatchError("\n".join(f"- {e}" for e in errors))
    return Batch(hypothesis, lessons, same_crash, stages, raw)


def load_batch(path: Path, metric_goals: Mapping[str, str | None]) -> Batch:
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise BatchError(f"cannot read {path}: {e}") from e
    return parse_batch(raw, metric_goals)


# ---------------------------------------------------------------------------
# Expansion and selection
# ---------------------------------------------------------------------------

def plan_stage(stage: Stage) -> list[PlannedRun]:
    """The runs of a stage without a source: base + each point, per replicate."""
    return [
        PlannedRun(stage.name, {**stage.base, **point}, replicate)
        for point in stage.points
        for replicate in range(stage.replicates)
    ]


def select_runs(stage: Stage, results: Sequence[Mapping], metric_goals: Mapping[str, str | None]) -> list[Mapping]:
    """The source stage's passing runs this stage promotes (views, see analysis.py)."""
    passing = [r for r in results if r["status"] == "pass"]
    if stage.rank_by == "front":
        return analysis.pareto_front(passing, analysis.active_goals(passing, metric_goals))[: stage.top]
    metric, goal = stage.rank_by, metric_goals[stage.rank_by]
    ranked = sorted(
        (r for r in passing if isinstance(r["values"].get(metric), (int, float))),
        key=lambda r: r["values"][metric] * (-1 if goal == "max" else 1),
    )
    return ranked[: stage.top]


def plan_promotion(stage: Stage, selected: Sequence[Mapping], params_of: Mapping[str, Mapping]) -> list[PlannedRun]:
    """Runs of a `from` stage: base + carried params of each selected run, per replicate."""
    planned = []
    for run in selected:
        carried = {k: params_of[run["id"]][k] for k in stage.carry if k in params_of[run["id"]]}
        planned.extend(
            PlannedRun(stage.name, {**stage.base, **carried}, replicate, parent_run_id=run["id"])
            for replicate in range(stage.replicates)
        )
    return planned


def crash_key(result: Mapping) -> str | None:
    """What makes two crashes 'the same' for early stopping; None for non-crashes."""
    if result.get("status") != "crash":
        return None
    return result.get("crash_signature") or result.get("status_reason")


def should_stop(completed: Sequence[Mapping], same_crash: int) -> str | None:
    """Reason to stop launching runs: the last `same_crash` completed runs crashed alike."""
    if same_crash == 0 or len(completed) < same_crash:
        return None
    keys = {crash_key(r) for r in completed[-same_crash:]}
    if len(keys) == 1 and None not in keys:
        return f"last {same_crash} runs crashed the same way: {keys.pop()}"
    return None
