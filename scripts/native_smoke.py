"""Qualify native HA imports and entities using synthetic WG4 data.

Run ``python -m scripts.native_smoke`` from the checkout in a Linux environment
with the intended Home Assistant version and pinned client installed. No HA
server is started, no cloud request is made, and no thermostat is modified.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import sys
import tempfile
from datetime import timedelta
from importlib.metadata import version
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from homeassistant.components.lovelace.const import LOVELACE_DATA
from homeassistant.components.lovelace.dashboard import LovelaceStorage
from homeassistant.components.lovelace.resources import ResourceStorageCollection
from homeassistant.components.network import async_get_adapters
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant
from homeassistant.helpers import frame
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import DATA_DOMAIN_PLATFORM_ENTITIES
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from ojmicroline_thermostat import WD5API, Thermostat

from custom_components.ojmicroline_thermostat.api import (
    api_from_config_entry_data,
    oj_microline_from_api,
)
from custom_components.ojmicroline_thermostat.climate import OJMicrolineThermostat
from custom_components.ojmicroline_thermostat.config_flow import OJMicrolineFlowHandler
from custom_components.ojmicroline_thermostat.coordinator import (
    OJMicrolineDataUpdateCoordinator,
)
from custom_components.ojmicroline_thermostat.frontend_resources import (
    async_register_native_card_resource,
)
from custom_components.ojmicroline_thermostat.reliability import (
    ReliableOJMicroline,
    ReliableWG4API,
)
from custom_components.ojmicroline_thermostat.services import (
    async_register_native_schedule_service,
)
from custom_components.ojmicroline_thermostat.wg4_schedule import WG4Schedule

MODULE_NAMES = (
    "api",
    "reliability",
    "config_flow",
    "climate",
    "models",
    "sensor",
    "binary_sensor",
    "coordinator",
    "date",
    "switch",
    "diagnostics",
    "wg4_schedule",
    "services",
    "frontend_resources",
)


class SmokeCoordinator(DataUpdateCoordinator[dict[str, Thermostat]]):
    """Use native HA coordinator state without running an external API poll."""

    wd5_api: WD5API | None = None


async def qualify_native_action(
    hass: HomeAssistant,
    model: Thermostat,
    api: ReliableWG4API,
    client: ReliableOJMicroline,
) -> None:
    """Exercise the real HA entity service and cached coordinator preview."""
    # Synthetic cache injection is intentional; the real constructor starts
    # account discovery, which this zero-network native qualification excludes.
    # pylint: disable=protected-access
    week = [
        {
            "WeekDayGrpNo": day,
            "Events": [
                {
                    "ScheduleType": slot,
                    "Clock": f"{6 + slot * 2:02d}:00:00",
                    "TempFloor": 2555,
                    "Active": slot == 0,
                }
                for slot in range(6)
            ],
        }
        for day in range(1, 8)
    ]
    # This disposable coordinator uses native HA publication and dispatch but
    # never starts a poll. Any accidental transport request fails qualification.
    coordinator = object.__new__(OJMicrolineDataUpdateCoordinator)
    # pylint: disable-next=unnecessary-dunder-call
    DataUpdateCoordinator.__init__(
        coordinator,
        hass,
        logging.getLogger("oj-native-action"),
        name="oj-native-action",
        config_entry=None,
    )
    coordinator._model_api = api  # noqa: SLF001
    coordinator._account_lock = asyncio.Lock()  # noqa: SLF001
    coordinator.wd5_api = None
    coordinator.api = client
    api._schedule_snapshots[model.serial_number] = {  # noqa: SLF001
        "Schedules": week,
        "MinTemp": 500,
        "MaxTemp": 4000,
        "TZOffset": "-04:00",
    }
    no_network = AsyncMock(side_effect=AssertionError("Unexpected cloud request"))
    api.request = no_network
    coordinator.async_set_updated_data({model.serial_number: model})
    entity = OJMicrolineThermostat(coordinator, model.serial_number, {})
    entity.hass = hass
    entity.entity_id = "climate.native_demo"
    # Entity state publication was qualified above; isolate that unrelated hook
    # while testing the real service helper's selection/validation/response path.
    entity.async_update_ha_state = AsyncMock()
    hass.data[DATA_DOMAIN_PLATFORM_ENTITIES] = {
        ("climate", "ojmicroline_thermostat"): {entity.entity_id: entity}
    }
    async_register_native_schedule_service(hass)
    service_data: dict[str, Any] = {
        "changes": [{"day": "monday", "slot": 0, "temperature": 80}],
        "expected_hash": WG4Schedule(week).fingerprint(),
        "temperature_unit": "F",
    }
    response = await hass.services.async_call(
        "ojmicroline_thermostat",
        "set_native_schedule",
        service_data,
        blocking=True,
        return_response=True,
        target={"entity_id": entity.entity_id},
    )
    assert response is not None
    preview = response[entity.entity_id]
    assert preview["dry_run"] is True
    assert preview["changed"] is True
    assert preview["temperature_unit"] == "C"
    assert preview["days"]["monday"][0]["temperature"] == 26.66
    assert preview["days"]["tuesday"][0]["temperature"] == 25.55
    no_network.assert_not_awaited()


async def qualify_native_resources(hass: HomeAssistant) -> None:
    """Use HA's real storage collection to check registration and idempotence."""
    resources = ResourceStorageCollection(hass, LovelaceStorage(hass, None))
    hass.data[LOVELACE_DATA] = SimpleNamespace(resources=resources)
    native_url = "/ojmicroline_thermostat/ojmicroline-native-schedule-card.js?v=1.6.1"
    assert await async_register_native_card_resource(hass, native_url)
    resource = resources.async_items()[0]
    assert resource["url"] == native_url
    assert resource["type"] == "module"
    assert await async_register_native_card_resource(hass, native_url)
    assert resources.async_items() == [resource]


