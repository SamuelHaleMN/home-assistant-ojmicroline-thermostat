"""Adversarial transport review regressions using synthetic cloud responses."""

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from ojmicroline_thermostat.exceptions import (
    OJMicrolineAuthError,
    OJMicrolineConnectionError,
    OJMicrolineError,
)
from test_reliability import MODULE, ReliableOJMicroline, Response, Session, api


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"Groups": None},
        {"Groups": [{}]},
        {"Groups": {}},
        {"Groups": [{"Thermostats": {}}]},
        {"Groups": [None]},
        {"Groups": [{"Thermostats": [None]}]},
        {"Groups": [{"Thermostats": [[]]}]},
    ],
)
async def test_malformed_discovery_has_a_safe_typed_error(payload):
    """Valid JSON with a broken thermostat envelope follows HA's error path."""
    model = api()
    model._session_id = "synthetic-session"
    model._session_calls_left = 10
    session = Session(lambda *_args: Response(payload=payload))
    client = ReliableOJMicroline(model, session)
    with pytest.raises(OJMicrolineError):
        await client.get_thermostats()
    assert len(session.calls) == 1


@pytest.fixture
def thermostat_payload():
    """Return one complete synthetic WG4 record, including vendor date syntax."""
    return {
        "TZOffset": "+00:00",
        "SerialNumber": "synthetic-thermostat",
        "SWVersion": "synthetic-version",
        "GroupName": "synthetic-group",
        "GroupId": 1,
        "Room": "synthetic-room",
        "Online": True,
        "Heating": False,
        "RegulationMode": 3,
        "LastPrimaryModeIsAuto": False,
        "Temperature": 2150,
        "SetPointTemp": 2100,
        "MinTemp": 500,
        "MaxTemp": 4000,
        "ComfortTemperature": 2200,
        "ManualTemperature": 2100,
        "ComfortEndTime": "03/10/2026 12:00:00 +00:00",
        "VacationEnabled": False,
        "VacationBeginDay": "03/10/2026 00:00:00",
        "VacationEndDay": "04/10/2026 00:00:00",
        "VacationTemperature": 1500,
    }


def test_valid_empty_inventories_and_placeholders_remain_supported():
    model = api()
    assert model.parse_thermostats_response({"Groups": []}) == []
    assert model.parse_thermostats_response({"Groups": [{"Thermostats": []}]}) == []
    assert model.parse_thermostats_response({"Groups": [{"Thermostats": [{}]}]}) == []


def test_valid_thermostat_is_parsed_by_upstream(thermostat_payload):
    records = api().parse_thermostats_response(
        {"Groups": [{"Thermostats": [thermostat_payload]}]}
    )
    assert len(records) == 1
    assert records[0].serial_number == "synthetic-thermostat"
    assert records[0].get_target_temperature() == 2100
    assert records[0].get_current_temperature() == 2150


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("SerialNumber", None),
        ("SerialNumber", " "),
        ("SerialNumber", 123),
        ("Online", "false"),
        ("Heating", 1),
        ("VacationEnabled", None),
        ("RegulationMode", True),
        ("MinTemp", "500"),
        ("MaxTemp", 4000.0),
        ("ManualTemperature", None),
        ("ComfortTemperature", float("nan")),
        ("SetPointTemp", "2100"),
        ("Temperature", "2150"),
    ],
)
def test_control_field_type_errors_never_reach_entities(thermostat_payload, key, value):
    thermostat_payload[key] = value
    with pytest.raises(OJMicrolineError, match="malformed thermostat"):
        api().parse_thermostats_response(
            {"Groups": [{"Thermostats": [thermostat_payload]}]}
        )


@pytest.mark.parametrize("fault", ["missing", "date", "date_type"])
def test_upstream_parser_errors_are_sanitized(thermostat_payload, fault):
    if fault == "missing":
        thermostat_payload.pop("TZOffset")
    elif fault == "date":
        thermostat_payload["ComfortEndTime"] = "synthetic-private-server-value"
    else:
        thermostat_payload["VacationBeginDay"] = None
    with pytest.raises(OJMicrolineError, match="malformed thermostat") as raised:
        api().parse_thermostats_response(
            {"Groups": [{"Thermostats": [thermostat_payload]}]}
        )
    assert raised.value.__suppress_context__
    assert "private" not in str(raised.value)


def test_offline_thermostat_can_have_no_current_temperature(thermostat_payload):
    thermostat_payload.update(Online=False, Temperature=None)
    records = api().parse_thermostats_response(
        {"Groups": [{"Thermostats": [thermostat_payload]}]}
    )
    assert len(records) == 1
    assert records[0].online is False


