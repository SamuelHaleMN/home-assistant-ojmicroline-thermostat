"""Adversarial regressions for account publication and native HA lifecycle.

Synthetic values only; these tests never contact HA or a thermostat API.
"""

import asyncio
import importlib
import importlib.util
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import MappingProxyType, ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import test_warmtiles_climate_flow as boundary_tests

SOURCE = Path(__file__).resolve().parents[1]
entity = boundary_tests.entity
modules = boundary_tests.modules


def snapshot(resource):
    """Model cloud responses as independent snapshots, rather than live aliases."""
    value = SimpleNamespace(**vars(resource))
    value.get_target_temperature = lambda: value.set_point_temperature
    return value


@pytest.mark.asyncio
async def test_late_account_readback_cannot_revert_other_confirmed_device(
    entity, modules
):
    """A delayed response for A must not overwrite newer confirmed state for B."""
    coordinator = entity.coordinator
    first = snapshot(coordinator.data[entity.idx])
    second = snapshot(first)
    second.serial_number = "second-id"
    cloud = {entity.idx: first, second.serial_number: second}
    coordinator.data = {key: snapshot(value) for key, value in cloud.items()}
    other = modules.climate.OJMicrolineThermostat(coordinator, "second-id", {})
    first_read_started = asyncio.Event()
    release_first_read = asyncio.Event()
    reads = 0

    async def write(resource, mode, temperature=None, duration=None):
        cloud[resource.serial_number].regulation_mode = mode
        cloud[resource.serial_number].set_point_temperature = temperature

    async def read():
        nonlocal reads
        reads += 1
        values = [snapshot(value) for value in cloud.values()]
        if reads == 1:
            first_read_started.set()
            await release_first_read.wait()
        return values

    coordinator.async_set_regulation_mode.side_effect = write
    coordinator.api.get_thermostats.side_effect = read
    command = asyncio.create_task(entity.async_set_temperature(temperature=19))
    other_command = None
    try:
        await asyncio.wait_for(first_read_started.wait(), 1)
        other_command = asyncio.create_task(other.async_set_temperature(temperature=20))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert coordinator.async_set_regulation_mode.await_count == 1
    finally:
        release_first_read.set()
        await command
        if other_command is not None:
            await other_command
    assert coordinator.data["second-id"].set_point_temperature == 2000


def test_options_flow_requests_native_automatic_reload(modules, monkeypatch):
    """HA replaces the options mapping; entities hold the prior mapping until reload."""

    class OptionsFlowWithReload:
        automatic_reload = True

    monkeypatch.setattr(
        sys.modules["homeassistant.config_entries"],
        "OptionsFlowWithReload",
        OptionsFlowWithReload,
        raising=False,
    )
    flow = importlib.reload(modules.flow)
    assert issubclass(flow.OJMicrolineOptionsFlowHandler, OptionsFlowWithReload)


@pytest.mark.asyncio
async def test_version_one_migration_uses_ha_update_api(modules, monkeypatch):
    """Current HA prohibits assigning ConfigEntry.version directly."""

    def stub(name, **values):
        value = ModuleType(name)
        value.__dict__.update(values)
        monkeypatch.setitem(sys.modules, name, value)
        return value

    stub("homeassistant.components.frontend", add_extra_js_url=Mock())
    stub("homeassistant.components.http", StaticPathConfig=Mock())
    stub("homeassistant.helpers.typing", ConfigType=dict)
    stub("homeassistant.loader", async_get_integration=AsyncMock())
    sys.modules["homeassistant.const"].Platform = SimpleNamespace(
        CLIMATE="climate",
        SENSOR="sensor",
        BINARY_SENSOR="binary_sensor",
        DATE="date",
        SWITCH="switch",
    )
    sys.modules[
        "homeassistant.helpers.config_validation"
    ].config_entry_only_config_schema = Mock()

    name = "custom_components.ojmicroline_thermostat.review_entry_lifecycle"
    spec = importlib.util.spec_from_loader(
        name,
        SourceFileLoader(
            name, str(SOURCE / "custom_components/ojmicroline_thermostat/__init__.py")
        ),
        is_package=False,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class NativeEntryGuard:
        def __init__(self):
            object.__setattr__(self, "version", 1)
            object.__setattr__(
                self, "data", MappingProxyType({"username": "synthetic"})
            )

        def __setattr__(self, key, value):
            if key in {"version", "data", "options"}:
                msg = f"{key} cannot be changed directly; use async_update_entry"
                raise AttributeError(msg)
            object.__setattr__(self, key, value)

    entry = NativeEntryGuard()
    update = Mock()
    hass = SimpleNamespace(config_entries=SimpleNamespace(async_update_entry=update))
    assert await module.async_migrate_entry(hass, entry)
    update.assert_called_once_with(
        entry,
        version=2,
        data={"model": "WD5 series", "username": "synthetic"},
    )
