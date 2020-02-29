"""Class to hold all thermostat accessories."""
import logging

from pyhap.const import CATEGORY_THERMOSTAT

from homeassistant.components.climate.const import (
    ATTR_CURRENT_TEMPERATURE,
    ATTR_HVAC_ACTION,
    ATTR_HVAC_MODE,
    ATTR_HVAC_MODES,
    ATTR_MAX_TEMP,
    ATTR_MIN_TEMP,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    ATTR_TARGET_TEMP_STEP,
    CURRENT_HVAC_COOL,
    CURRENT_HVAC_HEAT,
    CURRENT_HVAC_IDLE,
    CURRENT_HVAC_OFF,
    DEFAULT_MAX_TEMP,
    DEFAULT_MIN_TEMP,
    DOMAIN as DOMAIN_CLIMATE,
    HVAC_MODE_AUTO,
    HVAC_MODE_COOL,
    HVAC_MODE_FAN_ONLY,
    HVAC_MODE_HEAT,
    HVAC_MODE_HEAT_COOL,
    HVAC_MODE_OFF,
    SERVICE_SET_HVAC_MODE as SERVICE_SET_HVAC_MODE_THERMOSTAT,
    SERVICE_SET_TEMPERATURE as SERVICE_SET_TEMPERATURE_THERMOSTAT,
    SUPPORT_TARGET_TEMPERATURE_RANGE,
)
from homeassistant.components.water_heater import (
    DOMAIN as DOMAIN_WATER_HEATER,
    SERVICE_SET_TEMPERATURE as SERVICE_SET_TEMPERATURE_WATER_HEATER,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_SUPPORTED_FEATURES,
    ATTR_TEMPERATURE,
    TEMP_CELSIUS,
    TEMP_FAHRENHEIT,
)

from . import TYPES
from .accessories import HomeAccessory, debounce
from .const import (
    CHAR_COOLING_THRESHOLD_TEMPERATURE,
    CHAR_CURRENT_HEATING_COOLING,
    CHAR_CURRENT_TEMPERATURE,
    CHAR_HEATING_THRESHOLD_TEMPERATURE,
    CHAR_TARGET_HEATING_COOLING,
    CHAR_TARGET_TEMPERATURE,
    CHAR_TEMP_DISPLAY_UNITS,
    DEFAULT_MAX_TEMP_WATER_HEATER,
    DEFAULT_MIN_TEMP_WATER_HEATER,
    PROP_MAX_VALUE,
    PROP_MIN_STEP,
    PROP_MIN_VALUE,
    SERV_THERMOSTAT,
)
from .util import temperature_to_homekit, temperature_to_states

_LOGGER = logging.getLogger(__name__)

HC_HOMEKIT_VALID_MODES_WATER_HEATER = {
    "Heat": 1,
}
UNIT_HASS_TO_HOMEKIT = {TEMP_CELSIUS: 0, TEMP_FAHRENHEIT: 1}
UNIT_HOMEKIT_TO_HASS = {c: s for s, c in UNIT_HASS_TO_HOMEKIT.items()}
HC_HASS_TO_HOMEKIT = {
    HVAC_MODE_OFF: 0,
    HVAC_MODE_HEAT: 1,
    HVAC_MODE_COOL: 2,
    HVAC_MODE_AUTO: 3,
    HVAC_MODE_HEAT_COOL: 3,
    HVAC_MODE_FAN_ONLY: 2,
}
HC_HOMEKIT_TO_HASS = {c: s for s, c in HC_HASS_TO_HOMEKIT.items()}

HC_HASS_TO_HOMEKIT_ACTION = {
    CURRENT_HVAC_OFF: 0,
    CURRENT_HVAC_IDLE: 0,
    CURRENT_HVAC_HEAT: 1,
    CURRENT_HVAC_COOL: 2,
}


