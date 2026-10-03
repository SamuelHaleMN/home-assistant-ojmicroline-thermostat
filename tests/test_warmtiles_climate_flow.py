"""Focused HA boundary tests using isolated HA interface doubles."""

import asyncio
import importlib
import sys
from datetime import UTC, datetime
from enum import IntFlag, StrEnum
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from ojmicroline_thermostat import OJMicrolineAuthError, OJMicrolineError

SOURCE = Path(__file__).resolve().parents[1]


@pytest.fixture
def modules(monkeypatch):
    """Load only the edited modules, without starting HA or external APIs."""

    def module(name, **attributes):
        value = ModuleType(name)
        value.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, value)
        return value

    class HomeAssistantError(Exception):
        pass

    class ServiceValidationError(HomeAssistantError):
        pass

    class ConfigEntryAuthFailedError(HomeAssistantError):
        pass

    class HVACMode(StrEnum):
        HEAT = "heat"
        OFF = "off"
        AUTO = "auto"

    class HVACAction(StrEnum):
        HEATING = "heating"
        IDLE = "idle"
        OFF = "off"

    class ClimateEntityFeature(IntFlag):
        PRESET_MODE = 1
        TARGET_TEMPERATURE = 2

    class CoordinatorEntity:
        def __class_getitem__(cls, _item):
            return cls

        def __init__(self, coordinator):
            self.coordinator = coordinator

        @property
        def available(self):
            return self.coordinator.last_update_success

    class ConfigFlow:
        def __init_subclass__(cls, **kwargs):
            super().__init_subclass__()

        def __init__(self):
            self.existing_entries = []
            self._async_abort_entries_match = Mock()
            self.async_update_reload_and_abort = Mock(
                return_value={"type": "abort", "reason": "reauth_successful"}
            )

        def _async_current_entries(self):
            return self.existing_entries

        def _get_reauth_entry(self):
            return self.reauth_entry

        def async_show_form(self, **kwargs):
            return {"type": "form", **kwargs}

        def async_abort(self, **kwargs):
            return {"type": "abort", **kwargs}

        def async_create_entry(self, **kwargs):
            return {"type": "create_entry", **kwargs}

    class OptionsFlowWithReload:
        automatic_reload = True

    for name in (
        "homeassistant",
        "homeassistant.components",
        "homeassistant.helpers",
    ):
        module(name)
    module(
        "homeassistant.config_entries",
        ConfigEntry=object,
        ConfigFlow=ConfigFlow,
        OptionsFlowWithReload=OptionsFlowWithReload,
    )
    module(
        "homeassistant.const",
        CONF_API_KEY="api_key",
        CONF_HOST="host",
        CONF_PASSWORD="password",
        CONF_USERNAME="username",
        ATTR_TEMPERATURE="temperature",
        UnitOfTemperature=SimpleNamespace(CELSIUS="°C"),
    )
    module(
        "homeassistant.core", callback=lambda function: function, HomeAssistant=object
    )
    module("homeassistant.data_entry_flow", FlowResult=dict)
    module(
        "homeassistant.exceptions",
        HomeAssistantError=HomeAssistantError,
        ServiceValidationError=ServiceValidationError,
        ConfigEntryAuthFailed=ConfigEntryAuthFailedError,
    )
    module(
        "homeassistant.components.climate",
        ClimateEntity=object,
        ClimateEntityFeature=ClimateEntityFeature,
        HVACAction=HVACAction,
        HVACMode=HVACMode,
    )
    module(
        "homeassistant.components.climate.const",
        PRESET_BOOST="boost",
        PRESET_COMFORT="comfort",
        PRESET_ECO="eco",
    )
    module(
        "homeassistant.helpers.update_coordinator",
        CoordinatorEntity=CoordinatorEntity,
        DataUpdateCoordinator=object,
        UpdateFailed=HomeAssistantError,
    )
    module("homeassistant.helpers.aiohttp_client", async_get_clientsession=Mock())
    module("homeassistant.helpers.debounce", Debouncer=object)
    module("homeassistant.util", dt=SimpleNamespace())
    module("homeassistant.helpers.entity", DeviceInfo=dict)
    module("homeassistant.helpers.entity_platform", AddEntitiesCallback=object)
    module(
        "homeassistant.helpers.config_validation",
        date=object,
        time=object,
        ensure_list=list,
    )
    module("homeassistant.helpers.entity_platform")
    package = module("custom_components")
    package.__path__ = [str(SOURCE / "custom_components")]
    package = module("custom_components.ojmicroline_thermostat")
    package.__path__ = [str(SOURCE / "custom_components/ojmicroline_thermostat")]
    module(
        "custom_components.ojmicroline_thermostat.api",
        oj_microline_from_config_entry_data=Mock(),
        api_from_config_entry_data=Mock(),
        oj_microline_from_api=Mock(),
    )
    module("custom_components.ojmicroline_thermostat.energy", EnergyStatistics=object)
    module("custom_components.ojmicroline_thermostat.push", WD5PushClient=object)
    module(
        "custom_components.ojmicroline_thermostat.helpers",
        target_temperature=lambda thermostat: thermostat.set_point_temperature,
        wd5_date=lambda value: value.date() if value else None,
        format_wd5=Mock(),
        format_wd5_date=Mock(),
        is_wd5=lambda thermostat: thermostat.model == "OWD5",
    )
    module(
        "custom_components.ojmicroline_thermostat.schedule",
        SLOTS=6,
        WEEKDAYS=[],
        ScheduleError=ValueError,
        set_days=Mock(),
    )
    for name in ("climate", "config_flow", "const", "coordinator"):
        monkeypatch.delitem(
            sys.modules,
            f"custom_components.ojmicroline_thermostat.{name}",
            raising=False,
        )
    # The entity-platform stub must expose both the imported annotation and helper.
    sys.modules["homeassistant.helpers.entity_platform"].AddEntitiesCallback = object
    climate = importlib.import_module(
        "custom_components.ojmicroline_thermostat.climate"
    )
    flow = importlib.import_module(
        "custom_components.ojmicroline_thermostat.config_flow"
    )
    coordinator = importlib.import_module(
        "custom_components.ojmicroline_thermostat.coordinator"
    )
    return SimpleNamespace(
        climate=climate,
        flow=flow,
        coordinator=coordinator,
        HomeAssistantError=HomeAssistantError,
        ServiceValidationError=ServiceValidationError,
        HVACMode=HVACMode,
    )


