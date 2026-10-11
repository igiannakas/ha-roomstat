"""Repairs for entities that were deleted, renamed or disabled.

Runs the real repairs.py (check + fix flow) on a stubbed Home Assistant: the
issue and entity registries, the repairs flow base class, selectors and (when
it is not installed) voluptuous.
"""
from __future__ import annotations

import asyncio
import enum
import importlib.util
import sys
import types

import pytest

from tests.ha_harness import _ROOT, e2e

pytestmark = e2e

TRV = "climate.trv"
ROOM = "sensor.room"
WINDOW = "binary_sensor.window"


def _install_stubs() -> None:
    def mod(name, **attrs):
        m = sys.modules.get(name) or types.ModuleType(name)
        m.__dict__.update(attrs)
        sys.modules[name] = m
        parent, _, child = name.rpartition(".")
        if parent in sys.modules:
            setattr(sys.modules[parent], child, m)
        return m

    try:
        import voluptuous  # noqa: F401
    except ImportError:
        class _Marker:
            def __init__(self, schema):
                self.schema = schema

        class Required(_Marker):
            pass

        class Optional(_Marker):
            pass

        class Schema:
            def __init__(self, schema):
                self.schema = schema

        mod("voluptuous", Schema=Schema, Required=Required, Optional=Optional)

    class IssueSeverity(str, enum.Enum):  # noqa: UP042
        ERROR = "error"
        WARNING = "warning"

    def create_issue(hass, domain, issue_id, **kw):
        hass.issues[(domain, issue_id)] = kw

    def delete_issue(hass, domain, issue_id):
        hass.issues.pop((domain, issue_id), None)

    mod("homeassistant.helpers.issue_registry", IssueSeverity=IssueSeverity,
        async_create_issue=create_issue, async_delete_issue=delete_issue)
    mod("homeassistant.helpers.entity_registry",
        async_get=lambda hass: hass.entity_registry)
    mod("homeassistant.helpers.selector",
        EntitySelector=lambda config: ("entity", config),
        EntitySelectorConfig=lambda **kw: dict(kw))

    class RepairsFlow:
        hass = None

        def async_show_form(self, **kw):
            return {"type": "form", **kw}

        def async_create_entry(self, *, data, **_):
            return {"type": "create_entry", "data": data}

        def async_abort(self, *, reason):
            return {"type": "abort", "reason": reason}

    mod("homeassistant.components.repairs", RepairsFlow=RepairsFlow)


def _load_repairs():
    _install_stubs()
    name = "roomstat_e2e.repairs"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _ROOT / "repairs.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


repairs = _load_repairs() if "roomstat_e2e" in sys.modules else None


class _RegEntry:
    def __init__(self, disabled_by=None):
        self.disabled_by = disabled_by


class _Entry:
    def __init__(self, data=None, options=None):
        self.entry_id = "e1"
        self.title = "Bedroom"
        self.data = data if data is not None else {
            "source_entity_id": TRV, "external_temperature_entity_id": ROOM,
        }
        self.options = options if options is not None else {}


class _Hass:
    def __init__(self, entry):
        self.is_running = True
        self.issues: dict = {}
        self._states: dict = {}
        self._registry: dict = {}
        self.entity_registry = types.SimpleNamespace(async_get=self._registry.get)
        self.states = types.SimpleNamespace(get=self._states.get)
        self.entries = {entry.entry_id: entry}
        self.config_entries = types.SimpleNamespace(
            async_get_entry=self.entries.get,
            async_update_entry=self._update_entry,
        )

    def present(self, *entity_ids, registered=True, state="20.5"):
        for eid in entity_ids:
            self._states[eid] = types.SimpleNamespace(state=state)
            if registered:
                self._registry[eid] = _RegEntry()

    def remove(self, entity_id):
        self._states.pop(entity_id, None)
        self._registry.pop(entity_id, None)

    def disable(self, entity_id):
        self._states.pop(entity_id, None)
        self._registry[entity_id] = _RegEntry(disabled_by="user")

    def _update_entry(self, entry, *, data=None, options=None):
        if data is not None:
            entry.data = data
        if options is not None:
            entry.options = options
        return True


def _setup(options=None):
    entry = _Entry(options=options)
    hass = _Hass(entry)
    hass.present(TRV, ROOM, WINDOW)
    watch = repairs.MissingEntityWatch(300)
    return hass, entry, watch


