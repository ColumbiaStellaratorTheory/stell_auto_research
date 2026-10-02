"""Installed solver adapters, imported statically.

A campaign's config.json names an adapter by its key in REGISTRY. To install an
adapter, import it here and add one entry (`/setup-harness` does this). Every
adapter listed is imported on every run, so adapters must not read required
environment variables at import time (see contract.py).
"""

from examples.banana import simsopt_banana

from . import toy

REGISTRY = {
    "toy": toy,
    "simsopt_banana": simsopt_banana,
}
