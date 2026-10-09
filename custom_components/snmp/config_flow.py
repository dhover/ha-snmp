"""Config flow for the SNMP integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PORT,
    CONF_UNIT_OF_MEASUREMENT,
    CONF_USERNAME,
)
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_ACCEPT_ERRORS,
    CONF_AUTH_KEY,
    CONF_AUTH_PROTOCOL,
    CONF_BASEOID,
    CONF_COMMUNITY,
    CONF_DEFAULT_VALUE,
    CONF_PRIV_KEY,
    CONF_PRIV_PROTOCOL,
    CONF_SENSORS,
    CONF_VERSION,
    DEFAULT_AUTH_PROTOCOL,
    DEFAULT_COMMUNITY,
    DEFAULT_HOST,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DEFAULT_PRIV_PROTOCOL,
    DEFAULT_VERSION,
    DOMAIN,
    MAP_AUTH_PROTOCOLS,
    MAP_PRIV_PROTOCOLS,
    SNMP_VERSIONS,
)

_LOGGER = logging.getLogger(__name__)

CONF_DEVICE_CLASS = "device_class"
CONF_STATE_CLASS = "state_class"


def _device_schema() -> vol.Schema:
    """Return the SNMP device configuration schema."""
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=DEFAULT_NAME): cv.string,
            vol.Required(CONF_HOST, default=DEFAULT_HOST): cv.string,
            vol.Required(CONF_PORT, default=int(DEFAULT_PORT)): cv.port,
            vol.Required(CONF_VERSION, default=DEFAULT_VERSION): SelectSelector(
                SelectSelectorConfig(
                    options=list(SNMP_VERSIONS),
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(CONF_COMMUNITY, default=DEFAULT_COMMUNITY): cv.string,
            vol.Optional(CONF_USERNAME): cv.string,
            vol.Optional(CONF_AUTH_KEY): cv.string,
            vol.Optional(
                CONF_AUTH_PROTOCOL, default=DEFAULT_AUTH_PROTOCOL
            ): SelectSelector(
                SelectSelectorConfig(
                    options=list(MAP_AUTH_PROTOCOLS),
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(CONF_PRIV_KEY): cv.string,
            vol.Optional(
                CONF_PRIV_PROTOCOL, default=DEFAULT_PRIV_PROTOCOL
            ): SelectSelector(
                SelectSelectorConfig(
                    options=list(MAP_PRIV_PROTOCOLS),
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
        }
    )


def _sensor_schema() -> vol.Schema:
    """Return the sensor configuration schema."""
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=DEFAULT_NAME): cv.string,
            vol.Required(CONF_BASEOID): cv.string,
            vol.Optional(CONF_ACCEPT_ERRORS, default=False): cv.boolean,
            vol.Optional(CONF_DEFAULT_VALUE): cv.string,
            vol.Optional(CONF_DEVICE_CLASS): cv.string,
            vol.Optional(CONF_STATE_CLASS): cv.string,
            vol.Optional(CONF_UNIT_OF_MEASUREMENT): cv.string,
        }
    )


class SnmpConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for SNMP devices."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._device: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Configure the SNMP device connection."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input[CONF_VERSION] == "3" and not user_input.get(CONF_USERNAME):
                errors["base"] = "username_required"
            else:
                self._device = user_input
                await self.async_set_unique_id(
                    f"{user_input[CONF_HOST].casefold()}:{user_input[CONF_PORT]}"
                )
                self._abort_if_unique_id_configured()
                return await self.async_step_sensor()

        return self.async_show_form(
            step_id="user",
            data_schema=_device_schema(),
            errors=errors,
        )

    async def async_step_sensor(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Configure the first sensor for this device."""
        if user_input is not None:
            _LOGGER.debug(
                "Adding first sensor %s (OID %s) to new device %s",
                user_input[CONF_NAME],
                user_input[CONF_BASEOID],
                self._device[CONF_NAME],
            )
            return self.async_create_entry(
                title=self._device[CONF_NAME],
                data=self._device,
                options={CONF_SENSORS: [user_input]},
            )

        return self.async_show_form(step_id="sensor", data_schema=_sensor_schema())

    async def async_step_import(self, import_data: dict[str, Any]) -> dict[str, Any]:
        """Import a YAML sensor, merging it into an existing device entry."""
        device = {k: v for k, v in import_data.items() if k != CONF_SENSORS}
        sensors = import_data[CONF_SENSORS]
        unique_id = f"{device[CONF_HOST].casefold()}:{device[CONF_PORT]}"
        await self.async_set_unique_id(unique_id)

        entry = self.hass.config_entries.async_entry_for_domain_unique_id(
            DOMAIN, unique_id
        )
        if entry is not None:
            existing = list(entry.options.get(CONF_SENSORS, []))
            known = {sensor[CONF_BASEOID] for sensor in existing}
            new = [s for s in sensors if s[CONF_BASEOID] not in known]
            if new:
                _LOGGER.debug("Adding %d imported sensor(s) to %s", len(new), unique_id)
                self.hass.config_entries.async_update_entry(
                    entry, options={**entry.options, CONF_SENSORS: existing + new}
                )
            return self.async_abort(reason="already_configured")

        _LOGGER.debug("Creating device %s from YAML import", unique_id)
        return self.async_create_entry(
            title=device[CONF_NAME],
            data=device,
            options={CONF_SENSORS: sensors},
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Get the options flow for this handler."""
        return SnmpOptionsFlow()


class SnmpOptionsFlow(config_entries.OptionsFlow):
    """Manage sensors attached to an SNMP device."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Choose a sensor management action."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["edit_device", "add_sensor", "remove_sensor"],
        )

    async def async_step_edit_device(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Edit the connection settings of the device."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host_port = f"{user_input[CONF_HOST].casefold()}:{user_input[CONF_PORT]}"
            if user_input[CONF_VERSION] == "3" and not user_input.get(CONF_USERNAME):
                errors["base"] = "username_required"
            elif any(
                other.entry_id != self.config_entry.entry_id
                and f"{other.data[CONF_HOST].casefold()}:{other.data[CONF_PORT]}"
                == host_port
                for other in self.hass.config_entries.async_entries(DOMAIN)
            ):
                errors["base"] = "already_configured"
            else:
                _LOGGER.debug(
                    "Updating device %s (%s:%s, version %s)",
                    user_input[CONF_NAME],
                    user_input[CONF_HOST],
                    user_input[CONF_PORT],
                    user_input[CONF_VERSION],
                )
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    title=user_input[CONF_NAME],
                    data=user_input,
                )
                return self.async_create_entry(
                    title="",
                    data=dict(self.config_entry.options),
                )

        return self.async_show_form(
            step_id="edit_device",
            data_schema=self.add_suggested_values_to_schema(
                _device_schema(), user_input or self.config_entry.data
            ),
            errors=errors,
        )

    async def async_step_add_sensor(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Add a sensor to the configured device."""
        errors: dict[str, str] = {}
        sensors = list(self.config_entry.options.get(CONF_SENSORS, []))
        if user_input is not None:
            _LOGGER.debug(
                "Add sensor requested for entry %s: %s",
                self.config_entry.entry_id,
                user_input,
            )
            if any(
                sensor[CONF_BASEOID] == user_input[CONF_BASEOID]
                for sensor in sensors
            ):
                _LOGGER.debug(
                    "Sensor with OID %s already exists for entry %s",
                    user_input[CONF_BASEOID],
                    self.config_entry.entry_id,
                )
                errors["base"] = "sensor_exists"
            else:
                sensors.append(user_input)
                _LOGGER.debug(
                    "Saving %d sensor(s) for entry %s",
                    len(sensors),
                    self.config_entry.entry_id,
                )
                return self.async_create_entry(
                    title="",
                    data={CONF_SENSORS: sensors},
                )

        return self.async_show_form(
            step_id="add_sensor",
            data_schema=_sensor_schema(),
            errors=errors,
        )

    async def async_step_remove_sensor(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Remove a sensor from the configured device."""
        sensors = list(self.config_entry.options.get(CONF_SENSORS, []))
        if user_input is not None:
            sensors = [
                sensor
                for sensor in sensors
                if sensor[CONF_BASEOID] != user_input[CONF_BASEOID]
            ]
            return self.async_create_entry(
                title="",
                data={CONF_SENSORS: sensors},
            )

        if not sensors:
            return self.async_abort(reason="no_sensors")

        sensor_choices = {
            sensor[CONF_BASEOID]: sensor[CONF_NAME] for sensor in sensors
        }
        return self.async_show_form(
            step_id="remove_sensor",
            data_schema=vol.Schema(
                {vol.Required(CONF_BASEOID): vol.In(sensor_choices)}
            ),
        )