@TYPES.register("Thermostat")
class Thermostat(HomeAccessory):
    """Generate a Thermostat accessory for a climate."""

    def __init__(self, *args):
        """Initialize a Thermostat accessory object."""
        super().__init__(*args, category=CATEGORY_THERMOSTAT)
        self._unit = self.hass.config.units.temperature_unit
        self._flag_heat_cool = False
        self._flag_temperature = False
        self._flag_coolingthresh = False
        self._flag_heatingthresh = False
        min_temp, max_temp = self.get_temperature_range()
        temp_step = self.hass.states.get(self.entity_id).attributes.get(
            ATTR_TARGET_TEMP_STEP, 0.5
        )

        # Add additional characteristics if auto mode is supported
        self.chars = []
        state = self.hass.states.get(self.entity_id)
        features = state.attributes.get(ATTR_SUPPORTED_FEATURES, 0)

        if features & SUPPORT_TARGET_TEMPERATURE_RANGE:
            self.chars.extend(
                (CHAR_COOLING_THRESHOLD_TEMPERATURE, CHAR_HEATING_THRESHOLD_TEMPERATURE)
            )

        serv_thermostat = self.add_preload_service(SERV_THERMOSTAT, self.chars)

        # Current mode characteristics
        self.char_current_heat_cool = serv_thermostat.configure_char(
            CHAR_CURRENT_HEATING_COOLING, value=0
        )

        # Target mode characteristics
        hc_modes = state.attributes.get(ATTR_HVAC_MODES, None)
        if hc_modes is None:
            _LOGGER.error(
                "%s: HVAC modes not yet available. Please disable auto start for homekit.",
                self.entity_id,
            )
            hc_modes = (
                HVAC_MODE_HEAT,
                HVAC_MODE_COOL,
                HVAC_MODE_HEAT_COOL,
                HVAC_MODE_OFF,
            )

        # determine available modes for this entity, prefer AUTO over HEAT_COOL and COOL over FAN_ONLY
        self.hc_homekit_to_hass = {
            c: s
            for s, c in HC_HASS_TO_HOMEKIT.items()
            if (
                s in hc_modes
                and not (
                    (s == HVAC_MODE_HEAT_COOL and HVAC_MODE_AUTO in hc_modes)
                    or (s == HVAC_MODE_FAN_ONLY and HVAC_MODE_COOL in hc_modes)
                )
            )
        }
        hc_valid_values = {k: v for v, k in self.hc_homekit_to_hass.items()}

        self.char_target_heat_cool = serv_thermostat.configure_char(
            CHAR_TARGET_HEATING_COOLING,
            value=0,
            setter_callback=self.set_heat_cool,
            valid_values=hc_valid_values,
        )

        # Current and target temperature characteristics
        self.char_current_temp = serv_thermostat.configure_char(
            CHAR_CURRENT_TEMPERATURE, value=21.0
        )
        self.char_target_temp = serv_thermostat.configure_char(
            CHAR_TARGET_TEMPERATURE,
            value=21.0,
            properties={
                PROP_MIN_VALUE: min_temp,
                PROP_MAX_VALUE: max_temp,
                PROP_MIN_STEP: temp_step,
            },
            setter_callback=self.set_target_temperature,
        )

        # Display units characteristic
        self.char_display_units = serv_thermostat.configure_char(
            CHAR_TEMP_DISPLAY_UNITS, value=0
        )

        # If the device supports it: high and low temperature characteristics
        self.char_cooling_thresh_temp = None
        self.char_heating_thresh_temp = None
        if CHAR_COOLING_THRESHOLD_TEMPERATURE in self.chars:
            self.char_cooling_thresh_temp = serv_thermostat.configure_char(
                CHAR_COOLING_THRESHOLD_TEMPERATURE,
                value=23.0,
                properties={
                    PROP_MIN_VALUE: min_temp,
                    PROP_MAX_VALUE: max_temp,
                    PROP_MIN_STEP: temp_step,
                },
                setter_callback=self.set_cooling_threshold,
            )
        if CHAR_HEATING_THRESHOLD_TEMPERATURE in self.chars:
            self.char_heating_thresh_temp = serv_thermostat.configure_char(
                CHAR_HEATING_THRESHOLD_TEMPERATURE,
                value=19.0,
                properties={
                    PROP_MIN_VALUE: min_temp,
                    PROP_MAX_VALUE: max_temp,
                    PROP_MIN_STEP: temp_step,
                },
                setter_callback=self.set_heating_threshold,
            )

    def get_temperature_range(self):
        """Return min and max temperature range."""
        max_temp = self.hass.states.get(self.entity_id).attributes.get(ATTR_MAX_TEMP)
        max_temp = (
            temperature_to_homekit(max_temp, self._unit)
            if max_temp
            else DEFAULT_MAX_TEMP
        )
        max_temp = round(max_temp * 2) / 2

        min_temp = self.hass.states.get(self.entity_id).attributes.get(ATTR_MIN_TEMP)
        min_temp = (
            temperature_to_homekit(min_temp, self._unit)
            if min_temp
            else DEFAULT_MIN_TEMP
        )
        min_temp = round(min_temp * 2) / 2

        return min_temp, max_temp

    def set_heat_cool(self, value):
        """Change operation mode to value if call came from HomeKit."""
        _LOGGER.debug("%s: Set heat-cool to %d", self.entity_id, value)
        self._flag_heat_cool = True
        hass_value = self.hc_homekit_to_hass[value]
        params = {ATTR_ENTITY_ID: self.entity_id, ATTR_HVAC_MODE: hass_value}
        self.call_service(
            DOMAIN_CLIMATE, SERVICE_SET_HVAC_MODE_THERMOSTAT, params, hass_value
        )

    @debounce
    def set_cooling_threshold(self, value):
        """Set cooling threshold temp to value if call came from HomeKit."""
        _LOGGER.debug(
            "%s: Set cooling threshold temperature to %.1f°C", self.entity_id, value
        )
        self._flag_coolingthresh = True
        low = self.char_heating_thresh_temp.value
        temperature = temperature_to_states(value, self._unit)
        params = {
            ATTR_ENTITY_ID: self.entity_id,
            ATTR_TARGET_TEMP_HIGH: temperature,
            ATTR_TARGET_TEMP_LOW: temperature_to_states(low, self._unit),
        }
        self.call_service(
            DOMAIN_CLIMATE,
            SERVICE_SET_TEMPERATURE_THERMOSTAT,
            params,
            f"cooling threshold {temperature}{self._unit}",
        )

    @debounce
    def set_heating_threshold(self, value):
        """Set heating threshold temp to value if call came from HomeKit."""
        _LOGGER.debug(
            "%s: Set heating threshold temperature to %.1f°C", self.entity_id, value
        )
        self._flag_heatingthresh = True
        high = self.char_cooling_thresh_temp.value
        temperature = temperature_to_states(value, self._unit)
        params = {
            ATTR_ENTITY_ID: self.entity_id,
            ATTR_TARGET_TEMP_HIGH: temperature_to_states(high, self._unit),
            ATTR_TARGET_TEMP_LOW: temperature,
        }
        self.call_service(
            DOMAIN_CLIMATE,
            SERVICE_SET_TEMPERATURE_THERMOSTAT,
            params,
            f"heating threshold {temperature}{self._unit}",
        )

    @debounce
    def set_target_temperature(self, value):
        """Set target temperature to value if call came from HomeKit."""
        _LOGGER.debug("%s: Set target temperature to %.1f°C", self.entity_id, value)
        self._flag_temperature = True
        temperature = temperature_to_states(value, self._unit)
        params = {ATTR_ENTITY_ID: self.entity_id, ATTR_TEMPERATURE: temperature}
        self.call_service(
            DOMAIN_CLIMATE,
            SERVICE_SET_TEMPERATURE_THERMOSTAT,
            params,
            f"{temperature}{self._unit}",
        )

    def update_state(self, new_state):
        """Update thermostat state after state changed."""
        # Update current temperature
        current_temp = new_state.attributes.get(ATTR_CURRENT_TEMPERATURE)
        if isinstance(current_temp, (int, float)):
            current_temp = temperature_to_homekit(current_temp, self._unit)
            self.char_current_temp.set_value(current_temp)

        # Update target temperature
        target_temp = new_state.attributes.get(ATTR_TEMPERATURE)
        if isinstance(target_temp, (int, float)):
            target_temp = temperature_to_homekit(target_temp, self._unit)
            if not self._flag_temperature:
                self.char_target_temp.set_value(target_temp)
        self._flag_temperature = False

        # Update cooling threshold temperature if characteristic exists
        if self.char_cooling_thresh_temp:
            cooling_thresh = new_state.attributes.get(ATTR_TARGET_TEMP_HIGH)
            if isinstance(cooling_thresh, (int, float)):
                cooling_thresh = temperature_to_homekit(cooling_thresh, self._unit)
                if not self._flag_coolingthresh:
                    self.char_cooling_thresh_temp.set_value(cooling_thresh)
        self._flag_coolingthresh = False

        # Update heating threshold temperature if characteristic exists
        if self.char_heating_thresh_temp:
            heating_thresh = new_state.attributes.get(ATTR_TARGET_TEMP_LOW)
            if isinstance(heating_thresh, (int, float)):
                heating_thresh = temperature_to_homekit(heating_thresh, self._unit)
                if not self._flag_heatingthresh:
                    self.char_heating_thresh_temp.set_value(heating_thresh)
        self._flag_heatingthresh = False

        # Update display units
        if self._unit and self._unit in UNIT_HASS_TO_HOMEKIT:
            self.char_display_units.set_value(UNIT_HASS_TO_HOMEKIT[self._unit])

        # Update target operation mode
        hvac_mode = new_state.state
        if hvac_mode and hvac_mode in HC_HASS_TO_HOMEKIT:
            if not self._flag_heat_cool:
                self.char_target_heat_cool.set_value(HC_HASS_TO_HOMEKIT[hvac_mode])
        self._flag_heat_cool = False

        # Set current operation mode for supported thermostats
        hvac_action = new_state.attributes.get(ATTR_HVAC_ACTION)
        if hvac_action:
            self.char_current_heat_cool.set_value(
                HC_HASS_TO_HOMEKIT_ACTION[hvac_action]
            )


