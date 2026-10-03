"""Config flow to configure OJMicroline."""

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.const import CONF_API_KEY, CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from ojmicroline_thermostat import (
    OJMicrolineAuthError,
    OJMicrolineConnectionError,
    OJMicrolineError,
    OJMicrolineTimeoutError,
)
from ojmicroline_thermostat.const import COMFORT_DURATION

from .api import oj_microline_from_config_entry_data
from .const import (
    CONF_APPLICATION,
    CONF_COMFORT_MODE_DURATION,
    CONF_CUSTOMER_ID,
    CONF_MODEL,
    CONF_USE_COMFORT_MODE,
    CONFIG_FLOW_VERSION,
    DEFAULT_WG4_APPLICATION,
    DOMAIN,
    INTEGRATION_NAME,
    MODEL_WD5_SERIES,
    MODEL_WG4_SERIES,
)

MODEL_LEGACY_WARM_TILES = "Migrate existing Warm Tiles"

DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_MODEL): vol.In([MODEL_WD5_SERIES, MODEL_WG4_SERIES]),
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        CONF_HOST: str,
        CONF_CUSTOMER_ID: int,
        CONF_API_KEY: str,
        vol.Optional(CONF_APPLICATION): int,
    }
)

USER_STEP_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_MODEL): vol.In([MODEL_WD5_SERIES, MODEL_WG4_SERIES]),
    }
)

WD5_STEP_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Required(CONF_API_KEY): str,
        CONF_HOST: str,
        CONF_CUSTOMER_ID: int,
    }
)

WG4_STEP_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        CONF_HOST: str,
        vol.Optional(CONF_APPLICATION, default=DEFAULT_WG4_APPLICATION): int,
    }
)


