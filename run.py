#!/usr/bin/env python3
"""Run optimization experiments for a campaign and record them: the command line.

This file only parses arguments and dispatches. campaign.py selects the
campaign and resolves its configuration, runner.py runs experiments, replays
and batches and writes the digests, and records.py keeps the run files and
the results.db index. The campaign's solver adapter (named in its
config.json; see adapter.py / contract.py) owns everything solver-specific.

Usage (experiment flags come from the campaign's adapter; this shows the toy):
    python run.py --campaign demo --problem rastrigin --dim 4      # run one experiment
    python run.py --campaign demo --problem rastrigin --replicate 1  # another seed of it
    python run.py brief --campaign demo                            # fixed-size campaign digest
    python run.py query "SELECT ..." --campaign demo               # read-only SQL, compact output
    python run.py replay <run-id> --campaign demo                  # re-run and compare
    python run.py rebuild --campaign demo                          # regenerate results.db from runs/
    python run.py batch plan.json --campaign demo [--dry-run]      # run a planned batch of experiments
    python run.py status [--max-parallel N ...]                    # machine, slots, every campaign

`--campaign` (or $AUTORESEARCH_CAMPAIGN) may be omitted when exactly one
campaign exists. Each run is written atomically to the campaign's
runs/<run-id>.json — the source of truth — then indexed in results.db; a
single-line JSON summary goes to stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from pathlib import Path
from types import ModuleType

import batch
import machine
import records
import runner
from adapter import AdapterError, load_adapter
from campaign import (
    CAMPAIGN_ENV, HarnessError, apply_env, campaigns_root, check_required_env, load_config, machine_dir,
    resolve_campaign, resolve_layout, resolve_slots,
)
from contract import Cancelled

COMMANDS = ("replay", "rebuild", "brief", "query", "batch", "status")
CANCELLED_EXIT = 143
REPLAY_MISMATCH_EXIT = 2
QUERY_DEFAULT_LIMIT = 50


def build_parser(active: ModuleType, campaign: str | None) -> argparse.ArgumentParser:
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


def _status(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        description="Hardware, machine settings, run slots, and every campaign's runs and run cost; "
                    "flags save machine settings"
    )
    p.add_argument("--max-parallel", type=int, help="machine-wide run slots")
    p.add_argument("--usable-cores", type=int, help="cores the harness may use")
    p.add_argument("--usable-memory-gb", type=float, help="memory the harness may use")
    args = p.parse_args(argv)
    updates = {k: getattr(args, k) for k in machine.MACHINE_KEYS}
    if any(v is not None and v <= 0 for v in updates.values()):
        raise HarnessError("machine settings must be > 0")
    if any(v is not None for v in updates.values()):
        machine.write_settings(machine_dir(os.environ), updates)
    print(runner.status_report(os.environ, campaigns_root(os.environ)))
    return 0


def _dispatch(command: str, argv: list[str]) -> int:
    if command == "status":
        return _status(argv)

    # Campaign selection comes first: the campaign's adapter defines every
    # other flag, so the full parser can only be built once it is loaded.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--campaign", default=os.environ.get(CAMPAIGN_ENV))
    selection, _ = pre.parse_known_args(argv)
    campaign_dir = resolve_campaign(selection.campaign, campaigns_root(os.environ))
    config = load_config(campaign_dir)
    apply_env(config.env, os.environ)
    layout = resolve_layout(campaign_dir, os.environ)

    if command == "rebuild":
        p = argparse.ArgumentParser(description="Regenerate results.db from runs/*.json")
        p.add_argument("--campaign", default=selection.campaign)
        p.parse_args(argv)
        rows = records.rebuild(layout, load_adapter(config.adapter))
        print(json.dumps({"campaign": campaign_dir.name, "rows": rows}))
        return 0

    active = load_adapter(config.adapter)
    if command == "brief":
        p = argparse.ArgumentParser(description="Fixed-size digest of the campaign")
        p.add_argument("--campaign", default=selection.campaign)
        p.parse_args(argv)
        print(runner.brief(active, layout, config, os.environ))
        return 0
    if command == "query":
        p = argparse.ArgumentParser(description="Run one read-only SQL statement on results.db")
        p.add_argument("sql")
        p.add_argument("--limit", type=int, default=QUERY_DEFAULT_LIMIT)
        p.add_argument("--campaign", default=selection.campaign)
        args = p.parse_args(argv)
        print(records.query(layout, active, args.sql, args.limit))
        return 0

    parser = build_parser(active, selection.campaign)
    if command == "replay":
        p = argparse.ArgumentParser(description="Re-run a recorded experiment and compare")
        p.add_argument("run_id")
        p.add_argument("--campaign", default=selection.campaign)
        args = p.parse_args(argv)
        check_required_env(active, os.environ, campaign_dir)
        slots = resolve_slots(os.environ)
        return 0 if runner.replay(active, layout, parser, slots, args.run_id) else REPLAY_MISMATCH_EXIT

    if command == "batch":
        p = argparse.ArgumentParser(description="Run a planned batch of experiments (see batch.py)")
        p.add_argument("file", type=Path)
        p.add_argument("--campaign", default=selection.campaign)
        p.add_argument("--parallel", type=int, default=None, help="runs at once (capped by the campaign and machine)")
        p.add_argument("--dry-run", action="store_true", help="validate and show the plan without running")
        args = p.parse_args(argv)
        if args.parallel is not None and args.parallel < 1:
            raise HarnessError("--parallel must be >= 1")
        check_required_env(active, os.environ, campaign_dir)
        slots = resolve_slots(os.environ)
        parallel = min(args.parallel or config.max_parallel or slots.capacity, config.max_parallel or slots.capacity, slots.capacity)
        return runner.run_batch(active, layout, parser, campaign_dir.name, args.file, parallel, args.dry_run)

    args = parser.parse_args(argv)
    check_required_env(active, os.environ, campaign_dir)
    print(json.dumps(runner.run_once(active, layout, resolve_slots(os.environ), runner.with_seed(active, args))))
    return 0


def _raise_cancelled(_signum: int, _frame: object) -> None:
    raise Cancelled()


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    command = argv.pop(0) if argv and argv[0] in COMMANDS else "run"
    signal.signal(signal.SIGTERM, _raise_cancelled)
    try:
        code = _dispatch(command, argv)
    except (HarnessError, AdapterError, batch.BatchError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    except Cancelled:
        print("Cancelled.", file=sys.stderr)
        sys.exit(CANCELLED_EXIT)
    sys.exit(code)


if __name__ == "__main__":
    main()
