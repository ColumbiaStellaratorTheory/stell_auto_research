"""Running experiments: the run spec, one run end to end, replay, batches, and the digests.

The harness core is solver-agnostic: the campaign's solver adapter (named in
its config.json; see adapter.py / contract.py) runs the solver, and this
module turns each run into a record (records.py). It owns the run's argument
parser, the spec hash, the derived seed, the campaign's limits (config.json
`fixed`, `bounds`, `budget`), dedupe via per-spec claims, the machine's run
slots, the scratch/artifact lifecycle, replay, batch execution (planning is
batch.py), and the `brief` and `status` text.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import hashlib
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Callable, Mapping

import analysis
import batch
import machine
import records
from adapter import AdapterError, load_adapter
from campaign import (
    CAMPAIGN_ENV, CONFIG_NAME, CORE_FLAGS, DB_NAME, REPO_ROOT, Budget, CampaignConfig, CampaignError, HarnessError,
    Layout, Slots, Usage, busy_slots, list_campaigns, load_config, machine_dir, resolve_slots,
)
from contract import Cancelled, ExperimentOutcome, RunContext, clean, git_output, sha256_file
from locks import acquire_slot, release, report_waiting, try_lock

LESSONS_NAME = "LESSONS.md"
# Solver lessons, shared by every campaign of an adapter: <dir>/<adapter NAME>.md.
SOLVER_LESSONS_DIR = REPO_ROOT / "lessons"
LOG_TAIL_BYTES = 64 * 1024
BATCH_POLL_SECONDS = 0.2
# A spec another process is running right now is retried after this delay;
# once that run is recorded the retry is reused (or, after a crash, re-run).
IN_PROGRESS_RETRY_SECONDS = 5.0
BATCH_SUMMARY_ROWS = 10


def _uuid7() -> str:
    ts_ms = int(time.time() * 1000)
    rand_bits = uuid.uuid4().int & ((1 << 74) - 1)
    u = (ts_ms << 80) | (0x7 << 76) | rand_bits
    u = (u & ~(0x3 << 62)) | (0x2 << 62)
    return str(uuid.UUID(int=u))


def _canonical_hash(payload: object) -> str:
    """SHA-256 of `payload` as canonical JSON (sorted keys, no whitespace)."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Run specification: parser, spec, seed, spec hash
# ---------------------------------------------------------------------------

def build_parser(active: ModuleType, campaign: str | None) -> argparse.ArgumentParser:
    """The parser of one run: the core flags, --mode, and every flag the adapter registers."""
    p = argparse.ArgumentParser(description="Run one optimization experiment")
    p.add_argument(
        "--campaign",
        default=campaign,
        help=f"campaign under campaigns/ (default: ${CAMPAIGN_ENV}, or the only campaign)",
    )
    p.add_argument(
        "--replicate", type=int, default=0,
        help="sample index of this spec; a new index draws a new derived seed (default 0)",
    )
    p.add_argument("--batch-id", default=None, help="set by `run.py batch`: the batch this run belongs to")
    p.add_argument("--parent-run-id", default=None, help="the run this one builds on (set by batch promotion)")
    p.add_argument(
        "--mode",
        choices=list(active.MODES),
        default=active.MODES[0],
        help="mode exposed by the campaign's adapter",
    )
    active.add_arguments(p)
    return p


def run_spec(args: argparse.Namespace) -> dict:
    """Every parsed CLI value except core flags, NaN-cleaned.

    This is the full experiment specification: the mode, the target, and every
    flag the adapter registered (defaults included).
    """
    return {k: clean(v) for k, v in vars(args).items() if k not in CORE_FLAGS}


def _hashed_fields(active: ModuleType, spec: Mapping[str, object]) -> dict:
    return {k: v for k, v in spec.items() if k not in active.EXECUTION_FLAGS}


def _seedless_fields(active: ModuleType, spec: Mapping[str, object]) -> dict:
    return {k: v for k, v in _hashed_fields(active, spec).items() if k != active.SEED_FLAG}


def spec_base(active: ModuleType, spec: Mapping[str, object]) -> str:
    """Hash shared by every replicate of a spec: no seed, no execution flags, no solver."""
    return _canonical_hash({"adapter": active.NAME, "spec": _seedless_fields(active, spec)})


def derive_seed(active: ModuleType, spec: Mapping[str, object], replicate: int) -> int:
    """Seed for an unset SEED_FLAG: a function of the spec (minus the seed) and replicate.

    Independent of the solver identity, so the same experiment keeps its seed
    across solver versions and their results stay comparable.
    """
    digest = _canonical_hash(
        {"adapter": active.NAME, "spec": _seedless_fields(active, spec), "replicate": replicate}
    )
    return int(digest[:8], 16) % 2**31


