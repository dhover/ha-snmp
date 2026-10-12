"""Support for displaying collected data over SNMP."""

import asyncio
import logging
from struct import unpack
from typing import override
from datetime import timedelta

# pylint: disable=import-error
# pyright: reportMissingImports=false, reportMissingModuleSource=false

import probatio
from pyasn1.codec.ber import decoder
from pysnmp.error import PySnmpError
import pysnmp.hlapi.v3arch.asyncio as hlapi
from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData,
    Udp6TransportTarget,
    UdpTransportTarget,
    UsmUserData,
    get_cmd,
)
from pysnmp.proto.rfc1902 import Opaque
from pysnmp.proto.rfc1905 import NoSuchObject

from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.components.sensor import (
    CONF_STATE_CLASS,
    PLATFORM_SCHEMA as SENSOR_PLATFORM_SCHEMA,
)
from homeassistant.const import (
    CONF_DEVICE_CLASS,
    CONF_HOST,
    CONF_ICON,
    CONF_NAME,
    CONF_PORT,
    CONF_UNIQUE_ID,
    CONF_UNIT_OF_MEASUREMENT,
    CONF_USERNAME,
    CONF_VALUE_TEMPLATE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.template import Template
from homeassistant.helpers.trigger_template_entity import (
    CONF_AVAILABILITY,
    CONF_PICTURE,
    TEMPLATE_SENSOR_BASE_SCHEMA,
    ManualTriggerSensorEntity,
    ValueTemplate,
)
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

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
    DEFAULT_RETRIES,
    DEFAULT_TIMEOUT,
    DEFAULT_VERSION,
    DOMAIN,
    MAP_AUTH_PROTOCOLS,
    MAP_PRIV_PROTOCOLS,
    SNMP_VERSIONS,
)
from .util import async_create_request_cmd_args

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=10)

TRIGGER_ENTITY_OPTIONS = (
    CONF_AVAILABILITY,
    CONF_DEVICE_CLASS,
    CONF_ICON,
    CONF_PICTURE,
    CONF_UNIQUE_ID,
    CONF_STATE_CLASS,
    CONF_UNIT_OF_MEASUREMENT,
)

