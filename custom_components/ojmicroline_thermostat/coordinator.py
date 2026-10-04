"""OJMicroline Thermostat platform configuration."""

import asyncio
import copy
import logging
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from math import ceil
from time import monotonic
from typing import Any, override

import async_timeout
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from ojmicroline_thermostat import (
    WD5API,
    OJMicrolineAuthError,
    OJMicrolineError,
    Thermostat,
)
from ojmicroline_thermostat.const import (
    COMFORT_DURATION,
    REGULATION_BOOST,
    REGULATION_COMFORT,
    REGULATION_MANUAL,
    REGULATION_SCHEDULE,
    REGULATION_VACATION,
)
from ojmicroline_thermostat.ojmicroline import SessionOJMicrolineAPI

from .api import api_from_config_entry_data, oj_microline_from_api
from .const import (
    API_TIMEOUT,
    DOMAIN,
    ENERGY_UPDATE_INTERVAL,
    PUSH_ACTION_UPDATE,
    PUSH_UPDATE_INTERVAL,
    REFRESH_COOLDOWN,
    UPDATE_INTERVAL,
)
from .energy import EnergyStatistics
from .helpers import format_wd5, format_wd5_date, is_wd5
from .push import WD5PushClient
from .reliability import WG4_CONTROL_FIELDS, ReliableWG4API
from .wg4_schedule import WG4Schedule

_LOGGER = logging.getLogger(__name__)
COMMAND_TIMEOUT = 90.0
WG4_UPDATE_INTERVAL = 300
VERIFY_TIMEOUT = 30.0
VERIFY_DELAYS = (4, 8, 12)