def with_seed(active: ModuleType, args: argparse.Namespace) -> argparse.Namespace:
    """args with the adapter's seed filled in when the agent left it unset."""
    if active.SEED_FLAG is None or getattr(args, active.SEED_FLAG) is not None:
        return args
    seed = derive_seed(active, run_spec(args), args.replicate)
    return argparse.Namespace(**{**vars(args), active.SEED_FLAG: seed})


def spec_hash(active: ModuleType, spec: Mapping[str, object], solver_identity: str) -> str:
    """Hash of what was asked: adapter, non-execution flags, and the solver code."""
    return _canonical_hash(
        {"adapter": active.NAME, "spec": _hashed_fields(active, spec), "solver": solver_identity}
    )


# ---------------------------------------------------------------------------
# Campaign limits: config.json fixed, bounds, budget
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Limits:
    """What config.json allows a new run: `fixed` values, inclusive `bounds`, and the `budget`.

    `fixed` holds the values as the adapter's flags parse them, so they compare
    equal to a run's parsed arguments. Replay is exempt (it re-checks a record).
    """

    fixed: Mapping[str, object]
    bounds: Mapping[str, tuple[float, float]]
    budget: Budget

    def violations(self, args: argparse.Namespace, skip: tuple[str, ...] = ()) -> list[str]:
        """Each way `args` breaks `fixed` or `bounds` (params in `skip` are not checked)."""
        found = [
            f"{key}={analysis.fmt(getattr(args, key))}, fixed at {analysis.fmt(want)}"
            for key, want in self.fixed.items()
            if key not in skip and getattr(args, key) != want
        ]
        for key, (lo, hi) in self.bounds.items():
            value = getattr(args, key)
            numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
            if key not in skip and not (numeric and lo <= value <= hi):
                found.append(f"{key}={analysis.fmt(value)} outside bounds [{analysis.fmt(lo)}, {analysis.fmt(hi)}]")
        return found

    def describe(self) -> str:
        parts = []
        if self.fixed:
            parts.append("fixed " + ", ".join(f"{k}={analysis.fmt(v)}" for k, v in self.fixed.items()))
        if self.bounds:
            parts.append("bounds " + ", ".join(
                f"{k} in [{analysis.fmt(lo)}, {analysis.fmt(hi)}]" for k, (lo, hi) in self.bounds.items()
            ))
        return " · ".join(parts) or "none"

    def as_record(self) -> dict:
        """The limits as JSON, stored in each batch record so its history survives later config changes."""
        return {
            "fixed": clean(dict(self.fixed)),
            "bounds": clean(dict(self.bounds)),
            "budget": asdict(self.budget),
        }


def _param_problem(active: ModuleType, defaults: Mapping[str, object], key: str) -> str | None:
    """Why `key` cannot be constrained, or None when it is a solver parameter of the run spec."""
    if key in CORE_FLAGS:
        return "is set by the harness, not a solver parameter"
    if key in active.EXECUTION_FLAGS:
        return "is an execution flag: it changes how a run executes, not what it computes"
    if key not in defaults:
        known = sorted(k for k in defaults if k not in CORE_FLAGS and k not in active.EXECUTION_FLAGS)
        return f"is not a parameter of adapter '{active.NAME}' (parameters: {', '.join(known)})"
    return None


def resolve_limits(
    active: ModuleType, parser: argparse.ArgumentParser, config: CampaignConfig, campaign_dir: Path,
) -> Limits:
    """config.json's fixed / bounds checked against the adapter's parameters; CampaignError names the key.

    Keys must be run-spec params (argparse dests, as in batch specs), not core
    or execution flags. A fixed value is parsed by its flag the way a batch
    spec value is; a bounded param must not be a string or boolean flag.
    """
    path = campaign_dir / CONFIG_NAME
    defaults = vars(parser.parse_args([]))
    for section, keys in (("fixed", config.fixed), ("bounds", config.bounds)):
        for key in keys:
            problem = _param_problem(active, defaults, key)
            if problem:
                raise CampaignError(f"{path}: \"{section}\" key '{key}' {problem}")
    fixed = {}
    for key, value in config.fixed.items():
        parsed = _parse_planned(parser, [_flag(key), str(value)])
        if isinstance(parsed, str):
            raise CampaignError(f"{path}: \"fixed\" value for '{key}' is invalid: {parsed}")
        fixed[key] = getattr(parsed, key)
    for key in config.bounds:
        if isinstance(defaults[key], (str, bool)):
            raise CampaignError(
                f"{path}: \"bounds\" key '{key}' is not numeric (default {defaults[key]!r}); use \"fixed\""
            )
    return Limits(fixed, dict(config.bounds), config.budget)


def _budget_spent(active: ModuleType, layout: Layout, budget: Budget) -> str | None:
    """Why the campaign's budget allows no new run, or None (no DB query without a budget)."""
    return budget.exhausted(records.usage(layout, active)) if budget.limited else None


