"""Data classes for the Bluetooth Positioning System (BPS) integration."""

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, callback

from .bps_map_data_updater import BPSMapDataUpdater
from .bps_tri_data_updater import BPSTriDataUpdater
from .bps_ui_manager import BPSUiManager
from .const import DOMAIN


@dataclass
class BPSMapData:
    """Data structure to hold all the BPS map-related data."""

    def __init__(self) -> None:
        """Initialize the BPS map data structure."""
        self.floors = {}
        self.areas = {}
        self.receivers = {}

    def receivers_with_coords(self, map_data):
        """Return a list of receiver IDs that have coordinates set (via the UI) for the given floor."""
        return [
            rid
            for rid, receiver in map_data.receivers.items()
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
