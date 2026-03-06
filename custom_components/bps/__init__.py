"""Bluetooth Positioning System (BPS) integration for Home Assistant."""

import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path

import aiofiles
import aiofiles.os

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import EVENT_HOMEASSISTANT_STARTED

from .bps_map_data_updater import BPSMapDataUpdater
from .bps_tri_data_updater import BPSTriDataUpdater
from .bps_ui_manager import BPSUiManager
from .const import DOMAIN, PLATFORMS

# type BPSConfigEntry = ConfigEntry[BPSMapData, BPSRuntimeData]

_LOGGER = logging.getLogger(__name__)


@dataclass
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


@dataclass
class BPSStoredData:
    """Data structure to hold all BPS-related data under hass.data[DOMAIN].

    This is the main data container for the BPS integration. It wraps the map data in case we need to persist any non-map data in the future.
    """

    def __init__(self) -> None:
        """Initialize the BPS data structure."""
        self.map_data: BPSMapData = BPSMapData()


@dataclass
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
            hass, self.integration_data.map_data, self
        )
        self.bps_tri_data_updater: BPSTriDataUpdater = BPSTriDataUpdater(
            hass, self.integration_data.map_data, self
        )
        self.bps_ui_manager: BPSUiManager = BPSUiManager(
            hass, self.integration_data, self
        )
        self.my_tracker_entities = []  # List to hold entity IDs of the tracker entities created by this integration


@callback
def handle_launch_debugger(call: ServiceCall) -> None:
    """Handle the service action call."""
    import debugpy  # noqa: T100 PLC0415

    hass = call.hass
    stored_data = hass.data[DOMAIN]  # noqa: F841
    runtime_data = hass.config_entries.async_entries(DOMAIN)[0].runtime_data  # noqa: F841

    # TODO:  Figure out how to have a reasonable timeout for wait_for_client()

    debugpy.wait_for_client()  # noqa: T100
    debugpy.breakpoint()  # noqa: T100
    return None  # noqa: RET501 PLR1711


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the BPS integration config entry."""
    _LOGGER.info("Initializing BPS... ")

    hass.data.setdefault(DOMAIN, BPSStoredData())
    entry.runtime_data = BPSRuntimeData(hass, entry)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    target_dir = Path().joinpath(hass.config.path(), "www", "bps_maps")
    try:
        await aiofiles.os.makedirs(target_dir, exist_ok=True)
        _LOGGER.info("\tFolder %s has been created or already existed", target_dir)
    except Exception as e:
        _LOGGER.error("\tCould not create the folder %s: %s", target_dir, e, exc_info=e)
        raise

    updater = BPSTriDataUpdater(hass, hass.data[DOMAIN].map_data, entry.runtime_data)
    hass.data["bps_initialized"] = True
    hass.async_create_task(updater.update_tracked_entities())
    _LOGGER.info("The BPS integration is fully initialized")

    # TODO:  Create task to call entry.runtime_data.bps_ui_manager.async_config() to set up the UI components for the integration
    # hass.async_create_task(entry.runtime_data.bps_ui_manager.async_config())

    hass.services.async_register(DOMAIN, "bps_debug", handle_launch_debugger)

    async def handle_homeassistant_started(event):
        """Handle the Home Assistant start event."""
        _LOGGER.info(
            "Home Assistant has started. Performing post-start initialization tasks for BPS"
        )
        entry.runtime_data.ready_to_collect = True
        _LOGGER.info("BPS is now ready to collect data")

    hass.bus.async_listen_once(
        EVENT_HOMEASSISTANT_STARTED, handle_homeassistant_started
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Handle Home Assistant shutdown or integration reload."""
    entry.runtime_data.stop_integration = True

    ui_unload = hass.async_create_task(entry.runtime_data.bps_ui_manager.async_unload())

    _LOGGER.info("Removing sensors for integration unload")
    entity_registry = er.async_get(hass)

    for entity_id in [
        entity.entity_id
        for entity in entity_registry.entities.values()
        if entity.platform == "bps"
    ]:
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