def _check(hass, entry, watch, now):
    repairs.async_check_entities(hass, entry, watch, now)


def _issue(hass, key):
    return hass.issues.get(("roomstat", f"e1_{key}"))


def _check_unavailable(hass, entry, watch, now):
    repairs.async_check_entities(hass, entry, watch, now, unavailable_minutes=15)


def _setup_unavailable(options=None):
    hass, entry, _ = _setup(options)
    return hass, entry, repairs.MissingEntityWatch(300, {"unavailable": 900})


class TestCheck:

    def test_all_fine(self):
        hass, entry, watch = _setup({"window_sensor_id": WINDOW})
        _check(hass, entry, watch, 1000)
        assert hass.issues == {}

    def test_deleted_trv_reported_after_the_delay(self):
        hass, entry, watch = _setup()
        hass.remove(TRV)
        _check(hass, entry, watch, 1000)
        assert hass.issues == {}
        _check(hass, entry, watch, 1300)
        issue = _issue(hass, "source_entity_id")
        assert issue["translation_key"] == "entity_missing"
        assert issue["severity"] == "error"
        assert issue["is_fixable"] is True
        assert issue["translation_placeholders"] == {
            "room": "Bedroom", "entity_id": TRV, "minutes": "15"}
        assert issue["data"] == {"entry_id": "e1", "key": "source_entity_id"}

    def test_disabled_sensor(self):
        hass, entry, watch = _setup()
        hass.disable(ROOM)
        _check(hass, entry, watch, 1000)
        _check(hass, entry, watch, 1300)
        assert _issue(hass, "external_temperature_entity_id")["translation_key"] == (
            "entity_disabled")

    def test_optional_setting_is_a_warning(self):
        hass, entry, watch = _setup({"window_sensor_id": WINDOW})
        hass.remove(WINDOW)
        _check(hass, entry, watch, 1000)
        _check(hass, entry, watch, 1300)
        assert _issue(hass, "window_sensor_id")["severity"] == "warning"

    def test_registered_but_loading_is_not_reported(self):
        hass, entry, watch = _setup()
        hass._states.pop(TRV)
        _check(hass, entry, watch, 1000)
        _check(hass, entry, watch, 5000)
        assert hass.issues == {}

    def test_not_while_ha_is_starting(self):
        hass, entry, watch = _setup()
        hass.remove(TRV)
        hass.is_running = False
        _check(hass, entry, watch, 1000)
        _check(hass, entry, watch, 2000)
        assert hass.issues == {}

    def test_cleared_when_back(self):
        hass, entry, watch = _setup()
        hass.remove(TRV)
        _check(hass, entry, watch, 1000)
        _check(hass, entry, watch, 1300)
        assert _issue(hass, "source_entity_id")
        hass.present(TRV)
        _check(hass, entry, watch, 1360)
        assert hass.issues == {}

    def test_kept_across_a_reload(self):
        """A reload starts a new watch; the repair stays while it waits."""
        hass, entry, watch = _setup()
        hass.remove(TRV)
        _check(hass, entry, watch, 1000)
        _check(hass, entry, watch, 1300)
        watch = repairs.MissingEntityWatch(300)
        _check(hass, entry, watch, 1400)
        assert _issue(hass, "source_entity_id")

    def test_setting_removed_clears_its_repair(self):
        hass, entry, watch = _setup({"window_sensor_id": WINDOW})
        hass.remove(WINDOW)
        _check(hass, entry, watch, 1000)
        _check(hass, entry, watch, 1300)
        entry.options = {}
        _check(hass, entry, watch, 1360)
        assert hass.issues == {}

    def test_delete_all(self):
        hass, entry, watch = _setup()
        hass.remove(TRV)
        hass.remove(ROOM)
        _check(hass, entry, watch, 1000)
        _check(hass, entry, watch, 1300)
        assert len(hass.issues) == 2
        repairs.async_delete_entity_issues(hass, "e1")
        assert hass.issues == {}


def _flow(hass, key):
    flow = asyncio.run(repairs.async_create_fix_flow(
        hass, f"e1_{key}", {"entry_id": "e1", "key": key}))
    flow.hass = hass
    return flow