class OJMicrolineFlowHandler(ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg]
    """Handle an OJ Microline config flow."""

    VERSION = CONFIG_FLOW_VERSION

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,  # noqa: ARG004 # pylint: disable=unused-argument
    ) -> OptionsFlow:
        """Get the options flow for this handler.

        Args:
        ----
            config_entry: The ConfigEntry instance.

        Returns:
        -------
            The created config flow.

        """
        return OJMicrolineOptionsFlowHandler()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> Any:
        """Handle a flow initialized by the user.

        Args:
        ----
            user_input: The input received from the user or none.

        Returns:
        -------
            The created config entry or a form to re-enter the user input with errors.

        """
        if user_input:
            if user_input[CONF_MODEL] == MODEL_LEGACY_WARM_TILES:
                return await self.async_step_legacy()
            if user_input[CONF_MODEL] == MODEL_WD5_SERIES:
                return await self.async_step_wd5()
            return await self.async_step_wg4()
        models = [MODEL_WD5_SERIES, MODEL_WG4_SERIES]
        if self.hass.config_entries.async_entries("schluter"):
            models.append(MODEL_LEGACY_WARM_TILES)
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_MODEL): vol.In(models)}),
        )

    async def async_step_legacy(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm which existing Warm Tiles account to migrate inside HA."""
        choices = {
            entry.entry_id: entry.title
            for entry in self.hass.config_entries.async_entries("schluter")
        }
        if not choices:
            return self.async_abort(reason="invalid_legacy_entry")
        if user_input is not None:
            if user_input.get("legacy_entry_id") not in choices:
                return self.async_abort(reason="invalid_legacy_entry")
            return await self.async_step_import(user_input)
        return self.async_show_form(
            step_id="legacy",
            data_schema=vol.Schema({vol.Required("legacy_entry_id"): vol.In(choices)}),
        )

    async def async_step_wg4(self, user_input: dict[str, Any] | None = None) -> Any:
        """Step that gathers information for WG4-series thermostats.

        The result is a config entry if successful.

        Args:
        ----
            user_input: The input received from the user or none.

        Returns:
        -------
            The created config entry or a form to re-enter the user input with errors.

        """
        errors: dict[str, str] = {}
        if user_input:
            result = await self._async_try_create_entry(
                {
                    CONF_MODEL: MODEL_WG4_SERIES,
                    **user_input,
                },
                errors,
            )
            if result is not None:
                return result
        return self.async_show_form(
            step_id="wg4", data_schema=WG4_STEP_SCHEMA, errors=errors
        )

    async def async_step_wd5(self, user_input: dict[str, Any] | None = None) -> Any:
        """Step that gathers information for WD5-series thermostats.

        The result is a config entry if successful.

        Args:
        ----
            user_input: The input received from the user or none.

        Returns:
        -------
            The created config entry or a form to re-enter the user input with errors.

        """
        errors: dict[str, str] = {}
        if user_input:
            result = await self._async_try_create_entry(
                {
                    CONF_MODEL: MODEL_WD5_SERIES,
                    **user_input,
                },
                errors,
            )
            if result is not None:
                return result
        return self.async_show_form(
            step_id="wd5", data_schema=WD5_STEP_SCHEMA, errors=errors
        )

    async def _async_try_create_entry(
        self, data: dict[str, Any], errors: dict[str, str]
    ) -> FlowResult | None:
        """Validate the config entry data and logs in to the API.

        If successful, calls async_create_entry and returns the FlowResult.
        Otherwise, stores an error in the errors dict and returns None.
        """
        data = DATA_SCHEMA(data)
        # Disallow duplicate entries for the same cloud and account.
        self._async_abort_entries_match(
            {
                key: data[key]
                for key in data
                if key in [CONF_MODEL, CONF_HOST, CONF_USERNAME]
            }
        )
        if await self._async_validate_credentials(data, errors):
            return self.async_create_entry(
                title=f"{INTEGRATION_NAME} ({data[CONF_USERNAME]})", data=data
            )
        return None

    async def async_step_import(self, import_data: dict[str, Any]) -> FlowResult:
        """Import this installation's legacy Warm Tiles account inside HA."""
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")
        legacy_id = import_data.get("legacy_entry_id")
        if not isinstance(legacy_id, str):
            return self.async_abort(reason="invalid_legacy_entry")
        legacy = self.hass.config_entries.async_get_entry(legacy_id)
        if (
            legacy is None
            or legacy.domain != "schluter"
            or not isinstance(legacy.data.get(CONF_USERNAME), str)
            or not isinstance(legacy.data.get(CONF_PASSWORD), str)
        ):
            return self.async_abort(reason="invalid_legacy_entry")
        if legacy.disabled_by is None:
            return self.async_abort(reason="legacy_not_disabled")
        data = {
            CONF_MODEL: MODEL_WG4_SERIES,
            CONF_HOST: "warmtiles.mythermostat.info",
            CONF_APPLICATION: 13,
            CONF_USERNAME: legacy.data[CONF_USERNAME],
            CONF_PASSWORD: legacy.data[CONF_PASSWORD],
        }
        errors: dict[str, str] = {}
        result = await self._async_try_create_entry(data, errors)
        if result is not None:
            return result
        return self.async_abort(reason=errors.get("base", "unknown"))

    async def _async_validate_credentials(
        self, data: dict[str, Any], errors: dict[str, str]
    ) -> bool:
        """Validate credentials without changing a thermostat or config entry."""
        api = None
        try:
            api = oj_microline_from_config_entry_data(data, self.hass)
            await api.login()
        except OJMicrolineAuthError:
            errors["base"] = "invalid_auth"
        except OJMicrolineTimeoutError:
            errors["base"] = "timeout"
        except OJMicrolineConnectionError:
            errors["base"] = "connection_failed"
        except OJMicrolineError:
            errors["base"] = "unknown"
        else:
            return True
        finally:
            if api is not None:
                await api.close()
        return False

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> FlowResult:  # noqa: ARG002
        """Start password recovery for the existing account."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Update only the password, preserving cloud routing and entity identity."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            data = {**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
            if await self._async_validate_credentials(data, errors):
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            errors=errors,
        )


class OJMicrolineOptionsFlowHandler(OptionsFlow):
    """Handle options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle a flow initialized by the user.

        Args:
        ----
            user_input: The input received from the user or none.

        Returns:
        -------
            The created config entry.

        """
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_USE_COMFORT_MODE,
                        default=self.config_entry.options.get(
                            CONF_USE_COMFORT_MODE, False
                        ),
                    ): bool,
                    vol.Optional(
                        CONF_COMFORT_MODE_DURATION,
                        default=self.config_entry.options.get(
                            CONF_COMFORT_MODE_DURATION, COMFORT_DURATION
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=1)),
                }
            ),
        )