@pytest.fixture
def entity(modules, monkeypatch):
    thermostat = SimpleNamespace(
        name="offline fixture",
        software_version="1012S202",
        model="UWG4",
        serial_number="fixture-id",
        online=True,
        heating=False,
        regulation_mode=3,
        supported_regulation_modes=[1, 2, 3, 4],
        min_temperature=500,
        max_temperature=4000,
        set_point_temperature=1888,
        energy=[],
        comfort_end_time=datetime.now(UTC),
        vacation_begin_time=datetime.now(UTC),
        vacation_end_time=datetime.now(UTC),
    )
    coordinator = object.__new__(modules.coordinator.OJMicrolineDataUpdateCoordinator)
    coordinator.data = {"fixture-id": thermostat}
    coordinator.last_update_success = True
    coordinator.wd5_api = None
    coordinator._account_lock = asyncio.Lock()
    coordinator._verified_commands = 0
    coordinator.async_set_regulation_mode = AsyncMock()
    coordinator.async_change_vacation = AsyncMock()
    coordinator.async_request_delayed_refresh = AsyncMock()
    coordinator.api = SimpleNamespace(
        get_thermostats=AsyncMock(return_value=[thermostat])
    )
    thermostat.get_target_temperature = lambda: thermostat.set_point_temperature
    thermostat.get_current_temperature = lambda: 2069

    async def write_mode(resource, mode, temperature=None, duration=None):
        resource.regulation_mode = mode
        if temperature is not None:
            resource.set_point_temperature = temperature

    coordinator.async_set_regulation_mode.side_effect = write_mode
    coordinator.async_set_updated_data = Mock(
        side_effect=lambda data: setattr(coordinator, "data", data)
    )
    real_sleep = asyncio.sleep

    async def fast_sleep(_delay):
        await real_sleep(0)

    monkeypatch.setattr(modules.climate.asyncio, "sleep", fast_sleep)
    return modules.climate.OJMicrolineThermostat(coordinator, "fixture-id", {})


