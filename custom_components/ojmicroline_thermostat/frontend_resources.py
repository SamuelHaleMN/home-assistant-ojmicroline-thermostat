"""Register the bundled native schedule card as a persistent Lovelace module.

Lovelace resources give cached mobile frontends a discovery path independent
of the extra-module URLs embedded in their initial HTML. Only root-relative
URLs for this integration's native card are managed. YAML collections remain
untouched and use the caller's existing extra-JavaScript fallback.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast
from urllib.parse import urlsplit

from homeassistant.components.lovelace.const import LOVELACE_DATA
from homeassistant.components.lovelace.resources import ResourceStorageCollection

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_NATIVE_CARD_PATH = "/ojmicroline_thermostat/ojmicroline-native-schedule-card.js"
_LOCK_KEY = "ojmicroline_thermostat.frontend_resources_lock"


async def async_register_native_card_resource(hass: HomeAssistant, url: str) -> bool:
    """Create or upgrade one owned module resource, preserving its identifier.

    Return True after storage registration succeeds, or False when resources
    are unavailable or YAML-managed. Storage failures propagate to the caller,
    which can report them while retaining its extra-JavaScript fallback.
    Concurrent callers share a per-HA lock so they cannot create duplicates.
    """
    if not _is_owned_url(url):
        msg = "The native card resource requires its owned root-relative URL."
        raise ValueError(msg)
    lovelace = hass.data.get(LOVELACE_DATA)
    collection = None if lovelace is None else lovelace.resources
    if not isinstance(collection, ResourceStorageCollection):
        return False

    lock = cast("asyncio.Lock", hass.data.setdefault(_LOCK_KEY, asyncio.Lock()))
    async with lock:
        # This public method ensures an unloaded storage collection is loaded.
        await collection.async_get_info()
        owned = [
            item for item in collection.async_items() if _is_owned_url(item.get("url"))
        ]
        data = {"url": url, "res_type": "module"}
        if not owned:
            await collection.async_create_item(data)
            return True

        keeper, *duplicates = owned
        if keeper.get("url") != url or keeper.get("type") != "module":
            await collection.async_update_item(keeper["id"], data)
        # Update the retained resource first. A failed update must not remove
        # old resources, and an upgrade does not replace the original ID.
        for duplicate in duplicates:
            await collection.async_delete_item(duplicate["id"])
        return True


def _is_owned_url(value: object) -> bool:
    """Recognize only this root-relative path, independently of query version."""
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value.startswith("//")
    ):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return not parsed.scheme and not parsed.netloc and parsed.path == _NATIVE_CARD_PATH
