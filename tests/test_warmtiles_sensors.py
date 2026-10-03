"""Sensor membership, freshness and supported WG4 capability regressions."""

import importlib
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from test_warmtiles_climate_flow import modules as boundary_modules


@pytest.fixture
def sensor_modules(boundary_modules, monkeypatch):
    def module(name, **values):
        result = ModuleType(name)
        result.__dict__.update(values)
        monkeypatch.setitem(sys.modules, name, result)
        return result

    class Description:
        def __init__(self, **values):
            self.__dict__.update(values)

    class Entity:
        pass

    class SensorEntity:
        pass

    class BinarySensorEntity:
        pass

    module(
        "homeassistant.components.sensor",
        SensorEntity=SensorEntity,
        SensorEntityDescription=Description,
        SensorDeviceClass=SimpleNamespace(
            TEMPERATURE="temperature", ENERGY="energy", TIMESTAMP="timestamp"
        ),
        SensorStateClass=SimpleNamespace(
            MEASUREMENT="measurement", TOTAL_INCREASING="total_increasing"
        ),
    )
    module(
        "homeassistant.components.binary_sensor",
        BinarySensorEntity=BinarySensorEntity,
        BinarySensorEntityDescription=Description,
        BinarySensorDeviceClass=SimpleNamespace(CONNECTIVITY="connectivity"),
    )
    sys.modules["homeassistant.helpers.entity"].Entity = Entity
    sys.modules["homeassistant.const"].UnitOfEnergy = SimpleNamespace(
        KILO_WATT_HOUR="kWh"
    )
    module("homeassistant.util", dt=SimpleNamespace(now=Mock()))
    helpers = sys.modules["custom_components.ojmicroline_thermostat.helpers"]
    helpers.is_wd5 = lambda thermostat: thermostat.model == "OWD5"
    helpers.wd5_local_time = lambda value: value
    schedule = sys.modules["custom_components.ojmicroline_thermostat.schedule"]
    schedule.current_setpoint = Mock(return_value=21)
    schedule.schedule_attributes = Mock(return_value={"schedule": "fixture"})
    for name in ("models", "sensor", "binary_sensor"):
        monkeypatch.delitem(
            sys.modules,
            f"custom_components.ojmicroline_thermostat.{name}",
            raising=False,
        )
    sensor = importlib.import_module("custom_components.ojmicroline_thermostat.sensor")
    binary = importlib.import_module(
        "custom_components.ojmicroline_thermostat.binary_sensor"
    )
    return SimpleNamespace(sensor=sensor, binary=binary)


def coordinator(*, model="UWG4", online=True):
    thermostat = SimpleNamespace(
        model=model,
        name="synthetic thermostat",
        online=online,
        heating=True,
        temperature_room=None,
        temperature_floor=None,
        min_temperature=500,
        max_temperature=4000,
        set_point_temperature=2150,
        get_current_energy=lambda: 0,
        sensor_mode=None,
        boost_end_time=None,
        comfort_end_time=None,
        vacation_begin_time=None,
        vacation_end_time=None,
        regulation_mode=3,
        vacation_mode=False,
        schedule=None,
    )
    return SimpleNamespace(data={"synthetic-id": thermostat}, last_update_success=True)


@pytest.mark.parametrize("fault", ["missing", "poll_failure", "offline"])
def test_ordinary_sensor_never_exposes_missing_or_stale_data(sensor_modules, fault):
    data = coordinator()
    info = next(
        info
        for info in sensor_modules.sensor.SENSOR_TYPES
        if info.entity_description.key == "min_temperature"
    )
    entity = sensor_modules.sensor.OJMicrolineSensor(
        data, "synthetic-id", info.entity_description, info.formatter, info.value_getter
    )
    assert entity.available
    assert entity.native_value == 5
    if fault == "missing":
        data.data = {}
    elif fault == "poll_failure":
        data.last_update_success = False
    else:
        data.data["synthetic-id"].online = False
    assert not entity.available
    assert entity.native_value is None


@pytest.mark.parametrize("fault", ["missing", "poll_failure"])
def test_connectivity_does_not_expose_missing_or_stale_data(sensor_modules, fault):
    data = coordinator()
    description = next(
        desc
        for desc in sensor_modules.binary.BINARY_SENSOR_TYPES
        if desc.key == "online"
    )
    entity = sensor_modules.binary.OJMicrolineBinarySensor(
        data, "synthetic-id", description
    )
    if fault == "missing":
        data.data = None
    else:
        data.last_update_success = False
    assert not entity.available
    assert entity.is_on is None


def test_connectivity_can_report_false_while_heating_is_unavailable(sensor_modules):
    data = coordinator(online=False)
    descriptions = {
        desc.key: desc for desc in sensor_modules.binary.BINARY_SENSOR_TYPES
    }
    online = sensor_modules.binary.OJMicrolineBinarySensor(
        data, "synthetic-id", descriptions["online"]
    )
    heating = sensor_modules.binary.OJMicrolineBinarySensor(
        data, "synthetic-id", descriptions["heating"]
    )
    assert online.available
    assert online.is_on is False
    assert not heating.available
    assert heating.is_on is None


@pytest.mark.asyncio
@pytest.mark.parametrize("model,expected_energy", [("UWG4", False), ("OWD5", True)])
async def test_energy_created_only_when_api_provides_it(
    sensor_modules, model, expected_energy
):
    data = coordinator(model=model)
    hass = SimpleNamespace(data={"ojmicroline_thermostat": {"synthetic-entry": data}})
    added = []
    await sensor_modules.sensor.async_setup_entry(
        hass, SimpleNamespace(entry_id="synthetic-entry"), added.extend
    )
    assert (
        any(entity.entity_description.key == "energy_usage" for entity in added)
        is expected_energy
    )


@pytest.mark.parametrize("fault", ["missing", "poll_failure", "offline"])
def test_schedule_properties_tolerate_missing_or_stale_data(sensor_modules, fault):
    data = coordinator(model="OWD5")
    data.data["synthetic-id"].schedule = {"synthetic": "schedule"}
    entity = sensor_modules.sensor.OJMicrolineScheduleSensor(data, "synthetic-id")
    if fault == "missing":
        data.data = {}
    elif fault == "poll_failure":
        data.last_update_success = False
    else:
        data.data["synthetic-id"].online = False
    assert not entity.available
    assert entity.native_value is None
    assert entity.extra_state_attributes is None
