"""Derived views over a campaign's runs: crash signatures, Pareto fronts, the brief.

Pure functions over *run views* — dicts with the run's identity fields plus
`values` (every non-null metric) and `spec_base` (hash of the spec without seed
and execution flags, shared by replicates). `run.py` builds the views from
results.db. Nothing here decides anything for the agent; it summarizes.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Iterable, Mapping, Sequence

SIGNATURE_TAIL_LINES = 200
SIGNATURE_MAX_CHARS = 200
_EXCEPTION_LINE = re.compile(r"^[A-Za-z_][\w.]*(?:Error|Exception|Exit)\b")
_PATH = re.compile(r"(?:[A-Za-z]:)?[/\\][^\s:'\"()]+")
_HEX = re.compile(r"0x[0-9a-fA-F]+")
_NUMBER = re.compile(r"(?<![\w.])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?(?![\w.])")
_LESSON_HEADING = re.compile(r"^## (\d{4}-\d{2}-\d{2}) — (.+)$")

# Section caps keep the brief a fixed size however large the campaign grows.
MAX_GROUPS = 10
MAX_FRONT_GROUPS = 3
MAX_FRONT_ROWS = 5
MAX_RECENT = 5
MAX_CRASH_KINDS = 5
MAX_NOISE_GROUPS = 3
MAX_LESSON_TITLES = 3


# ---------------------------------------------------------------------------
# Crash signatures
# ---------------------------------------------------------------------------

def _normalize(line: str) -> str:
    line = _PATH.sub("<path>", line)
    line = _HEX.sub("<hex>", line)
    line = _NUMBER.sub("N", line)
    return line[:SIGNATURE_MAX_CHARS]


def crash_signature(log_text: str) -> str | None:
    """The line that best names why a run died, with paths and numbers normalized.

    The last exception line (`SomeError: ...`) in the log's tail wins; otherwise
    the last non-empty line. Normalizing makes the same failure group together
    across runs.
    """
    lines = [ln.strip() for ln in log_text.splitlines()[-SIGNATURE_TAIL_LINES:] if ln.strip()]
    if not lines:
        return None
    for line in reversed(lines):
        if _EXCEPTION_LINE.match(line):
            return _normalize(line)
    return _normalize(lines[-1])


# ---------------------------------------------------------------------------
# Pareto fronts
# ---------------------------------------------------------------------------

Goals = Sequence[tuple[str, str]]


def active_goals(runs: Iterable[Mapping], metric_goals: Mapping[str, str | None]) -> list[tuple[str, str]]:
    """The goal metrics (name, "min"/"max") that at least one of `runs` reports."""
    present = {k for r in runs for k in r["values"]}
    return [(m, g) for m, g in metric_goals.items() if g is not None and m in present]


def _dominates(a: Mapping, b: Mapping, goals: Goals) -> bool:
    better = False
    for metric, goal in goals:
        x, y = a["values"][metric], b["values"][metric]
        if goal == "max":
            x, y = -x, -y
        if x > y:
            return False
        better = better or x < y
    return better


def pareto_front(runs: Sequence[Mapping], goals: Goals) -> list[Mapping]:
    """Passing runs that report every goal and that no other such run dominates.

    Sorted by the first goal, best first.
    """
    if not goals:
        return []
    candidates = [
        r for r in runs
        if r["status"] == "pass" and all(isinstance(r["values"].get(m), (int, float)) for m, _ in goals)
    ]
    front = [r for r in candidates if not any(_dominates(o, r, goals) for o in candidates if o is not r)]
    first, goal = goals[0]
    return sorted(front, key=lambda r: r["values"][first] * (-1 if goal == "max" else 1))


def group_key(run: Mapping) -> str:
    return f"{run['solver']}/{run['equilibrium']}"


def by_group(runs: Iterable[Mapping]) -> dict[str, list[Mapping]]:
    groups: dict[str, list[Mapping]] = {}
    for r in runs:
        groups.setdefault(group_key(r), []).append(r)
    return groups


def front_ids(runs: Sequence[Mapping], metric_goals: Mapping[str, str | None]) -> set[str]:
    """Ids of every run on its (mode, target) group's Pareto front."""
    ids: set[str] = set()
    for group in by_group(runs).values():
        ids.update(r["id"] for r in pareto_front(group, active_goals(group, metric_goals)))
    return ids


def runs_since_front_change(runs: Sequence[Mapping], metric_goals: Mapping[str, str | None]) -> int | None:
    """How many runs were recorded after the newest run on any front (None: no front yet)."""
    ids = front_ids(runs, metric_goals)
    if not ids:
        return None
    newest = max(r["created_at"] for r in runs if r["id"] in ids)
    return sum(1 for r in runs if r["created_at"] > newest)


# ---------------------------------------------------------------------------
# Lessons
# ---------------------------------------------------------------------------

def lesson_titles(text: str) -> list[str]:
    """Titles of dated lesson entries (`## YYYY-MM-DD — title`), oldest first."""
    return [f"{m.group(1)} {m.group(2)}" for m in map(_LESSON_HEADING.match, text.splitlines()) if m]


