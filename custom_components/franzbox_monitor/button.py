"""Button-Plattform für FranzBoxMonitor – Telefonbuch auf Knopfdruck neu laden."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_HOST, DOMAIN
from .phonebook import FranzBoxPhonebookCache

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Richtet die Button-Entity für diesen Config-Entry ein."""
    runtime_data = hass.data[DOMAIN][entry.entry_id]

    async_add_entities([FranzBoxPhonebookReloadButton(runtime_data.phonebook, entry)])


class FranzBoxPhonebookReloadButton(ButtonEntity):
    """Lädt das FRITZ!Box-Telefonbuch sofort neu.

    Warum ein Button und kein Sensor: ein Sensor ist ein Messwert, den man nicht
    drücken kann. ButtonEntity ist der von HA dafür vorgesehene Typ - die Entity
    erscheint automatisch auf der Geräteseite, lässt sich mit der Standardkachel
    aufs Dashboard legen und ist über 'button.press' in Automationen nutzbar.
    Ein eigener Service wäre daneben nur ein zweiter Weg zum selben Ziel.
    """

    _attr_has_entity_name = True
    _attr_name = "Telefonbuch neu laden"
    _attr_icon = "mdi:refresh"
    # Bewusst KEIN _attr_entity_picture (Logo): das würde das Symbol verdrängen,
    # und ein Aktionsknopf soll als solcher erkennbar bleiben.
    # Bewusst auch keine EntityCategory: mit CONFIG würde HA den Knopf in die
    # Konfigurationsrubrik der Geräteseite verschieben.

    def __init__(
        self, phonebook: FranzBoxPhonebookCache, entry: ConfigEntry
    ) -> None:
        self._phonebook = phonebook
        self._entry = entry

        self._attr_unique_id = f"{entry.entry_id}_reload_phonebook"
        # Dieselben identifiers wie die Sensor-Entity - nur so landet der Knopf
        # auf dem vorhandenen Gerät, statt ein zweites daneben anzulegen.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="FRANZ!Box Monitor",
            manufacturer="PolliProgs",
            configuration_url=f"http://{entry.data[CONF_HOST]}",
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Sichtbare Rückmeldung: hat das Neuladen überhaupt etwas gebracht?

        Der Zustand der Entity ist bei einem Button ohnehin der Zeitpunkt des
        letzten Drückens - das sagt aber nichts über den Erfolg aus.
        """
        last_loaded = self._phonebook.last_loaded

        return {
            "phonebook_entries": self._phonebook.entry_count,
            "phonebook_updated": last_loaded.isoformat() if last_loaded else None,
        }

    async def async_press(self) -> None:
        """Knopfdruck: Telefonbuch neu einlesen."""
        # Import erst hier: __init__.py importiert diese Plattform nicht, HA lädt
        # sie über async_forward_entry_setups - ein Import auf Modulebene liefe
        # trotzdem in einen Zirkelbezug, sobald __init__.py selbst noch lädt.
        from . import async_reload_phonebook

        loaded = await async_reload_phonebook(self.hass, self._entry, self._phonebook)

        if not loaded:
            # Als HomeAssistantError, damit der Nutzer eine Fehlermeldung in der
            # Oberfläche sieht statt eines stillen Nichts. Der Call-Monitor ist
            # davon nicht betroffen: der Knopfdruck läuft in einem eigenen Task,
            # und phonebook.load() hat den bisherigen Bestand stehen lassen.
            raise HomeAssistantError(
                "Das Telefonbuch konnte nicht von der FRITZ!Box geladen werden. "
                "Bitte Zugangsdaten und Erreichbarkeit prüfen; Einzelheiten "
                "stehen im Protokoll."
            )

        _LOGGER.debug(
            "Telefonbuch auf Knopfdruck neu geladen: %d Nummern",
            self._phonebook.entry_count,
        )
        self.async_write_ha_state()