@pytest.mark.parametrize(
    ("poll_ok", "present", "online", "expected"),
    [
        (True, True, True, True),
        (False, True, True, False),
        (True, True, False, False),
        (True, False, True, False),
    ],
)
def test_availability_requires_poll_membership_and_online(
    entity, poll_ok, present, online, expected
):
    entity.coordinator.last_update_success = poll_ok
    entity.coordinator.data["fixture-id"].online = online
    if not present:
        entity.coordinator.data = {}
    assert entity.available is expected


@pytest.mark.asyncio
async def test_wg4_vacation_is_not_advertised_or_sent(entity, modules):
    assert entity.preset_modes == ["schedule", "manual"]
    with pytest.raises(modules.ServiceValidationError):
        await entity.async_set_preset_mode("vacation")
    with pytest.raises(modules.ServiceValidationError):
        await entity.async_cancel_vacation()
    entity.coordinator.async_set_regulation_mode.assert_not_awaited()
    entity.coordinator.async_change_vacation.assert_not_awaited()


@pytest.mark.asyncio
async def test_preset_failures_reach_ha_caller(entity, modules):
    entity.coordinator.async_set_regulation_mode.side_effect = OJMicrolineError(
        "offline rejection"
    )
    with pytest.raises(modules.HomeAssistantError, match="preset command failed"):
        await entity.async_set_preset_mode("schedule")
    entity.coordinator.async_request_delayed_refresh.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "temperature", [float("nan"), float("inf"), float("-inf"), 4.99, 40.01, True, "20"]
)
async def test_invalid_temperature_cannot_reach_api(entity, modules, temperature):
    with pytest.raises(modules.ServiceValidationError):
        await entity.async_set_temperature(temperature=temperature)
    entity.coordinator.async_set_regulation_mode.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "comfort_default", "expected"),
    [(3, True, 3), (2, False, 2), (1, True, 2), (1, False, 3)],
)
async def test_temperature_preserves_manual_and_comfort_mode(
    entity, mode, comfort_default, expected
):
    thermostat = entity.coordinator.data["fixture-id"]
    thermostat.regulation_mode = mode
    entity.options = {"use_comfort_mode": comfort_default}
    await entity.async_set_temperature(temperature=18.88)
    entity.coordinator.async_set_regulation_mode.assert_awaited_once_with(
        thermostat, expected, temperature=1888, duration=None
    )


@pytest.mark.asyncio
async def test_off_is_rejected_without_mutating_mode(entity, modules):
    with pytest.raises(modules.ServiceValidationError):
        await entity.async_set_hvac_mode(modules.HVACMode.OFF)
    await entity.async_set_hvac_mode(modules.HVACMode.HEAT)
    entity.coordinator.async_set_regulation_mode.assert_not_awaited()
    assert entity.coordinator.data["fixture-id"].regulation_mode == 3


def make_flow(modules):
    flow = modules.flow.OJMicrolineFlowHandler()
    flow.reauth_entry = SimpleNamespace(
        entry_id="fixture-entry",
        domain="ojmicroline_thermostat",
        data={
            "model": "WG4 series",
            "host": "warmtiles.mythermostat.info",
            "application": 13,
            "username": "fixture-account",
            "password": "fixture-old",
        },
        options={"use_comfort_mode": False, "comfort_mode_duration": 240},
    )
    flow.hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_get_entry=Mock(return_value=flow.reauth_entry),
            async_entries=Mock(return_value=[]),
        )
    )
    return flow


