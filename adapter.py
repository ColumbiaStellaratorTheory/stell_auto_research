"""Solver-adapter lookup.

The core never imports a solver directly. A campaign's `config.json` names its
adapter by key in `adapters.REGISTRY` (static imports, see
`adapters/__init__.py`); `load_adapter` checks that the adapter implements the
contract in `contract.py` before the core uses it.
"""

from __future__ import annotations

from types import ModuleType

from adapters import REGISTRY

CONTRACT_MEMBERS = (
    "NAME",
    "SOLVER_MODES",
    "TARGET_FLAG",
    "REQUIRED_ENV",
    "OPTIONAL_ENV",
    "EXECUTION_FLAGS",
    "SEED_FLAG",
    "REPLAY_TOLERANCE",
    "add_arguments",
    "solver_identity",
    "run_experiment",
)


class AdapterError(Exception):
    """The named adapter is not registered or does not implement the contract."""


def load_adapter(name: str, registry: dict[str, ModuleType] = REGISTRY) -> ModuleType:
    """Return the registered adapter `name`, verified against the contract."""
    module = registry.get(name)
    if module is None:
        installed = ", ".join(sorted(registry)) or "none"
        raise AdapterError(
            f"no adapter '{name}' in adapters/__init__.py REGISTRY (installed: {installed})"
        )
    missing = [m for m in CONTRACT_MEMBERS if not hasattr(module, m)]
    if missing:
        raise AdapterError(
            f"adapter '{name}' does not implement the contract — missing "
            f"{', '.join(missing)} (see contract.py)."
        )
    return module
