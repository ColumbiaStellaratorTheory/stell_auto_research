"""What this machine offers and what a campaign's runs cost: detection and sizing.

Detection uses the standard library plus a few read-only OS queries (sysctl on
macOS, nvidia-smi when installed). Everything it cannot see is reported as
None for the user (or /setup-harness) to fill in. Sizing turns usable cores and
memory, measured run cost, and the planning interval into run slots and a
batch size; the formulas live only here.
"""

from __future__ import annotations

import json
import math
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import median
from typing import Mapping, Sequence

if os.name != "nt":
    import resource

QUERY_TIMEOUT_SECONDS = 10
MACHINE_FILE = "machine.json"
MACHINE_KEYS = ("max_parallel", "usable_cores", "usable_memory_gb")


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def _query(cmd: list[str]) -> str | None:
    """Stdout of a read-only OS query, or None if the tool is missing or fails."""
    if shutil.which(cmd[0]) is None:
        return None
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=QUERY_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def usable_cpus(environ: Mapping[str, str]) -> int:
    """CPUs this process may use: affinity / cgroup-aware count, capped by a SLURM allocation."""
    if hasattr(os, "process_cpu_count"):
        count = os.process_cpu_count() or 1
    elif hasattr(os, "sched_getaffinity"):
        count = len(os.sched_getaffinity(0))
    else:
        count = os.cpu_count() or 1
    slurm = environ.get("SLURM_CPUS_PER_TASK") or environ.get("SLURM_CPUS_ON_NODE")
    return min(count, int(slurm)) if slurm and slurm.isdigit() else count


def performance_cores() -> int | None:
    """Performance-core count on Apple Silicon (efficiency cores slow OpenMP runs); else None."""
    if sys.platform != "darwin":
        return None
    out = _query(["sysctl", "-n", "hw.perflevel0.physicalcpu"])
    return int(out) if out and out.isdigit() else None


def total_memory_gb() -> float | None:
    if sys.platform == "darwin":
        out = _query(["sysctl", "-n", "hw.memsize"])
        return int(out) / 2**30 if out and out.isdigit() else None
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30
    except (AttributeError, ValueError, OSError):
        return None


def gpus() -> list[str]:
    """Descriptions of the GPUs the OS reports (NVIDIA via nvidia-smi; Apple's integrated GPU)."""
    out = _query(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"])
    found = [line.strip() for line in out.splitlines() if line.strip()] if out else []
    if sys.platform == "darwin" and platform.machine() == "arm64":
        found.append("Apple GPU (shares RAM with the CPU)")
    return found


def scheduler(environ: Mapping[str, str]) -> str | None:
    if environ.get("SLURM_JOB_ID"):
        return "slurm (inside a job)"
    if shutil.which("sbatch"):
        return "slurm (sbatch available)"
    return None


@dataclass(frozen=True)
class Hardware:
    os: str
    arch: str
    logical_cpus: int
    usable_cpus: int
    performance_cores: int | None
    memory_gb: float | None
    gpus: Sequence[str]
    scheduler: str | None


def detect(environ: Mapping[str, str]) -> Hardware:
    return Hardware(
        os=f"{platform.system()} {platform.release()}",
        arch=platform.machine(),
        logical_cpus=os.cpu_count() or 1,
        usable_cpus=usable_cpus(environ),
        performance_cores=performance_cores(),
        memory_gb=total_memory_gb(),
        gpus=gpus(),
        scheduler=scheduler(environ),
    )


# ---------------------------------------------------------------------------
# Peak memory of a run
# ---------------------------------------------------------------------------

def children_peak_rss_mb() -> float | None:
    """Peak resident memory of this process's finished children (the solver), in MB.

    Covers the children this process waited for and the descendants they
    waited for. None on Windows, where the resource module does not exist.
    """
    if os.name == "nt":
        return None
    peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    # ru_maxrss is in bytes on macOS and in kilobytes on Linux.
    return peak / 2**20 if sys.platform == "darwin" else peak / 2**10


# ---------------------------------------------------------------------------
# Machine settings (~/.autoresearch/machine.json)
# ---------------------------------------------------------------------------

def read_settings(machine_dir: Path) -> dict:
    path = machine_dir / MACHINE_FILE
    return json.loads(path.read_text()) if path.exists() else {}


def write_settings(machine_dir: Path, updates: Mapping[str, object]) -> dict:
    settings = {**read_settings(machine_dir), **{k: v for k, v in updates.items() if v is not None}}
    machine_dir.mkdir(parents=True, exist_ok=True)
    (machine_dir / MACHINE_FILE).write_text(json.dumps(settings, indent=1) + "\n")
    return settings


# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModeCost:
    """What one run of a (campaign, mode) costs, measured from its recorded runs."""

    mode: str
    runs: int
    median_seconds: float
    peak_memory_gb: float | None
    threads: int


def mode_costs(runs: Sequence[Mapping], threads_flag: str | None) -> list[ModeCost]:
    """Per-mode cost from run views with `elapsed`, `peak_rss_mb` and `params`.

    Crashes are left out when a mode has other runs (they often die early and
    understate the cost).
    """
    by_mode: dict[str, list[Mapping]] = {}
    for r in runs:
        by_mode.setdefault(r["mode"], []).append(r)
    costs = []
    for mode, group in sorted(by_mode.items()):
        finished = [r for r in group if r["status"] != "crash"] or group
        elapsed = [r["elapsed"] for r in finished if r.get("elapsed") is not None]
        peaks = [r["peak_rss_mb"] for r in finished if r.get("peak_rss_mb") is not None]
        threads = [r["params"].get(threads_flag) for r in finished if threads_flag] if threads_flag else []
        threads = [t for t in threads if isinstance(t, int) and t > 0]
        if not elapsed:
            continue
        costs.append(ModeCost(
            mode=mode,
            runs=len(finished),
            median_seconds=median(elapsed),
            peak_memory_gb=max(peaks) / 1024 if peaks else None,
            threads=max(set(threads), key=threads.count) if threads else 1,
        ))
    return costs


def runs_at_once(usable_cores: int, threads: int, usable_memory_gb: float | None, peak_memory_gb: float | None) -> int:
    """How many runs fit at once: limited by cores per run and, when known, memory per run."""
    by_cores = usable_cores // max(threads, 1)
    by_memory = math.floor(usable_memory_gb / peak_memory_gb) if usable_memory_gb and peak_memory_gb else by_cores
    return max(1, min(by_cores, by_memory))


def batch_size(parallel: int, plan_minutes: float, median_seconds: float) -> int:
    """The largest batch that finishes within one planning interval at `parallel` runs at once."""
    return max(1, math.floor(parallel * plan_minutes * 60 / max(median_seconds, 1e-9)))


def describe_hardware(hw: Hardware) -> list[str]:
    lines = []
    for key, value in asdict(hw).items():
        if key == "gpus":
            value = ", ".join(value) if value else "none detected"
        elif isinstance(value, float):
            value = f"{value:.1f}"
        lines.append(f"{key}: {'unknown' if value is None else value}")
    return lines
