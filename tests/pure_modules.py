"""Load the HA-free modules (parameters, lookahead, regulation, autotune,
climate_controllers, activity, preheat, dosing) for tests.

Not a test module.  Loads them as ``roomstat_pure.*`` so relative imports
work without importing the package ``__init__`` (which needs Home Assistant).
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).parent.parent / "custom_components" / "roomstat"
_PKG = "roomstat_pure"


def _load(name: str) -> types.ModuleType:
    full = f"{_PKG}.{name}"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, _ROOT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


if _PKG not in sys.modules:
    pkg = types.ModuleType(_PKG)
    pkg.__path__ = [str(_ROOT)]
    sys.modules[_PKG] = pkg

parameters = _load("parameters")
lookahead = _load("lookahead")
regulation = _load("regulation")
autotune = _load("autotune")
climate_controllers = _load("climate_controllers")
activity = _load("activity")
preheat = _load("preheat")
dosing = _load("dosing")
