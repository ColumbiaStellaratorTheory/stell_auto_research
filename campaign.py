"""Which campaign a command acts on, how it is configured, and where its files live.

Covers campaign selection, config.json, the campaign's directory layout, and
the machine-wide run slots. Every environment variable the harness itself
reads is named here.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Mapping, MutableMapping

import machine
from locks import release, try_lock

REPO_ROOT = Path(__file__).resolve().parent
CONFIG_NAME = "config.json"
DB_NAME = "results.db"
KEEP_ARTIFACTS_CHOICES = ("none", "pass", "all")

CAMPAIGN_ENV = "AUTORESEARCH_CAMPAIGN"
CAMPAIGNS_DIR_ENV = "AUTORESEARCH_CAMPAIGNS_DIR"
MAX_PARALLEL_ENV = "AUTORESEARCH_MAX_PARALLEL"
MACHINE_DIR_ENV = "AUTORESEARCH_MACHINE_DIR"
SCRATCH_DIR_ENV = "AUTORESEARCH_SCRATCH_DIR"
KEEP_ARTIFACTS_ENV = "AUTORESEARCH_KEEP_ARTIFACTS"
ARTIFACTS_DIR_ENV = "AUTORESEARCH_ARTIFACTS_DIR"


class HarnessError(Exception):
    """A user-correctable problem that stops run.py with a message (exit 1)."""


class CampaignError(HarnessError):
    """The campaign cannot be selected or its config.json is invalid."""


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CampaignConfig:
    """A campaign's config.json: which adapter it runs, plus default env vars.

    `env` supplies adapter configuration (solver paths, interpreters) without
    shell-specific export syntax; a variable already set in the environment
    takes precedence over the config value. `max_parallel` caps how many of
    the machine's run slots one batch of this campaign uses at once.
    `plan_minutes` is how often the agent plans a new batch; `brief` and
    `status` turn it into a suggested batch size.
    """

    adapter: str
    env: Mapping[str, str]
    max_parallel: int | None = None
    plan_minutes: float | None = None


def campaigns_root(environ: Mapping[str, str]) -> Path:
    return Path(environ.get(CAMPAIGNS_DIR_ENV, str(REPO_ROOT / "campaigns")))


def list_campaigns(root: Path) -> list[str]:
    """Names of the campaigns under `root` (directories holding a config.json), sorted."""
    return sorted(p.parent.name for p in root.glob(f"*/{CONFIG_NAME}"))


def resolve_campaign(name: str | None, root: Path) -> Path:
    """Return the campaign directory for `name`, or the only campaign when unnamed.

    Refuses to guess: an unnamed selection with zero or several campaigns raises
    CampaignError naming the way out.
    """
    existing = list_campaigns(root)
    if name:
        if name not in existing:
            listing = ", ".join(existing) or "none"
            raise CampaignError(
                f"no campaign '{name}' (expected {root / name / CONFIG_NAME}); "
                f"existing campaigns: {listing}"
            )
        return root / name
    if len(existing) == 1:
        return root / existing[0]
    if not existing:
        raise CampaignError(
            f"no campaign found under {root}. Run /setup-harness, or create "
            f"{root}/<name>/{CONFIG_NAME} with {{\"adapter\": \"toy\"}}."
        )
    raise CampaignError(
        f"{len(existing)} campaigns exist ({', '.join(existing)}); "
        f"name one with --campaign or ${CAMPAIGN_ENV}."
    )


def load_config(campaign_dir: Path) -> CampaignConfig:
    """Parse and validate a campaign's config.json."""
    path = campaign_dir / CONFIG_NAME
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise CampaignError(f"cannot read {path}: {e}") from e
    if not isinstance(raw, dict):
        raise CampaignError(f"{path}: top level must be a JSON object")
    adapter_name = raw.get("adapter")
    if not isinstance(adapter_name, str) or not adapter_name:
        raise CampaignError(f"{path}: \"adapter\" must be a non-empty string")
    env = raw.get("env", {})
    if not isinstance(env, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in env.items()
    ):
        raise CampaignError(f"{path}: \"env\" must map names to string values")
    max_parallel = raw.get("max_parallel")
    if max_parallel is not None and (isinstance(max_parallel, bool) or not isinstance(max_parallel, int) or max_parallel < 1):
        raise CampaignError(f"{path}: \"max_parallel\" must be an integer >= 1")
    plan_minutes = raw.get("plan_minutes")
    if plan_minutes is not None and (isinstance(plan_minutes, bool) or not isinstance(plan_minutes, (int, float)) or plan_minutes <= 0):
        raise CampaignError(f"{path}: \"plan_minutes\" must be a number > 0")
    return CampaignConfig(adapter=adapter_name, env=env, max_parallel=max_parallel, plan_minutes=plan_minutes)


