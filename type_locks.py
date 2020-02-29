"""Class to hold all lock accessories."""
import logging

from pyhap.const import CATEGORY_DOOR_LOCK

from homeassistant.components.lock import DOMAIN, STATE_LOCKED, STATE_UNLOCKED
from homeassistant.const import ATTR_CODE, ATTR_ENTITY_ID, STATE_UNKNOWN

from . import TYPES
from .accessories import HomeAccessory
from .const import CHAR_LOCK_CURRENT_STATE, CHAR_LOCK_TARGET_STATE, SERV_LOCK

_LOGGER = logging.getLogger(__name__)

HASS_TO_HOMEKIT = {
    STATE_UNLOCKED: 0,
    STATE_LOCKED: 1,
    # Value 2 is Jammed which hass doesn't have a state for
    STATE_UNKNOWN: 3,
}

HOMEKIT_TO_HASS = {c: s for s, c in HASS_TO_HOMEKIT.items()}

STATE_TO_SERVICE = {STATE_LOCKED: "lock", STATE_UNLOCKED: "unlock"}


@TYPES.register("Lock")
class Lock(HomeAccessory):
    """Generate a Lock accessory for a lock entity.

    The lock entity must support: unlock and lock.
    """

    def __init__(self, *args):
        """Initialize a Lock accessory object."""
        super().__init__(*args, category=CATEGORY_DOOR_LOCK)
        self._code = self.config.get(ATTR_CODE)
        self._flag_state = False

        serv_lock_mechanism = self.add_preload_service(SERV_LOCK)
        self.char_current_state = serv_lock_mechanism.configure_char(
            CHAR_LOCK_CURRENT_STATE, value=HASS_TO_HOMEKIT[STATE_UNKNOWN]
        )
        self.char_target_state = serv_lock_mechanism.configure_char(
            CHAR_LOCK_TARGET_STATE,
            value=HASS_TO_HOMEKIT[STATE_LOCKED],
            setter_callback=self.set_state,
        )

    def set_state(self, value):
        """Set lock state to value if call came from HomeKit."""
        _LOGGER.debug("%s: Set state to %d", self.entity_id, value)
        self._flag_state = True

        hass_value = HOMEKIT_TO_HASS.get(value)
        service = STATE_TO_SERVICE[hass_value]

        params = {ATTR_ENTITY_ID: self.entity_id}
        if self._code:
            params[ATTR_CODE] = self._code
        self.call_service(DOMAIN, service, params)

    def update_state(self, new_state):
        """Update lock after state changed."""
        hass_state = new_state.state
        if hass_state in HASS_TO_HOMEKIT:
            current_lock_state = HASS_TO_HOMEKIT[hass_state]
            self.char_current_state.set_value(current_lock_state)
            _LOGGER.debug(
                "%s: Updated current state to %s (%d)",
                self.entity_id,
                hass_state,
                current_lock_state,
            )

            # LockTargetState only supports locked and unlocked
            if hass_state in (STATE_LOCKED, STATE_UNLOCKED):
                if not self._flag_state:
                    self.char_target_state.set_value(current_lock_state)
                self._flag_state = False
