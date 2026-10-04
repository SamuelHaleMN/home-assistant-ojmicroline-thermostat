"""Register the native program action even when its account is unavailable."""

import voluptuous as vol
from homeassistant.core import HomeAssistant, SupportsResponse, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_platform_entity_service

from .const import DOMAIN, SERVICE_SET_NATIVE_SCHEDULE


@callback
def async_register_native_schedule_service(hass: HomeAssistant) -> None:
    """Expose one response-only WG4 action through HA's platform entity helper."""
    async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_SET_NATIVE_SCHEDULE,
        entity_domain="climate",
        schema={
            vol.Required("changes"): vol.All(
                cv.ensure_list, [dict], vol.Length(max=42)
            ),
            vol.Required("expected_hash"): vol.All(str, vol.Match(r"^[0-9a-f]{64}$")),
            vol.Required("temperature_unit"): vol.In(("C", "F")),
            vol.Optional("dry_run", default=True): bool,
        },
        func="async_set_native_schedule",
        supports_response=SupportsResponse.ONLY,
    )