PLATFORM_SCHEMA = SENSOR_PLATFORM_SCHEMA.extend(
    {
        probatio.Required(CONF_BASEOID): cv.string,
        probatio.Optional(CONF_ACCEPT_ERRORS, default=False): cv.boolean,
        probatio.Optional(CONF_COMMUNITY, default=DEFAULT_COMMUNITY): cv.string,
        probatio.Optional(CONF_DEFAULT_VALUE): cv.string,
        probatio.Optional(CONF_HOST, default=DEFAULT_HOST): cv.string,
        probatio.Optional(CONF_PORT, default=DEFAULT_PORT): probatio.Port(),
        probatio.Optional(CONF_VALUE_TEMPLATE): probatio.All(
            cv.template, ValueTemplate.from_template
        ),
        probatio.Optional(CONF_VERSION, default=DEFAULT_VERSION): probatio.In(
            SNMP_VERSIONS
        ),
        probatio.Optional(CONF_USERNAME): cv.string,
        probatio.Optional(probatio.Secret(CONF_AUTH_KEY)): cv.string,
        probatio.Optional(
            CONF_AUTH_PROTOCOL, default=DEFAULT_AUTH_PROTOCOL
        ): probatio.In(MAP_AUTH_PROTOCOLS),
        probatio.Optional(probatio.Secret(CONF_PRIV_KEY)): cv.string,
        probatio.Optional(
            CONF_PRIV_PROTOCOL, default=DEFAULT_PRIV_PROTOCOL
        ): probatio.In(MAP_PRIV_PROTOCOLS),
    }
).extend(TEMPLATE_SENSOR_BASE_SCHEMA.schema)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the SNMP sensor, importing the YAML configuration when possible."""
    if await _async_import_yaml(hass, config):
        return
    await async_setup_sensors(hass, config, [config], async_add_entities)


IMPORT_DEVICE_KEYS = (
    CONF_HOST,
    CONF_PORT,
    CONF_VERSION,
    CONF_COMMUNITY,
    CONF_USERNAME,
    CONF_AUTH_KEY,
    CONF_AUTH_PROTOCOL,
    CONF_PRIV_KEY,
    CONF_PRIV_PROTOCOL,
)
IMPORT_SENSOR_KEYS = (
    CONF_BASEOID,
    CONF_ACCEPT_ERRORS,
    CONF_DEFAULT_VALUE,
    CONF_DEVICE_CLASS,
    CONF_STATE_CLASS,
    CONF_UNIT_OF_MEASUREMENT,
    CONF_UNIQUE_ID,
    CONF_ICON,
)
# Options a config entry cannot store; sensors using them stay on YAML
IMPORT_UNSUPPORTED_KEYS = (CONF_VALUE_TEMPLATE, CONF_PICTURE, CONF_AVAILABILITY)


async def _async_import_yaml(hass: HomeAssistant, config: ConfigType) -> bool:
    """Import a YAML sensor into a config entry. Return True when imported."""
    host = config.get(CONF_HOST)
    if any(config.get(key) is not None for key in IMPORT_UNSUPPORTED_KEYS):
        _LOGGER.warning(
            "SNMP YAML sensor %s for %s uses a template option that cannot be "
            "imported into the UI; it stays configured in YAML",
            config.get(CONF_NAME),
            host,
        )
        return False

    name = config.get(CONF_NAME)
    if isinstance(name, Template):
        name = name.template
    device = {k: config[k] for k in IMPORT_DEVICE_KEYS if config.get(k) is not None}
    device[CONF_PORT] = int(device[CONF_PORT])
    device[CONF_NAME] = f"SNMP {host}"
    sensor = {k: config[k] for k in IMPORT_SENSOR_KEYS if config.get(k) is not None}
    sensor[CONF_NAME] = name or DEFAULT_NAME

    # Serialize imports so sensors of one host end up in the same entry
    lock = hass.data.setdefault(f"{DOMAIN}_import_lock", asyncio.Lock())
    async with lock:
        _LOGGER.debug(
            "Importing YAML sensor %s (%s:%s, OID %s)",
            sensor[CONF_NAME],
            host,
            device[CONF_PORT],
            sensor[CONF_BASEOID],
        )
        await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_IMPORT},
            data={**device, CONF_SENSORS: [sensor]},
        )
    _LOGGER.warning(
        "The SNMP YAML configuration for %s (OID %s) was imported into the UI; "
        "you can remove it from configuration.yaml",
        host,
        sensor[CONF_BASEOID],
    )
    return True


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SNMP sensors from a config entry."""
    device_info = DeviceInfo(
        identifiers={(DOMAIN, entry.unique_id)},
        name=entry.data[CONF_NAME],
    )
    await async_setup_sensors(
        hass,
        entry.data,
        entry.options.get(CONF_SENSORS, []),
        async_add_entities,
        device_info,
        f"{DOMAIN}_{entry.unique_id.replace(':', '_')}",
    )


