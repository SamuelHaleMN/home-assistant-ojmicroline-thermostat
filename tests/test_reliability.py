"""Isolated WG4 adapter regressions; no HA runtime or network is required."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientConnectionError
from ojmicroline_thermostat.exceptions import (
    OJMicrolineAuthError,
    OJMicrolineConnectionError,
    OJMicrolineError,
    OJMicrolineTimeoutError,
)

# Load the module without executing the integration's HA-dependent __init__.
SPEC = importlib.util.spec_from_file_location(
    "warmtiles_reliability",
    Path(__file__).parents[1]
    / "custom_components/ojmicroline_thermostat/reliability.py",
)
assert SPEC is not None
assert SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
ReliableWG4API = MODULE.ReliableWG4API
ReliableOJMicroline = MODULE.ReliableOJMicroline


class Response:
    """Minimal aiohttp-shaped response supporting delayed JSON consumption."""

    def __init__(
        self, status=200, payload=None, *, delay=0, content_type="application/json"
    ):
        self.status = status
        self.payload = payload
        self.delay = delay
        self.content_type = content_type
        self.headers = {}
        self.released = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        self.released = True

    async def json(self):
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.payload


class Session:
    """Deterministic transport that records synthetic requests."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append(
            (method, str(url), {**kwargs, "params": dict(kwargs.get("params", {}))})
        )
        return self.handler(method, str(url), kwargs)


def api():
    return ReliableWG4API("synthetic@example.invalid", "synthetic", application=13)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [1, 2])
async def test_invalid_credentials_never_install_session(code):
    model = api()
    model.request = AsyncMock(return_value={"ErrorCode": code, "SessionId": ""})
    with pytest.raises(OJMicrolineAuthError):
        await model.login()
    assert model._session_id is None
    assert model._session_calls_left < 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"ErrorCode": 0},
        {"ErrorCode": 0, "SessionId": " "},
        {"ErrorCode": 99, "SessionId": "bad"},
        {"ErrorCode": False, "SessionId": "bad"},
        {"ErrorCode": True, "SessionId": "bad"},
    ],
)
async def test_malformed_authentication_is_not_success(payload):
    model = api()
    model.request = AsyncMock(return_value=payload)
    with pytest.raises(OJMicrolineError) as raised:
        await model.login()
    assert type(raised.value) is OJMicrolineError
    assert model._session_id is None
    assert model._session_calls_left < 0


@pytest.mark.asyncio
async def test_expired_read_rebuilds_params_and_retries_once():
    model = api()
    model._session_id = "old"
    session = Session(
        lambda _method, url, kwargs: (
            Response(payload={"ErrorCode": 0, "SessionId": "new"})
            if url.endswith("user")
            else Response(401)
            if kwargs["params"]["sessionid"] == "old"
            else Response(payload={"Groups": []})
        )
    )
    client = ReliableOJMicroline(model, session)
    params = {"sessionid": "old"}
    assert await client._request("api/thermostats", params=params) == {"Groups": []}
    assert [call[2]["params"].get("sessionid") for call in session.calls] == [
        "old",
        None,
        "new",
    ]
    assert params == {"sessionid": "old"}
    assert model._session_id == "new"


@pytest.mark.asyncio
async def test_concurrent_expired_reads_share_one_login():
    model = api()
    model._session_id = "old"
    session = Session(
        lambda _method, url, kwargs: (
            Response(payload={"ErrorCode": 0, "SessionId": "new"}, delay=0.01)
            if url.endswith("user")
            else Response(401)
            if kwargs["params"]["sessionid"] == "old"
            else Response(payload={"Groups": []})
        )
    )
    client = ReliableOJMicroline(model, session)
    await asyncio.gather(
        *(
            client._request("api/thermostats", params={"sessionid": "old"})
            for _ in range(8)
        )
    )
    assert sum(url.endswith("user") for _, url, _ in session.calls) == 1


@pytest.mark.asyncio
async def test_late_old_session_failure_does_not_invalidate_new_login():
    model = api()
    model._session_id = "new"
    session = Session(
        lambda _method, _url, kwargs: (
            Response(401)
            if kwargs["params"]["sessionid"] == "old"
            else Response(payload={"Groups": []})
        )
    )
    client = ReliableOJMicroline(model, session)
    await client._request("api/thermostats", params={"sessionid": "old"})
    assert model._session_id == "new"
    assert len(session.calls) == 2


@pytest.mark.asyncio
async def test_second_unauthorized_read_stops_without_reauth_prompt():
    model = api()
    model._session_id = "old"
    session = Session(
        lambda _method, url, _kwargs: (
            Response(payload={"ErrorCode": 0, "SessionId": "new"})
            if url.endswith("user")
            else Response(401)
        )
    )
    client = ReliableOJMicroline(model, session)
    with pytest.raises(OJMicrolineConnectionError, match="renewed session"):
        await client._request("api/thermostats", params={"sessionid": "old"})
    assert len(session.calls) == 3
    assert model._session_id is None


@pytest.mark.asyncio
async def test_unauthorized_post_is_not_replayed_and_next_login_recovers():
    model = api()
    model._session_id = "old"
    session = Session(
        lambda _method, url, _kwargs: (
            Response(payload={"ErrorCode": 0, "SessionId": "new"})
            if url.endswith("user")
            else Response(401)
        )
    )
    client = ReliableOJMicroline(model, session)
    with pytest.raises(OJMicrolineConnectionError, match="not retried"):
        await client._request(
            "api/thermostat", method="POST", params={"sessionId": "old"}
        )
    assert len(session.calls) == 1
    assert model._session_id is None
    await model.login()
    assert model._session_id == "new"
    assert len(session.calls) == 2