class TestRoomSensorUnavailable:

    def test_reported_after_15_minutes(self):
        hass, entry, watch = _setup_unavailable()
        hass.present(ROOM, state="unavailable")
        _check_unavailable(hass, entry, watch, 1000)
        _check_unavailable(hass, entry, watch, 1800)
        assert hass.issues == {}
        _check_unavailable(hass, entry, watch, 1900)
        issue = _issue(hass, "external_temperature_entity_id")
        assert issue["translation_key"] == "entity_unavailable"
        assert issue["is_fixable"] is False
        assert issue["severity"] == "error"
        assert issue["translation_placeholders"] == {
            "room": "Bedroom", "entity_id": ROOM, "minutes": "15"}

    @pytest.mark.parametrize("value", ["unknown", "not a number", "85.0"])
    def test_no_usable_value_counts(self, value):
        hass, entry, watch = _setup_unavailable()
        hass.present(ROOM, state=value)
        _check_unavailable(hass, entry, watch, 1000)
        _check_unavailable(hass, entry, watch, 1900)
        assert _issue(hass, "external_temperature_entity_id")

    def test_cleared_when_it_reports_again(self):
        hass, entry, watch = _setup_unavailable()
        hass.present(ROOM, state="unavailable")
        _check_unavailable(hass, entry, watch, 1000)
        _check_unavailable(hass, entry, watch, 1900)
        hass.present(ROOM, state="19.8")
        _check_unavailable(hass, entry, watch, 1960)
        assert hass.issues == {}

    def test_other_entities_unavailable_are_not_reported(self):
        """A valve that drops off has the fallback and its own alert; window
        and presence sensors are optional."""
        hass, entry, watch = _setup_unavailable({"window_sensor_id": WINDOW})
        hass.present(TRV, WINDOW, state="unavailable")
        _check_unavailable(hass, entry, watch, 1000)
        _check_unavailable(hass, entry, watch, 5000)
        assert hass.issues == {}


class TestFixFlow:

    def test_form_asks_for_a_climate_entity(self):
        hass, entry, _ = _setup()
        result = asyncio.run(_flow(hass, "source_entity_id").async_step_init())
        assert result["type"] == "form" and result["step_id"] == "replace"
        (field, sel), = result["data_schema"].schema.items()
        assert type(field).__name__ == "Required"
        assert sel == ("entity", {"domain": ["climate"]})
        assert result["description_placeholders"] == {"room": "Bedroom", "entity_id": TRV}

    def test_room_sensor_form_keeps_the_device_class(self):
        hass, entry, _ = _setup()
        result = asyncio.run(
            _flow(hass, "external_temperature_entity_id").async_step_init())
        (_, sel), = result["data_schema"].schema.items()
        assert sel == ("entity", {"domain": ["sensor"], "device_class": "temperature"})

    def test_replacing_the_trv_updates_the_entry(self):
        hass, entry, _ = _setup()
        result = asyncio.run(_flow(hass, "source_entity_id").async_step_replace(
            {"source_entity_id": "climate.new_trv"}))
        assert result["type"] == "create_entry"
        assert entry.data["source_entity_id"] == "climate.new_trv"

    def test_optional_setting_left_empty_is_removed(self):
        hass, entry, _ = _setup({"window_sensor_id": WINDOW, "comfort_target": 21})
        flow = _flow(hass, "window_sensor_id")
        result = asyncio.run(flow.async_step_init())
        (field, _), = result["data_schema"].schema.items()
        assert type(field).__name__ == "Optional"
        asyncio.run(flow.async_step_replace({}))
        assert entry.options == {"comfort_target": 21}

    def test_room_removed_meanwhile(self):
        hass, entry, _ = _setup()
        hass.entries.clear()
        result = asyncio.run(_flow(hass, "source_entity_id").async_step_init())
        assert result == {"type": "abort", "reason": "entry_removed"}


@pytest.mark.parametrize("lang", ["strings.json", "translations/de.json"])
def test_every_setting_has_a_label(lang):
    import json

    issues = json.loads((_ROOT / lang).read_text())["issues"]
    for key in ("entity_missing", "entity_disabled"):
        labels = issues[key]["fix_flow"]["step"]["replace"]["data"]
        assert set(labels) == set(repairs.ROLE_BY_KEY)
    unavailable = issues["entity_unavailable"]
    assert {"{room}", "{entity_id}"} <= {
        w for w in unavailable["title"].replace(":", " ").split() if w.startswith("{")}
    assert "{minutes}" in unavailable["description"]
