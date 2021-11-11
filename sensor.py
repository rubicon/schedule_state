"""
A sensor that returns a string based on a defined schedule.
"""
from datetime import timedelta, time
import logging
from pprint import pformat
import portion as P

import voluptuous as vol

from homeassistant.components.sensor import PLATFORM_SCHEMA, SensorEntity
from homeassistant.const import (
    CONF_NAME,
    CONF_STATE,
    CONF_CONDITION,
)
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.reload import async_setup_reload_service
from homeassistant.helpers import condition
from homeassistant.util import dt
from homeassistant.exceptions import (
    ConditionError,
    ConditionErrorContainer,
    ConditionErrorIndex,
    HomeAssistantError,
)

_LOGGER = logging.getLogger(__name__)

DOMAIN = "schedule_state"
PLATFORMS = ["sensor"]

DEFAULT_NAME = "Schedule State Sensor"
DEFAULT_STATE = "default"

SCAN_INTERVAL = timedelta(seconds=60)

CONF_EVENTS = "events"
CONF_START = "start"
CONF_END = "end"
CONF_DEFAULT_STATE = "default_state"
CONF_REFRESH = "refresh"

_CONDITION_SCHEMA = vol.All(cv.ensure_list, [cv.CONDITION_SCHEMA])

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_EVENTS): [
            {
                vol.Required(CONF_START): cv.time,
                vol.Optional(CONF_END, default=time.max): cv.time,
                vol.Required(CONF_STATE, default=DEFAULT_STATE): cv.string,
                vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
                vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
                vol.Optional(CONF_CONDITION): _CONDITION_SCHEMA,
            }
        ],
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_DEFAULT_STATE, default=DEFAULT_STATE): cv.string,
        vol.Optional(CONF_REFRESH, default=timedelta(days=1)): cv.time_period_str,
    }
)

# from homeassistant.core import HomeAssistant
# from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
# from homeassistant.helpers.entity_platform import AddEntitiesCallback


async def async_setup_platform(
    hass,  #: HomeAssistant,
    config,  #: ConfigType,
    async_add_entities,  #: AddEntitiesCallback,
    discovery_info,  #: DiscoveryInfoType | None = None,
) -> None:
    """Set up the Schedule Sensor."""

    await async_setup_reload_service(hass, DOMAIN, PLATFORMS)

    events = config.get(CONF_EVENTS)
    name = config.get(CONF_NAME)
    refresh = config.get(CONF_REFRESH)
    data = ScheduleSensorData(hass, events, refresh)
    await data.process_events()

    async_add_entities([ScheduleSensor(hass, data, name)], True)


class ScheduleSensor(SensorEntity):
    """Representation of a sensor that returns a state name based on a predefined schedule."""

    def __init__(self, hass, data, name):
        """Initialize the sensor."""
        self._hass = hass
        self.data = data
        self._attributes = None
        self._name = name
        self._state = None

    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name

    @property
    def native_value(self):
        """Return the state of the device."""
        return self._state

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        return self._attributes

    async def async_update(self) -> None:
        """Get the latest data and updates the state."""
        await self.data.update()
        value = self.data.value

        if value is None:
            value = DEFAULT_STATE
        self._state = value


class ScheduleSensorData:
    """The class for handling the state computation."""

    def __init__(self, hass, events, refresh):
        """Initialize the data object."""
        self.value = None
        self.hass = hass
        self.events = events
        self.refresh = refresh
        self.states = {}
        self.refresh_time = None
        # pprint(events)

    async def process_events(self):
        """Process the list of events and derive the schedule for the day."""
        events = self.events
        states = {}

        for event in events:
            state = event.get("state", "default")
            cond = event.get("condition", None)

            variables = {}
            if cond is not None:
                # print(f"{state}: {cond}")
                cond_func = await _async_process_if(self.hass, event.get("name"), cond)
                if not cond_func(variables):
                    _LOGGER.info(
                        f"{state}: condition was not satisfied, skipping {event}"
                    )
                    continue

            i = P.open(event.get("start"), event.get("end", time.max))
            for xstate in states:
                if xstate == state:
                    continue
                overlap = i & states[xstate]
                if i.overlaps(states[xstate]):
                    _LOGGER.info(f"{state} overlaps with existing {xstate}: {overlap}")
                    states[xstate] -= overlap
                    _LOGGER.info(f"... reducing {xstate} to {states[xstate]}")

            if state not in states:
                states[state] = i
            else:
                states[state] = states[state] | i

        _LOGGER.info(pformat(states))
        self.states = states
        self.refresh_time = dt.as_local(dt.now())

    async def update(self):
        """Get the latest state based on the event schedule."""
        now = dt.as_local(dt.now())
        nu = time(now.hour, now.minute)

        time_since_refresh = now - self.refresh_time
        if time_since_refresh.total_seconds() >= self.refresh.total_seconds():
            await self.process_events()

        for state in self.states:
            if nu in self.states[state]:
                _LOGGER.debug(f"current state is {state} ({nu})")
                self.value = state
                return
        _LOGGER.info(f"current state not found ({nu})")
        self.value = None


async def _async_process_if(hass, name, if_configs):
    """Process if checks."""
    checks = []
    for if_config in if_configs:
        try:
            checks.append(await condition.async_from_config(hass, if_config, False))
        except HomeAssistantError as ex:
            _LOGGER.warning("Invalid condition: %s", ex)
            return None

    def if_action(variables=None):
        """AND all conditions."""
        errors = []
        for index, check in enumerate(checks):
            try:
                # with trace_path(["condition", str(index)]):
                #     if not check(hass, variables):
                #         return False
                if not check(hass, variables):
                    return False
            except ConditionError as ex:
                errors.append(
                    ConditionErrorIndex(
                        "condition", index=index, total=len(checks), error=ex
                    )
                )

        if errors:
            _LOGGER.warning(
                "Error evaluating condition in '%s':\n%s",
                name,
                ConditionErrorContainer("condition", errors=errors),
            )
            return False

        return True

    if_action.config = if_configs

    return if_action
