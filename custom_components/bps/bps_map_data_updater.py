"""Class to handle map data updates for BPS integration."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import re
from typing import TYPE_CHECKING

import aiofiles

from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    floor_registry as fr,
)

from .util import name_to_id

if TYPE_CHECKING:
    from .data_classes import BPSData, BPSRuntimeData

# from homeassistant.helpers.template import Template

_LOGGER = logging.getLogger(__name__)


class BPSMapDataUpdater:
    """Class to handle map data updates from the UI for BPS integration."""

    def __init__(
        self, hass: HomeAssistant, stored_data: BPSData, runtime_data: BPSRuntimeData
    ) -> None:
        """Initialize the data updater."""

        self.hass = hass
        self.stored_data = stored_data
        self.runtime_data = runtime_data
        self.map_data_ready = False
        self.floor_reg = fr.async_get(hass)
        self.area_reg = ar.async_get(self.hass)
        # self.ent_reg = er.async_get(self.hass)
        # self.dev_reg = dr.async_get(self.hass)
        self.bermuda_config_entry = self.hass.config_entries.async_entries("bermuda")[0]
        self.bermuda_coordinator = self.bermuda_config_entry.runtime_data.coordinator

        hass.async_create_background_task(
            self.wait_for_floor_data(),
            "BPS Floor Data Initializer",
        )

    async def wait_for_floor_data(self) -> None:
        """Wait for floor data to be available."""
        if not self.map_data_ready:
            _LOGGER.debug("Waiting for floor data to be available")

        while not self.map_data_ready:
            if self.bermuda_coordinator.get_scanners:
                _LOGGER.debug(
                    "Bermuda scanner data is now available, proceeding with initialization"
                )

                (
                    new_map_data,
                    self.stored_data.attic["bpsdata_file"],
                ) = await self.generate_new_map_data()

                # TODO: REMOVE these coords assignments out
                if (
                    "c6_feather_dev" not in new_map_data.scanners
                    or "c6_feather_dev2" not in new_map_data.scanners
                    or "c6_feather_dev3" not in new_map_data.scanners
                ):
                    await asyncio.sleep(1)
                    continue

                new_map_data.scanners["c6_feather_dev"]["coords"] = {"x": 50, "y": 50}
                new_map_data.scanners["c6_feather_dev2"]["coords"] = {
                    "x": 500,
                    "y": 500,
                }
                new_map_data.scanners["c6_feather_dev3"]["coords"] = {
                    "x": 1000,
                    "y": 1000,
                }

                self.stored_data.map_data.floors = new_map_data.floors
                self.stored_data.map_data.areas = new_map_data.areas
                self.stored_data.map_data.scanners = new_map_data.scanners

                if self.stored_data.map_data.floors:
                    _LOGGER.debug(
                        "Initial map data has been generated, proceeding with initialization"
                    )
                    self.map_data_ready = True
                    return True
                else:
                    _LOGGER.debug(
                        "Bermuda scanner data is still not available after generation, retrying in 2 seconds"
                    )
                    await asyncio.sleep(2)
                    continue
            else:
                _LOGGER.debug(
                    "Bermuda scanner data is still not available, retrying in 2 seconds"
                )
                await asyncio.sleep(2)
                continue
        return True

    def ready(self) -> bool:
        """Whether the BPS map data is ready."""
        return self.map_data_ready

    async def get_old_data(self) -> dict:
        """Update global_data with the contents of the file."""

        data_file_path = Path().joinpath(
            self.hass.config.path(), "www", "bps_maps", "bpsdata.txt"
        )
        old_json = None
        try:
            async with aiofiles.open(data_file_path) as file:
                old_json = await file.read()
        except FileNotFoundError:
            return None

        try:
            return json.loads(old_json) if old_json else None
        except json.JSONDecodeError as e:
            _LOGGER.exception("Error parsing old JSON data: %s", old_json)

    async def migrate_old_data_if_needed(self, fresh_data: dict) -> None:
        """Migrate old data from the previous json file if needed."""
        old_data = await self.get_old_data()
        if old_data:
            _LOGGER.debug("Old data found, migrating to new datastore")

            # TODO: Implement pulling old data from the old bps_data.txt file and merging it with
            # the new data structure, so that we don't lose scanner coords on HA restarts until we
            # implement the UI for setting them and saving to file

            # TODO: Rename old data file.

        return fresh_data, old_data

    async def generate_new_map_data(self):
        """Generate new floor data structure from HA registries."""

        from .data_classes import BPSMapData  # noqa: PLC0415

        fresh_data = BPSMapData()
        if not self.floor_reg.async_list_floors():
            _LOGGER.error("CANNOT START! No floors have been set up in HA!")
            raise Exception(
                "No floors have been set up in HA! Please set up at least one floor to use BPS."
            )

        for floor in self.floor_reg.async_list_floors():
            fresh_data.floors[floor.floor_id] = {
                "name": floor.name,
                "floor_id": floor.floor_id,
                "icon": floor.icon,
                "level": floor.level,
                "scale": 30,
                # TODO: REMOVE Scale should be set to None until set in UI
                "scanners": [],
                "areas": [],
            }

            areas = self.area_reg.async_list_areas()
            for area in areas:
                if area.floor_id != floor.floor_id:
                    continue

                my_area = {
                    "name": area.name,
                    "entity_id": area.id,
                    "floor_id": area.floor_id,
                    "icon": area.icon,
                    "type": "area",
                    "coords": [],
                }
                fresh_data.areas[area.id] = my_area
                fresh_data.floors[floor.floor_id]["areas"].append(my_area)

        bermuda_scanners = {
            name_to_id(s.name): s for s in list(self.bermuda_coordinator.get_scanners)
        }

        for scanner_id, scanner in bermuda_scanners.items():
            my_rec = {
                "scanner_id": scanner_id,
                "name": scanner.name,
                "type": "scanner",
                "floor_id": scanner.floor.floor_id,
                "level": scanner.floor.level,
                "coords": {},
            }
            fresh_data.scanners[scanner_id] = my_rec

        return await self.migrate_old_data_if_needed(fresh_data)

    # TODO: Implement floor change handling
    # TODO: Implement area change handling
    # TODO: Implement tracker change handling?

    # async def handle_update_map_data(self, call: ServiceCall) -> None:
    #     """Handle the listener callback."""
    #     pass

    # async def ensure_sensors_exist_for(self, tracker_id):
    # """Ensure sensors exist for the given tracker_id."""

    # sensors_to_add = []

    # unique_area_id = f"sensor.{tracker_id}_bps_area"
    # unique_area_uid = f"bps_area_{tracker_id}"
    # unique_floor_id = f"sensor.{tracker_id}_bps_floor"
    # unique_floor_uid = f"bps_floor_{tracker_id}"

    # if unique_area_id not in self.runtime_data.bps_tracker_entities:
    #     _LOGGER.debug("Creating new sensor for area: %s", unique_area_id)
    #     sensor = CustomDistanceSensor(f"{tracker_id} BPS Area", unique_area_uid)
    #     self.runtime_data.bps_tracker_entities.append(sensor)
    #     sensors_to_add.append(sensor)
    # else:
    #     _LOGGER.debug("Sensor for area %s already exists", unique_area_id)

    # if unique_floor_id not in self.runtime_data.bps_tracker_entities:
    #     _LOGGER.debug("Creating new sensor for floor: %s", unique_floor_id)
    #     sensor = CustomDistanceSensor(f"{tracker_id} BPS Floor", unique_floor_uid)
    #     self.runtime_data.bps_tracker_entities.append(sensor)
    #     sensors_to_add.append(sensor)
    # else:
    #     _LOGGER.debug("Sensor for floor %s already exists", unique_floor_id)

    # await async_add_bps_sensor(sensors_to_add, update_before_add=True)

    # TODO:  Make sure this is actually happening on integration unload and that it works
    # as expected to clear out old sensors. If HA's entity registry caching prevents this
    # from working properly, we may need to implement a different strategy for ensuring old
    # sensors are removed on reloads.
    async def async_unload(self) -> bool:
        """Ensure BPS tracking entities are removed."""

        _LOGGER.debug("Removing sensors for integration unload")
        entity_registry = er.async_get(self.hass)

        for entity_id in [
            entity.entity_id
            for entity in entity_registry.entities.values()
            if entity.platform == "bps"
        ]:
            _LOGGER.debug("\tRemoving sensor: %s", entity_id)
            entity_registry.async_remove(entity_id)

        await asyncio.sleep(2)
        not_removed = 0
        for entity_id in [
            entity.entity_id
            for entity in entity_registry.entities.values()
            if entity.platform == "bps"
        ]:
            not_removed += 1
            _LOGGER.debug("\tSensor still exists after unload: %s", entity_id)

        if not_removed:
            _LOGGER.warning(
                "\t%d sensors still exist after unload. This may be due to Home Assistant's entity registry caching. Restart Home Assistant to fully clear these entities",
                not_removed,
            )
        else:
            _LOGGER.debug("\tAll BPS sensors removed successfully")

        return True