def lesson_entries(text: str) -> list[str]:
    """Each dated lesson entry as text: its heading line through the line before the next one."""
    entries: list[list[str]] = []
    for line in text.splitlines():
        if _LESSON_HEADING.match(line):
            entries.append([line])
        elif entries:
            entries[-1].append(line)
    return ["\n".join(lines).rstrip() for lines in entries]


# ---------------------------------------------------------------------------
# The brief
# ---------------------------------------------------------------------------

def fmt(value: object) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value)


def _goal_label(metric: str, goal: str) -> str:
    return f"{metric}{'↓' if goal == 'min' else '↑'}"


def _values_text(run: Mapping, metrics: Iterable[str]) -> str:
    return " ".join(f"{m}={fmt(run['values'][m])}" for m in metrics if m in run["values"])


def _status_counts(runs: Iterable[Mapping]) -> Counter:
    return Counter(r["status"] for r in runs)


def _noise_lines(runs: Sequence[Mapping], goals: Goals) -> list[str]:
    if not goals:
        return []
    metric = goals[0][0]
    replicated: dict[tuple[str, str], list[float]] = {}
    for r in runs:
        if r["status"] == "pass" and metric in r["values"]:
            replicated.setdefault((group_key(r), r["spec_base"]), []).append(r["values"][metric])
    multi = sorted(
        ((k, v) for k, v in replicated.items() if len(v) >= 2), key=lambda kv: -len(kv[1])
    )[:MAX_NOISE_GROUPS]
    return [
        f"  {group} spec {base[:8]}: {len(vals)} runs, {metric} {fmt(min(vals))}–{fmt(max(vals))} "
        f"(spread {fmt(max(vals) - min(vals))})"
        for (group, base), vals in multi
    ]


def render_brief(
    campaign: str,
    adapter_name: str,
    runs: Sequence[Mapping],
    metric_goals: Mapping[str, str | None],
    lessons: Sequence[str],
) -> str:
    """A fixed-size text digest of the campaign for the start of each loop iteration."""
    counts = _status_counts(runs)
    head = (
        f"campaign {campaign} · adapter {adapter_name} · {len(runs)} runs: "
        f"{counts['pass']} pass, {counts['fail']} fail, {counts['crash']} crash"
    )
    if not runs:
        return head + "\nno runs yet"
    lines = [head + f" · last {max(r['created_at'] for r in runs)[:19]}"]
    goals_all = active_goals(runs, metric_goals)
    if goals_all:
        lines.append("goals: " + "  ".join(_goal_label(m, g) for m, g in goals_all))

    groups = by_group(runs)
    ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    lines.append("")
    lines.append("mode/target\truns\tpass\tfail\tcrash\tbest")
    for key, group in ordered[:MAX_GROUPS]:
        c = _status_counts(group)
        goals = active_goals(group, metric_goals)
        front = pareto_front(group, goals)
        best = _values_text(front[0], [goals[0][0]]) if front else "-"
        lines.append(f"{key}\t{len(group)}\t{c['pass']}\t{c['fail']}\t{c['crash']}\t{best}")
    if len(ordered) > MAX_GROUPS:
        lines.append(f"+{len(ordered) - MAX_GROUPS} more groups (run.py query)")

    for key, group in ordered[:MAX_FRONT_GROUPS]:
        goals = active_goals(group, metric_goals)
        front = pareto_front(group, goals)
        if not front:
            continue
        lines.append("")
        lines.append(f"front {key} ({min(len(front), MAX_FRONT_ROWS)} of {len(front)}):")
        for r in front[:MAX_FRONT_ROWS]:
            validated = f" validated={r['validated']}" if r.get("validated") else ""
            lines.append(f"  {r['id']} {_values_text(r, [m for m, _ in goals])}{validated}")

    recent = sorted(runs, key=lambda r: (r["created_at"], r["id"]))[-MAX_RECENT:]
    lines.append("")
    lines.append("recent:")
    for r in reversed(recent):
        detail = r.get("crash_signature") or _values_text(r, [m for m, _ in goals_all][:2])
        lines.append(f"  {r['id']} {group_key(r)} {r['status']} {r['status_reason']} {detail}".rstrip())

    crashes = Counter(
        r.get("crash_signature") or r["status_reason"] for r in runs if r["status"] == "crash"
    )
    if crashes:
        lines.append("")
        lines.append("crash causes:")
        for cause, n in crashes.most_common(MAX_CRASH_KINDS):
            lines.append(f"  {n}× {cause}")

    noise = _noise_lines(runs, goals_all)
    if noise:
        lines.append("")
        lines.append("replicates (noise floor):")
        lines.extend(noise)

    stall = runs_since_front_change(runs, metric_goals)
    lines.append("")
    lines.append(
        "front: none yet" if stall is None else f"runs since the newest front member: {stall}"
    )
    if lessons:
        shown = "; ".join(lessons[-MAX_LESSON_TITLES:])
        lines.append(f"lessons: {len(lessons)} entries; latest: {shown}")
    else:
        lines.append("lessons: none yet")
    return "\n".join(lines)
