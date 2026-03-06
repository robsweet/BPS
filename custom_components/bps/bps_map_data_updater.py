"""Class to handle map data updates for BPS integration."""

import logging
import re

from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    # device_registry as dr,
    # entity_registry as er,
    floor_registry as fr,
)

# from homeassistant.helpers.template import Template

_LOGGER = logging.getLogger(__name__)


class BPSMapDataUpdater:
    """Class to handle map data updates from the UI for BPS integration."""

    def __init__(
        self, hass: HomeAssistant, floor_data: BPSMapData, runtime_data: BPSRuntimeData
    ) -> None:
        """Initialize the data updater."""

        self.hass = hass
        self.floor_data = floor_data
        self.runtime_data = runtime_data

    def generate_new_floor_data(self):
        """Generate new floor data structure from HA registries."""

        floor_reg = fr.async_get(self.hass)
        area_reg = ar.async_get(self.hass)
        # ent_reg = er.async_get(self.hass)
        # dev_reg = dr.async_get(self.hass)

        fresh_data = BPSMapData()

        if not floor_reg.async_list_floors():
            _LOGGER.info("CANNOT START! No floors have been set up in HA!")
            return None

        for floor in floor_reg.async_list_floors():
            fresh_data.floors[floor.floor_id] = {
                "name": floor.name,
                "floor_id": floor.floor_id,
                "icon": floor.icon,
                "scale": None,
                "receivers": [],
                "areas": [],
            }

            areas = area_reg.async_list_areas()
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
            fresh_data.receivers.append(my_rec)

        return fresh_data

    # TODO: Implement floor change handling
    # TODO: Implement area change handling
    # TODO: Implement tracker change handling?

    async def handle_update_floor_data(self, call: ServiceCall) -> None:
        """Handle the listener callback."""
        pass