def limit_lines(limits: Limits, used: Usage) -> list[str]:
    """The constraints and budget lines of `brief` and `status`."""
    budget = limits.budget
    runs = f"{used.runs} runs"
    hours = f"{used.hours:.2f} h"
    if budget.runs is not None:
        runs = f"{used.runs} of {budget.runs} runs used ({budget.runs_left(used)} left)"
    if budget.hours is not None:
        hours = f"{used.hours:.2f} of {budget.hours:g} h used ({max(0.0, budget.hours - used.hours):.2f} h left)"
    spent = f"budget: {runs} · {hours}" if budget.limited else f"budget: none · {runs}, {hours} recorded"
    return [f"constraints: {limits.describe()}", spent]


# ---------------------------------------------------------------------------
# Record construction (single place where NaN cleaning happens)
# ---------------------------------------------------------------------------

def harness_provenance() -> dict:
    commit = git_output(REPO_ROOT, "rev-parse", "HEAD")
    status = git_output(REPO_ROOT, "status", "--porcelain", "--untracked-files=no")
    return {
        "commit": commit.strip() if commit else None,
        "dirty": bool(status) if status is not None else None,
    }


def platform_provenance() -> dict:
    return {
        "os": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
    }


def _build_record(
    active: ModuleType,
    args: argparse.Namespace,
    outcome: ExperimentOutcome,
    elapsed: float,
    *,
    run_id: str,
    digest: str,
    solver_identity: str,
    evidence: Mapping[str, object],
    crash_signature: str | None = None,
    replay_of: str | None = None,
) -> dict:
    """Assemble a run record from an adapter outcome. NaN cleaning happens here."""
    metrics = outcome.metrics
    record = {
        "id": run_id,
        "adapter": active.NAME,
        "mode": args.mode,
        "target": getattr(args, active.TARGET_FLAG),
        "spec_hash": digest,
        "replicate": args.replicate,
        "seed": getattr(args, active.SEED_FLAG) if active.SEED_FLAG else None,
        "parent_run_id": outcome.parent_run_id or getattr(args, "parent_run_id", None),
        "replay_of": replay_of,
        "batch_id": getattr(args, "batch_id", None),
        "status": outcome.status,
        "status_reason": outcome.status_reason,
        "crash_signature": crash_signature,
        "validated": outcome.validated,
        "elapsed": round(elapsed, 1),
        "created_at": _now(),
        "params": run_spec(args),
    }
    record["metrics"] = {k: clean(v) for k, v in metrics.items()}
    record["provenance"] = {
        "solver_identity": solver_identity,
        "adapter": dict(outcome.provenance),
        "harness": harness_provenance(),
        "platform": platform_provenance(),
    }
    record["evidence"] = dict(evidence)
    return record


def metric_values(record: Mapping[str, object]) -> dict:
    """Every non-null metric of a record."""
    return {k: v for k, v in (record.get("metrics") or {}).items() if v is not None}


def summary(record: Mapping[str, object], on_front: bool | None) -> dict:
    """The compact stdout view of a record: set identity fields, metrics, front membership.

    Provenance, evidence, params and empty fields stay in the run file.
    """
    out = {k: record[k] for k in records.SUMMARY_FIELDS if record.get(k) is not None}
    out["metrics"] = metric_values(record)
    if on_front is not None:
        out["on_front"] = on_front
    return out


# ---------------------------------------------------------------------------
# Analysis views (see analysis.py)
# ---------------------------------------------------------------------------

def _views(active: ModuleType, runs: list[dict]) -> list[dict]:
    """Analysis views of records.read_runs rows: metrics become `values`, plus `spec_base`."""
    return [
        {**{k: v for k, v in run.items() if k != "metrics"},
         "values": metric_values(run), "spec_base": spec_base(active, run["params"])}
        for run in runs
    ]


def load_views(layout: Layout, active: ModuleType) -> list[dict]:
    return _views(active, records.load_runs(layout, active))


def _tail_text(path: Path, max_bytes: int = LOG_TAIL_BYTES) -> str:
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - max_bytes))
        return f.read().decode(errors="replace")


def _log_signature(outcome: ExperimentOutcome) -> str | None:
    log = outcome.evidence.get("log")
    if outcome.status != "crash" or log is None or not log.is_file():
        return None
    return analysis.crash_signature(_tail_text(log))


# ---------------------------------------------------------------------------
# Core experiment
# ---------------------------------------------------------------------------