@pytest.mark.asyncio
async def test_reauth_preserves_routing_options_and_entry_id(modules, monkeypatch):
    flow = make_flow(modules)
    original = dict(flow.reauth_entry.data)
    options = dict(flow.reauth_entry.options)
    client = SimpleNamespace(login=AsyncMock(), close=AsyncMock())
    factory = Mock(return_value=client)
    monkeypatch.setattr(modules.flow, "oj_microline_from_config_entry_data", factory)
    result = await flow.async_step_reauth_confirm({"password": "fixture-new"})
    assert result["reason"] == "reauth_successful"
    factory.assert_called_once_with({**original, "password": "fixture-new"}, flow.hass)
    flow.async_update_reload_and_abort.assert_called_once_with(
        flow.reauth_entry, data_updates={"password": "fixture-new"}
    )
    assert flow.reauth_entry.data == original
    assert flow.reauth_entry.options == options
    flow._async_abort_entries_match.assert_not_called()
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_invalid_reauth_does_not_update_entry(modules, monkeypatch):
    flow = make_flow(modules)
    client = SimpleNamespace(
        login=AsyncMock(side_effect=OJMicrolineAuthError("offline authentication")),
        close=AsyncMock(),
    )
    monkeypatch.setattr(
        modules.flow, "oj_microline_from_config_entry_data", Mock(return_value=client)
    )
    result = await flow.async_step_reauth_confirm({"password": "fixture-invalid"})
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_auth"}
    assert flow.reauth_entry.data["password"] == "fixture-old"
    flow.async_update_reload_and_abort.assert_not_called()
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_import_keeps_credentials_inside_config_flow(modules, monkeypatch):
    flow = make_flow(modules)
    legacy = SimpleNamespace(
        domain="schluter",
        disabled_by="user",
        data={"username": "fixture-account", "password": "fixture-existing"},
    )
    flow.hass.config_entries.async_get_entry.return_value = legacy
    client = SimpleNamespace(login=AsyncMock(), close=AsyncMock())
    factory = Mock(return_value=client)
    monkeypatch.setattr(modules.flow, "oj_microline_from_config_entry_data", factory)
    result = await flow.async_step_import({"legacy_entry_id": "legacy-fixture"})
    assert result["type"] == "create_entry"
    assert result["data"] == {
        "model": "WG4 series",
        "host": "warmtiles.mythermostat.info",
        "application": 13,
        **legacy.data,
    }
    assert legacy.domain == "schluter"
    client.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "legacy",
    [
        None,
        SimpleNamespace(
            domain="other", data={"username": "fixture", "password": "fixture"}
        ),
        SimpleNamespace(domain="schluter", data={"username": "fixture"}),
    ],
)
async def test_import_rejects_unrelated_or_incomplete_entries(
    modules, monkeypatch, legacy
):
    flow = make_flow(modules)
    flow.hass.config_entries.async_get_entry.return_value = legacy
    factory = Mock()
    monkeypatch.setattr(modules.flow, "oj_microline_from_config_entry_data", factory)
    result = await flow.async_step_import({"legacy_entry_id": "legacy-fixture"})
    assert result["reason"] == "invalid_legacy_entry"
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_import_does_not_create_duplicate_oj_entry(modules):
    flow = make_flow(modules)
    flow.existing_entries = [flow.reauth_entry]
    result = await flow.async_step_import({"legacy_entry_id": "legacy-fixture"})
    assert result["reason"] == "already_configured"
    flow.hass.config_entries.async_get_entry.assert_not_called()


def test_missing_serial_keeps_capabilities_readable_but_unavailable(entity):
    entity.coordinator.data = {}
    assert entity.available is False
    assert entity.min_temp == 5
    assert entity.max_temp == 40
    assert entity.preset_modes == ["schedule", "manual"]
    assert entity.preset_mode == "manual"
    assert entity.device_info["model"] == "UWG4"
    assert entity.current_temperature == 20.69
    assert entity.target_temperature == 18.88


@pytest.mark.asyncio
async def test_user_flow_routes_to_existing_account_without_credentials(modules):
    flow = make_flow(modules)
    legacy = SimpleNamespace(entry_id="legacy-fixture", title="Warm Tiles fixture")
    flow.hass.config_entries.async_entries.return_value = [legacy]
    initial = await flow.async_step_user()
    assert initial["data_schema"]({"model": modules.flow.MODEL_LEGACY_WARM_TILES})
    result = await flow.async_step_user({"model": modules.flow.MODEL_LEGACY_WARM_TILES})
    assert result["step_id"] == "legacy"
    assert result["data_schema"]({"legacy_entry_id": "legacy-fixture"})
    assert "password" not in str(result["data_schema"].schema)


