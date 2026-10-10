"""Repairs for configured entities that were deleted, renamed or disabled.

A room set up with an entity that no longer exists would otherwise just wait
("waiting_for_sensors"), or silently lose a feature (window, presence,
schedule, summer, fallback), with nothing in the UI to say why.  The
coordinator calls ``async_check_entities`` every update; an entity that has
been a problem for ``ENTITY_MISSING_REPORT_S`` since HA started is reported
as a repair under Settings → Repairs.  Its fix flow picks the entity to use
instead (or, for an optional setting, none) and reloads the room.  The
repair disappears by itself once the entity is back.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import selector

from .climate_controllers import MissingEntityWatch, entity_problem, replace_entity_setting
from .const import (
    CONF_EXTERNAL_TEMPERATURE_ENTITY_ID,
    CONF_FALLBACK_ENTITY_ID,
    CONF_PRESENCE_SENSOR_ID,
    CONF_SCHEDULE_ENTITY_ID,
    CONF_SOURCE_ENTITY_ID,
    CONF_SUMMER_MODE_ENTITY_ID,
    CONF_WINDOW_SENSOR_ID,
    DOMAIN,
)


@dataclass(frozen=True)
class EntityRole:
    """A setting that names an entity, with what the config flow accepts."""

    key: str
    domains: tuple[str, ...]
    required: bool = False
    in_data: bool = False            # stored in the entry data, not options
    device_class: str | None = None


ROLES: tuple[EntityRole, ...] = (
    EntityRole(CONF_SOURCE_ENTITY_ID, ("climate",), required=True, in_data=True),
    EntityRole(
        CONF_EXTERNAL_TEMPERATURE_ENTITY_ID, ("sensor",), required=True,
        device_class="temperature",
    ),
    EntityRole(CONF_FALLBACK_ENTITY_ID, ("climate",)),
    EntityRole(CONF_WINDOW_SENSOR_ID, ("binary_sensor",)),
    EntityRole(CONF_PRESENCE_SENSOR_ID, ("binary_sensor", "input_boolean")),
    EntityRole(CONF_SUMMER_MODE_ENTITY_ID, ("input_boolean", "binary_sensor")),
    EntityRole(CONF_SCHEDULE_ENTITY_ID, ("input_select", "select")),
)
ROLE_BY_KEY = {role.key: role for role in ROLES}


def configured_entities(entry: ConfigEntry) -> dict[str, str]:
    """Setting key -> entity id, for every entity setting in use."""
    out: dict[str, str] = {}
    for role in ROLES:
        if role.in_data:
            entity_id = entry.data.get(role.key)
        else:
            entity_id = entry.options.get(role.key, entry.data.get(role.key))
        if entity_id:
            out[role.key] = entity_id
    return out


def issue_id(entry_id: str, key: str) -> str:
    return f"{entry_id}_{key}"


def async_check_entities(
    hass: HomeAssistant, entry: ConfigEntry, watch: MissingEntityWatch, now: float
) -> None:
    """Raise or clear the repairs for this room's entity settings."""
    if not hass.is_running:
        return
    registry = er.async_get(hass)
    configured = configured_entities(entry)
    problems: dict[str, str | None] = {}
    for key, entity_id in configured.items():
        reg_entry = registry.async_get(entity_id)
        problems[key] = entity_problem(
            has_state=hass.states.get(entity_id) is not None,
            registered=reg_entry is not None,
            disabled=reg_entry is not None and reg_entry.disabled_by is not None,
        )
    report = watch.update(now, problems)
    for role in ROLES:
        iid = issue_id(entry.entry_id, role.key)
        if role.key in report:
            ir.async_create_issue(
                hass,
                DOMAIN,
                iid,
                is_fixable=True,
                is_persistent=False,
                severity=(
                    ir.IssueSeverity.ERROR if role.required else ir.IssueSeverity.WARNING
                ),
                translation_key=f"entity_{report[role.key]}",
                translation_placeholders={
                    "room": entry.title,
                    "entity_id": configured[role.key],
                },
                data={"entry_id": entry.entry_id, "key": role.key},
            )
        elif problems.get(role.key) is None:
            # Fine again, or no longer configured.  (A problem still within
            # the delay keeps an existing repair, e.g. across a reload.)
            ir.async_delete_issue(hass, DOMAIN, iid)


def async_delete_entity_issues(hass: HomeAssistant, entry_id: str) -> None:
    """Remove every repair of a room (it was removed or disabled)."""
    for role in ROLES:
        ir.async_delete_issue(hass, DOMAIN, issue_id(entry_id, role.key))


class ReplaceEntityFlow(RepairsFlow):
    """Pick the entity a room should use instead of one that is gone."""

    def __init__(self, entry_id: str, key: str) -> None:
        self._entry_id = entry_id
        self._key = key

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        return await self.async_step_replace()

    async def async_step_replace(self, user_input: dict[str, Any] | None = None):
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        role = ROLE_BY_KEY.get(self._key)
        if entry is None or role is None:
            return self.async_abort(reason="entry_removed")

        if user_input is not None:
            data, options = replace_entity_setting(
                entry.data,
                entry.options,
                role.key,
                user_input.get(role.key) or None,
                in_data=role.in_data,
            )
            # The entry's update listener reloads the room.
            self.hass.config_entries.async_update_entry(entry, data=data, options=options)
            return self.async_create_entry(data={})

        config: dict[str, Any] = {"domain": list(role.domains)}
        if role.device_class:
            config["device_class"] = role.device_class
        field = vol.Required(role.key) if role.required else vol.Optional(role.key)
        return self.async_show_form(
            step_id="replace",
            data_schema=vol.Schema(
                {field: selector.EntitySelector(selector.EntitySelectorConfig(**config))}
            ),
            description_placeholders={
                "room": entry.title,
                "entity_id": configured_entities(entry).get(role.key, ""),
            },
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    """Create the fix flow for one of Roomstat's entity repairs."""
    data = data or {}
    return ReplaceEntityFlow(str(data.get("entry_id", "")), str(data.get("key", "")))
