"""
A sensor that returns a string based on a defined schedule.
"""
import asyncio
from datetime import datetime, time, timedelta
import hashlib
import locale
import logging
from pprint import pformat

from homeassistant.components.sensor import PLATFORM_SCHEMA, SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import (
    ConditionError,
    ConditionErrorContainer,
    ConditionErrorIndex,
    HomeAssistantError,
)
from homeassistant.helpers import condition
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.reload import async_setup_reload_service
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.util import dt
import portion as P
import voluptuous as vol

from .const import *

_LOGGER = logging.getLogger(__name__)

_CONDITION_SCHEMA = vol.All(cv.ensure_list, [cv.CONDITION_SCHEMA])


def unique(*keys):
    def fn(arg):
        seen = []
        for k in keys:
            if k in arg:
                seen.append(k)
        if len(seen) > 1:
            raise vol.Invalid(f"multiple conflicting keys provided: {seen}")
        return arg

    return fn


PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Optional(CONF_EVENTS): [
            vol.All(
                {
                    vol.Optional(CONF_START): cv.time,
                    vol.Optional(CONF_START_TEMPLATE): cv.template,
                    vol.Optional(CONF_END): cv.time,
                    vol.Optional(CONF_END_TEMPLATE): cv.template,
                    vol.Required(CONF_STATE, default=DEFAULT_STATE): cv.string,
                    vol.Optional(CONF_COMMENT): cv.string,
                    vol.Optional(CONF_CONDITION): _CONDITION_SCHEMA,
                    vol.Optional(CONF_ICON): cv.icon,
                },
                unique("start", "start_template"),
                unique("end", "end_template"),
            )
        ],
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_DEFAULT_STATE, default=DEFAULT_STATE): cv.string,
        vol.Optional(CONF_REFRESH, default="6:00:00"): cv.time_period_str,
        vol.Optional(CONF_ICON, default="mdi:calendar-check"): cv.icon,
        vol.Optional(CONF_ERROR_ICON, default="mdi:calendar-alert"): cv.icon,
        vol.Optional(CONF_MINUTES_TO_REFRESH_ON_ERROR, default=5): cv.positive_int,
    }
)

RECALCULATE_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
    }
)

OVERRIDE_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_ids,
        vol.Required(CONF_STATE): cv.string,
        vol.Optional(CONF_DURATION): cv.positive_int,
        vol.Optional(CONF_START): cv.time,
        vol.Optional(CONF_END): cv.time,
        vol.Optional(CONF_ICON): cv.icon,
    }
)

CLEAR_OVERRIDES_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
    }
)


class Override(dict):
    def __init__(self, state, start, end, expires):
        _LOGGER.info(f"New Override: {state} s={start} e={end} expires={expires}")
        self["state"] = state
        self["start"] = start
        self["end"] = end
        self["expires"] = expires


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType = None,
) -> None:
    """Set up the Schedule Sensor."""

    await async_setup_reload_service(hass, DOMAIN, PLATFORMS)

    events = config.get(CONF_EVENTS, [])
    name = config.get(CONF_NAME)
    refresh = config.get(CONF_REFRESH)
    minutes_to_refresh_on_error = config.get(CONF_MINUTES_TO_REFRESH_ON_ERROR)
    data = ScheduleSensorData(name, hass, events, refresh, minutes_to_refresh_on_error)
    await data.process_events()

    def get_target_devices(service):
        if entity_ids := service.data.get(ATTR_ENTITY_ID):
            target_devices = [
                dev
                for dev in hass.data["sensor"].entities
                if dev.entity_id in entity_ids
            ]
        else:
            target_devices = [e for e in hass.data["sensor"].entities]

        return target_devices

    async def async_recalculate_service_handler(service):
        target_devices = get_target_devices(service)
        update_tasks = []
        for target_device in target_devices:
            await target_device.async_recalculate()
            update_tasks.append(target_device.async_update_ha_state(True))

        if update_tasks:
            await asyncio.wait(update_tasks)

    async def async_set_override_service_handler(service):
        target_devices = get_target_devices(service)
        update_tasks = []
        for target_device in target_devices:
            await target_device.async_set_override(
                service.data[CONF_STATE],
                service.data.get(CONF_START, None),
                service.data.get(CONF_END, None),
                service.data.get(CONF_DURATION, None),
                service.data.get(CONF_ICON, None),
            )
            update_tasks.append(target_device.async_update_ha_state(True))

        if update_tasks:
            await asyncio.wait(update_tasks)

    async def async_clear_overrides_service_handler(service):
        target_devices = get_target_devices(service)
        update_tasks = []
        for target_device in target_devices:
            await target_device.async_clear_overrides()
            update_tasks.append(target_device.async_update_ha_state(True))

        if update_tasks:
            await asyncio.wait(update_tasks)

    hass.services.async_register(
        DOMAIN,
        "recalculate",
        async_recalculate_service_handler,
        schema=RECALCULATE_SERVICE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        "set_override",
        async_set_override_service_handler,
        schema=OVERRIDE_SERVICE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        "clear_overrides",
        async_clear_overrides_service_handler,
        schema=CLEAR_OVERRIDES_SERVICE_SCHEMA,
    )

    async_add_entities([ScheduleSensor(hass, name, data, config)], True)


