"""Utility funtions for BPS Integration."""

import re


def name_to_id(name) -> str:
    """Convert a device name string to the form that Bermuda uses in their sensor names."""
    return re.sub(r"[^A-Za-z0-9]+", "_", name.lower())
