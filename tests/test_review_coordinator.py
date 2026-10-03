"""Account publication, conservative budgets, and safe diagnostics regressions."""

import asyncio
import importlib
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from ojmicroline_thermostat import WG4API
from test_warmtiles_climate_flow import modules as _boundary_fixture


@pytest.fixture
def boundary_modules(monkeypatch):
    """Reuse the HA boundary fixture without hiding its imported definition."""
    return _boundary_fixture.__wrapped__(monkeypatch)


@pytest.fixture
def native_publication_model(boundary_modules, monkeypatch):
    """Model the core refresh fetch/publication boundary around the real override."""
    reached = asyncio.Event()
    release = asyncio.Event()

    class BaseCoordinator:
        async def _async_refresh(self, *args):
            data = await self._async_update_data()
            reached.set()
            await release.wait()
            self.data = data

    monkeypatch.setattr(
        sys.modules["homeassistant.helpers.update_coordinator"],
        "DataUpdateCoordinator",
        BaseCoordinator,
    )
    coordinator_module = importlib.reload(boundary_modules.coordinator)
    # Use the real WG4 type check; no login or HTTP request is performed.
    api = coordinator_module.ReliableWG4API("synthetic", "synthetic")
    assert isinstance(api, WG4API)
    coordinator = coordinator_module.OJMicrolineDataUpdateCoordinator.__new__(
        coordinator_module.OJMicrolineDataUpdateCoordinator
    )
    model = SimpleNamespace(
        serial_number="a",
        online=True,
        regulation_mode=3,
        get_target_temperature=lambda: 2000,
    )
    coordinator._model_api = api
    coordinator._account_lock = asyncio.Lock()
    coordinator.wd5_api = None
    coordinator.last_update_success = True
    coordinator.data = {"a": model}
    coordinator._verified_commands = 0
    coordinator._async_update_data = AsyncMock(return_value={"a": model})
    coordinator.async_set_regulation_mode = AsyncMock()
    coordinator.api = SimpleNamespace(get_thermostats=AsyncMock(return_value=[model]))
    coordinator.async_set_updated_data = Mock(
        side_effect=lambda data: setattr(coordinator, "data", data)
    )
    monkeypatch.setattr(coordinator_module.asyncio, "sleep", AsyncMock())
    return coordinator, coordinator_module, reached, release


@pytest.mark.asyncio
async def test_poll_publication_remains_inside_account_lock(native_publication_model):
    coordinator, module, reached, release = native_publication_model
    poll = asyncio.create_task(coordinator._async_refresh())
    await reached.wait()
    command = asyncio.create_task(coordinator.async_verified_command("a", 3, 2000))
    # Yield without using the mocked readback sleep.
    await asyncio.wait({command}, timeout=0.01)
    coordinator.async_set_regulation_mode.assert_not_awaited()
    assert not command.done()
    release.set()
    await asyncio.gather(poll, command)
    assert coordinator._verified_commands == 1
    assert module.WG4_UPDATE_INTERVAL == 300


@pytest.mark.asyncio
async def test_diagnostics_omit_credentials_ids_and_state(
    boundary_modules, monkeypatch
):
    module = importlib.import_module(
        "custom_components.ojmicroline_thermostat.diagnostics"
    )
    status = {"poll_successes": 2, "poll_interval_seconds": 300}
    coordinator = SimpleNamespace(diagnostic_status=lambda: status)
    entry = SimpleNamespace(
        entry_id="private-entry",
        data={
            "model": "WG4 series",
            "application": 13,
            "username": "private-account",
            "password": "private-password",
            "api_key": "private-key",
            "host": "private-host",
        },
    )
    hass = SimpleNamespace(
        data={"ojmicroline_thermostat": {entry.entry_id: coordinator}}
    )
    result = await module.async_get_config_entry_diagnostics(hass, entry)
    assert result == {
        "model": "WG4 series",
        "application": 13,
        "loaded": True,
        "status": status,
    }
    assert all(
        value not in str(result)
        for value in (
            "private-password",
            "private-account",
            "private-entry",
            "private-key",
            "private-host",
        )
    )
    monkeypatch.delitem(sys.modules, module.__name__)


@pytest.mark.asyncio
async def test_unloaded_entry_diagnostics_are_safe(boundary_modules, monkeypatch):
    module = importlib.import_module(
        "custom_components.ojmicroline_thermostat.diagnostics"
    )
    result = await module.async_get_config_entry_diagnostics(
        SimpleNamespace(data={}),
        SimpleNamespace(entry_id="entry", data={"password": "never-return"}),
    )
    assert result == {
        "model": None,
        "application": None,
        "loaded": False,
        "status": None,
    }
    monkeypatch.delitem(sys.modules, module.__name__)
