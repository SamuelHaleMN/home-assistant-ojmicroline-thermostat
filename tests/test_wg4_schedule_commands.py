"""Synthetic native-program transport, preview and transaction regressions."""

import asyncio
import copy
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from ojmicroline_thermostat.exceptions import (
    OJMicrolineConnectionError,
    OJMicrolineError,
)
from test_reliability import ReliableOJMicroline, Response, Session, api
from test_review_transport import thermostat_payload as _thermostat_fixture
from test_warmtiles_climate_flow import modules as _boundary_fixture


def program():
    """Supply neutral native events with harmless unknown extension fields."""
    clocks = ["06:00:00", "08:00:00", "12:00:00", "14:00:00", "17:00:00", "22:00:00"]
    return [
        {
            "WeekDayGrpNo": day,
            "Extension": "retain",
            "Events": [
                {
                    "ScheduleType": slot,
                    "Clock": clock,
                    "TempFloor": 2100,
                    "Active": slot in (0, 5),
                    "Extension": "retain",
                }
                for slot, clock in enumerate(clocks)
            ],
        }
        for day in range(1, 8)
    ]


def limits():
    return {
        "MinTimeLimits": [
            {"ScheduleType": slot, "Clock": seconds}
            for slot, seconds in enumerate([0, 1800, 3600, 5400, 7200, 14400])
        ],
        "MaxTimeLimits": [
            {"ScheduleType": slot, "Clock": seconds}
            for slot, seconds in enumerate([79200, 81000, 82800, 84600, 86400, 97200])
        ],
    }


@pytest.fixture
def record():
    value = _thermostat_fixture.__wrapped__()
    value["Schedules"] = program()
    return value


@pytest.fixture
def schedule_coordinator(monkeypatch, record):
    boundary = _boundary_fixture.__wrapped__(monkeypatch)
    module = boundary.coordinator
    model = module.ReliableWG4API("synthetic", "synthetic")
    state = copy.deepcopy(record)
    order = []
    storage = {}

    class Store:
        def __init__(self, _hass, _version, key):
            self.key = key

        async def async_load(self):
            order.append("backup_load")
            return copy.deepcopy(storage.get(self.key))

        async def async_save(self, value):
            order.append("backup_save")
            storage[self.key] = copy.deepcopy(value)

    storage_module = ModuleType("homeassistant.helpers.storage")
    storage_module.Store = Store
    monkeypatch.setitem(sys.modules, storage_module.__name__, storage_module)

    async def inventory():
        order.append("GET")
        return model.parse_thermostats_response(
            {"Groups": [{"Thermostats": [copy.deepcopy(state)]}]}
        )

    async def write(_resource, wire):
        order.append("POST")
        state["Schedules"] = copy.deepcopy(wire)
        return True

    coordinator = object.__new__(module.OJMicrolineDataUpdateCoordinator)
    coordinator._model_api = model
    coordinator._account_lock = asyncio.Lock()
    coordinator._verified_commands = 0
    coordinator.wd5_api = None
    coordinator.last_update_success = True
    initial = model.parse_thermostats_response(
        {"Groups": [{"Thermostats": [copy.deepcopy(state)]}]}
    )
    coordinator.data = {item.serial_number: item for item in initial}
    coordinator.config_entry = SimpleNamespace(entry_id="synthetic-entry")
    coordinator.hass = object()
    coordinator.async_set_updated_data = Mock(
        side_effect=lambda data: setattr(coordinator, "data", data)
    )
    coordinator.api = SimpleNamespace(
        get_thermostats=AsyncMock(side_effect=inventory),
        set_wg4_schedule=AsyncMock(side_effect=write),
        cached_wg4_schedule_limits=Mock(return_value=None),
        get_wg4_schedule_limits=AsyncMock(return_value=limits()),
    )
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
    schedule = module.WG4Schedule(program())
    return SimpleNamespace(
        coordinator=coordinator,
        module=module,
        boundary=boundary,
        state=state,
        order=order,
        storage=storage,
        serial=record["SerialNumber"],
        baseline_hash=schedule.fingerprint(),
    )


