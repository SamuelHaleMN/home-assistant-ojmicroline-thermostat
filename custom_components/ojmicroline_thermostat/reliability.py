"""Bounded WG4 authentication and transport for the pinned upstream client.

GETs may renew a rejected session once. Writes are never automatically replayed:
even a lost or unauthorized response must not produce an extra thermostat change.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from aiohttp import ClientError, ClientSession, hdrs
from yarl import URL

from ojmicroline_thermostat import WG4API, OJMicroline, Thermostat
from ojmicroline_thermostat.const import COMFORT_DURATION
from ojmicroline_thermostat.exceptions import (
    OJMicrolineAuthError,
    OJMicrolineConnectionError,
    OJMicrolineError,
    OJMicrolineTimeoutError,
)

REQUEST_TIMEOUT = 30.0


class _RejectedSessionError(Exception):
    """Internal status marker without a URL or server response payload."""


class ReliableWG4API(WG4API):
    """Serialize authentication and reject unusable login envelopes."""

    def __init__(
        self,
        username: str,
        password: str,
        host: str = "mythermostat.info",
        application: int = 2,
    ) -> None:
        """Initialize independent session state for this account."""
        super().__init__(username, password, host, application)
        self._auth_lock = asyncio.Lock()
        self._session_id = None
        self._session_calls_left = 0

    async def _authenticate(self) -> None:
        """Authenticate while the caller holds the account lock."""
        data = await self.request(
            self.login_path, method=hdrs.METH_POST, body=self.login_body()
        )
        if not isinstance(data, dict):
            msg = "Malformed WG4 authentication response"
            raise OJMicrolineError(msg)
        code = data.get("ErrorCode")
        if type(code) is not int:
            msg = "Malformed WG4 authentication error code"
            raise OJMicrolineError(msg)
        if code in (1, 2):
            msg = "WG4 credentials were rejected"
            raise OJMicrolineAuthError(msg)
        session = data.get("SessionId")
        if code != 0:
            msg = "Unexpected WG4 authentication error code"
            raise OJMicrolineError(msg)
        if not isinstance(session, str) or not session.strip():
            msg = "WG4 authentication returned no valid session"
            raise OJMicrolineError(msg)
        self._session_id = session
        self._session_calls_left = self._session_calls

    async def login(self) -> None:
        """Reuse valid sessions, with bounded lock waiting and login."""
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT), self._auth_lock:
                self._session_calls_left -= 1
                if self._session_id is None or self._session_calls_left < 0:
                    await self._authenticate()
        except TimeoutError:
            msg = "WG4 authentication timed out"
            raise OJMicrolineTimeoutError(msg) from None

    async def renew_rejected_session(self, rejected: str | None) -> str:
        """Renew once unless another caller already replaced this session."""
        async with self._auth_lock:
            # A late rejection of an older generation must not clear a new login.
            if self._session_id is None or self._session_id == rejected:
                self._session_id = None
                await self._authenticate()
            if self._session_id is None:
                msg = "WG4 session renewal returned no session"
                raise OJMicrolineError(msg)
            return self._session_id

    async def invalidate_rejected_session(self, rejected: str | None) -> None:
        """Invalidate only the generation actually rejected by the server."""
        async with self._auth_lock:
            if self._session_id == rejected:
                self._session_id = None
                self._session_calls_left = 0

    def update_regulation_mode_body(
        self,
        thermostat: Thermostat,
        regulation_mode: int,
        temperature: int | None,
        duration: int,
    ) -> dict[str, Any]:
        """Keep the stored setpoint when a preset change supplies no temperature."""
        body = super().update_regulation_mode_body(
            thermostat, regulation_mode, temperature, duration
        )
        if body.get("ManualTemperature", False) is None:
            body["ManualTemperature"] = thermostat.manual_temperature
        if body.get("ComfortTemperature", False) is None:
            body["ComfortTemperature"] = thermostat.comfort_temperature
        return body

    def parse_update_regulation_mode_response(self, data: Any) -> bool:
        """Accept only an explicit boolean acknowledgement."""
        if not isinstance(data, dict) or data.get("Success") is not True:
            msg = "WG4 rejected or did not acknowledge the command"
            raise OJMicrolineError(msg)
        return True


class ReliableOJMicroline(OJMicroline):
    """WG4 transport with full body deadlines and one safe read retry."""

    def __init__(self, api: ReliableWG4API, session: ClientSession) -> None:
        """Use the HA-owned session without taking ownership of its lifecycle."""
        self._wg4_api = api
        self._wg4_session = session
        self._command_locks: dict[str, asyncio.Lock] = {}
        super().__init__(api=api, session=session)

    async def get_thermostats(self) -> list[Thermostat]:
        """Include authentication in the public read's operation budget."""
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                return await super().get_thermostats()
        except TimeoutError:
            msg = "WG4 discovery timed out"
            raise OJMicrolineTimeoutError(msg) from None

    async def set_regulation_mode(
        self,
        resource: Thermostat,
        regulation_mode: int,
        temperature: int | None = None,
        duration: int = COMFORT_DURATION,
    ) -> bool:
        """Bound lock waiting and preserve write order for one thermostat."""
        lock = self._command_locks.setdefault(resource.serial_number, asyncio.Lock())
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT), lock:
                return await super().set_regulation_mode(
                    resource, regulation_mode, temperature, duration
                )
        except TimeoutError:
            msg = "WG4 command timed out"
            raise OJMicrolineTimeoutError(msg) from None

    async def _request(  # noqa: PLR0913 - matches the upstream transport interface
        self,
        uri: str,
        *,
        method: str = hdrs.METH_GET,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        request_timeout: float | None = None,
    ) -> Any:
        """Renew a rejected GET once, rebuilding its captured session parameter."""
        timeout = REQUEST_TIMEOUT if request_timeout is None else request_timeout
        request_params = dict(params or {})
        session_key = next(
            (key for key in request_params if key.lower() == "sessionid"), None
        )
        rejected = request_params.get(session_key) if session_key else None
        try:
            async with asyncio.timeout(timeout):
                try:
                    return await self._request_once(
                        uri, method, request_params, body, headers
                    )
                except _RejectedSessionError:
                    if uri == self._wg4_api.login_path:
                        msg = "WG4 credentials were rejected"
                        raise OJMicrolineAuthError(msg) from None
                    if method != hdrs.METH_GET or session_key is None:
                        await self._wg4_api.invalidate_rejected_session(rejected)
                        msg = "WG4 session expired; the command was not retried"
                        raise OJMicrolineConnectionError(msg) from None
                    request_params[
                        session_key
                    ] = await self._wg4_api.renew_rejected_session(rejected)
                try:
                    return await self._request_once(
                        uri, method, request_params, body, headers
                    )
                except _RejectedSessionError:
                    await self._wg4_api.invalidate_rejected_session(
                        request_params[session_key]
                    )
                    msg = "WG4 rejected the renewed session"
                    raise OJMicrolineConnectionError(msg) from None
        except TimeoutError:
            msg = "WG4 request timed out"
            raise OJMicrolineTimeoutError(msg) from None
        except ClientError:
            # aiohttp exceptions can embed the session-bearing request URL.
            msg = "WG4 request failed"
            raise OJMicrolineConnectionError(msg) from None

    async def _request_once(
        self,
        uri: str,
        method: str,
        params: dict[str, Any],
        body: dict[str, Any] | None,
        headers: dict[str, str] | None,
    ) -> Any:
        """Consume and release the complete response inside the deadline."""
        url = URL.build(scheme="https", host=self._wg4_api.host, path="/").join(
            URL(uri)
        )
        request_headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            **(headers or {}),
        }
        async with self._wg4_session.request(
            method, url, params=params, headers=request_headers, json=body, ssl=True
        ) as response:
            if response.status == 401:
                raise _RejectedSessionError
            if response.status >= 400:
                msg = f"WG4 request returned HTTP {response.status}"
                raise OJMicrolineConnectionError(msg)
            if response.content_type != "application/json":
                msg = "WG4 returned an unexpected content type"
                raise OJMicrolineError(msg)
            try:
                return await response.json()
            except (json.JSONDecodeError, UnicodeDecodeError):
                msg = "WG4 returned malformed JSON"
                raise OJMicrolineError(msg) from None
