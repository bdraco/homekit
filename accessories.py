"""Extend the basic Accessory and Bridge functions."""
from datetime import timedelta
from functools import partial, wraps
from inspect import getmodule
import logging

from pyhap.accessory import Accessory, Bridge
from pyhap.accessory_driver import AccessoryDriver
from pyhap.const import CATEGORY_OTHER

from homeassistant.const import (
    ATTR_BATTERY_CHARGING,
    ATTR_BATTERY_LEVEL,
    ATTR_ENTITY_ID,
    ATTR_SERVICE,
    __version__,
)
from homeassistant.core import callback as ha_callback, split_entity_id
from homeassistant.helpers.event import (
    async_track_state_change,
    track_point_in_utc_time,
)
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_DISPLAY_NAME,
    ATTR_VALUE,
    BRIDGE_MODEL,
    BRIDGE_SERIAL_NUMBER,
    CHAR_BATTERY_LEVEL,
    CHAR_CHARGING_STATE,
    CHAR_STATUS_LOW_BATTERY,
    CONF_LINKED_BATTERY_SENSOR,
    CONF_LOW_BATTERY_THRESHOLD,
    DEBOUNCE_TIMEOUT,
    DEFAULT_LOW_BATTERY_THRESHOLD,
    EVENT_HOMEKIT_CHANGED,
    MANUFACTURER,
    SERV_BATTERY_SERVICE,
)
from .util import convert_to_float, dismiss_setup_message, show_setup_message

_LOGGER = logging.getLogger(__name__)


def debounce(func):
    """Decorate function to debounce callbacks from HomeKit."""

    @ha_callback
    def call_later_listener(self, *args):
        """Handle call_later callback."""
        debounce_params = self.debounce.pop(func.__name__, None)
        if debounce_params:
            self.hass.async_add_executor_job(func, self, *debounce_params[1:])

    @wraps(func)
    def wrapper(self, *args):
        """Start async timer."""
        debounce_params = self.debounce.pop(func.__name__, None)
        if debounce_params:
            debounce_params[0]()  # remove listener
        remove_listener = track_point_in_utc_time(
            self.hass,
            partial(call_later_listener, self),
            dt_util.utcnow() + timedelta(seconds=DEBOUNCE_TIMEOUT),
        )
        self.debounce[func.__name__] = (remove_listener, *args)
        logger.debug(
            "%s: Start %s timeout", self.entity_id, func.__name__.replace("set_", "")
        )

    name = getmodule(func).__name__
    logger = logging.getLogger(name)
    return wrapper