def test_inventory_schedule_cache_is_isolated_and_atomic(record):
    model = api()
    model.parse_thermostats_response({"Groups": [{"Thermostats": [record]}]})
    captured = model.get_wg4_schedule_snapshot(record["SerialNumber"])
    captured["Schedules"][0]["Events"][0]["TempFloor"] = 9999
    assert (
        model.get_wg4_schedule_snapshot(record["SerialNumber"])["Schedules"]
        == program()
    )
    assert "SerialNumber" not in captured
    assert "Room" not in captured
    broken = copy.deepcopy(record)
    broken["ManualTemperature"] = None
    with pytest.raises(OJMicrolineError):
        model.parse_thermostats_response({"Groups": [{"Thermostats": [broken]}]})
    assert (
        model.get_wg4_schedule_snapshot(record["SerialNumber"])["Schedules"]
        == program()
    )
    model.parse_thermostats_response({"Groups": []})
    assert model.get_wg4_schedule_snapshot(record["SerialNumber"]) is None


def test_malformed_program_does_not_break_climate(schedule_coordinator):
    fixture = schedule_coordinator
    fixture.state["Schedules"] = "malformed-program"
    result = fixture.coordinator._model_api.parse_thermostats_response(
        {"Groups": [{"Thermostats": [fixture.state]}]}
    )
    assert result[0].online is True
    assert fixture.coordinator.wg4_schedule_snapshot(fixture.serial) is None


@pytest.mark.asyncio
async def test_schedule_transport_sends_only_program_once(record):
    model = api()
    model._session_id = "synthetic-session"
    model._session_calls_left = 10
    session = Session(lambda *_args: Response(payload={"Success": True}))
    client = ReliableOJMicroline(model, session)
    resource = SimpleNamespace(serial_number=record["SerialNumber"])
    assert await client.set_wg4_schedule(resource, program()) is True
    assert len(session.calls) == 1
    method, url, options = session.calls[0]
    assert method == "POST"
    assert url.endswith("api/thermostat")
    assert options["json"] == {"Schedules": program()}
    assert options["params"]["serialnumber"] == record["SerialNumber"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 429, 503])
async def test_schedule_write_is_never_replayed(status, record):
    model = api()
    model._session_id = "synthetic-session"
    model._session_calls_left = 10
    session = Session(lambda *_args: Response(status=status))
    client = ReliableOJMicroline(model, session)
    with pytest.raises(OJMicrolineConnectionError):
        await client.set_wg4_schedule(
            SimpleNamespace(serial_number=record["SerialNumber"]), program()
        )
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_defaults_read_is_account_wide_and_cached(record):
    model = api()
    model._session_id = "synthetic-session"
    model._session_calls_left = 10
    payload = {
        "Thermostats": [
            {"SerialNumber": record["SerialNumber"], **limits()},
            {"SerialNumber": "second-synthetic", **limits()},
        ]
    }
    session = Session(lambda *_args: Response(payload=payload))
    client = ReliableOJMicroline(model, session)
    first = await client.get_wg4_schedule_limits(record["SerialNumber"])
    first["MinTimeLimits"][0]["Clock"] = 1
    assert await client.get_wg4_schedule_limits("second-synthetic") == limits()
    assert client.cached_wg4_schedule_limits(record["SerialNumber"]) == limits()
    assert len(session.calls) == 1
    assert session.calls[0][0] == "GET"
    assert session.calls[0][1].endswith("api/defaults")