class ScheduleSensor(SensorEntity):
    """Representation of a sensor that returns a state name based on a predefined schedule."""

    def __init__(self, hass, name, data, config):
        """Initialize the sensor."""
        self.data = data
        self._attributes = {}
        self._name = name
        self._state = None
        self._default_state = config.get(CONF_DEFAULT_STATE)
        self._icon = config.get(CONF_ICON)
        self._error_icon = config.get(CONF_ERROR_ICON)

        unique_id = hashlib.sha3_512(name.encode("utf-8")).hexdigest()
        self._attr_unique_id = unique_id

    async def async_added_to_hass(self):
        """Handle added to Hass."""
        await super().async_added_to_hass()

        @callback
        def recalc_callback(*args):
            _LOGGER.debug(f"{self.data.name}: something changed {args}")
            self.data.force_refresh = dt.as_local(dt_now())
            self.schedule_update_ha_state(force_refresh=True)

        if len(self.data.entities):
            _LOGGER.info(
                f"{self.data.name}: installing callback to trigger on changes to {self.data.entities}"
            )
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, self.data.entities, recalc_callback
                )
            )

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
            value = self._default_state
        self._state = value
        self._attributes["states"] = self.data.known_states
        self._attributes["start"] = self.data.attributes.get("start", None)
        self._attributes["end"] = self.data.attributes.get("end", None)
        self._attributes["friendly_start"] = friendly_time(
            self.data.attributes.get("start", None)
        )
        self._attributes["friendly_end"] = friendly_time(
            self.data.attributes.get("end", None)
        )
        self._attributes["errors"] = self.data.error_states
        if len(self.data.error_states):
            self._attr_icon = self._error_icon
        else:
            self._attr_icon = self.data.attributes.get("icon", None) or self._icon

    async def async_recalculate(self):
        """Recalculate schedule state."""
        _LOGGER.info(f"{self._name}: recalculate")
        await self.data.process_events()

    async def async_set_override(self, state: str, start, end, duration, icon):
        """Set override state."""
        _LOGGER.info(
            f"{self._name}: override to {state} for s={start} e={end} d={duration}"
        )
        if self.data.set_override(state, start, end, duration, icon):
            return await self.data.process_events()
        return False

    async def async_clear_overrides(self):
        """Clear overrides, if any."""
        _LOGGER.info(f"{self._name}: clear overrides")
        if self.data.clear_overrides():
            return await self.data.process_events()
        return False


