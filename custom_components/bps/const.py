"""Constants for the BPS integration."""

from pathlib import Path

from homeassistant.helpers.entity_registry import Platform

DOMAIN = "bps"
FRONTEND_PATH = Path(__file__).parent / "frontend"

PLATFORMS = [Platform.SENSOR]