def execute(
    active: ModuleType,
    layout: Layout,
    args: argparse.Namespace,
    identity: str,
    *,
    replay_of: str | None = None,
) -> dict:
    """Run one experiment via the campaign's adapter; record it; return the record.

    `identity` is the adapter's solver_identity(args), computed once by the caller.
    """
    digest = spec_hash(active, run_spec(args), identity)
    run_id = _uuid7()
    run_dir = layout.scratch_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.monotonic()
    try:
        outcome = active.run_experiment(args, RunContext(run_id=run_id, dir=run_dir))
    except (Cancelled, KeyboardInterrupt):
        # SIGTERM or Ctrl-C: run_solver already killed the solver's process tree.
        print("Run cancelled; recording it.", file=sys.stderr)
        outcome = ExperimentOutcome("crash", "cancelled")
    except Exception as e:
        # Truly-unexpected adapter failure (the adapter never returned an
        # outcome). The record still carries the full run spec, so the
        # experiment stays reproducible.
        print(f"WARNING: adapter raised: {e}", file=sys.stderr)
        outcome = ExperimentOutcome("crash", f"adapter_error: {e}")
    # Total experiment wall time: includes any validation or
    # chained sub-steps the adapter runs internally, not just one solver call.
    elapsed = time.monotonic() - t0
    peak_rss_mb = machine.children_peak_rss_mb()

    evidence = records.store_evidence(layout.blobs_dir, outcome.evidence)
    record = _build_record(
        active, args, outcome, elapsed,
        run_id=run_id, digest=digest, solver_identity=identity, evidence=evidence,
        crash_signature=_log_signature(outcome), replay_of=replay_of,
    )
    record["peak_rss_mb"] = round(peak_rss_mb, 1) if peak_rss_mb is not None else None
    records.write_run_record(layout.runs_dir, record)
    records.index_record(layout, active, record)
    _finalize_run_dir(layout, run_dir, outcome.status, run_id)
    return record


def run_once(active: ModuleType, layout: Layout, slots: Slots, args: argparse.Namespace, limits: Limits) -> dict:
    """Run the experiment unless it is already recorded or running; return the stdout object.

    Order: refuse a spec that breaks the campaign's fixed / bounds, claim the
    spec (so concurrent agents never run it twice), check for an earlier
    pass/fail run, refuse when the budget is spent, then wait for a
    machine-wide slot and execute. A refusal raises HarnessError and records
    nothing; an already-recorded spec is answered whatever the budget.
    """
    violations = limits.violations(args)
    if violations:
        raise HarnessError(
            f"run refused, it breaks {layout.campaign_dir / CONFIG_NAME}: {'; '.join(violations)}"
        )
    identity = active.solver_identity(args)
    digest = spec_hash(active, run_spec(args), identity)
    claim = try_lock(layout.claims_dir / f"{digest}-{args.replicate}.lock")
    if claim is None:
        print("This spec is running in another process right now.", file=sys.stderr)
        return {"in_progress": True, "spec_hash": digest, "replicate": args.replicate}
    try:
        duplicate = records.find_duplicate(layout, active, digest, args.replicate)
        if duplicate:
            print(
                f"Already run as {duplicate['id']}; pass --replicate N for another sample.",
                file=sys.stderr,
            )
            return {"duplicate_of": duplicate["id"], "status": duplicate["status"],
                    "status_reason": duplicate["status_reason"], "replicate": args.replicate}
        spent = _budget_spent(active, layout, limits.budget)
        if spent:
            raise HarnessError(f"run refused, {spent} (\"budget\" in {layout.campaign_dir / CONFIG_NAME})")
        slot = acquire_slot(slots.dir, slots.capacity, report_waiting(slots.capacity))
        try:
            record = execute(active, layout, args, identity)
        finally:
            release(slot)
    finally:
        release(claim)
    on_front = None
    if record["status"] == "pass":
        on_front = record["id"] in analysis.front_ids(load_views(layout, active), active.METRICS)
    return summary(record, on_front)


def _finalize_run_dir(layout: Layout, run_dir: Path, status: str, run_id: str) -> None:
    """Apply keep_artifacts: move the run dir to artifacts_dir/<run-id> or discard it."""
    keep = layout.keep_artifacts == "all" or (
        layout.keep_artifacts == "pass" and status == "pass"
    )
    if not keep:
        shutil.rmtree(run_dir, ignore_errors=True)
        return
    layout.artifacts_dir.mkdir(parents=True, exist_ok=True)
    dest = layout.artifacts_dir / run_id
    shutil.move(str(run_dir), str(dest))
    print(f"Artifacts kept: {dest}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------

def _differs(a: object, b: object, tolerance: float) -> bool:
    numeric = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (a, b))
    if not numeric:
        return a != b
    scale = max(abs(a), abs(b))
    return scale > 0 and abs(a - b) / scale > tolerance


def compare_runs(original: Mapping[str, object], replay: Mapping[str, object], tolerance: float) -> list[dict]:
    """Differences that break a replay: status, or a metric beyond `tolerance` (relative)."""
    mismatches = []
    if original.get("status") != replay.get("status"):
        mismatches.append({"field": "status", "original": original.get("status"), "replay": replay.get("status")})
    before, after = metric_values(original), metric_values(replay)
    for key in sorted(before.keys() | after.keys()):
        a, b = before.get(key), after.get(key)
        if a is None or b is None or _differs(a, b, tolerance):
            mismatches.append({"field": key, "original": a, "replay": b})
    return mismatches