@pytest.mark.asyncio
async def test_429_retry_after_prevents_a_second_network_request():
    """A failed operation must retain account-level server backoff for new calls."""
    response = Response(status=429)
    response.headers = {"Retry-After": "300"}
    session = Session(lambda *_args: response)
    client = ReliableOJMicroline(api(), session)
    with pytest.raises(OJMicrolineConnectionError):
        await client._request("api/thermostats", params={"sessionid": "synthetic"})
    with pytest.raises(OJMicrolineConnectionError):
        await client._request("api/thermostats", params={"sessionid": "synthetic"})
    assert len(session.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("status", [429, 503])
async def test_account_cooldown_blocks_reads_and_writes_without_replay(
    monkeypatch, method, status
):
    monkeypatch.setattr(MODULE, "monotonic", lambda: 100.0)
    response = Response(status=status)
    response.headers = {"Retry-After": "300"}
    session = Session(lambda *_args: response)
    client = ReliableOJMicroline(api(), session)
    with pytest.raises(OJMicrolineConnectionError, match=str(status)):
        await client._request("api/thermostat", method=method)
    assert response.released
    assert client.rate_limit_remaining == 300
    for next_method in ("GET", "POST"):
        with pytest.raises(OJMicrolineConnectionError, match="paused"):
            await client._request("api/thermostat", method=next_method)
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_cooldown_expires_without_sleeping_or_replaying(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(MODULE, "monotonic", lambda: clock[0])
    first = Response(status=429)
    first.headers = {"Retry-After": "3"}
    replies = iter([first, Response(payload={"Groups": []})])
    session = Session(lambda *_args: next(replies))
    client = ReliableOJMicroline(api(), session)
    with pytest.raises(OJMicrolineConnectionError):
        await client._request("api/thermostats")
    clock[0] = 102
    with pytest.raises(OJMicrolineConnectionError, match="paused"):
        await client._request("api/thermostats")
    assert len(session.calls) == 1
    clock[0] = 130
    assert client.rate_limit_remaining == 0
    assert await client._request("api/thermostats") == {"Groups": []}
    assert len(session.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, 300),
        ("", 300),
        ("invalid-header", 300),
        ("nan", 300),
        ("1.5", 300),
        ("0", 30),
        ("-5", 30),
        ("1", 30),
        ("300", 300),
        ("99999999", 86400),
        ("9" * 5000, 300),
    ],
)
async def test_retry_after_delta_is_bounded_and_malformed_values_use_default(
    monkeypatch, header, expected
):
    monkeypatch.setattr(MODULE, "monotonic", lambda: 100.0)
    response = Response(status=429)
    if header is not None:
        response.headers = {"Retry-After": header}
    session = Session(lambda *_args: response)
    client = ReliableOJMicroline(api(), session)
    with pytest.raises(OJMicrolineConnectionError):
        await client._request("api/thermostats")
    assert client.rate_limit_remaining == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize(
    ("seconds", "expected"), [(-120, 30), (120, 120), (7200, 7200), (172800, 86400)]
)
async def test_retry_after_http_date_uses_utc_and_bounded_monotonic_cooldown(
    monkeypatch, seconds, expected, status
):
    frozen = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(MODULE, "datetime", SimpleNamespace(now=lambda _tz: frozen))
    monkeypatch.setattr(MODULE, "monotonic", lambda: 100.0)
    response = Response(status=status)
    response.headers = {
        "Retry-After": format_datetime(frozen + timedelta(seconds=seconds), usegmt=True)
    }
    session = Session(lambda *_args: response)
    client = ReliableOJMicroline(api(), session)
    with pytest.raises(OJMicrolineConnectionError):
        await client._request("api/thermostats")
    assert client.rate_limit_remaining == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("header", [None, "", "nonsense", "nan", "1.5"])
async def test_503_without_usable_retry_after_does_not_invent_a_cooldown(
    monkeypatch, header
):
    monkeypatch.setattr(MODULE, "monotonic", lambda: 100.0)
    response = Response(status=503)
    if header is not None:
        response.headers = {"Retry-After": header}
    session = Session(lambda *_args: response)
    client = ReliableOJMicroline(api(), session)
    for _ in range(2):
        with pytest.raises(OJMicrolineConnectionError, match="503"):
            await client._request("api/thermostats")
    assert client.rate_limit_remaining == 0
    assert len(session.calls) == 2


@pytest.mark.asyncio
async def test_cooldown_blocks_login_without_false_credentials_error(monkeypatch):
    monkeypatch.setattr(MODULE, "monotonic", lambda: 100.0)
    response = Response(status=429)
    response.headers = {"Retry-After": "300"}
    session = Session(lambda *_args: response)
    model = api()
    client = ReliableOJMicroline(model, session)
    with pytest.raises(OJMicrolineConnectionError):
        await client._request("api/thermostats")
    with pytest.raises(OJMicrolineConnectionError, match="paused"):
        await model.login()
    assert model.authentication_count == 0
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_authentication_count_only_tracks_successful_new_sessions():
    model = api()
    model.request = AsyncMock(return_value={"ErrorCode": 0, "SessionId": "synthetic"})
    assert model.authentication_count == 0
    await model.login()
    await model.login()
    assert model.authentication_count == 1
    assert model.request.await_count == 1
    await model.invalidate_rejected_session("synthetic")
    await model.login()
    assert model.authentication_count == 2
    await model.invalidate_rejected_session("synthetic")
    model.request.return_value = {"ErrorCode": 1}
    with pytest.raises(OJMicrolineAuthError):
        await model.login()
    assert model.authentication_count == 2
