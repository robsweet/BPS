"""Module to handle trilateration data updates for BPS integration."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import numpy as np
from scipy.optimize import least_squares
from shapely.geometry import Point, Polygon

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import AREA_ICON, FLOOR_ICON
from .util import name_to_id

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
        update_frequency=2,
    ) -> None:
        """Initialize the data updater."""
        self.hass = hass
        self.stored_data = stored_data
        self.runtime_data = runtime_data
        self.update_frequency = update_frequency
        self.ent_reg = er.async_get(self.hass)
        self.dev_registry = dr.async_get(self.hass)

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
                [  # noqa: C419
                    floor["scale"]  # noqa: C419
                    for floor in list(self.stored_data.map_data.floors.values())  # noqa: C419
                ]  # noqa: C419
            ):
                await self.cannot_trilaterate(
                    "No floors have scale data.  Maps probably haven't been set up in the BPS UI."
                )
                continue  # start over

            if len(self.stored_data.map_data.scanners_with_coords()) < 3:
                await self.cannot_trilaterate(
                    f"Only {len(self.stored_data.map_data.scanners_with_coords())} scanners have coords.  Place at least 3 scanners in the BPS UI."
                )
                continue  # start over

            new_tricoords = await self.gather_scanner_states()

            tracker_state_tasks = [
                self.update_trilateration_and_area(new_tricoords, tracker_id)
                for tracker_id in new_tricoords.keys()  # noqa: SIM118
                if any(new_tricoords[tracker_id]["scanners"].values())
            ]

            await asyncio.gather(*tracker_state_tasks)
            import json

            _LOGGER.debug(
                "Completed trilateration and area updates, proceeding with state updates for %d trackers, %d scanners",
                len(new_tricoords),
                max([len(t["scanners"]) for t in new_tricoords.values()]),
            )

            for tracker_id, tri_data in new_tricoords.items():
                if tracker_id not in self.runtime_data.tracked:
                    self.runtime_data.tracked[tracker_id] = next(
                        (
                            d
                            for d in self.dev_registry.devices.values()
                            if name_to_id(d.name) == tracker_id
                        ),
                        None,
                    )

                tracker_dev = self.runtime_data.tracked[tracker_id]

                # TODO:  Figure out why we sometimes get here without setting tridata['area']
                area = self.stored_data.map_data.areas.get(tri_data["area"], None)

                self.hass.states.async_set(
                    f"sensor.{tracker_id}_bps_area",
                    area.name if area else "unknown",
                    {
                        "name": f"{tracker_dev.name} BPS Area",
                        "friendly_name": f"{tracker_dev.name} BPS Area",
                        "attribution": "Based on data from the Bermuda integration",
                        "icon": area.icon if area else AREA_ICON,
                    },
                    False,
                    # self._context,
                )
                self.hass.states.async_set(
                    f"sensor.{tracker_id}_bps_floor",
                    tri_data.get("floor", "unknown"),
                    {
                        "name": f"{tracker_dev.name} BPS Floor",
                        "friendly_name": f"{tracker_dev.name} BPS Floor",
                        "attribution": "Based on data from the Bermuda integration",
                        "icon": FLOOR_ICON,
                    },
                    False,
                    # self._context,
                )

            self.runtime_data.tricoords = new_tricoords
            await asyncio.sleep(
                self.update_frequency
            )  # Run every X seconds, set timer in global variables
        _LOGGER.info("Finished BPS tracker updating loop")

    async def gather_scanner_states(self):
        """Gather scanner states and return new_tricoords with updated radius and coords for each scanner."""
        bermuda_entity_ids = [
            entity.entity_id
            for entity in list(self.ent_reg.entities.values())
            if not entity.disabled
            and "_distance_to_" in entity.entity_id
            and "unfiltered" not in entity.entity_id
        ]

        new_tricoords = {}
        scanner_state_tasks = []
        for tracker_id, scanner_id in [
            item.replace("sensor.", "").split("_distance_to_")
            for item in bermuda_entity_ids
        ]:
            if not self.stored_data.map_data.scanners[scanner_id]["coords"]:
                _LOGGER.debug(
                    "Receiver %s has not been placed using the BPS UI", scanner_id
                )
                continue

            if not self.stored_data.map_data.floors[
                self.stored_data.map_data.scanners[scanner_id]["floor_id"]
            ]["scale"]:
                _LOGGER.debug(
                    "Scale not set for floor '%s'. Skipping scanner %s",
                    self.stored_data.map_data.scanners[scanner_id]["floor_id"],
                    scanner_id,
                )
                continue

            if tracker_id not in new_tricoords:
                new_tricoords[tracker_id] = {"scanners": {}}

            new_tricoords[tracker_id]["scanners"][scanner_id] = {}
            scanner_state_tasks.append(
                self.update_scanner_state(new_tricoords, tracker_id, scanner_id)
            )

        await asyncio.gather(*scanner_state_tasks)
        # _LOGGER.debug(
        #     "Completed scanner state updates, proceeding with trilateration and area updates"
        # )

        return new_tricoords

    async def update_scanner_state(self, new_tricoords, tracker_id, scanner_id):
        """Update scanner state and radius in new_tricoords."""

        entity_id = f"sensor.{tracker_id}_distance_to_{scanner_id}"
        rvcr_state = self.hass.states.get(entity_id)
        new_tricoords[tracker_id]["scanners"][scanner_id] = {
            "state": (rvcr_state.state if rvcr_state else None),
            "radius": 10000,
            "coords": self.stored_data.map_data.scanners[scanner_id]["coords"],
        }

        if new_tricoords[tracker_id]["scanners"][scanner_id]["state"] is None:
            _LOGGER.debug("Entity had no value: %s", entity_id)
            pass
        else:
            try:
                scale = self.stored_data.map_data.floors[
                    self.stored_data.map_data.scanners[scanner_id]["floor_id"]
                ]["scale"]
                state = float(
                    new_tricoords[tracker_id]["scanners"][scanner_id]["state"]
                )
                new_tricoords[tracker_id]["scanners"][scanner_id]["radius"] = (
                    scale * state
                )
            except ValueError:
                _LOGGER.debug(
                    "Invalid numerical value: %s",
                    str(new_tricoords[tracker_id]["scanners"][scanner_id]["state"]),
                )

    async def update_trilateration_and_area(self, new_tricoords, tracker_id):
        """Trilateration with r-value filtering and moving average filtering."""

        # _LOGGER.debug(
        #     "Starting trilateration and area update for tracker %s", tracker_id
        # )

        filter_percent = 0.5  # 50% change in r-value
        filter_value_high = 1 * (1 + filter_percent)
        filter_value_low = 1 * (1 - filter_percent)

        # Store last r-values per sensor and entity
        self.runtime_data.cache.setdefault("last_r_values", {})
        # Store last positions for moving average filtering
        self.runtime_data.cache.setdefault("position_history", {})

        closest_floor_id = self.find_closest_floor_id(
            new_tricoords[tracker_id]["scanners"]
        )
        closest_floor_name = self.stored_data.map_data.floors[closest_floor_id]["name"]

        scanner_ids_on_floor = [
            scanner_id
            for scanner_id in new_tricoords[tracker_id]["scanners"].keys()  # noqa: SIM118
            if self.stored_data.map_data.scanners[scanner_id]["floor_id"]
            == closest_floor_id
        ]

        # Get previous r-values for this entity
        last_r = self.runtime_data.cache["last_r_values"].get(tracker_id) or {}

        # Filter out points where r has changed too much
        filtered = []
        for scanner_id in scanner_ids_on_floor:
            r = new_tricoords[tracker_id]["scanners"][scanner_id]["radius"]
            prev_r = last_r.get(scanner_id)
            if prev_r is not None:
                if (
                    r > prev_r * filter_value_high or r < prev_r * filter_value_low
                ):  # e.g. max 100% change
                    continue  # skip this point
            filtered.append(scanner_id)
        scanner_ids_on_floor = filtered

        # Store current r-values for next time
        self.runtime_data.cache["last_r_values"][tracker_id] = {
            (scanner_id): new_tricoords[tracker_id]["scanners"][scanner_id]["radius"]
            for scanner_id in scanner_ids_on_floor
        }

        if len(scanner_ids_on_floor) < 3:
            # Too few points left for trilateration
            # _LOGGER.debug(
            #     "Cannot trilaterate for tracker %s: only %d valid points after r-value filtering"
            # )
            return

        tricords = self.trilaterate(
            [
                (rec["coords"]["x"], rec["coords"]["y"], rec["radius"])
                for rec in new_tricoords[tracker_id]["scanners"].values()
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
            area_id = self.find_area_for_point(closest_floor_id, test_point)
            new_tricoords[tracker_id]["coords"] = {"x": avg_x, "y": avg_y}
            new_tricoords[tracker_id]["area"] = area_id
            new_tricoords[tracker_id]["floor"] = closest_floor_name

        else:
            new_tricoords[tracker_id]["coords"] = {"x": None, "y": None}
            new_tricoords[tracker_id]["area"] = "unknown"
            new_tricoords[tracker_id]["floor"] = closest_floor_name

    async def cannot_trilaterate(self, message):
        """Handle cases where trilateration can't be performed."""
        _LOGGER.info(message)
        await asyncio.sleep(10)  # Wait before trying again

    def find_closest_floor_id(self, scanners):
        """Find closest floor from filtered scanner coords."""
        closest_scanner_id = min(scanners, key=lambda k: scanners[k]["radius"])
        if self.stored_data.map_data.scanners[closest_scanner_id]["floor_id"]:
            if self.stored_data.map_data.floors.get(
                self.stored_data.map_data.scanners[closest_scanner_id]["floor_id"]
            ):
                return self.stored_data.map_data.scanners[closest_scanner_id][
                    "floor_id"
                ]
            else:
                return "unknown"
        else:
            return "unknown"

    def find_area_for_point(self, closest_floor_id, point):
        """Find area for point, prioritize correct polygon, select nearest buffer if no correct area matches."""
        buffer_percent = 0.05  # set to 5%
        buffer_candidates = []

        for area in self.stored_data.map_data.floors[closest_floor_id]["areas"]:
            if area["coords"]:
                polygon = Polygon(
                    [(coord["x"], coord["y"]) for coord in area["coords"]]
                )
                xs = [coord["x"] for coord in area["coords"]]
                ys = [coord["y"] for coord in area["coords"]]
                width = max(xs) - min(xs)
                height = max(ys) - min(ys)
                buffer_size = ((width + height) / 2) * buffer_percent
                if polygon.contains(point):
                    return area  # Prioritize correct polygon

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
