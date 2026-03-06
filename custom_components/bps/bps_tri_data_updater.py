"""Module to handle trilateration data updates for BPS integration."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import numpy as np
from scipy.optimize import least_squares
from shapely.geometry import Point, Polygon

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.template import Template

from .const import DOMAIN
from .sensor import CustomDistanceSensor

if TYPE_CHECKING:
    from .data_classes import BPSData, BPSRuntimeData

_LOGGER = logging.getLogger(__name__)


class BPSTriDataUpdater:
    """Class to handle trilateration data updates for BPS integration."""

    DISTANCE_ENTITIES_JINJA = """{{
        expand(states.sensor)
        | selectattr("entity_id", "search", "_distance_to_")
        | map(attribute="entity_id")
        | unique
        | list
    }}
    """

    def __init__(
        self,
        hass: HomeAssistant,
        stored_data: BPSData,
        runtime_data: BPSRuntimeData,
        update_frequency=1,
    ) -> None:
        """Initialize the data updater."""
        self.hass = hass
        self.stored_data = stored_data
        self.runtime_data = runtime_data
        self.update_frequency = update_frequency
        self.entity_registry = er.async_get(self.hass)

    async def cannot_trilaterate(self, message):
        """Handle cases where trilateration can't be performed."""
        _LOGGER.info(message)
        await asyncio.sleep(10)  # Wait before trying again

    async def ensure_sensors_exist_for(self, tracker_id):
        """Ensure sensors exist for the given tracker_id."""

        sensors_to_add = []

        unique_area_id = f"sensor.{tracker_id}_bps_area"
        unique_area_uid = f"bps_area_{tracker_id}"
        unique_floor_id = f"sensor.{tracker_id}_bps_floor"
        unique_floor_uid = f"bps_floor_{tracker_id}"

        if unique_area_id not in self.runtime_data.bps_tracker_entities:
            _LOGGER.debug("Creating new sensor for area: %s", unique_area_id)
            sensor = CustomDistanceSensor(f"{tracker_id} BPS Area", unique_area_uid)
            self.runtime_data.bps_tracker_entities.append(sensor)
            sensors_to_add.append(sensor)
        else:
            _LOGGER.debug("Sensor for area %s already exists", unique_area_id)

        if unique_floor_id not in self.runtime_data.bps_tracker_entities:
            _LOGGER.debug("Creating new sensor for floor: %s", unique_floor_id)
            sensor = CustomDistanceSensor(f"{tracker_id} BPS Floor", unique_floor_uid)
            self.runtime_data.bps_tracker_entities.append(sensor)
            sensors_to_add.append(sensor)
        else:
            _LOGGER.debug("Sensor for floor %s already exists", unique_floor_id)

        await self.hass.async_add_entities(sensors_to_add, update_before_add=True)

    async def update_tracked_entities(self):
        """Update tracked_entities with the result of trilateration once per second."""

        _LOGGER.info("Starting BPS tracker updating loop")
        while not self.runtime_data.stop_integration:
            if not self.runtime_data.bps_map_data_updater.ready():
                await self.cannot_trilaterate(
                    "Map data is not ready yet from map data updater"
                )
                continue  # Wait until the system is ready to collect data

            if not any(
                [
                    floor["scale"]
                    for floor in list(self.stored_data.map_data.floors.values())
                ]  # noqa: C419
            ):
                await self.cannot_trilaterate(
                    "No floors have scale data.  Maps probably haven't been set up in the BPS UI."
                )
                continue  # start over

            if len(self.stored_data.map_data.receivers_with_coords()) < 3:
                await self.cannot_trilaterate(
                    f"Only {len(self.stored_data.map_data.receivers_with_coords())} receivers have coords.  Place at least 3 receivers in the BPS UI."
                )
                continue  # start over

            bermuda_entities = [
                entity.entity_id
                for entity in list(self.entity_registry.entities.values())
                if entity.platform == "bermuda"
            ]

            new_tricoords = {}

            receiver_state_tasks = []
            for tracker_id, receiver_id in [
                item.replace("sensor.", "").split("_distance_to_")
                for item in bermuda_entities
            ]:
                if not self.stored_data.map_data.receivers[receiver_id]["coords"]:
                    _LOGGER.debug(
                        "Receiver %s has not been placed using the BPS UI", receiver_id
                    )
                    continue

                if not self.stored_data.map_data.floors[
                    self.stored_data.map_data.receivers[receiver_id]["floor"]
                ]["scale"]:
                    _LOGGER.debug(
                        "Scale not set for floor '%s'. Skipping receiver %s",
                        self.stored_data.map_data.receivers[receiver_id]["floor"],
                        receiver_id,
                    )
                    continue

                new_tricoords[tracker_id].setdefault({})
                receiver_state_tasks.append(
                    asyncio.create_task(
                        self.update_receiver_state(
                            new_tricoords, tracker_id, receiver_id
                        )
                    )
                )

            await asyncio.gather(receiver_state_tasks)

            tracker_state_tasks = [
                asyncio.create_task(
                    self.update_trilateration_and_area(new_tricoords, tracker_id)
                )
                for tracker_id in new_tricoords.keys()  # noqa: SIM118
            ]

            tricoords_updates = await asyncio.gather(tracker_state_tasks)

            for tracker_id, area, floor in tricoords_updates:
                await self.ensure_sensors_exist_for(tracker_id)
                self.hass.states.async_set(f"sensor.{tracker_id}_bps_area", area)
                self.hass.states.async_set(f"sensor.{tracker_id}_bps_floor", floor)

            self.runtime_data.tricoords = new_tricoords
            await asyncio.sleep(
                self.update_frequency
            )  # Run every X seconds, set timer in global variables
        _LOGGER.info("Finished BPS tracker updating loop")

    async def update_receiver_state(self, new_tricoords, tracker_id, receiver_id):
        """Update receiver state and radius in new_tricoords."""

        entity_id = f"{tracker_id}_distance_to_{receiver_id}"
        rcvr = new_tricoords[tracker_id][receiver_id]
        rcvr = {
            "state": self.hass.states.get(entity_id),
            "radius": None,
            "coords": self.stored_data.map_data.receivers[receiver_id]["coords"],
        }

        if rcvr["state"] is None:
            _LOGGER.debug("Entity had no value: %s", entity_id)
        else:
            try:
                scale = self.stored_data.map_data.floors[
                    self.stored_data.map_data.receivers[receiver_id]["floor"]
                ]["scale"]
                state = float(rcvr["state"])
                rcvr["radius"] = scale * state
            except ValueError:
                _LOGGER.debug("Invalid numerical value: %s", str(rcvr["state"]))

    async def update_trilateration_and_area(self, new_tricoords, tracker_id):
        """Trilateration with r-value filtering and moving average filtering."""
        filter_percent = 0.5  # 50% change in r-value
        filter_value_high = 1 * (1 + filter_percent)
        filter_value_low = 1 * (1 - filter_percent)

        # Store last r-values per sensor and entity
        self.runtime_data.cache.setdefault("last_r_values", {})
        # Store last positions for moving average filtering
        self.runtime_data.cache.setdefault("position_history", {})

        closest_floor_id = self.find_closest_floor_id(new_tricoords[tracker_id])
        closest_floor_name = self.stored_data.map_data[closest_floor_id]["name"]

        receiver_ids_on_floor = [
            receiver_id
            for receiver_id, rec in new_tricoords[tracker_id].receivers.items()
            if rec["floor_id"] == closest_floor_id
        ]
        # receivers_on_floor: list of the receivers on the closest floor to the tracker

        # Get previous r-values for this entity
        last_r = self.runtime_data.cache["last_r_values"].getdefault(tracker_id, {})

        # Filter out points where r has changed too much
        filtered = []
        for receiver_id in receiver_ids_on_floor:
            r = new_tricoords[tracker_id][receiver_id]["radius"]
            prev_r = last_r.get(receiver_id)
            if prev_r is not None:
                if (
                    r > prev_r * filter_value_high or r < prev_r * filter_value_low
                ):  # e.g. max 100% change
                    continue  # skip this point
            filtered.append(receiver_id)
        receiver_ids_on_floor = filtered

        # Store current r-values for next time
        self.runtime_data.cache["last_r_values"][tracker_id] = {
            (rec["receiver_id"]): rec["radius"] for rec in receiver_ids_on_floor
        }

        if len(receiver_ids_on_floor) < 3:
            # Too few points left for trilateration
            return

        tricords = self.trilaterate(
            [
                (rec["coords"]["x"], rec["coords"]["y"], rec["radius"])
                for rec in new_tricoords[tracker_id].values()
            ]
        )
        if tricords is not None:
            # Moving average filtering
            history = self.runtime_data.cache["position_history"].setdefault(
                tracker_id, []
            )
            history.append(tricords)
            if len(history) > 3:  # Keep only the last 3 positions
                history.pop(0)
            avg_x = sum(pos[0] for pos in history) / len(history)
            avg_y = sum(pos[1] for pos in history) / len(history)

            test_point = Point(float(avg_x), float(avg_y))
            area = self.find_area_for_point(closest_floor_id, test_point)
            new_tricoords[tracker_id].merge({"cords": [avg_x, avg_y], "area": area})

            return [tracker_id, area, closest_floor_name]
        return ["Unknown", "Unknown", "Unknown"]

    def find_closest_floor_id(self, receivers):
        """Find closest floor and filter receiver cords."""
        return min([rec["radius"] for rec in receivers if rec["radius"]])["floor_id"]

    def find_area_for_point(self, closest_floor_id, point):
        """Find area for point, prioritize correct polygon, select nearest buffer if no correct area matches."""
        buffer_percent = 0.05  # set to 5%
        buffer_candidates = []

        for area in [
            area
            for area in self.stored_data.map_data.areas
            if area["floor_id"] == closest_floor_id
        ]:
            polygon = Polygon([(coord["x"], coord["y"]) for coord in area["cords"]])
            xs = [coord["x"] for coord in area["cords"]]
            ys = [coord["y"] for coord in area["cords"]]
            width = max(xs) - min(xs)
            height = max(ys) - min(ys)
            buffer_size = ((width + height) / 2) * buffer_percent
            if polygon.contains(point):
                return area["entity_id"]  # Prioritize correct polygon

            if polygon.buffer(buffer_size).contains(point):
                # Save candidate: (distance to edge, entity_id)
                distance_to_edge = polygon.exterior.distance(point)
                buffer_candidates.append((distance_to_edge, area["entity_id"]))

        if buffer_candidates:
            # Select area whose edge is closest to the point
            buffer_candidates.sort()
            return buffer_candidates[0][1]

        return "unknown"

    # Trilateration function
    def trilaterate(self, known_points):
        """Perform trilateration using weighted nonlinear least squares fitting."""
        num_points = len(known_points)

        if num_points < 3:
            # Make sure there are enough points (min 3) to do a trilataration

            _LOGGER.error("At least three known points are required for trilateration")
            return None

        def objective_function(
            X, known_points
        ):  # Define the objective function loss for the least squares method.
            x, y = X
            residuals = []
            for xi, yi, ri in known_points:
                residual = np.sqrt((xi - x) ** 2 + (yi - y) ** 2) - ri
                residuals.append(residual)
            weights = 1.0 / np.array([ri**2 for _, _, ri in known_points])
            return np.sqrt(weights) * np.array(residuals)

        x0 = np.array([0, 0])  # Initial guess value for unknown coordinates

        result = least_squares(
            objective_function, x0, args=(known_points,)
        )  # Perform weighting adjustment for the least squares method.

        if not result.success:  # Check if the fitting was successful
            _LOGGER.error("Weighted nonlinear least squares fitting did not converge")
            return None
        x, y = result.x  # Extract the calculated coordinates
        return x, y  # return the result

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