class OJMicrolineDataUpdateCoordinator(DataUpdateCoordinator):
    """Define an object to fetch data."""

    data: dict[str, Thermostat]

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Class to manage fetching OJ Microline data.

        Args:
        ----
            hass: The HomeAssistant instance.
            entry: The ConfigEntry containing the user input.

        """
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
            request_refresh_debouncer=Debouncer(
                hass, _LOGGER, cooldown=REFRESH_COOLDOWN, immediate=True
            ),
        )
        model_api = api_from_config_entry_data(entry.data)
        self._model_api = model_api
        self._account_lock = asyncio.Lock()
        self._poll_successes = 0
        self._poll_failures = 0
        self._last_poll_success: str | None = None
        self._last_poll_error: str | None = None
        self._verified_commands = 0
        if isinstance(model_api, ReliableWG4API):
            self.update_interval = timedelta(seconds=WG4_UPDATE_INTERVAL)
        self._energy_updated: float | None = None
        self.wd5_api: WD5API | None = (
            model_api if isinstance(model_api, WD5API) else None
        )
        self.api = oj_microline_from_api(model_api, hass)
        self.energy = EnergyStatistics(hass, self)

    @override
    async def _async_refresh(
        self,
        log_failures: bool = True,
        raise_on_auth_failed: bool = False,
        scheduled: bool = False,
        raise_on_entry_error: bool = False,
    ) -> None:
        """Serialize WG4 polls through publication, not only their HTTP fetch.

        HA assigns the result and notifies listeners outside _async_update_data.
        Holding the account lock around that complete native refresh prevents a
        delayed poll/readback from overwriting a different device's new state.
        """
        if isinstance(self._model_api, ReliableWG4API):
            async with self._account_lock:
                await super()._async_refresh(
                    log_failures, raise_on_auth_failed, scheduled, raise_on_entry_error
                )
            return
        await super()._async_refresh(
            log_failures, raise_on_auth_failed, scheduled, raise_on_entry_error
        )

    async def async_verified_command(  # pylint: disable=too-many-arguments
        self,
        idx: str,
        regulation_mode: int | None,
        temperature: int | None = None,
        duration: int | None = None,
        *,
        use_comfort_mode: bool = False,
    ) -> None:
        """Apply one WG4 command and publish conservatively verified cloud state."""
        if self.wd5_api is not None:
            msg = "Verified account commands apply only to WG4 thermostats."
            raise ServiceValidationError(msg)
        try:
            async with asyncio.timeout(COMMAND_TIMEOUT), self._account_lock:
                resource = (self.data or {}).get(idx)
                if (
                    not self.last_update_success
                    or resource is None
                    or not resource.online
                ):
                    msg = "The thermostat is unavailable."
                    raise HomeAssistantError(msg)
                mode = regulation_mode
                if mode is None:
                    mode = resource.regulation_mode
                    if mode not in {REGULATION_MANUAL, REGULATION_COMFORT}:
                        mode = (
                            REGULATION_COMFORT
                            if use_comfort_mode
                            else REGULATION_MANUAL
                        )
                await self.async_set_regulation_mode(
                    resource, mode, temperature=temperature, duration=duration
                )
                msg = (
                    "The command was accepted, but matching cloud state "
                    "was not confirmed."
                )
                try:
                    async with asyncio.timeout(VERIFY_TIMEOUT):
                        for delay in VERIFY_DELAYS:
                            await asyncio.sleep(delay)
                            inventory = await self.api.get_thermostats()
                            fresh = {item.serial_number: item for item in inventory}
                            self.async_set_updated_data(fresh)
                            current = fresh.get(idx)
                            if current is None or not current.online:
                                raise HomeAssistantError(msg)
                            if current.regulation_mode != mode:
                                continue
                            if (
                                temperature is not None
                                and current.get_target_temperature() != temperature
                            ):
                                continue
                            self._verified_commands += 1
                            return
                except (TimeoutError, OJMicrolineError) as error:
                    raise HomeAssistantError(msg) from error
                raise HomeAssistantError(msg)
        except TimeoutError as error:
            msg = "The thermostat command timed out; its state was not confirmed."
            raise HomeAssistantError(msg) from error

    def wg4_schedule_snapshot(self, idx: str) -> dict[str, Any] | None:
        """Return an isolated, usable stored program without making a request."""
        if not isinstance(self._model_api, ReliableWG4API):
            return None
        snapshot = self._model_api.get_wg4_schedule_snapshot(idx)
        if snapshot is None:
            return None
        try:
            WG4Schedule(snapshot.get("Schedules"))
        except ValueError:
            return None
        return snapshot

    async def async_wg4_schedule(  # pylint: disable=too-many-arguments,too-many-locals
        self,
        idx: str,
        changes: list[dict[str, Any]],
        expected_hash: str,
        temperature_unit: str,
        *,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Preview or save a native WG4 program, preserving regulation settings.

        A preview uses the existing inventory only. Applying takes one fresh
        account snapshot, requires the preview's hash, saves a recoverable
        preimage, and sends one per-device POST. No command is replayed or
        schedule mode activated. Cloud readback is bounded and serialized with
        all account polling and other controls.
        """
        if not isinstance(self._model_api, ReliableWG4API):
            msg = "Native schedule editing applies only to WG4 thermostats."
            raise ServiceValidationError(msg)
        try:
            async with asyncio.timeout(COMMAND_TIMEOUT), self._account_lock:
                if not dry_run:
                    await self._async_wg4_schedule_inventory()
                snapshot = self.wg4_schedule_snapshot(idx)
                resource = (self.data or {}).get(idx)
                if snapshot is None or resource is None:
                    msg = "The thermostat has no usable native weekly program."
                    raise ServiceValidationError(msg)
                if not dry_run and not resource.online:
                    msg = "The thermostat is unavailable."
                    raise HomeAssistantError(msg)
                baseline = WG4Schedule(snapshot["Schedules"])
                if expected_hash != baseline.fingerprint():
                    msg = (
                        "The native program changed; "
                        "obtain a new preview before saving."
                    )
                    raise ServiceValidationError(msg)
                limits = self.api.cached_wg4_schedule_limits(idx)
                updated = self._wg4_schedule_patch(
                    baseline, changes, temperature_unit, snapshot, limits
                )
                result = self._wg4_schedule_result(
                    baseline,
                    updated,
                    changes,
                    snapshot,
                    dry_run=dry_run,
                    limits_checked=limits is not None,
                )
                if dry_run:
                    return result
                if not result["changed"]:
                    # A fresh cloud inventory already matched; no write or
                    # backup rotation is needed for an unchanged program.
                    result["verified"] = True
                    return result
                if limits is None:
                    # /defaults is an account-wide extra read. Reserve it for
                    # changed saves; unchanged canaries need only inventory.
                    limits = await self.api.get_wg4_schedule_limits(idx)
                    updated = self._wg4_schedule_patch(
                        baseline, changes, temperature_unit, snapshot, limits
                    )
                    result = self._wg4_schedule_result(
                        baseline,
                        updated,
                        changes,
                        snapshot,
                        dry_run=False,
                        limits_checked=True,
                    )
                backup_key = await self._async_wg4_schedule_backup(idx, snapshot)
                await self.api.set_wg4_schedule(resource, updated.to_payload())
                await self._async_verify_wg4_schedule(
                    idx, updated.fingerprint(), snapshot
                )
                result["verified"] = True
                result["backup_key"] = backup_key
                self._verified_commands += 1
                return result
        except OJMicrolineError as error:
            raise HomeAssistantError(str(error)) from error
        except TimeoutError as error:
            msg = (
                "The native schedule operation timed out; its state was not confirmed."
            )
            raise HomeAssistantError(msg) from error

    @staticmethod
    def _wg4_schedule_patch(
        baseline: WG4Schedule,
        changes: list[dict[str, Any]],
        temperature_unit: str,
        snapshot: dict[str, Any],
        limits: dict[str, Any] | None,
    ) -> WG4Schedule:
        """Translate model validation errors into a visible HA service error."""
        try:
            return baseline.with_changes(
                changes,
                unit=temperature_unit,
                min_temp=snapshot["MinTemp"],
                max_temp=snapshot["MaxTemp"],
                limits=limits,
            )
        except ValueError as error:
            raise ServiceValidationError(str(error)) from error

    async def _async_wg4_schedule_inventory(self) -> None:
        """Publish a fresh inventory while the caller holds the account lock."""
        inventory = await self.api.get_thermostats()
        self.async_set_updated_data({item.serial_number: item for item in inventory})

    @staticmethod
    def _wg4_schedule_result(  # noqa: PLR0913 # pylint: disable=too-many-arguments
        baseline: WG4Schedule,
        updated: WG4Schedule,
        changes: list[dict[str, Any]],
        snapshot: dict[str, Any],
        *,
        dry_run: bool,
        limits_checked: bool,
    ) -> dict[str, Any]:
        """Return the exact canonical preview in Celsius and device-local time."""
        days = updated.attributes()
        return {
            "dry_run": dry_run,
            "changed": updated.fingerprint() != baseline.fingerprint(),
            "baseline_hash": baseline.fingerprint(),
            "schedule_hash": updated.fingerprint(),
            "changes": [
                {"day": change["day"], **days[change["day"]][change["slot"]]}
                for change in changes
            ],
            "days": days,
            "temperature_unit": "C",
            "time_basis": "thermostat_local",
            "timezone_offset": snapshot.get("TZOffset"),
            "verified": False,
            "limits_checked": limits_checked,
        }

    async def _async_wg4_schedule_backup(
        self, idx: str, snapshot: dict[str, Any]
    ) -> str:
        """Durably retain three exact preimages before any thermostat POST."""
        # Keep the optional schedule-edit storage dependency off climate reads.
        # pylint: disable-next=import-outside-toplevel
        from homeassistant.helpers.storage import Store  # noqa: PLC0415

        entry_id = self.config_entry.entry_id
        identity = sha256(f"{entry_id}:{idx}".encode()).hexdigest()
        key = f"{DOMAIN}.wg4_schedule_backup.{identity}"
        store = Store(self.hass, 1, key)
        try:
            previous = await store.async_load()
            if previous is not None and (
                not isinstance(previous, dict)
                or previous.get("serial_number") != idx
                or not isinstance(previous.get("snapshots"), list)
            ):
                msg = "The native schedule backup is malformed; no write was sent."
                raise HomeAssistantError(msg)
            history = previous["snapshots"] if previous is not None else []
            saved = {
                "saved_at_utc": datetime.now(UTC).isoformat(),
                "schedule_hash": WG4Schedule(snapshot["Schedules"]).fingerprint(),
                "snapshot": copy.deepcopy(snapshot),
            }
            await store.async_save(
                {"serial_number": idx, "snapshots": [*history[-2:], saved]}
            )
        except (OSError, TypeError, ValueError) as error:
            msg = "The native schedule backup could not be saved; no write was sent."
            raise HomeAssistantError(msg) from error
        return key

    async def _async_verify_wg4_schedule(
        self, idx: str, expected_hash: str, baseline: dict[str, Any]
    ) -> None:
        """Confirm the stored program and unchanged controls by cloud readback."""
        msg = (
            "The schedule was accepted, but matching cloud program and unchanged "
            "regulation settings were not confirmed."
        )
        try:
            async with asyncio.timeout(VERIFY_TIMEOUT):
                for delay in VERIFY_DELAYS:
                    await asyncio.sleep(delay)
                    await self._async_wg4_schedule_inventory()
                    resource = (self.data or {}).get(idx)
                    current = self.wg4_schedule_snapshot(idx)
                    if resource is None or not resource.online or current is None:
                        raise HomeAssistantError(msg)
                    if any(
                        current.get(key) != baseline.get(key)
                        for key in WG4_CONTROL_FIELDS
                    ):
                        raise HomeAssistantError(msg)
                    if WG4Schedule(current["Schedules"]).fingerprint() == expected_hash:
                        return
        except (TimeoutError, OJMicrolineError) as error:
            raise HomeAssistantError(msg) from error
        raise HomeAssistantError(msg)

    def diagnostic_status(self) -> dict[str, Any]:
        """Return operational counts without account/device identifiers or state."""
        data = self.data or {}
        return {
            "last_update_success": self.last_update_success,
            "last_successful_poll_utc": self._last_poll_success,
            "last_poll_error_type": self._last_poll_error,
            "poll_successes": self._poll_successes,
            "poll_failures": self._poll_failures,
            "poll_interval_seconds": self.update_interval.total_seconds()
            if self.update_interval
            else None,
            "inventory_device_count": len(data),
            "inventory_online_count": sum(item.online for item in data.values()),
            "verified_command_count": self._verified_commands,
            "authentication_count": getattr(
                self._model_api, "authentication_count", None
            ),
            "rate_limit_remaining_seconds": ceil(
                getattr(self.api, "rate_limit_remaining", 0)
            ),
        }

    async def _async_update_data(self) -> dict[str, Thermostat]:
        """Fetch data from API endpoint.

        This is the place to pre-process the data to lookup tables
        so entities can quickly look up their data.

        Returns
        -------
            An object containing the serial number as a key, and
            the resource as a value.

        Raises
        ------
            ConfigEntryAuthFailed: An invalid config was ued.
            UpdateFailed: An error occurred when updating the data.

        """
        try:
            async with async_timeout.timeout(API_TIMEOUT):
                thermostats = await self._async_fetch_thermostats()
                result = {resource.serial_number: resource for resource in thermostats}
                self._poll_successes += 1
                self._last_poll_success = datetime.now(UTC).isoformat()
                self._last_poll_error = None
                return result

        except OJMicrolineAuthError as error:
            self._poll_failures += 1
            self._last_poll_error = type(error).__name__
            raise ConfigEntryAuthFailed from error

        except OJMicrolineError as error:
            self._poll_failures += 1
            self._last_poll_error = type(error).__name__
            retry_after = ceil(getattr(self.api, "rate_limit_remaining", 0))
            raise UpdateFailed(error, retry_after=retry_after or None) from error
        except TimeoutError:
            self._poll_failures += 1
            self._last_poll_error = "TimeoutError"
            raise

    async def _async_fetch_thermostats(self) -> list[Thermostat]:
        """Fetch the thermostats, reusing recent energy usage where possible.

        The library fetches energy usage for every thermostat on every poll,
        which is one extra request per thermostat. Energy usage changes
        slowly, so only refresh it every ENERGY_UPDATE_INTERVAL.
        """
        api = self._model_api
        if not isinstance(api, SessionOJMicrolineAPI):
            return await self.api.get_thermostats()

        await self.api.login()
        data = await api.request(
            api.get_thermostats_path,
            method="GET",
            params={
                # pylint: disable-next=protected-access
                "sessionid": api._session_id,  # noqa: SLF001
                **api.get_thermostats_params(),
            },
        )
        thermostats = api.parse_thermostats_response(data)

        now = monotonic()
        refresh_energy = (
            self._energy_updated is None
            or now - self._energy_updated >= ENERGY_UPDATE_INTERVAL
        )
        for thermostat in thermostats:
            previous = (self.data or {}).get(thermostat.serial_number)
            if not refresh_energy and previous is not None:
                thermostat.energy = previous.energy
            elif is_wd5(thermostat):
                # Today's usage per local hour; the library's own request uses
                # the UTC date, so it shows yesterday's total until 02:00.
                today = await self.energy.async_today(thermostat)
                thermostat.energy = [round(sum(today), 4)]
                self.energy.schedule_import(thermostat, today)
            else:
                thermostat.energy = await api.get_energy_usage(thermostat)
        if refresh_energy:
            self._energy_updated = now
        return thermostats

    def async_start_push(self, entry: ConfigEntry) -> None:
        """Start receiving push updates (WD5 series only)."""
        if self.wd5_api is None:
            return
        WD5PushClient(
            self.hass,
            async_get_clientsession(self.hass),
            self.wd5_api,
            self._async_handle_push_message,
            self._async_handle_push_connection,
        ).start(entry)

    @callback
    def _async_handle_push_connection(self, connected: bool) -> None:  # noqa: FBT001
        # Polling is still needed for energy usage and as a fallback, but
        # can be much less frequent while push updates are coming in.
        seconds = PUSH_UPDATE_INTERVAL if connected else UPDATE_INTERVAL
        # pylint: disable-next=attribute-defined-outside-init
        self.update_interval = timedelta(seconds=seconds)
        if connected:
            # Catch up on anything missed while disconnected.
            self.hass.async_create_task(self.async_request_refresh())

    @callback
    def _async_handle_push_message(self, message: dict[str, Any]) -> None:
        _LOGGER.debug(
            "Push message received: %s",
            {
                key: len(value)
                for key, value in message.items()
                if isinstance(value, list)
            },
        )
        # "data" is defined by DataUpdateCoordinator, which pylint cannot see.
        # pylint: disable=access-member-before-definition
        if not self.data:
            return

        data = dict(self.data)
        # pylint: enable=access-member-before-definition
        changed = False
        # Group changes (mode, setpoints, schedule) apply to every thermostat
        # in the group; fetch everything again rather than guessing.
        needs_refresh = bool(message.get("Groups"))

        for item in message.get("ThermostatRealTimes") or []:
            current = data.get(item.get("SerialNumber"))
            if current is None:
                continue
            data[current.serial_number] = replace(
                current,
                online=item.get("Online", current.online),
                heating=item.get("Heating", current.heating),
                temperature_room=item.get("RoomTemperature", current.temperature_room),
                temperature_floor=item.get(
                    "FloorTemperature", current.temperature_floor
                ),
                sensor_mode=item.get("SensorAppl", current.sensor_mode),
            )
            changed = True

        for item in message.get("Thermostats") or []:
            current = data.get(item.get("SerialNumber"))
            if current is None or item.get("Action") != PUSH_ACTION_UPDATE:
                # Added or removed thermostat.
                needs_refresh = True
                continue
            try:
                thermostat = Thermostat.from_wd5_json(item)
            except (KeyError, TypeError, ValueError):
                needs_refresh = True
                continue
            thermostat.energy = current.energy
            data[current.serial_number] = thermostat
            changed = True

        if changed:
            # Unlike async_set_updated_data this keeps the polling schedule,
            # so energy usage keeps being refreshed.
            self.data = data  # pylint: disable=attribute-defined-outside-init
            self.async_update_listeners()
        if needs_refresh:
            self.hass.async_create_task(self.async_request_refresh())

    async def async_set_vacation(
        self,
        thermostat: Thermostat,
        start: date,
        end: date,
        *,
        enabled: bool,
    ) -> None:
        """Set the vacation period of a thermostat's group, and enable or disable it.

        Mirrors the vacation screen of the OJ Microline and SWATT apps: the
        vacation runs from 00:00 on the start date until 00:00 on the end date.
        If an enabled vacation has already begun, vacation mode is activated
        right away. When vacation mode is left (disabled, or moved to the
        future), the thermostat returns to schedule or manual mode, whichever
        was used last.

        Args:
        ----
            thermostat: The thermostat whose group to update.
            start: The first day of the vacation.
            end: The day normal regulation resumes.
            enabled: Whether the vacation is enabled.

        Raises:
        ------
            OJMicrolineError: The API refused the update.

        """
        regulation_mode = thermostat.regulation_mode
        if enabled and dt_util.start_of_local_day(start) <= dt_util.now():
            regulation_mode = REGULATION_VACATION
        elif regulation_mode == REGULATION_VACATION:
            regulation_mode = (
                REGULATION_SCHEDULE
                if thermostat.last_primary_mode_is_auto
                else REGULATION_MANUAL
            )

        await self._async_update_group(
            thermostat,
            {
                "RegulationMode": regulation_mode,
                "VacationEnabled": enabled,
                "VacationBeginDay": format_wd5_date(start),
                "VacationEndDay": format_wd5_date(end),
            },
        )

    async def async_change_vacation(
        self,
        thermostat: Thermostat,
        start: date,
        end: date,
        *,
        enabled: bool,
    ) -> None:
        """Validate and apply a vacation change requested by the user.

        Raises
        ------
            ServiceValidationError: The dates are not valid.
            HomeAssistantError: The API refused the update.

        """
        if self.wd5_api is None:
            msg = "Vacation can only be set on WD5-series thermostats."
            raise ServiceValidationError(msg)
        if end <= start:
            msg = "The vacation end date must be after the start date."
            raise ServiceValidationError(msg)
        if enabled and end <= dt_util.now().date():
            msg = "The vacation end date must be in the future."
            raise ServiceValidationError(msg)
        try:
            await self.async_set_vacation(thermostat, start, end, enabled=enabled)
        except OJMicrolineError as error:
            raise HomeAssistantError(str(error)) from error
        await self.async_request_delayed_refresh()

    async def async_change_schedule(
        self, thermostat: Thermostat, schedule: dict[str, Any]
    ) -> None:
        """Apply a schedule change requested by the user.

        Raises
        ------
            ServiceValidationError: The thermostat has no schedule.
            HomeAssistantError: The API refused the update.

        """
        if self.wd5_api is None:
            msg = "The schedule can only be set on WD5-series thermostats."
            raise ServiceValidationError(msg)
        try:
            await self.async_set_schedule(thermostat, schedule)
        except OJMicrolineError as error:
            raise HomeAssistantError(str(error)) from error
        await self.async_request_delayed_refresh()

    async def async_request_delayed_refresh(self) -> None:
        """Refresh shortly after a change; the API returns stale data right away.

        Push updates normally arrive first; this is the fallback.
        """
        await asyncio.sleep(2)
        await self.async_request_refresh()

    async def async_set_schedule(
        self, thermostat: Thermostat, schedule: dict[str, Any]
    ) -> None:
        """Set the weekly schedule of a thermostat's group.

        Raises
        ------
            OJMicrolineError: The API refused the update.

        """
        await self._async_update_group(
            thermostat, {"Schedule": schedule}, exclude_vacation=True
        )

    async def async_fetch_energy(
        self, thermostat: Thermostat, view_type: int, day: date, history: int
    ) -> Any:
        """Fetch the raw energy usage response (WD5 series only).

        Args:
        ----
            thermostat: The thermostat.
            view_type: The API's view type (2 = days, 4 = months in the apps).
            day: The reference date sent to the API.
            history: The API's history parameter.

        """
        api = self.wd5_api
        if api is None:
            msg = "Energy usage history is only supported on WD5-series thermostats."
            raise OJMicrolineError(msg)
        await self.api.login()
        return await api.request(
            api.get_energy_usage_path,
            method="POST",
            # pylint: disable-next=protected-access
            params={"sessionid": api._session_id},  # noqa: SLF001
            body={
                **api.get_thermostats_params(),
                "ThermostatID": thermostat.serial_number,
                "ViewType": view_type,
                "DateTime": day.isoformat(),
                "History": history,
            },
        )

    async def async_set_regulation_mode(
        self,
        thermostat: Thermostat,
        regulation_mode: int,
        temperature: int | None = None,
        duration: int | None = None,
    ) -> None:
        """Set the regulation mode (preset) and optionally the temperature.

        Raises
        ------
            OJMicrolineError: The API refused the update.

        """
        duration = duration or COMFORT_DURATION
        if self.wd5_api is None:
            await self.api.set_regulation_mode(
                thermostat, regulation_mode, temperature, duration
            )
            return
        await self._async_update_group(
            thermostat,
            regulation_mode=regulation_mode,
            temperature=temperature,
            duration=duration,
        )

    async def _async_update_group(  # noqa: PLR0913 # pylint: disable=too-many-arguments
        self,
        thermostat: Thermostat,
        changes: dict[str, Any] | None = None,
        *,
        regulation_mode: int | None = None,
        temperature: int | None = None,
        duration: int = COMFORT_DURATION,
        exclude_vacation: bool = False,
    ) -> None:
        """Update settings of a thermostat's group (WD5 series only).

        The API replaces all group settings at once, so the thermostat's current
        settings are sent along with the changes. Date/times are sent back as
        the wall clock times the API returned (see helpers.py), except for the
        comfort/boost end time when that mode is being set.
        """
        api = self.wd5_api
        if api is None:
            msg = "This is only supported on WD5-series thermostats."
            raise OJMicrolineError(msg)

        body = api.update_regulation_mode_body(
            thermostat,
            thermostat.regulation_mode if regulation_mode is None else regulation_mode,
            temperature,
            duration,
        )
        group = body["SetGroup"]
        group.update(
            ExcludeVacationData=exclude_vacation,
            VacationBeginDay=format_wd5(thermostat.vacation_begin_time),
            VacationEndDay=format_wd5(thermostat.vacation_end_time),
        )
        if regulation_mode != REGULATION_COMFORT:
            group["ComfortEndTime"] = format_wd5(thermostat.comfort_end_time)
        if regulation_mode != REGULATION_BOOST:
            group["BoostEndTime"] = format_wd5(thermostat.boost_end_time)
        group.update(changes or {})

        await self.api.login()
        response = await api.request(
            api.update_regulation_mode_path,
            method="POST",
            # pylint: disable-next=protected-access
            params={"sessionid": api._session_id},  # noqa: SLF001
            body=body,
        )
        if not api.parse_update_regulation_mode_response(response):
            msg = "Unable to update the thermostat group."
            raise OJMicrolineError(msg)
