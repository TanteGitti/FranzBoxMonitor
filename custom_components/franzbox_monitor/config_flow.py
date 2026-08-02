"""Config Flow für die FranzBoxMonitor Integration."""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback

from .call_monitor import async_test_connection
from .const import (
    CONF_HISTORY_LENGTH,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_USERNAME,
    DEFAULT_HISTORY_LENGTH,
    DOMAIN,
)
from .phonebook import ERROR_INVALID_AUTH, FranzBoxPhonebookCache

_LOGGER = logging.getLogger(__name__)


def _build_schema(defaults: dict | None = None) -> vol.Schema:
    """Baut das Formular-Schema; 'defaults' füllt die Felder vor (OptionsFlow)."""
    current = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=current.get(CONF_HOST, "")): str,
            vol.Optional(
                CONF_USERNAME, default=current.get(CONF_USERNAME, "")
            ): str,
            vol.Optional(
                CONF_PASSWORD, default=current.get(CONF_PASSWORD, "")
            ): str,
            vol.Optional(
                CONF_HISTORY_LENGTH,
                default=current.get(CONF_HISTORY_LENGTH, DEFAULT_HISTORY_LENGTH),
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=200)),
        }
    )


async def _async_validate_input(
    hass: HomeAssistant, user_input: dict
) -> dict[str, str]:
    """Prüft Host und Zugangsdaten. Gibt das errors-Dict für das Formular zurück."""
    errors: dict[str, str] = {}

    try:
        await async_test_connection(user_input[CONF_HOST])
    except OSError as err:
        _LOGGER.debug("Call-Monitor nicht erreichbar: %s", err)
        errors["base"] = "cannot_connect"
        # Ohne erreichbaren Port ist der Telefonbuch-Test wenig aussagekräftig
        return errors

    username = user_input.get(CONF_USERNAME, "")
    password = user_input.get(CONF_PASSWORD, "")
    if not username and not password:
        # Zugangsdaten sind optional: dann gibt es eben keine Namensauflösung
        return errors

    phonebook = FranzBoxPhonebookCache(
        host=user_input[CONF_HOST], username=username, password=password
    )
    result = await hass.async_add_executor_job(phonebook.test_credentials)

    # Nur falsche Zugangsdaten blockieren die Einrichtung. Ist das Telefonbuch
    # aus anderen Gründen nicht abrufbar, darf der Call-Monitor trotzdem laufen.
    if result == ERROR_INVALID_AUTH:
        errors["base"] = ERROR_INVALID_AUTH

    return errors


class FranzBoxMonitorConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Verwaltet den Einrichtungsdialog (config_flow) dieser Integration."""

    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None):
        errors: dict[str, str] = {}

        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_HOST])
            self._abort_if_unique_id_configured()

            errors = await _async_validate_input(self.hass, user_input)
            if not errors:
                return self.async_create_entry(
                    title=f"FranzBoxMonitor ({user_input[CONF_HOST]})",
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_build_schema(user_input),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> FranzBoxMonitorOptionsFlow:
        """Registriert unseren OptionsFlow für das Zahnrad-Symbol in der UI."""
        return FranzBoxMonitorOptionsFlow(config_entry)


class FranzBoxMonitorOptionsFlow(config_entries.OptionsFlow):
    """Dialog zum nachträglichen Bearbeiten von Host, Zugangsdaten und
    Historienlänge - erreichbar über das Zahnrad neben der Integration."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(self, user_input: dict | None = None):
        errors: dict[str, str] = {}

        if user_input is not None:
            errors = await _async_validate_input(self.hass, user_input)
            if not errors:
                # Wir schreiben bewusst nach entry.data zurück (nicht entry.options),
                # weil __init__.py und phonebook.py ausschließlich aus entry.data
                # lesen. async_update_entry löst danach automatisch die
                # registrierten update_listener aus (siehe __init__.py).
                self.hass.config_entries.async_update_entry(
                    self._config_entry, data=user_input
                )
                # Leerer Rückgabewert: Wir zweckentfremden den Options-Mechanismus
                # nur als UI-Vehikel, es gibt keine separaten "echten" Optionen.
                return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="init",
            data_schema=_build_schema(user_input or dict(self._config_entry.data)),
            errors=errors,
        )
