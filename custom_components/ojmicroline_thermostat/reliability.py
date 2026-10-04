"""Bounded WG4 authentication and transport for the pinned upstream client.

GETs may renew a rejected session once. Writes are never automatically replayed:
even a lost or unauthorized response must not produce an extra thermostat change.
"""

# Exact primitive checks prevent bool from being accepted as an integer.
# pylint: disable=unidiomatic-typecheck

from __future__ import annotations

import asyncio
import copy
import json
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from time import monotonic
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
DEFAULT_RATE_LIMIT_DELAY = 300.0
MIN_RATE_LIMIT_DELAY = 30.0
MAX_RATE_LIMIT_DELAY = 86400.0
SCHEDULE_LIMITS_CACHE_SECONDS = 86400.0
WG4_CONTROL_FIELDS = (
    "RegulationMode",
    "LastPrimaryModeIsAuto",
    "ManualTemperature",
    "ComfortTemperature",
    "ComfortEndTime",
    "VacationEnabled",
    "VacationTemperature",
    "VacationBeginDay",
    "VacationEndDay",
)


def _retry_after_delay(
    value: str | None, *, fallback: float | None = DEFAULT_RATE_LIMIT_DELAY
) -> float | None:
    """Bound Retry-After; use the caller's fallback only when it is unusable."""
    if value is None:
        return fallback
    try:
        return float(max(MIN_RATE_LIMIT_DELAY, min(int(value), MAX_RATE_LIMIT_DELAY)))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        delay = (retry_at - datetime.now(UTC)).total_seconds()
    except (OverflowError, TypeError, ValueError):
        return fallback
    return max(MIN_RATE_LIMIT_DELAY, min(delay, MAX_RATE_LIMIT_DELAY))


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
        self._session_id: str | None = None
        self._session_calls_left = 0
        self._authentication_count = 0
        self._schedule_snapshots: dict[str, dict[str, Any]] = {}

    def get_wg4_schedule_snapshot(self, serial: str) -> dict[str, Any] | None:
        """Return isolated stored-program data from the last valid inventory.

        Schedule validation belongs to the WG4 schedule model. A malformed or
        absent program must not make otherwise usable climate data unavailable.
        The cache contains no credentials, session, account or location fields.
        """
        snapshot = self._schedule_snapshots.get(serial)
        return copy.deepcopy(snapshot) if snapshot is not None else None

    @property
    def authentication_count(self) -> int:
        """Return successful logins since this account instance was created."""
        return self._authentication_count

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
        self._authentication_count += 1

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

    def parse_thermostats_response(self, data: Any) -> list[Thermostat]:
        """Reject broken inventories before updating HA or verifying a command."""
        msg = "WG4 returned malformed thermostat data"
        if not isinstance(data, dict) or not isinstance(data.get("Groups"), list):
            raise OJMicrolineError(msg)
        for group in data["Groups"]:
            if not isinstance(group, dict) or not isinstance(
                group.get("Thermostats"), list
            ):
                raise OJMicrolineError(msg)
            for item in group["Thermostats"]:
                if not isinstance(item, dict):
                    raise OJMicrolineError(msg)
                # Upstream ignores empty placeholders returned by the cloud.
                if not item:
                    continue
                serial = item.get("SerialNumber")
                if not isinstance(serial, str) or not serial.strip():
                    raise OJMicrolineError(msg)
                if any(
                    type(item.get(key)) is not bool
                    for key in ("Online", "Heating", "VacationEnabled")
                ):
                    raise OJMicrolineError(msg)
                if any(
                    type(item.get(key)) is not int
                    for key in (
                        "RegulationMode",
                        "MinTemp",
                        "MaxTemp",
                        "ManualTemperature",
                        "ComfortTemperature",
                        "SetPointTemp",
                    )
                ):
                    raise OJMicrolineError(msg)
                if (
                    item.get("Temperature") is not None
                    and type(item["Temperature"]) is not int
                ):
                    raise OJMicrolineError(msg)
        try:
            thermostats = super().parse_thermostats_response(data)
        except (KeyError, TypeError, ValueError):
            # Date parsers can include the raw server value in their exceptions.
            raise OJMicrolineError(msg) from None
        # Publish the whole cache only after climate validation and parsing pass.
        # Missing devices disappear from this generation rather than retaining a
        # stale schedule that could later be used as the basis of a write.
        self._schedule_snapshots = {
            item["SerialNumber"]: copy.deepcopy(
                {
                    key: item[key]
                    for key in (
                        "Schedules",
                        "TZOffset",
                        "MinTemp",
                        "MaxTemp",
                        *WG4_CONTROL_FIELDS,
                    )
                    if key in item
                }
            )
            for group in data["Groups"]
            for item in group["Thermostats"]
            if item and "Schedules" in item
        }
        return thermostats

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
        self._rate_limit_until = 0.0
        self._schedule_limits: dict[str, dict[str, Any]] = {}
        self._schedule_limits_loaded_at: float | None = None
        super().__init__(api=api, session=session)

    @property
    def rate_limit_remaining(self) -> float:
        """Return remaining account cooldown seconds without cloud identifiers."""
        return max(0.0, self._rate_limit_until - monotonic())

    def cached_wg4_schedule_limits(self, serial: str) -> dict[str, Any] | None:
        """Return per-device editing limits without adding background requests."""
        if (
            self._schedule_limits_loaded_at is None
            or monotonic() - self._schedule_limits_loaded_at
            >= SCHEDULE_LIMITS_CACHE_SECONDS
        ):
            return None
        limits = self._schedule_limits.get(serial)
        return copy.deepcopy(limits) if limits is not None else None

    async def get_wg4_schedule_limits(self, serial: str) -> dict[str, Any]:
        """Fetch account-wide /defaults at most daily, only for explicit editing."""
        cached = self.cached_wg4_schedule_limits(serial)
        if cached is not None:
            return cached
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                await self.login()
                data = await self._wg4_api.request(
                    "api/defaults",
                    # The pinned API exposes its current session only privately.
                    # pylint: disable-next=protected-access
                    params={"sessionid": self._wg4_api._session_id},  # noqa: SLF001
                )
                if not isinstance(data, dict) or not isinstance(
                    data.get("Thermostats"), list
                ):
                    msg = "WG4 returned malformed schedule limits"
                    raise OJMicrolineError(msg)
                limits: dict[str, dict[str, Any]] = {}
                for item in data["Thermostats"]:
                    if (
                        not isinstance(item, dict)
                        or not isinstance(item.get("SerialNumber"), str)
                        or not item["SerialNumber"].strip()
                        or item["SerialNumber"] in limits
                    ):
                        msg = "WG4 returned malformed schedule limits"
                        raise OJMicrolineError(msg)
                    limits[item["SerialNumber"]] = {
                        key: copy.deepcopy(item.get(key))
                        for key in ("MinTimeLimits", "MaxTimeLimits")
                    }
                if serial not in limits:
                    msg = "WG4 schedule limits are unavailable for this thermostat"
                    raise OJMicrolineError(msg)
                self._schedule_limits = limits
                self._schedule_limits_loaded_at = monotonic()
                return copy.deepcopy(limits[serial])
        except TimeoutError:
            msg = "WG4 schedule limits request timed out"
            raise OJMicrolineTimeoutError(msg) from None

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

    async def set_wg4_schedule(
        self, resource: Thermostat, wire: list[dict[str, Any]]
    ) -> bool:
        """Save one native WG4 program without activation or write replay.

        The vendor WG4 website's SaveSchedule sends only the full Schedules
        array to the same per-thermostat endpoint used for mode changes.
        """
        lock = self._command_locks.setdefault(resource.serial_number, asyncio.Lock())
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT), lock:
                await self.login()
                data = await self._wg4_api.request(
                    self._wg4_api.update_regulation_mode_path,
                    method=hdrs.METH_POST,
                    params={
                        # pylint: disable-next=protected-access
                        "sessionid": self._wg4_api._session_id,  # noqa: SLF001
                        "serialnumber": resource.serial_number,
                    },
                    body={"Schedules": copy.deepcopy(wire)},
                )
                return self._wg4_api.parse_update_regulation_mode_response(data)
        except TimeoutError:
            msg = "WG4 schedule command timed out"
            raise OJMicrolineTimeoutError(msg) from None

    # Request arguments follow the pinned upstream transport interface.
    async def _request(  # noqa: PLR0913 # pylint: disable=too-many-arguments
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

    async def _request_once(  # pylint: disable=too-many-arguments
        self,
        uri: str,
        method: str,
        params: dict[str, Any],
        body: dict[str, Any] | None,
        headers: dict[str, str] | None,
    ) -> Any:
        """Consume and release the complete response inside the deadline."""
        if self.rate_limit_remaining > 0:
            msg = "WG4 requests are paused after cloud rate limiting"
            raise OJMicrolineConnectionError(msg)
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
            if response.status in (429, 503):
                delay = _retry_after_delay(
                    response.headers.get("Retry-After"),
                    fallback=DEFAULT_RATE_LIMIT_DELAY
                    if response.status == 429
                    else None,
                )
                if delay is not None:
                    self._rate_limit_until = max(
                        self._rate_limit_until, monotonic() + delay
                    )
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
