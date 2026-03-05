"""Bluetooth Positioning System (BPS) integration for Home Assistant."""

import logging
from pathlib import Path

import aiofiles
import aiofiles.os

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from . import DOMAIN, PLATFORMS, BPSMapDataUpdater, BPSTriDataUpdater, BPSUiManager

# type BPSConfigEntry = ConfigEntry[BPSMapData, BPSRuntimeData]

_LOGGER = logging.getLogger(__name__)


class BPSMapData:
    """Data structure to hold all the BPS map-related data."""

    def __init__(self) -> None:
        """Initialize the BPS map data structure."""
        self.floors = {}
        self.areas = {}
        self.receivers = {}

    def receivers_with_coords(self, floor_data):
        """Return a list of receiver IDs that have coordinates set (via the UI) for the given floor."""
        return [
            rid
            for rid, receiver in floor_data.receivers.items()
            if any(receiver["coords"])
        ]


class BPSStoredData:
    """Data structure to hold all BPS-related data under hass.data[DOMAIN].

    This is the main data container for the BPS integration. It wraps the map data in case we need to persist any non-map data in the future.
    """

    def __init__(self) -> None:
        """Initialize the BPS data structure."""
        self.map_data: BPSMapData = BPSMapData()


class BPSRuntimeData:
    """Data structure to hold trilateration data.

    Runtime data is stored in the config entry's runtime_data and is meant to hold data that
    is relevant only during the runtime of the integration and should not persist across reloads.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the trilateration data structure."""

        self.hass: HomeAssistant = hass
        self.entry: ConfigEntry = entry
        self.integration_data: BPSStoredData = hass.data.get(DOMAIN)
        self.tricoords: dict = {}
        self.cache: dict = {}
        self.stop_integration: bool = False
        self.ready_to_collect: bool = False
        self.bps_map_data_updater: BPSMapDataUpdater = BPSMapDataUpdater(
            hass, self.integration_data, entry.runtime_data.map_data
        )
        self.bps_tri_data_updater: BPSTriDataUpdater = BPSTriDataUpdater(
            hass, self.integration_data, entry.runtime_data
        )
        self.bps_ui_manager: BPSUiManager = BPSUiManager(
            hass, self.integration_data, entry.runtime_data
        )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the BPS integration config entry."""
    _LOGGER.info("Initializing BPS... ")

    hass.data.setdefault(DOMAIN, BPSStoredData())
    entry.runtime_data = BPSRuntimeData(hass, entry)

    returns = await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    target_dir = Path.joinpath(hass.config.path(), "www", "bps_maps")
    try:
        await aiofiles.os.makedirs(target_dir, exist_ok=True)
        _LOGGER.info("\tFolder %s has been created or already existed", target_dir)
    except Exception as e:
        _LOGGER.error("\tCould not create the folder %s: %s", target_dir, e, exc_info=e)
        raise

    updater = BPSTriDataUpdater(hass, hass.data[DOMAIN].map_data, entry.runtime_data)
    hass.data["bps_initialized"] = True
    hass.async_create_task(updater.update_tracked_entities)
    _LOGGER.info("The BPS integration is fully initialized")

    returns.append(entry.runtime_data.bps_ui_manager.async_config())
    return returns


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Handle Home Assistant shutdown or integration reload."""
    entry.runtime_data.stop_integration = True

    ui_unload = hass.async_create_task(entry.runtime_data.bps_ui_manager.async_unload())

    _LOGGER.info("Removing sensors for integration unload")
    entity_registry = er.async_get(hass)

    # Find and remove all entities that belong to "bps"
    entities_to_remove = [
        entity.entity_id
        for entity in entity_registry.entities.values()
        if entity.platform == "bps"
    ]

    for entity_id in entities_to_remove:
        _LOGGER.info("\tRemoving sensor: %s", entity_id)
        entity_registry.async_remove(entity_id)
    _LOGGER.info("Done removing sensors")

    _LOGGER.info("Attempting to unload platforms for entry: %s", entry.entry_id)
    try:  # Attempt to unload platforms
        unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    except Exception as e:
        _LOGGER.error(
            "\tError during unloading of platforms for entry %s: %s",
            entry.entry_id,
            exc_info=e,
        )
        return False

    if not unload_ok:
        _LOGGER.error("\tFailed to unload platforms for entry: %s", entry.entry_id)
        return False

    await ui_unload
    return True
