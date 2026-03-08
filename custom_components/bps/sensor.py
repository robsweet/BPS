"""Custom BPS sensors for Home Assistant."""

import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)


def get_filtered_entities(hass: HomeAssistant):
    """Fetch and filter sensors based on their entity_id."""
    sensors = [
        state
        for state in hass.states.async_all()
        if state.entity_id.startswith("sensor.")
    ]
    filtered = [
        state.entity_id.replace("sensor.", "").split("_distance_to_")[0]
        for state in sensors
        if "_distance_to_" in state.entity_id
    ]
    return list(set(filtered))


class CustomDistanceSensor(SensorEntity):
    """A representation of a custom sensor."""

    def __init__(self, name, unique_id) -> None:
        """Initialize the sensor."""
        self._name = name
        self._unique_id = unique_id
        self._state = "unknown"
        self._attr_extra_state_attributes = {}

    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name

    @property
    def unique_id(self):
        """Return the unique ID of the sensor."""
        return self._unique_id

    @property
    def state(self):
        """Return the state of the sensor."""
        return self._state

    @property
    def extra_state_attributes(self):
        """Return entity specific state attributes."""
        return self._attr_extra_state_attributes


# Alternatively, using the attribute shorthand:
# self._attr_extra_state_attributes = {"attribute_one": "value_one", ...}


async def async_setup_entry(hass: HomeAssistant, config_entry, async_add_entities):
    """Set dynamic sensors based on the filtered entities."""
    # _LOGGER.info("Setting up our own sensors for each tracked device")
    await async_setup_platform(hass, config_entry, async_add_entities)
    return True


async def async_setup_platform(
    hass: HomeAssistant, config, async_add_entities, discovery_info=None
):
    """Set up the sensor platform."""

    @callback
    def state_changed_listener(event):
        """Listen for state changes to update dynamic sensors."""
        if "_distance_to_" not in event.data.get("entity_id"):
            return  # Only care about entities that are relevant to our sensors

        new_entities = get_filtered_entities(hass)
        new_sensors = []

        entity_registry = er.async_get(hass)

        existing_bps_sensors = [
            entity.entity_id
            for entity in list(entity_registry.entities.values())
            if entity.platform == "bps"
        ]

        for entity_id in new_entities:
            unique_area_id = f"sensor.{entity_id}_bps_area"
            unique_area_uid = f"bps_area_{entity_id}"
            unique_floor_id = f"sensor.{entity_id}_bps_floor"
            unique_floor_uid = f"bps_floor_{entity_id}"

            if not any(s.startswith(unique_area_id) for s in existing_bps_sensors):
                _LOGGER.debug("Creating new sensor for area: %s", unique_area_id)
                sensor = CustomDistanceSensor(f"{entity_id} BPS Area", unique_area_uid)
                new_sensors.append(sensor)
            else:
                _LOGGER.debug("Sensor for area %s already exists", unique_area_id)

            if not any(s.startswith(unique_floor_id) for s in existing_bps_sensors):
                _LOGGER.debug("Creating new sensor for floor: %s", unique_floor_id)
                sensor = CustomDistanceSensor(
                    f"{entity_id} BPS Floor", unique_floor_uid
                )
                new_sensors.append(sensor)
            else:
                _LOGGER.debug("Sensor for floor %s already exists", unique_floor_id)

        async_add_entities(new_sensors, update_before_add=True)

    return True