def args_from_params(
    parser: argparse.ArgumentParser, params: Mapping[str, object], replicate: int
) -> argparse.Namespace:
    """The recorded run's arguments: parser defaults overlaid with the recorded params.

    Values are used exactly as recorded (lists, booleans, explicit None),
    which are the parsed values in JSON form — paths as strings (see
    contract.py). Defaults fill only flags added to the adapter since; a
    recorded flag the adapter no longer has is refused.
    """
    defaults = vars(parser.parse_args([]))
    removed = sorted(set(params) - set(defaults))
    if removed:
        raise HarnessError(f"cannot rebuild the run's arguments: the adapter no longer has {removed}")
    return argparse.Namespace(**{**defaults, **params, "replicate": replicate})


def replay(
    active: ModuleType, layout: Layout, parser: argparse.ArgumentParser, slots: Slots, run_id: str
) -> bool:
    """Re-run a recorded experiment from its spec, compare, and print the verdict.

    The re-run bypasses dedupe (that is its point) but not the machine's run slots.
    """
    original = records.read_run_record(layout.runs_dir, run_id)
    args = with_seed(active, args_from_params(parser, original["params"], original.get("replicate") or 0))
    slot = acquire_slot(slots.dir, slots.capacity, report_waiting(slots.capacity))
    try:
        record = execute(active, layout, args, active.solver_identity(args), replay_of=run_id)
    finally:
        release(slot)
    mismatches = compare_runs(original, record, active.REPLAY_TOLERANCE)
    before = (original.get("provenance") or {}).get("solver_identity")
    print(json.dumps({
        "replay_of": run_id,
        "run_id": record["id"],
        "match": not mismatches,
        "tolerance": active.REPLAY_TOLERANCE,
        "solver_changed": before is not None and before != record["provenance"]["solver_identity"],
        "mismatches": mismatches,
    }))
    return not mismatches


# ---------------------------------------------------------------------------
# Brief and status
# ---------------------------------------------------------------------------

def lesson_titles(path: Path) -> list[str] | None:
    """Titles of the lesson entries in `path`, oldest first; None when the file does not exist."""
    return analysis.lesson_titles(path.read_text()) if path.exists() else None


def capacity_lines(
    active: ModuleType, views: list[dict], config: CampaignConfig, environ: Mapping[str, str],
) -> list[str]:
    """Measured cost per mode and what fits: runs at once and, with plan_minutes, batch size."""
    settings = machine.read_settings(machine_dir(environ))
    cores = settings.get("usable_cores") or machine.usable_cpus(environ)
    memory = settings.get("usable_memory_gb") or machine.total_memory_gb()
    slots = resolve_slots(environ)
    lines = []
    for cost in machine.mode_costs(views, active.THREADS_FLAG):
        fit = machine.runs_at_once(cores, cost.threads, memory, cost.peak_memory_gb)
        parallel = min(fit, slots.capacity, config.max_parallel or slots.capacity)
        peak = f"{cost.peak_memory_gb:.2f} GB" if cost.peak_memory_gb is not None else "peak memory unknown"
        line = (
            f"  {cost.mode}: {cost.runs} runs · median {cost.median_seconds:.3g}s · {peak} · "
            f"{cost.threads} threads → {fit} fit at once, {parallel} with current slots"
        )
        if config.plan_minutes:
            size = machine.batch_size(parallel, config.plan_minutes, cost.median_seconds)
            line += f" · batch ≤ {size} per {config.plan_minutes:g} min"
        lines.append(line)
    return lines


def brief(
    active: ModuleType, layout: Layout, config: CampaignConfig, limits: Limits, environ: Mapping[str, str],
) -> str:
    views = load_views(layout, active)
    slots = resolve_slots(environ)
    machine_lines = [f"machine: {slots.capacity} run slots, {busy_slots(slots)} busy",
                     *capacity_lines(active, views, config, environ)]
    return analysis.render_brief(
        layout.campaign_dir.name,
        active.NAME,
        views,
        active.METRICS,
        lesson_titles(layout.campaign_dir / LESSONS_NAME) or [],
        lesson_titles(SOLVER_LESSONS_DIR / f"{active.NAME}.md"),
        limit_lines(limits, Usage.of(views)),
        machine_lines,
    )


