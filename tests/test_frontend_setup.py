"""Frontend bootstrap ordering, version parity and storage-failure fallback."""

import importlib.util
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from test_warmtiles_climate_flow import modules as _boundary_fixture

modules = _boundary_fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("registration", [True, False, OSError("Synthetic failure")])
async def test_setup_registers_versioned_module_and_keeps_global_fallback(
    modules, monkeypatch, registration, caplog
):
    def stub(name, **values):
        value = ModuleType(name)
        value.__dict__.update(values)
        monkeypatch.setitem(sys.modules, name, value)
        return value

    extra = Mock()
    resource = AsyncMock(return_value=registration)
    if isinstance(registration, Exception):
        resource.side_effect = registration
    stub("homeassistant.components.frontend", add_extra_js_url=extra)
    stub("homeassistant.components.http", StaticPathConfig=Mock())
    stub("homeassistant.helpers.typing", ConfigType=dict)
    stub(
        "homeassistant.loader",
        async_get_integration=AsyncMock(return_value=SimpleNamespace(version="1.6.1")),
    )
    stub(
        "custom_components.ojmicroline_thermostat.frontend_resources",
        async_register_native_card_resource=resource,
    )
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
    path = (
        Path(__file__).parents[1]
        / "custom_components/ojmicroline_thermostat/__init__.py"
    )
    name = "custom_components.ojmicroline_thermostat.frontend_setup_probe"
    spec = importlib.util.spec_from_loader(
        name, SourceFileLoader(name, str(path)), is_package=False
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    static = AsyncMock()
    hass = SimpleNamespace(http=SimpleNamespace(async_register_static_paths=static))
    assert await module.async_setup(hass, {}) is True
    static.assert_awaited_once()
    native_url = "/ojmicroline_thermostat/ojmicroline-native-schedule-card.js?v=1.6.1"
    resource.assert_awaited_once_with(hass, native_url)
    assert extra.call_args_list[-1].args == (hass, native_url)
    assert len(extra.call_args_list) == 2
    if isinstance(registration, Exception):
        assert "OSError" in caplog.text
        assert "Synthetic failure" not in caplog.text
