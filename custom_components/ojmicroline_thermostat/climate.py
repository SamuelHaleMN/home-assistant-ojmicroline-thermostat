"""Climate sensors for OJMicroline."""

import asyncio
from collections.abc import Mapping  # pylint: disable=import-error
from datetime import date
from math import isfinite
from typing import Any, ClassVar

import voluptuous as vol
from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.components.climate.const import (
    PRESET_BOOST,
    PRESET_COMFORT,
    PRESET_ECO,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ojmicroline_thermostat import OJMicrolineError, Thermostat
from ojmicroline_thermostat.const import (
    REGULATION_BOOST,
    REGULATION_COMFORT,
    REGULATION_ECO,
    REGULATION_FROST_PROTECTION,
    REGULATION_MANUAL,
    REGULATION_SCHEDULE,
    REGULATION_VACATION,
)

from .const import (
    ATTR_DAYS,
    ATTR_END_DATE,
    ATTR_EVENTS,
    ATTR_START_DATE,
    ATTR_TIME,
    CONF_COMFORT_MODE_DURATION,
    CONF_USE_COMFORT_MODE,
    DOMAIN,
    MANUFACTURER,
    PRESET_FROST_PROTECTION,
    PRESET_MANUAL,
    PRESET_SCHEDULE,
    PRESET_VACATION,
    SERVICE_CANCEL_VACATION,
    SERVICE_SET_SCHEDULE,
    SERVICE_SET_VACATION,
)
from .coordinator import OJMicrolineDataUpdateCoordinator
from .helpers import target_temperature, wd5_date
from .schedule import SLOTS, WEEKDAYS, ScheduleError, set_days

VENDOR_TO_HA_STATE = {
    REGULATION_SCHEDULE: PRESET_SCHEDULE,
    REGULATION_COMFORT: PRESET_COMFORT,
    REGULATION_MANUAL: PRESET_MANUAL,
    REGULATION_VACATION: PRESET_VACATION,
    REGULATION_FROST_PROTECTION: PRESET_FROST_PROTECTION,
    REGULATION_BOOST: PRESET_BOOST,
    REGULATION_ECO: PRESET_ECO,
}
HA_TO_VENDOR_STATE = {v: k for k, v in VENDOR_TO_HA_STATE.items()}
COMMAND_TIMEOUT = 45.0


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Load all OJMicroline Thermostat devices.

    Args:
    ----
        hass: The HomeAssistant instance.
        entry: The ConfigEntry containing the user input.
        async_add_entities: The callback to provide the created entities to.

    """
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities = []
    for idx in coordinator.data:
        entities.append(  # noqa: PERF401
            OJMicrolineThermostat(
                coordinator=coordinator, idx=idx, options=entry.options
            )
        )
    async_add_entities(entities)

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_SET_VACATION,
        {
            vol.Required(ATTR_START_DATE): cv.date,
            vol.Required(ATTR_END_DATE): cv.date,
        },
        "async_set_vacation",
    )
    platform.async_register_entity_service(
        SERVICE_CANCEL_VACATION, {}, "async_cancel_vacation"
    )
    platform.async_register_entity_service(
        SERVICE_SET_SCHEDULE,
        {
            vol.Required(ATTR_DAYS): vol.All(
                cv.ensure_list, [vol.In(WEEKDAYS)], vol.Length(min=1)
            ),
            vol.Required(ATTR_EVENTS): vol.All(
                cv.ensure_list,
                [
                    vol.Schema(
                        {
                            vol.Required(ATTR_TIME): cv.time,
                            vol.Required(ATTR_TEMPERATURE): vol.Coerce(float),
                        }
                    )
                ],
                vol.Length(min=1, max=SLOTS),
            ),
        },
        "async_set_schedule",
    )


class OJMicrolineThermostat(
    CoordinatorEntity[OJMicrolineDataUpdateCoordinator], ClimateEntity
):
    """OJMicrolineThermostat climate."""

    _attr_hvac_modes: ClassVar[list[HVACMode]] = [HVACMode.HEAT]
    _attr_hvac_mode = HVACMode.HEAT
    _attr_supported_features = (
        ClimateEntityFeature.PRESET_MODE | ClimateEntityFeature.TARGET_TEMPERATURE
    )
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_has_entity_name = True
    _attr_name = None
    _attr_translation_key = "ojthermostat"

    idx: str
    options: Mapping[str, Any]

    def __init__(
        self,
        coordinator: OJMicrolineDataUpdateCoordinator,
        idx: str,
        options: Mapping[str, Any],
    ) -> None:
        """Initialise the entity.

        Args:
        ----
            coordinator: The data coordinator updating the models.
            idx: The identifier for this entity.
            options: The options provided by the user.

        """
        super().__init__(coordinator)
        self.idx = idx
        self.options = options
        self._attr_unique_id = self.idx
        self._last_thermostat = coordinator.data[idx]
        self._command_lock = asyncio.Lock()

    def _get_thermostat(self) -> Thermostat:
        """Keep metadata and capabilities readable when a serial disappears."""
        if thermostat := (self.coordinator.data or {}).get(self.idx):
            self._last_thermostat = thermostat
        return self._last_thermostat

    @property
    def device_info(self) -> DeviceInfo:
        """Set up the device information for this thermostat.

        Returns
        -------
            The device identifiers to make sure the entity is attached
            to the correct device.

        """
        thermostat = self._get_thermostat()
        return DeviceInfo(
            identifiers={(DOMAIN, self.idx)},
            manufacturer=MANUFACTURER,
            name=thermostat.name,
            sw_version=thermostat.software_version,
            model=thermostat.model,
        )

    @property
    def available(self) -> bool:
        """Require a successful poll and a present, cloud-online thermostat."""
        thermostat = (self.coordinator.data or {}).get(self.idx)
        return super().available and thermostat is not None and thermostat.online

    @property
    def preset_modes(self) -> list[str] | None:
        """Return a list of available preset modes.

        Returns
        -------
            A list of supported preset modes in string format.

        """
        return [
            VENDOR_TO_HA_STATE[mode]
            for mode in self._get_thermostat().supported_regulation_modes
            if mode != REGULATION_VACATION or self.coordinator.wd5_api is not None
            if (
                mode != REGULATION_COMFORT
                or self.coordinator.wd5_api is not None
                or self.options.get(CONF_USE_COMFORT_MODE)
            )
        ]

    @property
    def preset_mode(self) -> str:
        """Return the current preset mode, e.g., schedule, manual.

        Returns
        -------
            The preset mode in a string format.

        """
        return VENDOR_TO_HA_STATE.get(self._get_thermostat().regulation_mode)  # type: ignore[return-value]

    @property
    def current_temperature(self) -> float:
        """Return current temperature.

        Returns
        -------
            The current temperature in a float format..

        """
        return self._get_thermostat().get_current_temperature() / 100

    @property
    def target_temperature(self) -> float:
        """Return target temperature.

        Returns
        -------
            The target temperature in a float format.

        """
        return target_temperature(self._get_thermostat()) / 100

    @property
    def max_temp(self) -> float:
        """Return max temperature.

        Returns
        -------
            The max temperature in a float format.

        """
        return self._get_thermostat().max_temperature / 100

    @property
    def min_temp(self) -> float:
        """Return min temperature.

        Returns
        -------
            The min temperature in a float format.

        """
        return self._get_thermostat().min_temperature / 100

    @property
    def hvac_action(self) -> HVACAction | None:
        """Indicates whether the thermostat is currently heating.

        Returns
        -------
            The HVACAction.

        """
        thermostat = self._get_thermostat()
        if thermostat.heating:
            return HVACAction.HEATING
        if thermostat.online:
            return HVACAction.IDLE
        return HVACAction.OFF

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set new preset mode.

        Args:
        ----
            preset_mode: The preset mode to set the thermostat to.

        """
        if not self.available:
            msg = "The thermostat is unavailable."
            raise HomeAssistantError(msg)
        if preset_mode not in (self.preset_modes or []):
            msg = "The thermostat does not support this preset."
            raise ServiceValidationError(msg)
        try:
            await self._async_command_with_readback(HA_TO_VENDOR_STATE[preset_mode])
        except OJMicrolineError as error:
            msg = "The thermostat preset command failed."
            raise HomeAssistantError(msg) from error

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new temperature.

        Args:
        ----
            **kwargs: All arguments passed to the method.

        """
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is None:
            return

        if not self.available:
            msg = "The thermostat is unavailable."
            raise HomeAssistantError(msg)
        if (
            isinstance(temperature, bool)
            or not isinstance(temperature, (int, float))
            or not isfinite(temperature)
            or not self.min_temp <= temperature <= self.max_temp
        ):
            msg = "The target temperature is outside the thermostat limits."
            raise ServiceValidationError(msg)

        try:
            await self._async_command_with_readback(
                None,
                temperature=round(temperature * 100),
                duration=self.options.get(CONF_COMFORT_MODE_DURATION),
            )
        except OJMicrolineError as error:
            msg = "The thermostat temperature command failed."
            raise HomeAssistantError(msg) from error

    async def _async_command_with_readback(
        self,
        regulation_mode: int | None,
        temperature: int | None = None,
        duration: int | None = None,
    ) -> None:
        """Serialize a write and confirm fresh cloud state, without replaying POSTs."""
        try:
            async with asyncio.timeout(COMMAND_TIMEOUT):
                await self._async_write_and_verify(
                    regulation_mode, temperature, duration
                )
        except TimeoutError as error:
            msg = "The thermostat command timed out; its state was not confirmed."
            raise HomeAssistantError(msg) from error

    async def _async_write_and_verify(
        self,
        regulation_mode: int | None,
        temperature: int | None,
        duration: int | None,
    ) -> None:
        """Complete the command inside the overall deadline, including lock wait."""
        async with self._command_lock:
            if not self.available:
                msg = "The thermostat is unavailable."
                raise HomeAssistantError(msg)
            thermostat = self.coordinator.data[self.idx]
            if regulation_mode is None:
                regulation_mode = thermostat.regulation_mode
                if regulation_mode not in {REGULATION_MANUAL, REGULATION_COMFORT}:
                    regulation_mode = (
                        REGULATION_COMFORT
                        if self.options.get(CONF_USE_COMFORT_MODE)
                        else REGULATION_MANUAL
                    )
            await self.coordinator.async_set_regulation_mode(
                thermostat,
                regulation_mode,
                temperature=temperature,
                duration=duration,
            )
            msg = (
                "The command was accepted, but matching cloud state was not confirmed."
            )
            try:
                async with asyncio.timeout(15):
                    for _ in range(6):
                        await asyncio.sleep(2)
                        thermostats = await self.coordinator.api.get_thermostats()
                        fresh = {
                            thermostat.serial_number: thermostat
                            for thermostat in thermostats
                        }
                        self.coordinator.async_set_updated_data(fresh)
                        thermostat = fresh.get(self.idx)
                        if thermostat is None or not thermostat.online:
                            raise HomeAssistantError(msg)
                        if thermostat.regulation_mode != regulation_mode:
                            continue
                        if (
                            temperature is not None
                            and thermostat.get_target_temperature() != temperature
                        ):
                            continue
                        return
            except (TimeoutError, OJMicrolineError) as error:
                raise HomeAssistantError(msg) from error
            raise HomeAssistantError(msg)

    async def async_set_vacation(self, start_date: date, end_date: date) -> None:
        """Schedule a vacation for this thermostat's group.

        Args:
        ----
            start_date: The first day of the vacation.
            end_date: The day normal regulation resumes.

        """
        if self.coordinator.wd5_api is None:
            msg = "Vacation changes are only supported on WD5-series thermostats."
            raise ServiceValidationError(msg)
        await self.coordinator.async_change_vacation(
            self.coordinator.data[self.idx], start_date, end_date, enabled=True
        )

    async def async_cancel_vacation(self) -> None:
        """Cancel the (scheduled or active) vacation for this thermostat's group."""
        if self.coordinator.wd5_api is None:
            msg = "Vacation changes are only supported on WD5-series thermostats."
            raise ServiceValidationError(msg)
        thermostat = self.coordinator.data[self.idx]
        start = wd5_date(thermostat.vacation_begin_time)
        end = wd5_date(thermostat.vacation_end_time)
        if start is None or end is None or end <= start:
            msg = "Vacation can only be cancelled on WD5-series thermostats."
            raise ServiceValidationError(msg)
        await self.coordinator.async_change_vacation(
            thermostat, start, end, enabled=False
        )

    async def async_set_schedule(
        self, days: list[str], events: list[dict[str, Any]]
    ) -> None:
        """Set the events of one or more weekdays in the group's schedule.

        Args:
        ----
            days: The weekdays to change (monday ... sunday).
            events: The day's events, each with a time and a temperature.

        """
        thermostat = self.coordinator.data[self.idx]
        if thermostat.schedule is None:
            msg = "The schedule can only be set on WD5-series thermostats."
            raise ServiceValidationError(msg)
        try:
            schedule = set_days(
                thermostat.schedule,
                days,
                [(event[ATTR_TIME], event[ATTR_TEMPERATURE]) for event in events],
            )
        except ScheduleError as error:
            raise ServiceValidationError(str(error)) from error
        await self.coordinator.async_change_schedule(thermostat, schedule)

    async def _async_delayed_request_refresh(self) -> None:
        """Get delayed data from the coordinator.

        Refreshing immediately after an API call can return stale data,
        probably due to DB propagation on the API backend.

        The *ideal* fix would be to switch away from polling; the API
        does support some sort of HTTP-long-poll notification mechanism.

        As a temporary band-aid, sleep for 2 seconds and then request a
        refresh. Manual testing indicates this seems to work well enough;
        1 second was verified to be too short.
        """
        await asyncio.sleep(2)
        await self.coordinator.async_request_refresh()

    async def async_set_hvac_mode(
        self,
        hvac_mode: HVACMode,
    ) -> None:
        """Set new hvac mode.

        Only heating is supported; other modes must not imply successful control.

        Args:
        ----
            hvac_mode: Requested mode.

        """
        if hvac_mode != HVACMode.HEAT:
            msg = "This thermostat supports heating only; use its presets."
            raise ServiceValidationError(msg)