def campaign_status(campaign_dir: Path, environ: Mapping[str, str]) -> list[str]:
    """One campaign's block of `run.py status`, read without modifying anything."""
    name = campaign_dir.name
    try:
        config = load_config(campaign_dir)
        active = load_adapter(config.adapter)
        limits = resolve_limits(active, build_parser(active, name), config, campaign_dir)
    except (HarnessError, AdapterError) as e:
        return [f"campaign {name}: {e}"]
    plan = f" · plans every {config.plan_minutes:g} min" if config.plan_minutes else ""
    head = f"campaign {name} · adapter {active.NAME}{plan}"
    runs = records.load_runs_readonly(campaign_dir / DB_NAME)
    if runs is None:
        return [head, f"  results.db needs `python run.py rebuild --campaign {name}`"]
    limit_text = [f"  {line}" for line in limit_lines(limits, Usage.of(runs))]
    if not runs:
        return [head, "  no runs yet", *limit_text]
    views = _views(active, runs)
    counts = Counter(v["status"] for v in views)
    last = max(v["created_at"] for v in views)[:19]
    stall = analysis.runs_since_front_change(views, active.METRICS)
    front = "no front yet" if stall is None else f"{stall} runs since the front moved"
    return [
        head,
        f"  {len(views)} runs: {counts['pass']} pass, {counts['fail']} fail, {counts['crash']} crash"
        f" · last {last} · {front}",
        *limit_text,
        *capacity_lines(active, views, config, environ),
    ]


def status_report(environ: Mapping[str, str], root: Path) -> str:
    """Hardware, machine settings and slots, then each campaign's state and measured run cost."""
    hw = machine.detect(environ)
    slots = resolve_slots(environ)
    settings = machine.read_settings(machine_dir(environ))
    lines = ["hardware:", *(f"  {line}" for line in machine.describe_hardware(hw))]
    shown = ", ".join(f"{k}={v}" for k, v in settings.items()) or "none (defaults: 1 slot, detected cores/memory)"
    lines.append(f"settings ({machine_dir(environ) / machine.MACHINE_FILE}): {shown}")
    lines.append(f"run slots: {slots.capacity}, {busy_slots(slots)} busy")
    names = list_campaigns(root)
    if not names:
        lines.append(f"no campaigns under {root}")
    for name in names:
        lines.extend(campaign_status(root / name, environ))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Batches (planning in batch.py; execution here)
# ---------------------------------------------------------------------------

def _flag(dest: str) -> str:
    return "--" + dest.replace("_", "-")


def planned_argv(planned: batch.PlannedRun, campaign: str, batch_id: str) -> list[str]:
    """The run.py command-line arguments that execute one planned run."""
    argv = ["--campaign", campaign, "--batch-id", batch_id, "--replicate", str(planned.replicate)]
    if planned.parent_run_id:
        argv += ["--parent-run-id", planned.parent_run_id]
    for dest, value in sorted(planned.spec.items()):
        argv += [_flag(dest), str(value)]
    return argv


def _parse_planned(parser: argparse.ArgumentParser, argv: list[str]) -> argparse.Namespace | str:
    """The parsed args for `argv`, or argparse's error message."""
    errors = io.StringIO()
    try:
        with contextlib.redirect_stderr(errors):
            return parser.parse_args(argv)
    except SystemExit:
        return errors.getvalue().strip().splitlines()[-1]


@dataclass
class _IdentityCache:
    """solver_identity per distinct execution-flag values (contract: it depends on nothing else)."""

    active: ModuleType
    known: dict = field(default_factory=dict)

    def __call__(self, args: argparse.Namespace) -> str:
        key = json.dumps({k: getattr(args, k, None) for k in self.active.EXECUTION_FLAGS}, sort_keys=True)
        if key not in self.known:
            self.known[key] = self.active.solver_identity(args)
        return self.known[key]


def check_planned(
    active: ModuleType, layout: Layout, parser: argparse.ArgumentParser, planned: list[batch.PlannedRun],
    campaign: str, batch_id: str, identity: _IdentityCache, limits: Limits,
) -> tuple[list[list[str]], int, list[str]]:
    """(argv per distinct run, how many of them are already recorded, errors), running nothing.

    A run that does not parse or breaks the campaign's fixed / bounds is an
    error. Planned runs that resolve to the same (spec hash, replicate) are
    launched once. Already-recorded ones are still launched: their child
    answers `duplicate_of` at once, which gives later stages their results.
    """
    argvs, recorded, errors, seen = [], 0, [], set()
    for i, run_plan in enumerate(planned):
        argv = planned_argv(run_plan, campaign, batch_id)
        parsed = _parse_planned(parser, argv)
        if isinstance(parsed, str):
            errors.append(f"{run_plan.stage} run {i} {dict(run_plan.spec)}: {parsed}")
            continue
        args = with_seed(active, parsed)
        violations = limits.violations(args)
        if violations:
            errors.append(f"{run_plan.stage} run {i} {dict(run_plan.spec)}: {'; '.join(violations)}")
            continue
        digest = spec_hash(active, run_spec(args), identity(args))
        key = (digest, args.replicate)
        if key in seen:
            continue
        seen.add(key)
        recorded += records.find_duplicate(layout, active, digest, args.replicate) is not None
        argvs.append(argv)
    return argvs, recorded, errors


def _result_view(record: Mapping[str, object]) -> dict:
    """A run's record as an analysis view (enough for promotion and the summary)."""
    return {
        "id": record["id"], "status": record["status"], "status_reason": record.get("status_reason"),
        "crash_signature": record.get("crash_signature"), "target": record.get("target"),
        "mode": record.get("mode"), "values": metric_values(record),
    }


