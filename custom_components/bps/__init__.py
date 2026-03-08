"""Bluetooth Positioning System (BPS) integration for Home Assistant."""

import logging
from pathlib import Path

import aiofiles
import aiofiles.os

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant, ServiceCall, callback

from .bps_tri_data_updater import BPSTriDataUpdater
from .const import DOMAIN, PLATFORMS
from .data_classes import BPSRuntimeData, BPSStoredData

_LOGGER = logging.getLogger(__name__)


@callback
def handle_launch_debugger(call: ServiceCall) -> None:
    """Handle the service action call."""
    import debugpy  # noqa: T100 PLC0415

    hass = call.hass
    stored_data = hass.data[DOMAIN]  # noqa: F841
    runtime_data = hass.config_entries.async_entries(DOMAIN)[0].runtime_data  # noqa: F841

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
    if await aiofiles.os.path.exists(target_dir):
        _LOGGER.debug("Folder %s already existed", target_dir)
    else:
        try:
            await aiofiles.os.makedirs(target_dir, exist_ok=True)
            _LOGGER.info("Folder %s has been created ", target_dir)
        except Exception as e:
            _LOGGER.error(
                "Could not create the folder %s: %s", target_dir, e, exc_info=e
            )
            raise

    # if hass.data[DOMAIN].map_data.floors:
    #     _LOGGER.debug("Map data already exists in stored data, skipping generation")
    # else:
    #     _LOGGER.debug("Generating initial map data from HA registries")
    #     hass.data[
    #         DOMAIN
    #     ].map_data = entry.runtime_data.bps_map_data_updater.generate_new_map_data()

    _LOGGER.info("The BPS integration is fully initialized")

    hass.services.async_register(DOMAIN, "bps_debug", handle_launch_debugger)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    hass.async_create_background_task(
        entry.runtime_data.bps_tri_data_updater.update_tracked_entities(),
        "BPS Trilateration Updater Loop",
    )

    # Example of seeing which config entries aren't loaded
    # ce = self.hass.config_entries._entries
    # for entry_id, entry in ce.data.items():
    #     if entry.state.value != "loaded" and not entry.disabled_by:
    #         mystr = f"{entry_id} - {entry.title} - {entry.state.value}"
    #         print(mystr)

    return True


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    hass.config_entries.async_schedule_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Handle Home Assistant shutdown or integration reload."""

    _LOGGER.debug("Unloading BPS Integration")
    entry.runtime_data.stop_integration = True

    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        _LOGGER.debug("Unloaded platforms successfully, proceeding with cleanup")

    if await entry.runtime_data.bps_tri_data_updater.async_unload():
        _LOGGER.debug("Trilateration data updater unloaded successfully")

    if await entry.runtime_data.bps_map_data_updater.async_unload():
        _LOGGER.debug("Map data updater unloaded successfully")

    if await entry.runtime_data.bps_ui_manager.async_unload():
        _LOGGER.debug("UI manager unloaded successfully")

    _LOGGER.debug("BPS Integration unloaded successfully")
    return True