class ScheduleSensorData:
    """The class for handling the state computation."""

    def __init__(self, name, hass, events, refresh, minutes_to_refresh_on_error):
        """Initialize the data object."""
        self.name = name
        self.value = None
        self.hass = hass
        self.events = events
        self.refresh = refresh
        self.minutes_to_refresh_on_error = minutes_to_refresh_on_error
        self.states = {}
        self.refresh_time = None
        self.overrides = []
        self.known_states = set()
        self.error_states = set()
        self.attributes = {}
        self.entities = set()
        self.force_refresh = None
        self.icon_map = {}

    async def process_events(self):
        """Process the list of events and derive the schedule for the day."""
        states = {}

        # keep track of known states and report them in the attributes
        self.known_states = set()

        # keep track of the states with errors and report them in the attributes
        self.error_states = set()

        for event in self.events + self.overrides:
            _LOGGER.debug(f"{self.name}: processing event {event}")
            state = event.get(CONF_STATE, DEFAULT_STATE)
            cond = event.get(CONF_CONDITION, None)
            self.known_states.add(state)

            icon = event.get(CONF_ICON, None)
            if icon is not None:
                self.icon_map[state] = icon

            # Calculate new refresh time to be used if there was a problem evaluating the template or condition.
            # This can happen if the things that the template is dependent on have not been started up by HA yet...
            # or it could be a problem with the template/conditon definition, it doesn't seem possible to know which.
            new_refresh_time = dt.as_local(dt_now()) + timedelta(
                minutes=self.minutes_to_refresh_on_error
            )
            if self.force_refresh is not None:
                force_refresh = min(self.force_refresh, new_refresh_time)
            else:
                force_refresh = new_refresh_time

            variables = {}
            if cond is not None:
                _LOGGER.debug(f"{self.name}: condition {cond}")
                cond_func = await _async_process_if(self.hass, self.name, cond)
                for conf in cond_func.config:
                    referenced = condition.async_extract_entities(conf)
                    _LOGGER.debug(f"... entities used: {referenced}")
                    self.entities.update(referenced)

                cond_result = cond_func(variables)
                if cond_result is False:
                    _LOGGER.debug(
                        f"{self.name}: {state}: condition was not satisfied - skipping"
                    )
                    continue
                elif cond_result is None:
                    # There was a problem evaluating the condition - force a refresh
                    self.force_refresh = force_refresh
                    self.error_states.add(state)
                    _LOGGER.error(
                        f"{self.name}: {state}: error evaluating condition - skipping, will try again in {self.minutes_to_refresh_on_error} minutes"
                    )
                    continue

            start = await self.get_start(event)
            end = await self.get_end(event)
            if None in (start, end):
                # There was a problem evaluating the template - force a refresh
                self.force_refresh = force_refresh
                self.error_states.add(state)
                _LOGGER.error(
                    f"{self.name}: {state}: error with event definition - skipping, will try again in {self.minutes_to_refresh_on_error} minutes"
                )
                continue

            if start > end:
                self.error_states.add(state)
                _LOGGER.error(
                    f"{self.name}: {state}: error with event definition - start:{start} > end:{end} - skipping"
                )
                continue

            i = P.closedopen(start, end)
            for xstate in states:
                if xstate == state:
                    continue
                overlap = i & states[xstate]
                if i.overlaps(states[xstate]):
                    _LOGGER.debug(
                        f"{self.name}: {state} overlaps with existing {xstate}: {overlap}"
                    )
                    states[xstate] -= overlap
                    _LOGGER.debug(
                        f"{self.name}: ... reducing {xstate} to {states[xstate]}"
                    )

            if state not in states:
                states[state] = i
            else:
                states[state] = states[state] | i

        _LOGGER.info(f"{self.name}:\n{pformat(states)}")
        self.states = states
        self.refresh_time = dt.as_local(dt_now())

    async def get_start(self, event):
        return self.evaluate_template(event, CONF_START, CONF_START_TEMPLATE, time.min)

    async def get_end(self, event):
        return self.evaluate_template(event, CONF_END, CONF_END_TEMPLATE, time.max)

    def evaluate_template(self, event, prefix, prefixt, default):
        s = event.get(prefix, None)
        st = event.get(prefixt, None)

        if s is None and st is None:
            _LOGGER.debug(f"{self.name}: ... no {prefix} provided, using default")
            ret = default

        elif s is not None:
            ret = s

        else:
            st.hass = self.hass
            # TODO should be able to combine these two
            try:
                temp = st.async_render_with_possible_json_value(st, time.min)
                info = st.async_render_to_info(None, parse_result=False)
            except (ValueError, TypeError) as e:
                _LOGGER.error(f"{self.name}: ... failed to evaluate {prefixt}: {e}")
                return None

            _LOGGER.debug(
                f"{self.name}: ... {prefixt} text: {temp} -- entities used: {info.entities}"
            )
            for e in info.entities:
                self.entities.add(e)
            ret = self.guess_value(temp)

            if ret is None:
                _LOGGER.error(f"{self.name}: FAILED - could not parse '{temp}'")

        _LOGGER.debug(f"{self.name}: ... >> {prefix} time: {ret}")
        return ret

    def guess_value(self, text):
        """After evaluating a template, try to figure out what the resulting value means.
        We are looking for a time value. Dates don't matter."""
        try:
            date = dt.parse_datetime(text)
            if date is not None:
                _LOGGER.debug(f"{self.name}: ...... found datetime: {date}")
                tme = dt.as_local(date).time()
                return tme
        except (ValueError, TypeError):
            pass

        try:
            date = datetime.fromisoformat(text)
            _LOGGER.debug(f"{self.name}: ...... found isoformat date: {date}")
            tme = dt.as_local(date).time()
            return tme
        except (ValueError, TypeError):
            pass

        try:
            tme = dt.parse_time(text)
            if tme is not None:
                _LOGGER.debug(f"{self.name}: ...... found time: {tme}")
                return localtime_from_time(tme)
        except (ValueError, TypeError):
            pass

        try:
            tme = time.fromisoformat(text)
            if tme is not None:
                _LOGGER.debug(f"{self.name}: ...... found isoformat time: {tme}")
                return localtime_from_time(tme)
        except (ValueError, TypeError):
            pass

        try:
            date = dt.utc_from_timestamp(int(float(text)))
            _LOGGER.debug(f"{self.name}: ...... found timestamp: {date}")
            tme = dt.as_local(date).time()
            return tme
        except:
            pass

        return None

    async def update(self):
        """Get the latest state based on the event schedule."""
        now = dt.as_local(dt_now())
        nu = time(now.hour, now.minute)

        self.overrides = [o for o in self.overrides if dt.as_local(o["expires"]) > now]
        for o in self.overrides:
            _LOGGER.debug(
                f"{self.name}: override = {o['start']} - {o['end']} == {o['state']} [expires {o['expires']}]"
            )

        self.attributes = {}
        time_since_refresh = now - self.refresh_time
        if time_since_refresh.total_seconds() >= self.refresh.total_seconds() or (
            self.force_refresh is not None and now > self.force_refresh
        ):
            await self.process_events()
            self.force_refresh = None

        state, i = self.find_interval(nu)
        if state is not None:
            _LOGGER.debug(f"{self.name}: current state is {state} ({nu})")
            self.attributes["start"] = i.lower
            self.attributes["end"] = i.upper
            self.value = state
            self.attributes["icon"] = self.icon_map.get(state, None)
            if i.upper == time.max:
                # If the interval ends at midnight, peek ahead to the next day.
                # This won't necessarily be right, because the schedule could be recalculated
                # the next day, but it is arguably more useful.
                next_state, next_i = self.find_interval(time.min)
                if next_state == state and next_i is not None:
                    self.attributes["end"] = next_i.upper
        else:
            _LOGGER.debug(f"{self.name}: using default state ({nu})")
            self.value = None

    def find_interval(self, nu):
        for state in self.states:
            if nu in self.states[state]:
                for i in self.states[state]._intervals:
                    if nu >= i.lower and nu < i.upper:
                        return state, i

        return None, None

    def set_override(self, state, start, end, duration, icon):
        now = dt.as_local(dt_now())
        allow_split = True

        if start is None and end is None and duration is None:
            # 000
            _LOGGER.error(
                "override failed: must provide one of start/end/duration"
            )
            return False
        elif start is not None and end is not None and duration is not None:
            # 111
            _LOGGER.error(
                "override failed: cannot provide start+end+duration together"
            )
            return False
        elif start is None and end is None and duration is not None:
            # 001 --> now to now+d
            start = simple_time(now)
            end = start + timedelta(minutes=duration)
        elif start is not None and end is None and duration is not None:
            # 101 --> start to start+d
            start = next_time(now, start)
            end = start + timedelta(minutes=duration)
        elif start is None and end is not None and duration is not None:
            # 011 --> end-d to end
            end = next_time(now, end)
            start = end - timedelta(minutes=duration)
        elif start is None and end is not None and duration is None:
            # 010 --> now to end
            start = next_time(now, now)
            end = next_time(now, end)
        elif start is not None and end is not None and duration is None:
            # 110 --> start to end
            # don't try to fix wraparounds in this case, where both start and end times were provided
            start = next_time(now, start)
            end = next_time(now, end)
            allow_split = False
        else:
            # 100 --> start to ???
            _LOGGER.error("override failed: no duration provided")
            return False

        start = start.time()
        expires = end + timedelta(seconds=30)
        end = end.time()

        if not (start > end):
            pass
        elif start > end and allow_split:
            # split into two overrides if there is a wraparound (eg: 23:55 to 00:10)
            ev = Override(state, start, time.max, start_of_next_day(now))
            _LOGGER.info(f"adding override: {ev} (split)")
            self.overrides.append(ev)
            start = time.min
        else:
            _LOGGER.error(f"override failed: start ({start}) > end ({end})")
            return False

        ev = Override(state, start, end, expires)
        self.overrides.append(ev)
        if icon is not None:
            self.icon_map[state] = icon
        return True

    def clear_overrides(self):
        if len(self.overrides):
            self.overrides = []
            return True
        return False