class HomeAccessory(Accessory):
    """Adapter class for Accessory."""

    def __init__(
        self, hass, driver, name, entity_id, aid, config, category=CATEGORY_OTHER
    ):
        """Initialize a Accessory object."""
        super().__init__(driver, name, aid=aid)
        model = split_entity_id(entity_id)[0].replace("_", " ").title()
        self.set_info_service(
            firmware_revision=__version__,
            manufacturer=MANUFACTURER,
            model=model,
            serial_number=entity_id,
        )
        self.category = category
        self.config = config or {}
        self.entity_id = entity_id
        self.hass = hass
        self.debounce = {}
        self._support_battery_level = False
        self._support_battery_charging = True
        self.linked_battery_sensor = self.config.get(CONF_LINKED_BATTERY_SENSOR)
        self.low_battery_threshold = self.config.get(
            CONF_LOW_BATTERY_THRESHOLD, DEFAULT_LOW_BATTERY_THRESHOLD
        )

        """Add battery service if available"""
        battery_found = self.hass.states.get(self.entity_id).attributes.get(
            ATTR_BATTERY_LEVEL
        )

        if self.linked_battery_sensor:
            state = self.hass.states.get(self.linked_battery_sensor)
            if state is not None:
                battery_found = state.state
            else:
                self.linked_battery_sensor = None
                _LOGGER.warning(
                    "%s: Battery sensor state missing: %s",
                    self.entity_id,
                    self.linked_battery_sensor,
                )

        if battery_found is None:
            return
        _LOGGER.debug("%s: Found battery level", self.entity_id)
        self._support_battery_level = True
        serv_battery = self.add_preload_service(SERV_BATTERY_SERVICE)
        self._char_battery = serv_battery.configure_char(CHAR_BATTERY_LEVEL, value=0)
        self._char_charging = serv_battery.configure_char(CHAR_CHARGING_STATE, value=2)
        self._char_low_battery = serv_battery.configure_char(
            CHAR_STATUS_LOW_BATTERY, value=0
        )

    async def run(self):
        """Handle accessory driver started event.

        Run inside the HAP-python event loop.
        """
        self.hass.add_job(self.run_handler)

    async def run_handler(self):
        """Handle accessory driver started event.

        Run inside the Home Assistant event loop.
        """
        state = self.hass.states.get(self.entity_id)
        self.hass.async_add_job(self.update_state_callback, None, None, state)
        async_track_state_change(self.hass, self.entity_id, self.update_state_callback)

        if self.linked_battery_sensor:
            battery_state = self.hass.states.get(self.linked_battery_sensor)
            self.hass.async_add_job(
                self.update_linked_battery, None, None, battery_state
            )
            async_track_state_change(
                self.hass, self.linked_battery_sensor, self.update_linked_battery
            )

    @ha_callback
    def update_state_callback(self, entity_id=None, old_state=None, new_state=None):
        """Handle state change listener callback."""
        _LOGGER.debug("New_state: %s", new_state)
        if new_state is None:
            return
        if self._support_battery_level and not self.linked_battery_sensor:
            self.hass.async_add_executor_job(self.update_battery, new_state)
        self.hass.async_add_executor_job(self.update_state, new_state)

    @ha_callback
    def update_linked_battery(self, entity_id=None, old_state=None, new_state=None):
        """Handle linked battery sensor state change listener callback."""
        self.hass.async_add_executor_job(self.update_battery, new_state)

    def update_battery(self, new_state):
        """Update battery service if available.

        Only call this function if self._support_battery_level is True.
        """
        battery_level = convert_to_float(new_state.attributes.get(ATTR_BATTERY_LEVEL))
        if self.linked_battery_sensor:
            battery_level = convert_to_float(new_state.state)
        if battery_level is None:
            return
        self._char_battery.set_value(battery_level)
        self._char_low_battery.set_value(battery_level < self.low_battery_threshold)
        _LOGGER.debug("%s: Updated battery level to %d", self.entity_id, battery_level)
        if not self._support_battery_charging:
            return
        charging = new_state.attributes.get(ATTR_BATTERY_CHARGING)
        if charging is None:
            self._support_battery_charging = False
            return
        hk_charging = 1 if charging is True else 0
        self._char_charging.set_value(hk_charging)
        _LOGGER.debug("%s: Updated battery charging to %d", self.entity_id, hk_charging)

    def update_state(self, new_state):
        """Handle state change to update HomeKit value.

        Overridden by accessory types.
        """
        raise NotImplementedError()

    def call_service(self, domain, service, service_data, value=None):
        """Fire event and call service for changes from HomeKit."""
        self.hass.add_job(self.async_call_service, domain, service, service_data, value)

    async def async_call_service(self, domain, service, service_data, value=None):
        """Fire event and call service for changes from HomeKit.

        This method must be run in the event loop.
        """
        event_data = {
            ATTR_ENTITY_ID: self.entity_id,
            ATTR_DISPLAY_NAME: self.display_name,
            ATTR_SERVICE: service,
            ATTR_VALUE: value,
        }

        self.hass.bus.async_fire(EVENT_HOMEKIT_CHANGED, event_data)
        await self.hass.services.async_call(domain, service, service_data)


class HomeBridge(Bridge):
    """Adapter class for Bridge."""

    def __init__(self, hass, driver, name):
        """Initialize a Bridge object."""
        super().__init__(driver, name)
        self.set_info_service(
            firmware_revision=__version__,
            manufacturer=MANUFACTURER,
            model=BRIDGE_MODEL,
            serial_number=BRIDGE_SERIAL_NUMBER,
        )
        self.hass = hass

    def setup_message(self):
        """Prevent print of pyhap setup message to terminal."""


class HomeDriver(AccessoryDriver):
    """Adapter class for AccessoryDriver."""

    def __init__(self, hass, **kwargs):
        """Initialize a AccessoryDriver object."""
        super().__init__(**kwargs)
        self.hass = hass

    def pair(self, client_uuid, client_public):
        """Override super function to dismiss setup message if paired."""
        success = super().pair(client_uuid, client_public)
        if success:
            dismiss_setup_message(self.hass)
        return success

    def unpair(self, client_uuid):
        """Override super function to show setup message if unpaired."""
        super().unpair(client_uuid)
        show_setup_message(self.hass, self.state.pincode)