@pytest.mark.asyncio
async def test_dry_run_has_no_http_or_storage(schedule_coordinator):
    fixture = schedule_coordinator
    result = await fixture.coordinator.async_wg4_schedule(
        fixture.serial,
        [{"day": "monday", "slot": 0, "temperature": 78}],
        fixture.baseline_hash,
        "F",
    )
    assert result["dry_run"] is True
    assert result["changed"] is True
    assert result["verified"] is False
    assert result["limits_checked"] is False
    assert result["days"]["monday"][0]["temperature"] == 25.55
    assert fixture.order == []
    assert fixture.storage == {}
    fixture.coordinator.api.get_wg4_schedule_limits.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_backs_up_before_single_post_and_verifies(schedule_coordinator):
    fixture = schedule_coordinator
    result = await fixture.coordinator.async_wg4_schedule(
        fixture.serial,
        [{"day": "monday", "slot": 0, "temperature": 78}],
        fixture.baseline_hash,
        "F",
        dry_run=False,
    )
    assert fixture.order == ["GET", "backup_load", "backup_save", "POST", "GET"]
    assert result["verified"] is True
    assert result["limits_checked"] is True
    assert fixture.coordinator._verified_commands == 1
    backup = fixture.storage[result["backup_key"]]
    assert backup["snapshots"][0]["snapshot"]["Schedules"] == program()
    assert fixture.state["RegulationMode"] == 3
    assert fixture.state["ManualTemperature"] == 2100
    assert fixture.state["VacationEnabled"] is False
    wire = fixture.coordinator.api.set_wg4_schedule.await_args.args[1]
    assert wire[0]["Events"][0]["TempFloor"] == 2555
    assert wire[1:] == program()[1:]
    assert wire[0]["Events"][1:] == program()[0]["Events"][1:]


@pytest.mark.asyncio
async def test_apply_noop_requires_fresh_hash_but_never_writes(schedule_coordinator):
    fixture = schedule_coordinator
    result = await fixture.coordinator.async_wg4_schedule(
        fixture.serial, [], fixture.baseline_hash, "C", dry_run=False
    )
    assert result["changed"] is False
    assert result["verified"] is True
    assert fixture.order == ["GET"]
    assert fixture.storage == {}
    fixture.coordinator.api.set_wg4_schedule.assert_not_awaited()
    fixture.coordinator.api.get_wg4_schedule_limits.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_app_change_rejects_stale_preview(schedule_coordinator):
    fixture = schedule_coordinator
    fixture.state["Schedules"][0]["Events"][0]["TempFloor"] = 2200
    with pytest.raises(fixture.boundary.ServiceValidationError, match="changed"):
        await fixture.coordinator.async_wg4_schedule(
            fixture.serial,
            [{"day": "monday", "slot": 0, "temperature": 23}],
            fixture.baseline_hash,
            "C",
            dry_run=False,
        )
    assert fixture.order == ["GET"]
    assert fixture.storage == {}
    fixture.coordinator.api.get_wg4_schedule_limits.assert_not_awaited()
    fixture.coordinator.api.set_wg4_schedule.assert_not_awaited()


@pytest.mark.asyncio
async def test_backup_failure_prevents_heating_write(schedule_coordinator, monkeypatch):
    fixture = schedule_coordinator
    store = sys.modules["homeassistant.helpers.storage"].Store
    monkeypatch.setattr(store, "async_save", AsyncMock(side_effect=OSError("disk")))
    with pytest.raises(fixture.boundary.HomeAssistantError, match="backup"):
        await fixture.coordinator.async_wg4_schedule(
            fixture.serial,
            [{"day": "monday", "slot": 0, "temperature": 23}],
            fixture.baseline_hash,
            "C",
            dry_run=False,
        )
    fixture.coordinator.api.set_wg4_schedule.assert_not_awaited()


@pytest.mark.asyncio
async def test_uncertain_write_keeps_backup_and_is_not_replayed(schedule_coordinator):
    fixture = schedule_coordinator
    fixture.coordinator.api.set_wg4_schedule.side_effect = OJMicrolineConnectionError(
        "WG4 request failed"
    )
    with pytest.raises(fixture.boundary.HomeAssistantError, match="failed"):
        await fixture.coordinator.async_wg4_schedule(
            fixture.serial,
            [{"day": "monday", "slot": 0, "temperature": 23}],
            fixture.baseline_hash,
            "C",
            dry_run=False,
        )
    assert len(fixture.storage) == 1
    assert fixture.coordinator.api.set_wg4_schedule.await_count == 1
    assert fixture.coordinator.api.get_thermostats.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("RegulationMode", 1),
        ("LastPrimaryModeIsAuto", True),
        ("VacationEnabled", True),
        ("ManualTemperature", 2500),
    ],
)
async def test_control_change_during_readback_is_unconfirmed(
    schedule_coordinator, field, value
):
    fixture = schedule_coordinator
    original = fixture.coordinator.api.set_wg4_schedule.side_effect

    async def write(resource, wire):
        await original(resource, wire)
        fixture.state[field] = value

    fixture.coordinator.api.set_wg4_schedule.side_effect = write
    with pytest.raises(fixture.boundary.HomeAssistantError, match="not confirmed"):
        await fixture.coordinator.async_wg4_schedule(
            fixture.serial,
            [{"day": "monday", "slot": 0, "temperature": 23}],
            fixture.baseline_hash,
            "C",
            dry_run=False,
        )
    assert fixture.coordinator._verified_commands == 0
    assert fixture.coordinator.api.set_wg4_schedule.await_count == 1
    assert len(fixture.storage) == 1