def simple_time(n: datetime) -> datetime:
    """return n with seconds/microseconds removed"""
    return datetime(
        year=n.year,
        month=n.month,
        day=n.day,
        hour=n.hour,
        minute=n.minute,
        tzinfo=n.tzinfo,
    )


def next_time(now: datetime, t: time) -> datetime:
    '''what is the next datetime that the given time occurs, relative to "now"'''
    if now.hour > t.hour:
        # we have passed the time t, so we need it for tomorrow
        v = start_of_next_day(now)
        return datetime(v.year, v.month, v.day, t.hour, t.minute)
    else:
        # later today
        return datetime(now.year, now.month, now.day, t.hour, t.minute)


def start_of_next_day(d: datetime) -> datetime:
    v = d + timedelta(1)
    return datetime(v.year, v.month, v.day)


def localtime_from_time(tme: time) -> time:
    date = dt_now()
    date = datetime(
        date.year,
        date.month,
        date.day,
        tme.hour,
        tme.minute,
        tme.second,
        tme.microsecond,
        date.tzinfo,
    )
    return dt.as_local(date).time()


def friendly_time(t):
    """Simple time formatting so that you don't have to do it in Lovelace everywhere.
    For more advanced uses, you can use the start/end attributes instead of the friendly versions.
    """
    # TODO translations...
    if t is None:
        return "-"
    elif t == time.max:
        return "midnight"
    return t.strftime(locale.nl_langinfo(locale.T_FMT))


async def _async_process_if(hass, name, if_configs):
    """Process if checks."""
    checks = []
    for if_config in if_configs:
        try:
            checks.append(await condition.async_from_config(hass, if_config))
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
            return None

        return True

    if_action.config = if_configs

    return if_action


def dt_now():
    """Return now(). Tests will override the return value."""
    return dt.now()