@pytest.mark.asyncio
async def test_legacy_selection_rejects_bad_id(modules):
    flow = make_flow(modules)
    legacy = SimpleNamespace(entry_id="legacy-fixture", title="Warm Tiles fixture")
    flow.hass.config_entries.async_entries.return_value = [legacy]
    result = await flow.async_step_legacy({"legacy_entry_id": "unrelated-entry"})
    assert result["reason"] == "invalid_legacy_entry"
    flow.hass.config_entries.async_get_entry.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_enabled_writer_is_rejected_before_login(modules, monkeypatch):
    flow = make_flow(modules)
    legacy = SimpleNamespace(
        domain="schluter",
        disabled_by=None,
        data={"username": "fixture", "password": "fixture"},
    )
    flow.hass.config_entries.async_get_entry.return_value = legacy
    factory = Mock()
    monkeypatch.setattr(modules.flow, "oj_microline_from_config_entry_data", factory)
    result = await flow.async_step_import({"legacy_entry_id": "legacy-fixture"})
    assert result["reason"] == "legacy_not_disabled"
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_write_confirmed_by_direct_fresh_readback(entity):
    await entity.async_set_temperature(temperature=19.38)
    entity.coordinator.api.get_thermostats.assert_awaited_once()
    entity.coordinator.async_set_updated_data.assert_called_once()
    entity.coordinator.async_request_delayed_refresh.assert_not_awaited()
    assert entity.target_temperature == 19.38


@pytest.mark.asyncio
async def test_accepted_but_stale_readback_is_unconfirmed_without_post_retry(
    entity, modules
):
    entity.coordinator.async_set_regulation_mode.side_effect = None
    with pytest.raises(modules.HomeAssistantError, match="not confirmed"):
        await entity.async_set_temperature(temperature=19.38)
    entity.coordinator.async_set_regulation_mode.assert_awaited_once()
    assert entity.coordinator.api.get_thermostats.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(("missing", "online"), [(True, True), (False, False)])
async def test_missing_or_offline_readback_is_unconfirmed(
    entity, modules, missing, online
):
    resource = entity.coordinator.data["fixture-id"]
    resource.online = online
    entity.coordinator.api.get_thermostats.return_value = [] if missing else [resource]
    if not online:
        # Device goes offline only after the command has been sent.
        entity.coordinator.async_set_regulation_mode.side_effect = (
            lambda *args, **kwargs: setattr(resource, "online", False)
        )
        resource.online = True
    with pytest.raises(modules.HomeAssistantError, match="not confirmed"):
        await entity.async_set_temperature(temperature=19.38)
    entity.coordinator.async_set_regulation_mode.assert_awaited_once()
    assert entity.available is False


@pytest.mark.asyncio
async def test_changed_mode_is_unconfirmed_even_with_matching_temperature(
    entity, modules
):
    resource = entity.coordinator.data["fixture-id"]
    resource.regulation_mode = 1
    resource.set_point_temperature = 1938
    entity.coordinator.async_set_regulation_mode.side_effect = None
    with pytest.raises(modules.HomeAssistantError, match="not confirmed"):
        await entity.async_set_temperature(temperature=19.38)
    entity.coordinator.async_set_regulation_mode.assert_awaited_once()


@pytest.mark.asyncio
async def test_readback_failure_is_unconfirmed_without_post_retry(entity, modules):
    entity.coordinator.api.get_thermostats.side_effect = OJMicrolineError(
        "offline rejected read"
    )
    with pytest.raises(modules.HomeAssistantError, match="not confirmed"):
        await entity.async_set_temperature(temperature=19.38)
    entity.coordinator.async_set_regulation_mode.assert_awaited_once()


@pytest.mark.asyncio
async def test_readback_timeout_is_bounded(entity, modules, monkeypatch):
    actual_timeout = asyncio.timeout
    monkeypatch.setattr(
        modules.climate.asyncio, "timeout", lambda _delay: actual_timeout(0.001)
    )

    async def hanging_read():
        await asyncio.Event().wait()

    entity.coordinator.api.get_thermostats.side_effect = hanging_read
    with pytest.raises(modules.HomeAssistantError, match="not confirmed"):
        await entity.async_set_temperature(temperature=19.38)
    entity.coordinator.async_set_regulation_mode.assert_awaited_once()


