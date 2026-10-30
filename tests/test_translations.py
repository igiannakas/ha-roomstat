"""The UI strings: the frontend reads them as ICU messages, where ``<word>``
opens a markup tag (30 Oct 2026: ``sensor.<zone>_heating`` in a help text
showed "Translation error: UNCLOSED_TAG"), so no string may hold one;
``strings.json`` and ``en.json`` are identical, and ``de.json`` has every key."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "roomstat"
_FILES = ["strings.json", "translations/en.json", "translations/de.json"]
_TAG = re.compile(r"</?[A-Za-z][^>]*>?")


def _strings(node, path=""):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _strings(value, f"{path}.{key}" if path else key)
    elif isinstance(node, str):
        yield path, node


def _load(name):
    return json.loads((_ROOT / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", _FILES)
def test_no_string_holds_a_markup_tag(name):
    bad = [(path, text) for path, text in _strings(_load(name)) if _TAG.search(text)]
    assert not bad, bad


def test_strings_and_english_are_identical():
    assert _load("strings.json") == _load("translations/en.json")


def test_german_has_every_key():
    english = {path for path, _ in _strings(_load("strings.json"))}
    german = {path for path, _ in _strings(_load("translations/de.json"))}
    assert english - german == set()
