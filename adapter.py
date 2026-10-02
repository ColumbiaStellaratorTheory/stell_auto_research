"""Solver-adapter loader.

The core never imports a solver directly. A campaign's `config.json` names its
adapter as a module path: a bare name (`toy`) means `adapters.<name>`; a dotted
path (`examples.banana.simsopt_banana`) is imported as given. The module must
implement the contract in `contract.py`; `load_adapter` checks the required
members before the core uses it.
"""

from __future__ import annotations

import importlib
from types import ModuleType

CONTRACT_MEMBERS = (
    "NAME",
    "SOLVER_MODES",
    "TARGET_FLAG",
    "ENV_REQUIREMENTS",
    "add_arguments",
    "run_experiment",
)


class AdapterError(Exception):
    """The named adapter cannot be imported or does not implement the contract."""


def module_path(name: str) -> str:
    """Resolve an adapter name from config.json to an importable module path."""
    return name if "." in name else f"adapters.{name}"


def load_adapter(name: str) -> ModuleType:
    """Import the adapter `name` and verify it implements the contract."""
    path = module_path(name)
    try:
        module = importlib.import_module(path)
    except ImportError as e:
        raise AdapterError(f"cannot import adapter '{path}': {e}") from e

    missing = [m for m in CONTRACT_MEMBERS if not hasattr(module, m)]
    if missing:
        raise AdapterError(
            f"adapter '{path}' does not implement the contract — missing "
            f"{', '.join(missing)} (see contract.py)."
        )
    return module