async def async_setup_sensors(
    hass: HomeAssistant,
    device_config: ConfigType,
    sensor_configs: list[ConfigType],
    async_add_entities: AddEntitiesCallback,
    device_info: DeviceInfo | None = None,
    unique_id_prefix: str | None = None,
) -> None:
    """Create SNMP sensors that share a device connection."""
    if not sensor_configs:
        _LOGGER.debug("No SNMP sensors configured, nothing to add")
        return

    host = device_config.get(CONF_HOST)
    port = device_config.get(CONF_PORT)
    community = device_config.get(CONF_COMMUNITY)
    version = device_config[CONF_VERSION]
    username = device_config.get(CONF_USERNAME)
    authkey = device_config.get(CONF_AUTH_KEY)
    authproto = device_config[CONF_AUTH_PROTOCOL]
    privkey = device_config.get(CONF_PRIV_KEY)
    privproto = device_config[CONF_PRIV_PROTOCOL]

    _LOGGER.debug(
        "Setting up %d SNMP sensor(s) for %s:%s (version %s, community length %s, "
        "has surrounding whitespace %s, timeout %s, retries %s)",
        len(sensor_configs),
        host,
        port,
        version,
        len(community) if community else 0,
        bool(community) and community != community.strip(),
        DEFAULT_TIMEOUT,
        DEFAULT_RETRIES,
    )

    try:
        # Try IPv4 first.
        target = await UdpTransportTarget.create(
            (host, port), timeout=DEFAULT_TIMEOUT, retries=DEFAULT_RETRIES
        )
    except PySnmpError:
        # Then try IPv6.
        try:
            target = Udp6TransportTarget(
                (host, port), timeout=DEFAULT_TIMEOUT, retries=DEFAULT_RETRIES
            )
        except PySnmpError as err:
            _LOGGER.error("Invalid SNMP host %s:%s: %s", host, port, err)
            return

    if version == "3":
        if not authkey:
            authproto = "none"
        if not privkey:
            privproto = "none"
        auth_data = UsmUserData(
            username,
            authKey=authkey or None,
            privKey=privkey or None,
            authProtocol=getattr(hlapi, MAP_AUTH_PROTOCOLS[authproto]),
            privProtocol=getattr(hlapi, MAP_PRIV_PROTOCOLS[privproto]),
        )
    else:
        auth_data = CommunityData(community, mpModel=SNMP_VERSIONS[version])

    entities = []
    for sensor_config in sensor_configs:
        baseoid: str = sensor_config[CONF_BASEOID]
        _LOGGER.debug(
            "Creating SNMP sensor %s for %s:%s OID %s (unique_id prefix %s)",
            sensor_config.get(CONF_NAME),
            host,
            port,
            baseoid,
            unique_id_prefix,
        )
        request_args = await async_create_request_cmd_args(
            hass, auth_data, target, baseoid
        )
        name = sensor_config.get(CONF_NAME, Template(DEFAULT_NAME, hass))
        if isinstance(name, str):
            name = Template(name, hass)
        trigger_entity_config = {CONF_NAME: name}
        for key in TRIGGER_ENTITY_OPTIONS:
            if key in sensor_config:
                trigger_entity_config[key] = sensor_config[key]
        if device_info and CONF_UNIQUE_ID not in trigger_entity_config:
            prefix = unique_id_prefix or f"{DOMAIN}_{host.casefold()}_{port}"
            trigger_entity_config[CONF_UNIQUE_ID] = f"{prefix}_{baseoid}"

        value_template: ValueTemplate | None = sensor_config.get(CONF_VALUE_TEMPLATE)
        data = SnmpData(
            request_args,
            baseoid,
            sensor_config.get(CONF_ACCEPT_ERRORS, False),
            sensor_config.get(CONF_DEFAULT_VALUE),
            f"{host}:{port}",
        )
        entity = SnmpSensor(
            hass, data, trigger_entity_config, value_template, device_info
        )
        entities.append(entity)
    _LOGGER.debug("Adding %d SNMP sensor entities for %s:%s", len(entities), host, port)
    async_add_entities(entities)