def _child_result(proc: subprocess.Popen, stdout) -> dict:
    stdout.seek(0)
    lines = stdout.read().decode(errors="replace").strip().splitlines()
    stdout.close()
    return json.loads(lines[-1]) if lines else {}


def launch_runs(
    layout: Layout, argvs: list[list[str]], parallel: int, same_crash: int,
    completed: list[dict], log: Path, budget_spent: Callable[[], str | None],
    program: tuple[str, ...] = (sys.executable, str(REPO_ROOT / "run.py")),
) -> tuple[list[dict], str | None, int]:
    """Run each argv as its own `program` process (default: run.py), `parallel` at a time.

    Children print to a temporary file, never a pipe, so a large result cannot
    block them. A child that finds its spec running elsewhere (`in_progress`)
    is retried after IN_PROGRESS_RETRY_SECONDS. Launching stops (in-flight
    runs finish) once the early-stop rule fires, or once `budget_spent` —
    asked after each child that did not reuse a recorded run — gives a
    reason. Returns (result views of this call's runs, stop reason or None,
    number of children that exited without a result — their stderr is in
    `log`). On cancellation, children are sent SIGTERM so each records itself
    as cancelled.
    """
    queue = [(0.0, argv) for argv in argvs]  # (not before, argv)
    running: list[tuple[subprocess.Popen, object, list[str]]] = []
    results, stop, failed = [], None, 0
    with open(log, "a") as stderr:
        try:
            while running or (queue and stop is None):
                now = time.monotonic()
                while stop is None and len(running) < parallel:
                    ready = next((i for i, (at, _) in enumerate(queue) if at <= now), None)
                    if ready is None:
                        break
                    argv = queue.pop(ready)[1]
                    stdout = tempfile.TemporaryFile()
                    proc = subprocess.Popen([*program, *argv], stdout=stdout, stderr=stderr)
                    running.append((proc, stdout, argv))
                finished = [entry for entry in running if entry[0].poll() is not None]
                if not finished:
                    time.sleep(BATCH_POLL_SECONDS)
                    continue
                for entry in finished:
                    running.remove(entry)
                    proc, stdout, argv = entry
                    printed = _child_result(proc, stdout)
                    if printed.get("in_progress"):
                        queue.append((time.monotonic() + IN_PROGRESS_RETRY_SECONDS, argv))
                        continue
                    run_id = printed.get("id") or printed.get("duplicate_of")
                    if run_id is None:
                        failed += 1
                        stop = stop or budget_spent()
                        continue
                    record = records.read_run_record(layout.runs_dir, run_id)
                    view = {**_result_view(record), "reused": "duplicate_of" in printed}
                    results.append(view)
                    completed.append(view)
                    stop = stop or batch.should_stop(completed, same_crash)
                    if not view["reused"]:
                        stop = stop or budget_spent()
        except BaseException:
            for proc, _, _ in running:
                proc.terminate()
            for proc, stdout, _ in running:
                proc.wait()
                stdout.close()
            raise
    return results, stop, failed


def _summary_lines(stage: str, results: list[dict], metric_goals: Mapping[str, str | None]) -> list[str]:
    counts = Counter(r["status"] for r in results)
    reused = sum(1 for r in results if r.get("reused"))
    goals = analysis.active_goals(results, metric_goals)
    lines = [
        f"{stage}: {len(results)} runs ({reused} already recorded) — "
        f"{counts['pass']} pass, {counts['fail']} fail, {counts['crash']} crash"
    ]
    front = analysis.pareto_front(results, goals)
    for r in front[:BATCH_SUMMARY_ROWS]:
        values = " ".join(f"{m}={analysis.fmt(r['values'][m])}" for m, _ in goals)
        lines.append(f"  front {r['id']} {r['mode']}/{r['target']} {values}")
    causes = Counter(batch.crash_key(r) for r in results if r["status"] == "crash")
    for cause, n in causes.most_common(3):
        lines.append(f"  crash {n}× {cause}")
    return lines


def _batch_budget_problem(budget: Budget, used: Usage, new_runs: int) -> str | None:
    """Why a batch planning at most `new_runs` new runs does not fit the remaining budget, or None."""
    if new_runs == 0:
        return None
    spent = budget.exhausted(used)
    if spent:
        return f"{spent}; this batch plans up to {new_runs} new runs"
    left = budget.runs_left(used)
    if left is not None and new_runs > left:
        return f"this batch plans up to {new_runs} new runs, but {left} of the {budget.runs}-run budget remain"
    return None


