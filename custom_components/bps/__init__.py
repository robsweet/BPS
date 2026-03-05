import aiofiles  # type: ignore
import aiofiles.os  # pyright: ignore[reportMissingModuleSource]
from pathlib import Path
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.frontend import (
    async_register_built_in_panel,
    async_remove_panel,
)
from homeassistant.components import panel_custom
from homeassistant.components.websocket_api import (
    async_register_command,
    ActiveConnection,
    websocket_command,
)
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers import (
    floor_registry as fr,
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.template import Template
from homeassistant.core import HomeAssistant
from homeassistant.const import Platform

import numpy as np
from scipy.optimize import least_squares
import voluptuous as vol
import logging
import asyncio
import os
import re
import json
from shapely.geometry import Point, Polygon
from asyncio import Lock, Queue
import traceback

_LOGGER = logging.getLogger(__name__)

DOMAIN = "bps"
FRONTEND_PATH = Path(__file__).parent / "frontend"

PLATFORMS = [Platform.SENSOR]

# Global data
state_change_lock = Lock()
state_change_counter = {}
update_queue = Queue()
secToUpdate = 1


class BPSMapData:
    def __init__(self):
        self.floors = {}
        self.areas = {}
        self.receivers = {}

    def receivers_with_coords(self, floor_data):
        return [
            id
            for id, receiver in floor_data.receivers.items()
            if any(receiver["coords"])
        ]


class BPSTriData:
    def __init__(self):
        self.tricoords = {}
        self.cache = {}


async def cannot_trilaterate(message):
    _LOGGER.info(message)
    await asyncio.sleep(10)


async def update_tracked_entities(hass, floor_data, runtime_data):
    """Update tracked_entities with the result of trilateration once per second."""

    ## TODO:
    ## Add floor_id to floor_data.receivers objects

    global secToUpdate

    jinja_code = """{{
            expand(states.sensor)
            | selectattr("entity_id", "search", "_distance_to_")
            | map(attribute="entity_id")
            | unique
            | list
    }}
    """

    new_tricoords = {}
    while hass.data.get("bps_initialized", False):
        if not any([floor.scale for floor in floor_data.floors.values()]):
            await cannot_trilaterate(
                "No floors have scale data.  Maps probably haven't been set up in the BPS UI."
            )
            continue  # start over

        if len(floor_data.receivers_with_coords(floor_data)) < 3:
            await cannot_trilaterate(
                f"Only {len(floor_data.receivers_with_coords(floor_data))} receivers have coords.  Place at least 3 receivers in the BPS UI."
            )
            continue  # start over

        try:
            template = Template(jinja_code, hass)
            bermuda_entities = template.async_render()
        except Exception as e:
            _LOGGER.info(
                f"Error executing Jinja code: {e} {''.join(traceback.format_exception(e))}"
            )

        new_tricoords = {}

        receiver_state_tasks = []
        for tracker_id, receiver_id in [
            item.replace("sensor.", "").split("_distance_to_")
            for item in bermuda_entities
        ]:
            if not floor_data.receivers[receiver_id]["coords"]:
                _LOGGER.debug(
                    f"Receiver {receiver_id} has not been placed using the BPS UI."
                )
                continue

            if not floor_data.floors[floor_data.receivers[receiver_id]["floor"]][
                "scale"
            ]:
                _LOGGER.debug(
                    f"Scale not set for floor '{floor_data.receivers[receiver_id]['floor']}'. Skipping receiver {receiver_id}."
                )
                continue

            new_tricoords[tracker_id].setdefault({})
            receiver_state_tasks.append(
                asyncio.create_task(
                    update_receiver_state(
                        hass, floor_data, new_tricoords, tracker_id, receiver_id
                    )
                )
            )

        await asyncio.gather(receiver_state_tasks)

        tracker_state_tasks = [
            asyncio.create_task(
                update_trilateration_and_area(
                    hass, floor_data, runtime_data, new_tricoords, tracker_id
                )
            )
            for tracker_id in new_tricoords.keys()
        ]

        await asyncio.gather(tracker_state_tasks)
        ## await asyncio.gather(*state_tasks)  # Run all entities in parallel, but maintain the correct internal order
        ## Why the *?
        await asyncio.sleep(
            secToUpdate
        )  # Run every X seconds, set timer in global variables
        runtime_data.tricoords = new_tricoords


async def update_receiver_state(
    hass, floor_data, new_tricoords, tracker_id, receiver_id
):
    entity_id = f"{tracker_id}_distance_to_{receiver_id}"
    new_tricoords[tracker_id][receiver_id] = {
        "state": hass.states.get(entity_id),
        "radius": None,
        "coords": floor_data.receivers[receiver_id]["coords"],
    }

    if new_tricoords[tracker_id][receiver_id]["state"] is not None:
        try:
            scale = floor_data.floors[floor_data.receivers[receiver_id]["floor"]][
                "scale"
            ]
            state = float(new_tricoords[tracker_id][receiver_id]["state"])
            new_tricoords[tracker_id][receiver_id]["radius"] = scale * state
        except ValueError:
            _LOGGER.debug(
                f"Invalid numerical value: {new_tricoords[tracker_id][receiver_id]['state']}"
            )
    else:
        _LOGGER.debug(f"Entity had no value: {receiver_id}")


async def update_trilateration_and_area(
    hass, floor_data, runtime_data, new_tricoords, tracker_id
):
    """Trilateration with r-value filtering and moving average filtering."""
    filter_percent = 0.5  # 50% change in r-value
    filter_value_high = 1 * (1 + filter_percent)
    filter_value_low = 1 * (1 - filter_percent)

    # Store last r-values per sensor and entity
    runtime_data.cache.setdefault("last_r_values", {})
    # Store last positions for moving average filtering
    runtime_data.cache.setdefault("position_history", {})

    closest_floor_id = find_closest_floor_id(floor_data, new_tricoords[tracker_id])
    closest_floor_name = floor_data[closest_floor_id]["name"]

    receiver_ids_on_floor = [
        receiver_id
        for receiver_id, rec in new_tricoords[tracker_id].receivers.items()
        if rec["floor_id"] == closest_floor_id
    ]
    # receivers_on_floor: list of the receivers on the closest floor to the tracker

    # Get previous r-values for this entity
    last_r = runtime_data.cache["last_r_values"].getdefault(tracker_id, {})

    # Filter out points where r has changed too much
    filtered = []
    for receiver_id in receiver_ids_on_floor:
        r = new_tricoords[tracker_id]["receivers"][receiver_id]["radius"]
        prev_r = last_r.get(receiver_id)
        if prev_r is not None:
            if (
                r > prev_r * filter_value_high or r < prev_r * filter_value_low
            ):  # e.g. max 100% change
                continue  # skip this point
        filtered.append(receiver_id)
    receiver_ids_on_floor = filtered

    # Store current r-values for next time
    runtime_data.cache["last_r_values"][tracker_id] = {
        (rec["receiver_id"]): rec["radius"] for rec in receiver_ids_on_floor
    }

    if len(receiver_ids_on_floor) < 3:
        # Too few points left for trilateration
        return

    tricords = trilaterate(
        [
            (rec["coords"]["x"], rec["coords"]["y"], rec["radius"])
            for rec in new_tricoords[tracker_id][receiver_id]
        ]
    )
    if tricords is not None:
        # Moving average filtering
        history = runtime_data.cache["position_history"].setdefault(tracker_id, [])
        history.append(tricords)
        if len(history) > 3:  # Keep only the last 3 positions
            history.pop(0)
        avg_x = sum(pos[0] for pos in history) / len(history)
        avg_y = sum(pos[1] for pos in history) / len(history)

        test_point = Point(float(avg_x), float(avg_y))
        area = find_area_for_point(floor_data.floors[closest_floor_id], test_point)
        new_tricoords[tracker_id].merge({"cords": [avg_x, avg_y], "area": area})

        hass.states.async_set(f"sensor.{tracker_id}_bps_area", area)
        hass.states.async_set(f"sensor.{tracker_id}_bps_floor", closest_floor_name)


def find_closest_floor_id(floor_data, receivers):
    """Find closest floor and filter receiver cords."""
    return min([rec["radius"] for rec in receivers if rec["radius"]])["floor_id"]


def find_area_for_point(data, entity, floor_data, point):
    """Find area for point, prioritize correct polygon, select nearest buffer if no correct area matches."""
    buffer_percent = 0.05  # set to 5%
    buffer_candidates = []

    for area in floor_data["areas"]:
        polygon = Polygon([(coord["x"], coord["y"]) for coord in area["cords"]])
        xs = [coord["x"] for coord in area["cords"]]
        ys = [coord["y"] for coord in area["cords"]]
        width = max(xs) - min(xs)
        height = max(ys) - min(ys)
        buffer_size = ((width + height) / 2) * buffer_percent
        if polygon.contains(point):
            return area["entity_id"]  # Prioritize correct polygon
        elif polygon.buffer(buffer_size).contains(point):
            # Save candidate: (distance to edge, entity_id)
            distance_to_edge = polygon.exterior.distance(point)
            buffer_candidates.append((distance_to_edge, area["entity_id"]))

    if buffer_candidates:
        # Select area whose edge is closest to the point
        buffer_candidates.sort()
        return buffer_candidates[0][1]
    return "unknown"


async def _ensure_panel_registered(hass: HomeAssistant) -> bool:
    """Ensure the panel is registered, used for retry scenarios."""
    _LOGGER.debug("\tBPS: Ensuring panel registration")

    # Check if www directory exists
    www_path = hass.config.path("custom_components/bps/frontend")
    try:
        js_file = os.path.join(www_path, "rob_test_panel.js")
        if not os.path.exists(js_file):
            _LOGGER.error("\t\tBPS: Frontend JS file missing at %s", js_file)
            return False

        _LOGGER.info("\t\tBPS: Frontend files verified at %s", www_path)
    except Exception as e:
        _LOGGER.error("\t\tBPS: Error checking frontend files: %s", e)
        return False

    try:
        # Register static paths if not already registered
        await hass.http.async_register_static_paths(
            [StaticPathConfig("/bps/", www_path, False)]
        )
        _LOGGER.debug("\t\tBPS: Static paths registered successfully")
    except Exception as e:
        # Static paths might already be registered, this is not critical
        _LOGGER.debug(
            "\t\tBPS: Static paths registration skipped or failed (likely already registered): %s",
            e,
        )

    # Register the panel with defensive error handling
    try:
        await panel_custom.async_register_panel(
            hass,
            frontend_url_path="bps",
            webcomponent_name="rob-test-panel",
            sidebar_title="BPS",
            sidebar_icon="mdi:graph",
            js_url="/bps/rob-test-panel.js",
            # module_url="/bps/rob-test-panel.js",
            config={},
            require_admin=False,
        )
        _LOGGER.info(
            "\t\tBPS: ✅ Panel registered successfully - look for 'BPS' in your sidebar!"
        )
        return True
    except ValueError as e:
        if "Overwriting panel" in str(e):
            _LOGGER.debug("\t\tBPS: Panel already registered, skipping registration")
            return True
        else:
            _LOGGER.error("\t\tBPS: ❌ Failed to register panel (ValueError): %s", e)
            return False
    except Exception as e:
        _LOGGER.error(
            "\t\tBPS: ❌ Unexpected error during panel registration: %s",
            e,
            exc_info=True,
        )
        return False


async def do_async_setup(hass, config):
    """Set up the BPS integration."""
    if hass.data.get("bps_initialized", False):
        _LOGGER.warning("BPS has already started initializing. Aborting")
        return True  # Abort if already running

    def wait_until_hass_has_states(hass):
        _LOGGER.debug("\tWaiting for hass.states to exist...")
        while not hass.states:
            _LOGGER.debug("\t\tStill waiting...")
            sleep(3)

    def generate_new_data(hass):
        floor_reg = fr.async_get(hass)
        area_reg = ar.async_get(hass)
        ent_reg = er.async_get(hass)
        dev_reg = dr.async_get(hass)

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

        receiver_ids = {
            re.sub(".*_distance_to_", "", key)
            for key in hass.data["entity_info"]
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

    async def start_data_processing_when_ready(hass, runtime_data):
        wait_until_hass_has_states(hass)

        if not hass.data[DOMAIN].floors:
            hass.data[DOMAIN] = generate_new_data(hass)

        hass.async_create_task(
            update_tracked_entities(hass, hass.data[DOMAIN], runtime_data)
        )

        _LOGGER.info("The BPS integration is fully initialized")

    async def initialize_bps():
        """Initialize the BPS component"""
        _LOGGER.info("Initializing BPS...")

        hass.data.setdefault(DOMAIN, BPSMapData())
        hass.data["bps_initialized"] = True  # Set flag

        target_dir = os.path.join(hass.config.path(), "www", "bps_maps")
        try:
            await aiofiles.os.makedirs(target_dir, exist_ok=True)
            _LOGGER.info(f"\tFolder {target_dir} has been created or already existed")
        except Exception as e:
            _LOGGER.error(f"\tCould not create the folder {target_dir}: {e}")
            return

        hass.async_create_task(
            start_data_processing_when_ready(hass, config.runtime_data)
        )

        # if "bps_websocket" not in hass.data:
        #     websocket = BPSEntityWebSocket(hass)
        #     websocket.register()
        #     hass.data["bps_websocket"] = websocket

        # panels = hass.data.get("frontend_panels", {})
        # if "bps" in panels:
        #     async_remove_panel(hass, "bps")
        # try:
        #     _LOGGER.debug("\tRegistering the built-in panel for BPS...")
        #     async_register_built_in_panel(
        #         hass=hass,
        #         component_name="iframe",
        #         sidebar_title="BPS",
        #         sidebar_icon="mdi:map",
        #         frontend_url_path="bps",
        #         config={"url": "/bps/index.html"},
        #     )
        #     _LOGGER.info("\t\tPanel registered successfully.")
        # except Exception as e:
        #     _LOGGER.error(f"\t\tFailed to register panel: {e}")

    async def handle_homeassistant_started(event):
        await initialize_bps()

    async def remove_sensors(hass, config):
        _LOGGER.info(f"Removing sensors for integration unload")
        entity_registry = er.async_get(hass)

        # Find and remove all entities that belong to "bps"
        entities_to_remove = [
            entity.entity_id
            for entity in entity_registry.entities.values()
            if entity.platform == "bps"
        ]

        for entity_id in entities_to_remove:
            _LOGGER.info(f"\tRemoving sensor: {entity_id}")
            entity_registry.async_remove(entity_id)
        _LOGGER.info(f"Done removing sensors")

    async def handle_homeassistant_stop(event):
        hass.data["bps_initialized"] = False
        await remove_sensors(hass, config)

    if hass.is_running:
        await initialize_bps()
    else:
        hass.bus.async_listen_once(
            "homeassistant_started", handle_homeassistant_started
        )

    hass.bus.async_listen_once("homeassistant_stop", handle_homeassistant_stop)

    return True


async def async_unload_entry(hass: HomeAssistant, entry):
    """Remove a configuration entry"""
    _LOGGER.info("Attempting to unload platforms for entry: %s", entry.entry_id)

    hass.data["bps_initialized"] = False

    try:  # Attempt to unload platforms
        unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    except Exception as e:
        _LOGGER.error(
            f"\tError during unloading of platforms for entry {entry.entry_id}: {e}"
        )
        return False

    if not unload_ok:
        _LOGGER.error("\tFailed to unload platforms for entry: %s", entry.entry_id)
        return False

    try:  # Remove the frontend panel
        async_remove_panel(hass, frontend_url_path="bps")
        _LOGGER.info("\tFrontend-panel removed for entry: %s", entry.entry_id)
    except Exception as e:
        _LOGGER.error(
            f"\tError when removing frontend-panel for entry {entry.entry_id}: {e}"
        )
        return False

    return True


async def async_setup_entry(hass, entry):
    """Set the integration from a configuration entry"""
    _LOGGER.info("\tStarting integration setup")
    entry.runtime_data = BPSTriData()

    returns = await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    """Set up BPS from a config entry."""
    return await do_async_setup(hass, entry)


class BPSFrontendView(HomeAssistantView):
    """Serve the frontend files."""

    url = "/bps/{file_name}"
    name = "bps:frontend"
    requires_auth = False
    # requires_auth = True

    async def get(self, request, file_name):
        """Serve static files from the frontend folder."""
        frontend_path = FRONTEND_PATH / file_name

        _LOGGER.info(f"Serving file: {frontend_path}")

        if not frontend_path.is_file():
            _LOGGER.error(f"Requested file not found: {frontend_path}")
            return web.Response(status=404, text="File not found")

        return web.FileResponse(path=str(frontend_path))


class BPSSaveAPIText(HomeAssistantView):
    """Handle saving of BPS coordinates to a text file."""

    url = "/api/bps/save_text"
    name = "api:bps:save_text"
    requires_auth = False

    async def post(self, request):
        """Handle saving coordinates to a text file."""
        hass = request.app["hass"]
        data = await request.post()

        coordinates = data.get("coordinates")

        if not coordinates:
            return web.Response(status=400, text="Missing coordinates")

        # Define the path to the bpsdata file
        maps_path = hass.config.path("www/bps_maps")
        bpsdata_file_path = Path(maps_path) / "bpsdata.txt"

        try:  # Save coordinates to the bpsdata file
            async with aiofiles.open(bpsdata_file_path, "w") as f:
                await f.write(coordinates)
                if (
                    data.get("new_floor") == "true"
                ):  # If it is a new floor then save the file
                    map_file = data.get("file")
                    if not map_file:
                        return web.Response(status=400, text="Missing file")
                    map_file_path = Path(maps_path) / map_file.filename
                    try:
                        async with aiofiles.open(map_file_path, "wb") as f:
                            await f.write(map_file.file.read())
                    except Exception as e:
                        _LOGGER.error(f"Failed to save maps: {e}")
                        return web.Response(status=500, text="Failed to save maps")

                # Check if "remove" key exists and delete the specified file
                remove_file = data.get("remove")
                if remove_file:
                    remove_file_path = Path(maps_path) / remove_file
                    if remove_file_path.exists():
                        _LOGGER.warning(f"File exist: {remove_file_path}")
                        try:
                            remove_file_path.unlink()  # Delete the file
                            _LOGGER.info(f"Removed file: {remove_file_path}")
                        except Exception as e:
                            _LOGGER.error(
                                f"Failed to remove file {remove_file_path}: {e}"
                            )
                            return web.Response(
                                status=500, text="Failed to remove file"
                            )

            _LOGGER.info(f"Saved coordinates to bpsdata: {coordinates}")
            return web.Response(status=200, text="Coordinates saved successfully")

        except Exception as e:
            _LOGGER.error(f"Failed to save coordinates: {e}")
            return web.Response(status=500, text="Failed to save coordinates")


class BPSReadAPIText(HomeAssistantView):
    """Handle reading of BPS coordinates from a text file."""

    url = "/api/bps/read_text"
    name = "api:bps:read_text"
    requires_auth = False

    async def get(self, request):
        """Handle reading coordinates from the text file."""
        hass = request.app["hass"]
        maps_path = hass.config.path(
            "www/bps_maps"
        )  # Define the path to the bpsdata file
        bpsdata_file_path = Path(maps_path) / "bpsdata.txt"
        entityjinja = """
        {{
            expand(states.sensor)
            | selectattr("entity_id", "search", "_distance_to_")
            | map(attribute="entity_id")
            | map("replace", "sensor.", "")
            | map("regex_replace", "_distance_to_.*", "")
            | unique
            | list
        }}
        """

        try:
            template = Template(entityjinja, hass)  # Render Jinja code
            entities = template.async_render()
        except Exception as e:
            _LOGGER.info(f"Error during the execution of the Jinja code: {e}")

        try:
            if not bpsdata_file_path.is_file():  # Check if the file exists
                return web.Response(status=404, text="bpsdata.txt not found")

            async with aiofiles.open(
                bpsdata_file_path, "r"
            ) as f:  # Read the content of the file
                content = await f.read()

            _LOGGER.info(f"Read coordinates from bpsdata: {content}")
            return web.json_response({"coordinates": content, "entities": entities})

        except Exception as e:
            _LOGGER.error(f"Failed to read coordinates: {e}")
            return web.Response(status=500, text="Failed to read coordinates")


class BPSMapsListAPI(HomeAssistantView):
    """API to list map files in /www/bps_maps."""

    url = "/api/bps/maps"
    name = "api:bps:maps"
    requires_auth = False

    async def get(self, request):
        """Return a list of map files as JSON."""
        hass = request.app["hass"]
        maps_path = hass.config.path("www/bps_maps")

        try:
            files = [
                f
                for f in os.scandir(maps_path)
                if f.is_file() and f.name.lower().endswith((".png", ".jpg"))
            ]
            file_names = [f.name for f in files]
            return web.json_response(file_names)
        except Exception as e:
            _LOGGER.error(f"Error listing map files: {e}")
            return web.Response(status=500, text="Error listing map files")


class BPSCordsAPI(HomeAssistantView):
    """API för att skicka tillbaka apitricords"""

    url = "/api/bps/cords"
    name = "api:bps:cords"
    requires_auth = False  # Ändra till True om du vill kräva autentisering

    def __init__(self, hass):
        """Spara referens till hass"""
        self.hass = hass

    async def get(self, request):
        """Returnera apitricords från hass.data"""
        apitricords = self.hass.runtime_data.get(DOMAIN, None)
        if not apitricords:
            return web.json_response({"error": "No data available"}, status=404)

        return web.json_response(apitricords)


class BPSEntityWebSocket:
    def __init__(self, hass):
        self.hass = hass
        self.tracked_entities = {}
        self.connections = []

    async def handle_subscribe(self, hass, connection: ActiveConnection, msg: dict):
        """Managing subscription for entities"""
        _LOGGER.debug(f"Received subscription request: {msg}")
        entity_ids = msg["entities"]
        if not entity_ids:
            connection.send_message(
                {
                    "id": msg["id"],
                    "type": "result",
                    "success": False,
                    "error": {
                        "code": "invalid_request",
                        "message": "No entities provided.",
                    },
                }
            )
            return

        self.connections.append(connection)  # Add a connection to subscribed entities
        for entity_id in entity_ids:
            if entity_id not in self.tracked_entities:
                self.tracked_entities[entity_id] = []
            self.tracked_entities[entity_id].append(connection)

        current_states = []  # Send the current state for all subscribed entities
        for entity_id in entity_ids:
            state = hass.states.get(entity_id)
            if state:
                current_states.append(
                    {
                        "entity_id": entity_id,
                        "state": state.state,
                        "attributes": state.attributes,
                    }
                )

        connection.send_message(
            {
                "id": msg["id"],
                "type": "result",
                "success": True,
                "message": f"Subscribed to entities: {entity_ids}",
                "current_states": current_states,
            }
        )
        async_track_state_change_event(
            hass, entity_ids, self.state_change_listener
        )  # Listen for state_change

    async def handle_unsubscribe(self, hass, connection: ActiveConnection, msg: dict):
        """Managing unsubscription"""
        _LOGGER.debug(f"Received unsubscribe request: {msg}")
        entity_ids = msg.get("entities", [])
        for entity_id in entity_ids:
            if entity_id in self.tracked_entities:
                if connection in self.tracked_entities[entity_id]:
                    self.tracked_entities[entity_id].remove(connection)
                if not self.tracked_entities[entity_id]:
                    del self.tracked_entities[entity_id]

        connection.send_message(
            {
                "id": msg["id"],
                "type": "result",
                "success": True,
                "message": f"Unsubscribed from entities: {entity_ids}",
            }
        )

    async def handle_known_points(self, hass, connection: ActiveConnection, msg: dict):
        try:
            known_points = msg.get("knownPoints")  # Read knownPoints from the message
            if not known_points:
                connection.send_message(
                    {
                        "id": msg["id"],
                        "type": "tri_result",
                        "success": False,
                        "error": {
                            "code": "invalid_request",
                            "message": "No knownPoints provided.",
                        },
                    }
                )
                return

            result = trilaterate(known_points)  # Perform trilateration

            if result is None:  # If the result is None, return an error
                connection.send_message(
                    {
                        "id": msg["id"],
                        "type": "tri_result",
                        "success": False,
                        "error": {
                            "code": "calculation_error",
                            "message": "Trilateration failed.",
                        },
                    }
                )
                return

            connection.send_message(
                {  # Send back the result
                    "id": msg["id"],
                    "type": "tri_result",
                    "success": True,
                    "result": {"x": result[0], "y": result[1]},
                }
            )

        except Exception as e:
            _LOGGER.error(f"Error processing knownPoints: {e}")
            connection.send_message(
                {
                    "id": msg["id"],
                    "type": "tri_result",
                    "success": False,
                    "error": {"code": "server_error", "message": str(e)},
                }
            )

    async def state_change_listener(self, event):
        """Listens for status changes and sends them to connections."""
        entity_id = event.data.get("entity_id")
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")

        _LOGGER.debug(f"State change for {entity_id}: {old_state} -> {new_state}")

        # Create the message to send to subscribing clients
        message = {
            "type": "state_changed",
            "entity_id": entity_id,
            "old_state": old_state.state if old_state else None,
            "new_state": new_state.state if new_state else None,
        }

        # Send to all connected clients who are subscribed to this entity
        for connection in self.tracked_entities.get(entity_id, []):
            connection.send_message(message)

    def register(self):
        """Registers WebSocket commands"""
        _LOGGER.debug("\tRegistering WebSocket commands")

        def subscribe_wrapper(hass, connection, msg):
            """Wrapper to invoke handle_subscribe."""
            hass.async_create_task(self.handle_subscribe(hass, connection, msg))

        def unsubscribe_wrapper(hass, connection, msg):
            """Wrapper to invoke handle_unsubscribe."""
            hass.async_create_task(self.handle_unsubscribe(hass, connection, msg))

        def known_points_wrapper(hass, connection, msg):
            """Wrapper to invoke handle_known_points."""
            hass.async_create_task(self.handle_known_points(hass, connection, msg))

        async_register_command(
            self.hass,
            "bps/subscribe",
            subscribe_wrapper,  # The wrapper handles async
            schema=vol.Schema(
                {
                    vol.Required("type"): "bps/subscribe",  # Type for API
                    vol.Required("entities"): [str],
                    vol.Optional("id"): int,
                }
            ),
        )
        async_register_command(
            self.hass,
            "bps/unsubscribe",
            unsubscribe_wrapper,  # The wrapper handles async
            schema=vol.Schema(
                {
                    vol.Required("type"): "bps/unsubscribe",  # Type for API
                    vol.Required("entities"): [str],
                    vol.Optional("id"): int,
                }
            ),
        )
        async_register_command(
            self.hass,
            "bps/known_points",
            known_points_wrapper,  # The wrapper handles async
            schema=vol.Schema(
                {
                    vol.Required("type"): "bps/known_points",  # Type for API
                    vol.Required("knownPoints"): vol.All(
                        list, [vol.All([float, float, float])]
                    ),
                    vol.Optional("id"): int,
                }
            ),
        )

        _LOGGER.info("\t\tAll WebSocket commands registered successfully.")


# Trilateration function
def trilaterate(known_points):
    num_points = len(known_points)

    if (
        num_points < 3
    ):  # Make sure there are enough points (min 3) to do a trilataration
        _LOGGER.error("At least three known points are required for trilateration.")
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
        _LOGGER.error("Weighted nonlinear least squares fitting did not converge.")
        return None
    x, y = result.x  # Extract the calculated coordinates
    return x, y  # return the result
