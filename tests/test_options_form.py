"""The options form's number fields (config_flow.py needs the real
voluptuous, so it is read here, not run).

The owner, 13 Nov 2026: the Ki field "cannot accept multi-decimal values".
Ki lives around 0.0003-0.001, and with ``step=0.001`` (the finest step Home
Assistant's NumberSelector takes as a number) the frontend refuses a box
value that is not a multiple of it, so 0.00036 could not be typed.
"""
from __future__ import annotations

import ast
from pathlib import Path

_FLOW = Path(__file__).resolve().parent.parent / "custom_components" / "roomstat" / "config_flow.py"


def _selector_step(field: str):
    """The ``step`` of the NumberSelectorConfig that the options form gives
    ``field`` (its vol.Required key, a CONF_* name)."""
    tree = ast.parse(_FLOW.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if (isinstance(key, ast.Call) and key.args
                    and isinstance(key.args[0], ast.Name) and key.args[0].id == field):
                for call in ast.walk(value):
                    if isinstance(call, ast.Call) and getattr(call.func, "attr", "") == "NumberSelectorConfig":
                        for kw in call.keywords:
                            if kw.arg == "step":
                                return ast.literal_eval(kw.value)
    raise AssertionError(f"no number selector for {field}")


def test_ki_takes_any_decimals():
    assert _selector_step("CONF_CORRECTION_KI") == "any"


def test_kp_still_steps_in_tenths():
    assert _selector_step("CONF_CORRECTION_KP") == 0.1
