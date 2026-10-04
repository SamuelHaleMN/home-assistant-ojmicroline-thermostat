"""Native program exposure and response-only HA action boundary regressions."""

import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from test_warmtiles_climate_flow import entity as _entity_fixture
from test_warmtiles_climate_flow import modules as _modules_fixture
from test_warmtiles_sensors import coordinator
from test_warmtiles_sensors import sensor_modules as _sensor_fixture

entity = _entity_fixture
modules = _modules_fixture
boundary_modules = _modules_fixture
sensor_modules = _sensor_fixture


def native_week():
    return [
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


def test_native_sensor_reads_complete_cached_slots_and_revision(sensor_modules):
    data = coordinator()
    snapshot = {"Schedules": native_week(), "TZOffset": "-04:00"}
    data.wg4_schedule_snapshot = Mock(return_value=snapshot)
    sensor = sensor_modules.sensor.OJMicrolineNativeScheduleSensor(data, "synthetic-id")
    assert sensor.available
    assert sensor.native_value == "stored"
    attributes = sensor.extra_state_attributes
    assert len(attributes["schedule_hash"]) == 64
    assert attributes["temperature_unit"] == "C"
    assert attributes["time_basis"] == "thermostat_local"
    assert attributes["timezone_offset"] == "-04:00"
    monday = attributes["days"]["monday"]
    assert len(monday) == 6
    assert monday[0]["temperature"] == 25.55
    assert monday[5]["slot"] == 5
    assert monday[5]["active"] is False


@pytest.mark.parametrize("fault", ["missing", "poll", "offline", "malformed"])
def test_native_sensor_never_exposes_stale_or_broken_program(sensor_modules, fault):
    data = coordinator()
    snapshot = {"Schedules": native_week()}
    data.wg4_schedule_snapshot = Mock(return_value=snapshot)
    sensor = sensor_modules.sensor.OJMicrolineNativeScheduleSensor(data, "synthetic-id")
    if fault == "missing":
        data.data = {}
    elif fault == "poll":
        data.last_update_success = False
    elif fault == "offline":
        data.data["synthetic-id"].online = False
    else:
        snapshot["Schedules"] = []
    assert not sensor.available
    assert sensor.native_value is None
    assert sensor.extra_state_attributes is None


@pytest.mark.asyncio
async def test_native_action_requires_response_and_preserves_wd5_service(modules):
    platform = Mock()
    modules.climate.entity_platform.async_get_current_platform = Mock(
        return_value=platform
    )
    data = SimpleNamespace(data={})
    hass = SimpleNamespace(data={"ojmicroline_thermostat": {"entry": data}})
    await modules.climate.async_setup_entry(
        hass, SimpleNamespace(entry_id="entry", options={}), Mock()
    )
    calls = platform.async_register_entity_service.call_args_list
    services = {call.args[0]: call for call in calls}
    assert "set_schedule" in services
    assert "set_native_schedule" not in services
    registration = importlib.import_module(
        "custom_components.ojmicroline_thermostat.services"
    )
    registration.async_register_native_schedule_service(hass)
    call = registration.async_register_platform_entity_service.call_args
    assert call.args == (hass, "ojmicroline_thermostat", "set_native_schedule")
    assert call.kwargs["supports_response"] == "only"
    assert call.kwargs["entity_domain"] == "climate"
    schema = modules.climate.vol.Schema(call.kwargs["schema"])
    valid = {"changes": [], "expected_hash": "a" * 64, "temperature_unit": "F"}
    assert schema(valid)["dry_run"] is True
    with pytest.raises(modules.climate.vol.Invalid):
        schema({**valid, "temperature_unit": "auto"})
    with pytest.raises(modules.climate.vol.Invalid):
        schema({**valid, "expected_hash": "bad"})


@pytest.mark.asyncio
async def test_entity_native_action_passes_explicit_unit_and_preview(entity):
    entity.coordinator.async_wg4_schedule = AsyncMock(return_value={"dry_run": True})
    changes = [{"day": "monday", "slot": 1, "temperature": 78}]
    assert await entity.async_set_native_schedule(changes, "a" * 64, "F") == {
        "dry_run": True
    }
    entity.coordinator.async_wg4_schedule.assert_awaited_once_with(
        entity.idx, changes, "a" * 64, "F", dry_run=True
    )


@pytest.mark.asyncio
async def test_entity_rejects_native_action_for_wd5(entity, modules):
    entity.coordinator.wd5_api = object()
    with pytest.raises(modules.ServiceValidationError):
        await entity.async_set_native_schedule([], "a" * 64, "C", dry_run=False)