async def qualify(config_dir: str) -> dict[str, str | int]:
    """Return successful native checks; exceptions fail the command."""
    assert version("ojmicroline-thermostat") == "3.6.0"
    for name in MODULE_NAMES:
        importlib.import_module(f"custom_components.ojmicroline_thermostat.{name}")

    hass = HomeAssistant(config_dir)
    frame.async_setup(hass)
    hass.config.units = US_CUSTOMARY_SYSTEM
    hass.config_entries = ConfigEntries(hass, {})
    # The real shared-session factory needs HA's loaded network adapters.
    # This prepares only this disposable instance; no cloud/API call is made.
    await async_get_adapters(hass)
    await qualify_native_resources(hass)
    api = api_from_config_entry_data(
        {
            "model": "WG4 series",
            "username": "synthetic@example.invalid",
            "password": "synthetic",
            "host": "warmtiles.mythermostat.info",
            "application": 13,
        }
    )
    session = async_get_clientsession(hass)
    client = oj_microline_from_api(api, hass)
    try:
        assert isinstance(client, ReliableOJMicroline)
        # pylint: disable-next=protected-access
        assert client._wg4_session is session  # noqa: SLF001 - ownership contract
        model = Thermostat.from_wg4_json(
            {
                "SerialNumber": "native-demo",
                "SWVersion": "demo",
                "GroupName": "demo",
                "GroupId": 1,
                "Room": "demo",
                "Online": True,
                "Heating": False,
                "RegulationMode": 3,
                "LastPrimaryModeIsAuto": False,
                "Temperature": 2000,
                "SetPointTemp": 2100,
                "MinTemp": 500,
                "MaxTemp": 4000,
                "ManualTemperature": 2100,
                "ComfortTemperature": 2100,
                "ComfortEndTime": "01/01/2020 00:00:00 +00:00",
                "VacationEnabled": False,
                "VacationBeginDay": "01/01/2020 00:00:00",
                "VacationEndDay": "02/01/2020 00:00:00",
                "VacationTemperature": 500,
                "TZOffset": "-04:00",
            }
        )
        coordinator = SmokeCoordinator(
            hass,
            logging.getLogger("oj-native-smoke"),
            name="oj-native-smoke",
            config_entry=None,
            update_interval=timedelta(seconds=60),
        )
        coordinator.async_set_updated_data({"native-demo": model})
        entity = OJMicrolineThermostat(coordinator, "native-demo", {})
        entity.hass = hass
        assert entity.available
        attributes = entity.capability_attributes
        assert attributes["min_temp"] == 41
        assert attributes["max_temp"] == 104
        assert "comfort" not in entity.preset_modes
        state = entity.state_attributes
        assert abs(state["temperature"] - 69.8) < 0.51
        coordinator.async_set_updated_data({})
        assert not entity.available
        assert entity.capability_attributes["min_temp"] == 41
        flow = OJMicrolineFlowHandler()
        flow.hass = hass
        result = await flow.async_step_user()
        assert result["step_id"] == "user"
        assert isinstance(api, ReliableWG4API)
        await qualify_native_action(hass, model, api, client)
        await client.close()
        assert not session.closed
        return {
            "python": sys.version.split()[0],
            "homeassistant": version("homeassistant"),
            "client": version("ojmicroline-thermostat"),
            "native_import_count": len(MODULE_NAMES),
            "model_factory_adapter": "passed",
            "passed_session_preserved": "passed",
            "fahrenheit_capabilities": "passed",
            "fahrenheit_state": "passed",
            "missing_device_capabilities": "passed",
            "native_user_flow": "passed",
            "native_schedule_action_response": "passed",
            "native_schedule_fahrenheit_patch": "passed",
            "native_lovelace_resource_registration": "passed",
            "cloud_requests": 0,
        }
    finally:
        await client.close()
        # Let HA's registered shutdown callbacks close its shared resources.
        await hass.async_stop(force=True)


def main() -> None:
    """Run the checks with disposable HA configuration and emit JSON."""
    with tempfile.TemporaryDirectory(prefix="oj-native-smoke-") as config_dir:
        result = asyncio.run(qualify(config_dir))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
