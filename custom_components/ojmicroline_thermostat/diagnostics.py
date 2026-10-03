"""Bounded operational diagnostics; never include credentials or home state."""

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_APPLICATION, CONF_MODEL, DOMAIN


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Provide HA's native diagnostic download with only safe operational data."""
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    return {
        "model": entry.data.get(CONF_MODEL),
        "application": entry.data.get(CONF_APPLICATION),
        "loaded": coordinator is not None,
        "status": coordinator.diagnostic_status() if coordinator is not None else None,
    }