class SnmpSensor(ManualTriggerSensorEntity):
    """Representation of a SNMP sensor."""

    _attr_should_poll = True

    def __init__(
        self,
        hass: HomeAssistant,
        data: "SnmpData",
        config: ConfigType,
        value_template: ValueTemplate | None,
        device_info: DeviceInfo | None = None,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(hass, config)
        self.data = data
        self._state = None
        self._value_template = value_template
        self._attr_device_info = device_info

    @override
    async def async_added_to_hass(self) -> None:
        """Handle adding to Home Assistant."""
        await super().async_added_to_hass()
        await self.async_update()

    async def async_update(self) -> None:
        """Get the latest data and updates the states."""
        await self.data.async_update()

        variables = self._template_variables_with_value(self.data.value)
        raw_value = self.data.value
        value = raw_value
        if raw_value is None:
            # None gives an unknown state; the string "unknown" breaks numeric sensors
            value = None
        elif self._value_template is not None:
            value = self._value_template.async_render_as_value_template(
                self.entity_id, variables, STATE_UNKNOWN
            )
        _LOGGER.debug(
            "Sensor %s (%s, OID %s): raw value=%r, value template=%s, final value=%r",
            self.entity_id,
            self.data.host,
            self.data.baseoid,
            raw_value,
            self._value_template,
            value,
        )

        self._set_native_value_with_possible_timestamp(value)
        self._process_manual_data(variables)


class SnmpData:
    """Get the latest data and update the states."""

    def __init__(
        self, request_args, baseoid, accept_errors, default_value, host=None
    ) -> None:
        """Initialize the data object."""
        self._request_args = request_args
        self._baseoid = baseoid
        self.baseoid = baseoid
        self._accept_errors = accept_errors
        self._default_value = default_value
        self.host = host
        self.value = None

    async def async_update(self):
        """Get the latest data from the remote SNMP capable host."""

        _LOGGER.debug("Polling %s for OID %s", self.host, self._baseoid)
        get_result = await get_cmd(*self._request_args)
        errindication, errstatus, errindex, restable = get_result
        _LOGGER.debug(
            "SNMP response from %s for OID %s: errindication=%s errstatus=%s "
            "errindex=%s rows=%s accept_errors=%s default_value=%s",
            self.host,
            self._baseoid,
            errindication,
            errstatus.prettyPrint() if errstatus else errstatus,
            errindex,
            [[var.prettyPrint() for var in row] for row in restable]
            if restable
            else restable,
            self._accept_errors,
            self._default_value,
        )

        if errindication and not self._accept_errors:
            _LOGGER.error(
                "SNMP error from %s for OID %s: %s",
                self.host,
                self._baseoid,
                errindication,
            )
        elif errstatus and not self._accept_errors:
            _LOGGER.error(
                "SNMP error from %s: %s at %s",
                self.host,
                errstatus.prettyPrint(),
                restable[-1][int(errindex) - 1] if errindex else "?",
            )
        elif (errindication or errstatus) and self._accept_errors:
            _LOGGER.debug(
                "SNMP %s OID %s: error ignored (accept_errors), using default value %s",
                self.host,
                self._baseoid,
                self._default_value,
            )
            self.value = self._default_value
        else:
            for resrow in restable:
                self.value = self._decode_value(resrow[-1])

    def _decode_value(self, value):
        """Decode the different results we could get into strings."""

        _LOGGER.debug(
            "SNMP OID %s received type=%s and data %s",
            self._baseoid,
            type(value),
            value,
        )
        if isinstance(value, NoSuchObject):
            _LOGGER.error(
                "SNMP error for OID %s: No Such Object currently exists at this OID",
                self._baseoid,
            )
            return self._default_value

        if isinstance(value, Opaque):
            # Float data type is not supported by the pyasn1 library,
            # so we need to decode this type ourselves based on:
            # https://tools.ietf.org/html/draft-perkins-opaque-01
            if bytes(value).startswith(b"\x9f\x78"):
                return str(unpack("!f", bytes(value)[3:])[0])
            # Otherwise Opaque types should be asn1 encoded
            try:
                decoded_value, _ = decoder.decode(bytes(value))
                return str(decoded_value)
            except Exception as decode_exception:  # noqa: BLE001
                _LOGGER.error(
                    "SNMP error in decoding opaque type: %s", decode_exception
                )
                return self._default_value
        return str(value)