def apply_env(defaults: Mapping[str, str], environ: MutableMapping[str, str]) -> None:
    """Set each config env var that the environment does not already define."""
    for key, value in defaults.items():
        environ.setdefault(key, value)


def check_required_env(active: ModuleType, environ: Mapping[str, str], campaign_dir: Path) -> None:
    """Refuse to start a run when the adapter's REQUIRED_ENV is incomplete."""
    missing = [name for name in active.REQUIRED_ENV if not environ.get(name)]
    if missing:
        raise HarnessError(
            f"adapter '{active.NAME}' needs {', '.join(missing)}: add to the \"env\" map in "
            f"{campaign_dir / CONFIG_NAME} or set in the shell."
        )


# ---------------------------------------------------------------------------
# Directory layout
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Layout:
    """Where one campaign's records, scratch, evidence, and kept artifacts live.

    scratch_dir: live run dirs, one per run id ($AUTORESEARCH_SCRATCH_DIR,
        default <campaign>/scratch).
    artifacts_dir: where kept run dirs land, named by run id
        ($AUTORESEARCH_ARTIFACTS_DIR, default <campaign>/artifacts).
    keep_artifacts: retention for completed runs ($AUTORESEARCH_KEEP_ARTIFACTS)
        — "none" discards the run dir after recording, "pass" keeps passing
        runs, "all" keeps every run. Evidence files are kept regardless.
    """

    campaign_dir: Path
    scratch_dir: Path
    artifacts_dir: Path
    keep_artifacts: str

    @property
    def runs_dir(self) -> Path:
        return self.campaign_dir / "runs"

    @property
    def blobs_dir(self) -> Path:
        return self.campaign_dir / "blobs"

    @property
    def claims_dir(self) -> Path:
        return self.campaign_dir / "claims"

    @property
    def batches_dir(self) -> Path:
        return self.campaign_dir / "batches"

    @property
    def db_path(self) -> Path:
        return self.campaign_dir / DB_NAME


def resolve_layout(campaign_dir: Path, environ: Mapping[str, str]) -> Layout:
    keep = environ.get(KEEP_ARTIFACTS_ENV, "none")
    if keep not in KEEP_ARTIFACTS_CHOICES:
        print(f"WARNING: unknown {KEEP_ARTIFACTS_ENV} '{keep}', using 'none'", file=sys.stderr)
        keep = "none"
    return Layout(
        campaign_dir=campaign_dir,
        scratch_dir=Path(environ.get(SCRATCH_DIR_ENV, str(campaign_dir / "scratch"))),
        artifacts_dir=Path(environ.get(ARTIFACTS_DIR_ENV, str(campaign_dir / "artifacts"))),
        keep_artifacts=keep,
    )


# ---------------------------------------------------------------------------
# Machine-wide run slots
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Slots:
    """The machine-wide run-slot pool: `capacity` lock files under `dir`.

    Every run, from any campaign, holds one slot while it executes, so the
    machine never runs more than `capacity` experiments at once.
    """

    dir: Path
    capacity: int


def machine_dir(environ: Mapping[str, str]) -> Path:
    """Where machine-wide state lives: machine.json and the slot locks (~/.autoresearch)."""
    return Path(environ.get(MACHINE_DIR_ENV, str(Path.home() / ".autoresearch")))


def resolve_slots(environ: Mapping[str, str]) -> Slots:
    """Slots under <machine dir>/slots; capacity $AUTORESEARCH_MAX_PARALLEL, else machine.json's max_parallel, else 1."""
    configured = machine.read_settings(machine_dir(environ)).get("max_parallel", 1)
    raw = environ.get(MAX_PARALLEL_ENV, str(configured))
    if not raw.isdigit() or int(raw) < 1:
        raise HarnessError(f"run slots must be an integer >= 1, got {raw!r} (${MAX_PARALLEL_ENV} or machine.json)")
    return Slots(machine_dir(environ) / "slots", int(raw))


def busy_slots(slots: Slots) -> int:
    """How many of the machine's slots are held right now."""
    busy = 0
    for index in range(slots.capacity):
        fd = try_lock(slots.dir / f"slot-{index}.lock")
        if fd is None:
            busy += 1
        else:
            release(fd)
    return busy