@pytest.mark.asyncio
async def test_bounded_readback_never_repeats_schedule_post(schedule_coordinator):
    fixture = schedule_coordinator
    fixture.coordinator.api.set_wg4_schedule.side_effect = None
    fixture.coordinator.api.set_wg4_schedule.return_value = True
    with pytest.raises(fixture.boundary.HomeAssistantError, match="not confirmed"):
        await fixture.coordinator.async_wg4_schedule(
            fixture.serial,
            [{"day": "monday", "slot": 0, "temperature": 23}],
            fixture.baseline_hash,
            "C",
            dry_run=False,
        )
    assert fixture.coordinator.api.set_wg4_schedule.await_count == 1
    assert fixture.coordinator.api.get_thermostats.await_count == 4
    assert [call.args[0] for call in fixture.module.asyncio.sleep.await_args_list] == [
        4,
        8,
        12,
    ]


@pytest.mark.asyncio
async def test_waiting_schedule_cancelled_before_any_request(schedule_coordinator):
    fixture = schedule_coordinator
    await fixture.coordinator._account_lock.acquire()
    task = asyncio.create_task(
        fixture.coordinator.async_wg4_schedule(
            fixture.serial, [], fixture.baseline_hash, "C", dry_run=False
        )
    )
    await asyncio.wait({task}, timeout=0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    fixture.coordinator._account_lock.release()
    assert fixture.order == []


@pytest.mark.asyncio
async def test_cached_limits_are_checked_without_network_on_preview(
    schedule_coordinator,
):
    fixture = schedule_coordinator
    fixture.coordinator.api.cached_wg4_schedule_limits.return_value = limits()
    result = await fixture.coordinator.async_wg4_schedule(
        fixture.serial,
        [{"day": "monday", "slot": 0, "temperature": 22}],
        fixture.baseline_hash,
        "C",
    )
    assert result["limits_checked"] is True
    assert fixture.order == []
    fixture.coordinator.api.get_wg4_schedule_limits.assert_not_awaited()


@pytest.mark.asyncio
async def test_unusable_device_limits_prevent_save(schedule_coordinator):
    fixture = schedule_coordinator
    fixture.coordinator.api.get_wg4_schedule_limits.return_value = {
        "MinTimeLimits": None,
        "MaxTimeLimits": [],
    }
    with pytest.raises(fixture.boundary.ServiceValidationError):
        await fixture.coordinator.async_wg4_schedule(
            fixture.serial,
            [{"day": "monday", "slot": 0, "temperature": 22}],
            fixture.baseline_hash,
            "C",
            dry_run=False,
        )
    assert fixture.order == ["GET"]
    assert fixture.storage == {}
    fixture.coordinator.api.set_wg4_schedule.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_changed_save_fetches_limits_but_second_reuses_cache(
    schedule_coordinator,
):
    fixture = schedule_coordinator
    first = await fixture.coordinator.async_wg4_schedule(
        fixture.serial,
        [{"day": "monday", "slot": 0, "temperature": 22}],
        fixture.baseline_hash,
        "C",
        dry_run=False,
    )
    fixture.coordinator.api.cached_wg4_schedule_limits.return_value = limits()
    await fixture.coordinator.async_wg4_schedule(
        fixture.serial,
        [{"day": "tuesday", "slot": 0, "temperature": 22}],
        first["schedule_hash"],
        "C",
        dry_run=False,
    )
    assert fixture.coordinator.api.get_wg4_schedule_limits.await_count == 1
    assert fixture.coordinator.api.set_wg4_schedule.await_count == 2


@pytest.mark.asyncio
async def test_backup_keeps_three_recent_exact_programs(schedule_coordinator):
    fixture = schedule_coordinator
    fixture.coordinator.api.cached_wg4_schedule_limits.return_value = limits()
    current_hash = fixture.baseline_hash
    for temperature in (22, 23, 24, 25):
        result = await fixture.coordinator.async_wg4_schedule(
            fixture.serial,
            [{"day": "monday", "slot": 0, "temperature": temperature}],
            current_hash,
            "C",
            dry_run=False,
        )
        current_hash = result["schedule_hash"]
    snapshots = fixture.storage[result["backup_key"]]["snapshots"]
    assert len(snapshots) == 3
    assert [
        saved["snapshot"]["Schedules"][0]["Events"][0]["TempFloor"]
        for saved in snapshots
    ] == [2200, 2300, 2400]
    assert all(saved["snapshot"]["RegulationMode"] == 3 for saved in snapshots)


@pytest.mark.asyncio
async def test_corrupt_backup_is_not_overwritten(schedule_coordinator, monkeypatch):
    fixture = schedule_coordinator
    store = sys.modules["homeassistant.helpers.storage"].Store
    monkeypatch.setattr(store, "async_load", AsyncMock(return_value={"wrong": "shape"}))
    with pytest.raises(fixture.boundary.HomeAssistantError, match="backup"):
        await fixture.coordinator.async_wg4_schedule(
            fixture.serial,
            [{"day": "monday", "slot": 0, "temperature": 22}],
            fixture.baseline_hash,
            "C",
            dry_run=False,
        )
    assert fixture.storage == {}
    fixture.coordinator.api.set_wg4_schedule.assert_not_awaited()


@pytest.mark.asyncio
async def test_fresh_offline_thermostat_prevents_write(schedule_coordinator):
    fixture = schedule_coordinator
    fixture.state["Online"] = False
    with pytest.raises(fixture.boundary.HomeAssistantError, match="unavailable"):
        await fixture.coordinator.async_wg4_schedule(
            fixture.serial,
            [{"day": "monday", "slot": 0, "temperature": 22}],
            fixture.baseline_hash,
            "C",
            dry_run=False,
        )
    assert fixture.order == ["GET"]
    fixture.coordinator.api.set_wg4_schedule.assert_not_awaited()


@pytest.mark.asyncio
async def test_two_same_previews_cannot_overwrite_each_other(schedule_coordinator):
    fixture = schedule_coordinator
    requests = [
        fixture.coordinator.async_wg4_schedule(
            fixture.serial,
            [{"day": day, "slot": 0, "temperature": 22}],
            fixture.baseline_hash,
            "C",
            dry_run=False,
        )
        for day in ("monday", "tuesday")
    ]
    results = await asyncio.gather(*requests, return_exceptions=True)
    assert sum(isinstance(value, dict) for value in results) == 1
    assert (
        sum(
            isinstance(value, fixture.boundary.ServiceValidationError)
            for value in results
        )
        == 1
    )
    assert fixture.coordinator.api.set_wg4_schedule.await_count == 1
    assert fixture.state["Schedules"][1]["Events"][0]["TempFloor"] == 2100


@pytest.mark.asyncio
async def test_waiting_operation_timeout_never_leaves_a_queued_write(
    schedule_coordinator, monkeypatch
):
    fixture = schedule_coordinator
    monkeypatch.setattr(fixture.module, "COMMAND_TIMEOUT", 0.01)
    await fixture.coordinator._account_lock.acquire()
    with pytest.raises(fixture.boundary.HomeAssistantError, match="timed out"):
        await fixture.coordinator.async_wg4_schedule(
            fixture.serial, [], fixture.baseline_hash, "C", dry_run=False
        )
    fixture.coordinator._account_lock.release()
    assert fixture.order == []
    fixture.coordinator.api.set_wg4_schedule.assert_not_awaited()