@pytest.mark.asyncio
async def test_two_commands_serialize_each_write_and_its_readback(entity):
    events = []
    resource = entity.coordinator.data["fixture-id"]

    async def write(thermostat, mode, temperature=None, duration=None):
        events.append(("write", temperature))
        thermostat.regulation_mode = mode
        thermostat.set_point_temperature = temperature

    async def read():
        events.append(("read", resource.set_point_temperature))
        return [resource]

    entity.coordinator.async_set_regulation_mode.side_effect = write
    entity.coordinator.api.get_thermostats.side_effect = read
    await asyncio.gather(
        entity.async_set_temperature(temperature=19),
        entity.async_set_temperature(temperature=20),
    )
    assert events == [("write", 1900), ("read", 1900), ("write", 2000), ("read", 2000)]


@pytest.mark.asyncio
async def test_queued_temperature_uses_preceding_confirmed_preset(entity):
    entity.options = {"use_comfort_mode": True}
    await asyncio.gather(
        entity.async_set_preset_mode("comfort"),
        entity.async_set_temperature(temperature=19),
    )
    calls = entity.coordinator.async_set_regulation_mode.await_args_list
    assert [call.args[1] for call in calls] == [2, 2]
    assert calls[1].kwargs["temperature"] == 1900


@pytest.mark.asyncio
async def test_waiting_for_held_command_lock_times_out_without_post(
    entity, modules, monkeypatch
):
    monkeypatch.setattr(modules.coordinator, "COMMAND_TIMEOUT", 0.001)
    await entity.coordinator._account_lock.acquire()
    try:
        with pytest.raises(modules.HomeAssistantError, match="timed out"):
            await entity.async_set_temperature(temperature=19)
    finally:
        entity.coordinator._account_lock.release()
    entity.coordinator.async_set_regulation_mode.assert_not_awaited()
    entity.coordinator.api.get_thermostats.assert_not_awaited()


@pytest.mark.asyncio
async def test_comfort_requires_opt_in_without_changing_existing_mode(entity, modules):
    resource = entity.coordinator.data["fixture-id"]
    resource.regulation_mode = 2
    assert entity.preset_mode == "comfort"
    assert "comfort" not in entity.preset_modes
    with pytest.raises(modules.ServiceValidationError):
        await entity.async_set_preset_mode("comfort")
    entity.coordinator.async_set_regulation_mode.assert_not_awaited()
    assert resource.regulation_mode == 2
    entity.options = {"use_comfort_mode": True}
    assert "comfort" in entity.preset_modes
    await entity.async_set_preset_mode("comfort")
    entity.coordinator.async_set_regulation_mode.assert_awaited_once()


@pytest.mark.asyncio
async def test_wd5_write_preserves_stock_refresh_and_avoids_full_readback(entity):
    """WG4 verification must not replace WD5's push and energy-aware state."""
    entity.coordinator.wd5_api = object()
    await entity.async_set_temperature(temperature=19)
    entity.coordinator.async_set_regulation_mode.assert_awaited_once()
    entity.coordinator.async_request_delayed_refresh.assert_awaited_once()
    entity.coordinator.api.get_thermostats.assert_not_awaited()
    entity.coordinator.async_set_updated_data.assert_not_called()


@pytest.mark.asyncio
async def test_wd5_rejected_write_never_requests_success_refresh(entity, modules):
    """Retain a visible WD5 error without pretending the write succeeded."""
    entity.coordinator.wd5_api = object()
    entity.coordinator.async_set_regulation_mode.side_effect = OJMicrolineError(
        "synthetic rejection"
    )
    with pytest.raises(modules.HomeAssistantError, match="temperature command failed"):
        await entity.async_set_temperature(temperature=19)
    entity.coordinator.async_request_delayed_refresh.assert_not_awaited()
    entity.coordinator.api.get_thermostats.assert_not_awaited()


def test_wd5_retains_comfort_and_vacation_capabilities(entity):
    """WG4 capability restrictions must not hide supported WD5 presets."""
    entity.coordinator.wd5_api = object()
    assert entity.preset_modes == ["schedule", "comfort", "manual", "vacation"]