@pytest.mark.asyncio
async def test_post_body_timeout_releases_response_without_replay():
    model = api()
    response = Response(payload={"Success": True}, delay=1)
    session = Session(lambda *_args: response)
    client = ReliableOJMicroline(model, session)
    with pytest.raises(OJMicrolineTimeoutError):
        await client._request("api/thermostat", method="POST", request_timeout=0.01)
    assert len(session.calls) == 1
    assert response.released


@pytest.mark.asyncio
async def test_authentication_lock_wait_is_inside_request_deadline():
    model = api()
    model._session_id = "old"
    session = Session(lambda *_args: Response(401))
    client = ReliableOJMicroline(model, session)
    async with model._auth_lock:
        with pytest.raises(OJMicrolineTimeoutError):
            await client._request(
                "api/thermostats", params={"sessionid": "old"}, request_timeout=0.01
            )
    assert model._session_id == "old"
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_connection_failure_does_not_leak_request_or_retry_post():
    model = api()

    def fail(*_args):
        raise ClientConnectionError("synthetic secret-bearing URL")

    session = Session(fail)
    client = ReliableOJMicroline(model, session)
    with pytest.raises(OJMicrolineConnectionError) as raised:
        await client._request("api/thermostat", method="POST")
    assert "secret" not in str(raised.value)
    assert raised.value.__suppress_context__
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    "payload", [None, {}, {"Success": False}, {"Success": 1}, {"Success": "true"}]
)
def test_false_or_malformed_write_acknowledgement_fails(payload):
    with pytest.raises(OJMicrolineError):
        api().parse_update_regulation_mode_response(payload)


def test_preset_without_temperature_preserves_stored_value():
    resource = SimpleNamespace(
        manual_temperature=2150, comfort_temperature=2250, vacation_mode=False
    )
    assert (
        api().update_regulation_mode_body(resource, 3, None, 240)["ManualTemperature"]
        == 2150
    )
    assert (
        api().update_regulation_mode_body(resource, 2, None, 240)["ComfortTemperature"]
        == 2250
    )


@pytest.mark.asyncio
async def test_command_lock_wait_times_out_without_late_write(monkeypatch):
    monkeypatch.setattr(MODULE, "REQUEST_TIMEOUT", 0.01)
    model = api()
    session = Session(lambda *_args: Response(payload={"Success": True}))
    client = ReliableOJMicroline(model, session)
    resource = SimpleNamespace(serial_number="synthetic")
    lock = asyncio.Lock()
    client._command_locks[resource.serial_number] = lock
    async with lock:
        with pytest.raises(OJMicrolineTimeoutError):
            await client.set_regulation_mode(resource, 3, 2100)
    assert not session.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 503])
async def test_http_failure_is_bounded_without_post_retry(status):
    session = Session(lambda *_args: Response(status))
    client = ReliableOJMicroline(api(), session)
    with pytest.raises(OJMicrolineConnectionError, match=str(status)):
        await client._request("api/thermostat", method="POST")
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_login_http_401_is_credentials_error_without_retry():
    model = api()
    session = Session(lambda *_args: Response(401))
    ReliableOJMicroline(model, session)
    with pytest.raises(OJMicrolineAuthError):
        await model.login()
    assert len(session.calls) == 1
    assert model._session_id is None


@pytest.mark.asyncio
async def test_non_json_response_is_safe_and_released():
    response = Response(content_type="text/html", payload="synthetic secret")
    session = Session(lambda *_args: response)
    client = ReliableOJMicroline(api(), session)
    with pytest.raises(OJMicrolineError, match="content type") as raised:
        await client._request("api/thermostats")
    assert "secret" not in str(raised.value)
    assert response.released


@pytest.mark.asyncio
async def test_json_decoder_error_is_typed_and_released():
    response = Response()
    response.json = AsyncMock(
        side_effect=json.JSONDecodeError("bad", "synthetic secret", 0)
    )
    session = Session(lambda *_args: response)
    client = ReliableOJMicroline(api(), session)
    with pytest.raises(OJMicrolineError, match="malformed JSON") as raised:
        await client._request("api/thermostats")
    assert raised.value.__suppress_context__
    assert response.released


@pytest.mark.asyncio
async def test_cancellation_releases_response_and_propagates():
    entered = asyncio.Event()
    response = Response()

    async def body():
        entered.set()
        await asyncio.Event().wait()

    response.json = body
    session = Session(lambda *_args: response)
    client = ReliableOJMicroline(api(), session)
    task = asyncio.create_task(client._request("api/thermostat", method="POST"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert response.released
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_public_discovery_budget_includes_login_and_body(monkeypatch):
    monkeypatch.setattr(MODULE, "REQUEST_TIMEOUT", 0.2)
    session = Session(
        lambda _method, url, _kwargs: (
            Response(payload={"ErrorCode": 0, "SessionId": "new"}, delay=0.12)
            if url.endswith("user")
            else Response(payload={"Groups": []}, delay=0.12)
        )
    )
    client = ReliableOJMicroline(api(), session)
    with pytest.raises(OJMicrolineTimeoutError):
        await client.get_thermostats()
    assert len(session.calls) == 2