def run_batch(
    active: ModuleType, layout: Layout, parser: argparse.ArgumentParser, campaign: str,
    path: Path, parallel: int, dry_run: bool, limits: Limits,
) -> int:
    """Validate a batch file against the campaign's limits, then run its stages in order; print a capped summary.

    Every planned run is checked against fixed / bounds up front, and the
    batch's new runs (promotion stages counted at their most: top ×
    replicates) against the remaining run budget; any problem refuses the
    whole batch. While it runs, launching stops once the budget is spent.
    """
    plan = batch.load_batch(path, active.METRICS)
    identity = _IdentityCache(active)
    dests = set(vars(parser.parse_args([])))
    errors, previews = [], []
    new_runs = promoted = 0
    for stage in plan.stages:
        if stage.source is None:
            argvs, recorded, stage_errors = check_planned(
                active, layout, parser, batch.plan_stage(stage), campaign, "check", identity, limits
            )
            errors += stage_errors
            new_runs += len(argvs) - recorded
            previews.append(f"{stage.name}: {len(argvs) - recorded} to run, {recorded} already recorded")
        else:
            unknown = sorted(k for k in [*stage.base, *stage.carry] if k not in dests)
            if unknown:
                errors.append(f"{stage.name}: unknown params {unknown}")
            else:
                # Carried values come from earlier runs; the stage's own values can be checked now,
                # by parsing only (no solver fingerprint: carried flags may move the solver).
                parsed = _parse_planned(
                    parser, planned_argv(batch.PlannedRun(stage.name, stage.base, 0), campaign, "check")
                )
                if isinstance(parsed, str):
                    errors.append(f"{stage.name} base {dict(stage.base)}: {parsed}")
                else:
                    # Carried params come from earlier runs of this batch, checked when they were planned.
                    violations = limits.violations(with_seed(active, parsed), skip=tuple(stage.carry))
                    if violations:
                        errors.append(f"{stage.name} base {dict(stage.base)}: {'; '.join(violations)}")
            promoted += stage.top * stage.replicates
            previews.append(
                f"{stage.name}: top {stage.top} of {stage.source} by {stage.rank_by} × {stage.replicates} replicates"
            )
    used = records.usage(layout, active)
    over_budget = _batch_budget_problem(limits.budget, used, new_runs + promoted)
    if over_budget:
        errors.append(over_budget)
    if errors:
        raise HarnessError("batch refused; nothing was launched:\n" + "\n".join(f"- {e}" for e in errors))
    if dry_run:
        planned = f"this batch: {new_runs} new runs" + (f" + up to {promoted} promoted" if promoted else "")
        print("\n".join([
            f"batch {path} (valid) · parallel {parallel}", *previews,
            *limit_lines(limits, used), planned,
        ]))
        return 0

    batch_id = _uuid7()
    started = time.monotonic()
    record_path = layout.batches_dir / f"{batch_id}.json"
    batch_record = {
        "id": batch_id, "source": str(path), "sha256": sha256_file(path),
        "created_at": _now(),
        "hypothesis": plan.hypothesis, "lessons": plan.lessons, "parallel": parallel,
        "limits": limits.as_record(), "batch": plan.raw, "status": "running", "stages": {},
    }
    records.write_atomic(record_path, json.dumps(batch_record, indent=1).encode())
    log = layout.batches_dir / f"{batch_id}.log"
    results_by_stage: dict[str, list[dict]] = {}
    completed: list[dict] = []
    stop = None
    failure = None
    lines = []
    try:
        for stage in plan.stages:
            if stop:
                break
            if stage.source is None:
                planned = batch.plan_stage(stage)
            else:
                selected = batch.select_runs(stage, results_by_stage.get(stage.source, []), active.METRICS)
                params = {r["id"]: records.read_run_record(layout.runs_dir, r["id"])["params"] for r in selected}
                planned = batch.plan_promotion(stage, selected, params)
            argvs, _, stage_errors = check_planned(
                active, layout, parser, planned, campaign, batch_id, identity, limits
            )
            if stage_errors:
                failure = f"{stage.name}: invalid promoted runs: {stage_errors[0]}"
                break
            results, stop, failed = launch_runs(
                layout, argvs, parallel, plan.same_crash_stop, completed, log,
                lambda: _budget_spent(active, layout, limits.budget),
            )
            results_by_stage[stage.name] = results
            batch_record["stages"][stage.name] = [r["id"] for r in results]
            lines += _summary_lines(stage.name, results, active.METRICS)
            if failed:
                lines.append(f"  {failed} runs exited without a result (see the stderr log)")
        batch_record["status"] = "failed" if failure else "stopped" if stop else "done"
    except BaseException:
        batch_record["status"] = "cancelled"
        raise
    finally:
        batch_record["stop_reason"] = failure or stop
        batch_record["finished_at"] = _now()
        records.write_atomic(record_path, json.dumps(batch_record, indent=1).encode())
    outcome = f"failed: {failure}" if failure else f"stopped: {stop}" if stop else "done"
    head = f"batch {batch_id} · {len(completed)} runs · {round(time.monotonic() - started)}s · {outcome}"
    print("\n".join([head, *lines, f"details: {record_path} · children's stderr: {log}"]))
    return 1 if failure else 0
