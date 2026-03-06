"""Class to handle map data updates for BPS integration."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    # device_registry as dr,
    # entity_registry as er,
    floor_registry as fr,
)

if TYPE_CHECKING:
    from .data_classes import BPSData
    from .data_classes import BPSRuntimeData

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

        hass.async_create_background_task(
            self.wait_for_floor_data(),
            "BPS Floor Data Initializer",
        )

    async def wait_for_floor_data(self) -> None:
        """Wait for floor data to be available."""
        if not self.map_data_ready:
            _LOGGER.debug("Waiting for floor data to be available")

        while not self.map_data_ready:
            if self.floor_reg.async_list_floors():
                _LOGGER.debug(
                    "Floor data is now available from the floor registry, proceeding with initialization"
                )
                self.stored_data.map_data = self.generate_new_map_data()
                if self.stored_data.map_data.floors:
                    _LOGGER.debug(
                        "Initial floor data has been generated, proceeding with initialization"
                    )
                    self.map_data_ready = True
                    return True
                else:
                    _LOGGER.debug(
                        "Floor data is still not available after generation, retrying in 2 seconds"
                    )
            else:
                _LOGGER.debug(
                    "Floor registry data is still not available, retrying in 2 seconds"
                )
            await asyncio.sleep(2)
            continue
        return True

    def ready(self) -> bool:
        """Whether the BPS map data is ready."""
        return self.map_data_ready

    def generate_new_map_data(self):
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
                # TODO scale should be set to None until set in UI
                "receivers": [],
                "areas": [],
            }

            areas = self.area_reg.async_list_areas()
            for area in areas:
                if area.floor_id != floor.floor_id:
                    continue

                my_area = {
                    "name": area.name,
                    "entity_id": area.id,
                    "icon": area.icon,
                    "type": "area",
                    "cords": [],
                }
                fresh_data.areas[area.id] = my_area
                fresh_data.floors[floor.floor_id]["areas"].append(my_area)

        # TODO:  Figure out how/when to get receiver -> floor data mapping

        receiver_ids = {
            re.sub(".*_distance_to_", "", key)
            for key in self.hass.data["entity_info"]
            if "_distance_to_" in key
        }
        for receiver_id in receiver_ids:
            my_rec = {
                "receiver_id": receiver_id,
                "type": "receiver",
                "floor_id": None,
                "cords": {},
            }
            fresh_data.receivers[receiver_id] = my_rec

        return fresh_data

    # TODO: Implement floor change handling
    # TODO: Implement area change handling
    # TODO: Implement tracker change handling?

    # async def handle_update_map_data(self, call: ServiceCall) -> None:
    #     """Handle the listener callback."""
    #     pass

    async def async_unload(self) -> None:
        """Clean up any resources when the integration is unloaded."""

        # TODO:  Clean up event listeners

        pass
