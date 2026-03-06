"""Class to handle UI interactions for the BPS integration."""

from __future__ import annotations

# from aiohttp import web
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from homeassistant.components import panel_custom
from homeassistant.components.frontend import (
    # async_register_built_in_panel,
    async_remove_panel,
)
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

# from homeassistant.helpers import (
#     area_registry as ar,
#     device_registry as dr,
#     entity_registry as er,
#     floor_registry as fr,
# )
# from homeassistant.helpers.template import Template
from .const import DOMAIN

if TYPE_CHECKING:
    from .data_classes import BPSRuntimeData, BPSStoredData

_LOGGER = logging.getLogger(__name__)


class BPSUiManager:
    """Class to handle map data updates from the UI for BPS integration."""

    def __init__(
        self, hass: HomeAssistant, bps_data: BPSStoredData, runtime_data: BPSRuntimeData
    ) -> None:
        """Initialize the data updater."""

        self.hass = hass
        self.bps_data = bps_data
        self.runtime_data = runtime_data

    async def ensure_panel_js_exists_for_frontend(self, www_path):
        """Ensure the necessary JS files exist for the frontend panel."""

        # Check if www directory exists
        try:
            js_file = Path().joinpath(www_path, "rob_test_panel.js")
            if not Path.exists(js_file):
                _LOGGER.error("\t\tBPS: Frontend JS file missing at %s", js_file)
                return False

            _LOGGER.info("\t\tBPS: Frontend files verified at %s", www_path)
        except Exception:
            _LOGGER.exception("\t\tBPS: Error checking frontend files")
            return False

    async def ensure_static_path_set_up_for_frontend(self, www_path):
        """Ensure the static paths are set up to serve the frontend files."""
        # Register static paths to serve with the UI
        try:
            # Register static paths if not already registered
            self.hass.http.async_register_static_paths(
                [StaticPathConfig("/bps/", www_path, False)]
            )
            _LOGGER.debug("\t\tBPS: Static paths registered successfully")
        except Exception:
            # Static paths might already be registered, this is not critical
            _LOGGER.debug(
                "\t\tBPS: Static paths registration skipped or failed (likely already registered)"
            )

    async def config_main_bps_panel(self):
        """Configure the main BPS panel in the HA UI."""
        # Register panels for the UI with defensive error handling
        # Ensure the panel is registered.
        _LOGGER.debug("\tBPS: Ensuring panel registration")

        try:
            await panel_custom.async_register_panel(
                self.hass,
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

        except ValueError as e:
            if "Overwriting panel" in str(e):
                _LOGGER.debug(
                    "\t\tBPS: Panel already registered, skipping registration"
                )
            else:
                _LOGGER.error("\t\tBPS: ❌ Failed to register panel (ValueError)")
                return False
        except Exception:
            _LOGGER.exception("\t\tBPS: ❌ Unexpected error during panel registration")
            return False

    async def async_config(self) -> bool:
        """Configure the UI components for the integration."""

        _LOGGER.debug("Starting BPS UI configuration")
        www_path = Path().joinpath(self.hass.config.path(), "www", "bps_maps")
        if not await self.ensure_panel_js_exists_for_frontend(www_path):
            return False

        if not await self.ensure_static_path_set_up_for_frontend(www_path):
            return False

        if not await self.config_main_bps_panel():
            return False

        _LOGGER.info("BPS UI configuration complete")
        return True

    async def async_unload(self) -> bool:
        """Ensure UI panels are removed."""

        try:  # Remove the frontend panel
            async_remove_panel(self.hass, frontend_url_path="bps")
            _LOGGER.info(
                "\tFrontend-panel removed for entry: %s",
                self.runtime_data.entry.entry_id,
            )
        except Exception:
            _LOGGER.exception(
                "\tError when removing frontend-panel for entry %s",
                self.runtime_data.entry.entry_id,
            )
            return False

        return True