@TYPES.register("WaterHeater")
class WaterHeater(HomeAccessory):
    """Generate a WaterHeater accessory for a water_heater."""

    def __init__(self, *args):
        """Initialize a WaterHeater accessory object."""
        super().__init__(*args, category=CATEGORY_THERMOSTAT)
        self._unit = self.hass.config.units.temperature_unit
        self._flag_heat_cool = False
        self._flag_temperature = False
        min_temp, max_temp = self.get_temperature_range()

        serv_thermostat = self.add_preload_service(SERV_THERMOSTAT)

        self.char_current_heat_cool = serv_thermostat.configure_char(
            CHAR_CURRENT_HEATING_COOLING, value=1
        )
        self.char_target_heat_cool = serv_thermostat.configure_char(
            CHAR_TARGET_HEATING_COOLING,
            value=1,
            setter_callback=self.set_heat_cool,
            valid_values=HC_HOMEKIT_VALID_MODES_WATER_HEATER,
        )

        self.char_current_temp = serv_thermostat.configure_char(
            CHAR_CURRENT_TEMPERATURE, value=50.0
        )
        self.char_target_temp = serv_thermostat.configure_char(
            CHAR_TARGET_TEMPERATURE,
            value=50.0,
            properties={
                PROP_MIN_VALUE: min_temp,
                PROP_MAX_VALUE: max_temp,
                PROP_MIN_STEP: 0.5,
            },
            setter_callback=self.set_target_temperature,
        )

        self.char_display_units = serv_thermostat.configure_char(
            CHAR_TEMP_DISPLAY_UNITS, value=0
        )

    def get_temperature_range(self):
        """Return min and max temperature range."""
        max_temp = self.hass.states.get(self.entity_id).attributes.get(ATTR_MAX_TEMP)
        max_temp = (
            temperature_to_homekit(max_temp, self._unit)
            if max_temp
            else DEFAULT_MAX_TEMP_WATER_HEATER
        )
        max_temp = round(max_temp * 2) / 2

        min_temp = self.hass.states.get(self.entity_id).attributes.get(ATTR_MIN_TEMP)
        min_temp = (
            temperature_to_homekit(min_temp, self._unit)
            if min_temp
            else DEFAULT_MIN_TEMP_WATER_HEATER
        )
        min_temp = round(min_temp * 2) / 2

        return min_temp, max_temp

    def set_heat_cool(self, value):
        """Change operation mode to value if call came from HomeKit."""
        _LOGGER.debug("%s: Set heat-cool to %d", self.entity_id, value)
        self._flag_heat_cool = True
        hass_value = HC_HOMEKIT_TO_HASS[value]
        if hass_value != HVAC_MODE_HEAT:
            self.char_target_heat_cool.set_value(1)  # Heat

    @debounce
    def set_target_temperature(self, value):
        """Set target temperature to value if call came from HomeKit."""
        _LOGGER.debug("%s: Set target temperature to %.1f°C", self.entity_id, value)
        self._flag_temperature = True
        temperature = temperature_to_states(value, self._unit)
        params = {ATTR_ENTITY_ID: self.entity_id, ATTR_TEMPERATURE: temperature}
        self.call_service(
            DOMAIN_WATER_HEATER,
            SERVICE_SET_TEMPERATURE_WATER_HEATER,
            params,
            f"{temperature}{self._unit}",
        )

    def update_state(self, new_state):
        """Update water_heater state after state change."""
        # Update current and target temperature
        temperature = new_state.attributes.get(ATTR_TEMPERATURE)
        if isinstance(temperature, (int, float)):
            temperature = temperature_to_homekit(temperature, self._unit)
            self.char_current_temp.set_value(temperature)
            if not self._flag_temperature:
                self.char_target_temp.set_value(temperature)
        self._flag_temperature = False

        # Update display units
        if self._unit and self._unit in UNIT_HASS_TO_HOMEKIT:
            self.char_display_units.set_value(UNIT_HASS_TO_HOMEKIT[self._unit])

        # Update target operation mode
        operation_mode = new_state.state
        if operation_mode and not self._flag_heat_cool:
            self.char_target_heat_cool.set_value(1)  # Heat
        self._flag_heat_cool = False
