"""Constants for the BPS integration."""

from pathlib import Path

from homeassistant.helpers.entity_registry import Platform

DOMAIN = "bps"
FRONTEND_PATH = Path(__file__).parent / "frontend"

PLATFORMS = [Platform.SENSOR]

AREA_ICON = "mdi:home-map-marker"
SCANNER_ICON = "mdi:radio"
TRACKER_ICON = "mdi:cellphone-marker"
FLOOR_ICON = "mdi:floor-plan"
DISTANCE_ICON = "mdi:signal-distance-variant"
