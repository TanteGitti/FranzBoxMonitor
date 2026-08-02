"""Zugriff auf das FRITZ!Box-Telefonbuch über TR-064 (via fritzconnection)."""
from __future__ import annotations

import logging
import re
from datetime import datetime

from fritzconnection.lib.fritzphonebook import FritzPhonebook

try:  # ab fritzconnection 1.12; defensiv, damit ältere Versionen nicht crashen
    from fritzconnection.core.exceptions import FritzAuthorizationError
except ImportError:  # pragma: no cover
    FritzAuthorizationError = None  # type: ignore[assignment]

_LOGGER = logging.getLogger(__name__)

# Rückgabewerte von test_credentials(); die Strings sind gleichzeitig die
# Fehlerschlüssel im Config-Flow (siehe translations/*.json).
ERROR_INVALID_AUTH = "invalid_auth"
ERROR_PHONEBOOK_UNAVAILABLE = "phonebook_unavailable"


def _normalize_number(number: str) -> str:
    """Reduziert eine Telefonnummer auf reine Ziffern, zum Vergleichen."""
    return re.sub(r"\D", "", number or "")


class FranzBoxPhonebookCache:
    """Lädt das Telefonbuch einmalig und hält es im Speicher für Lookups.

    Läuft komplett SYNCHRON (fritzconnection ist nicht asyncio-fähig) -
    wird daher immer über hass.async_add_executor_job aufgerufen, nie
    direkt aus einer async-Funktion heraus.
    """

    def __init__(self, host: str, username: str, password: str) -> None:
        self._host = host
        self._username = username or None
        self._password = password or None
        # Schlüssel: normalisierte Nummer (nur Ziffern), Wert: Name
        self._number_to_name: dict[str, str] = {}
        self._last_loaded: datetime | None = None

    @property
    def entry_count(self) -> int:
        """Anzahl der Nummern im Cache - Rückmeldung für den Nutzer."""
        return len(self._number_to_name)

    @property
    def last_loaded(self) -> datetime | None:
        """Zeitpunkt des letzten ERFOLGREICHEN Ladens, None wenn nie geglückt."""
        return self._last_loaded

    def load(self) -> bool:
        """Lädt (synchron!) alle Telefonbücher der Fritzbox neu.

        Gibt True zurück, wenn das Laden geklappt hat. Fehler werden bewusst
        nicht weitergeworfen: Eine gerade nicht erreichbare Telefonbuch-API soll
        den Call-Monitor nicht lahmlegen - Nummern bleiben dann einfach
        unaufgelöst, statt die ganze Integration zum Absturz zu bringen. Der
        Rückgabewert existiert nur, damit ein bewusst ausgelöstes Neuladen
        (Knopfdruck) dem Nutzer einen Fehler melden kann; die übrigen Aufrufer
        ignorieren ihn.
        """
        try:
            phonebook = FritzPhonebook(
                address=self._host,
                user=self._username,
                password=self._password,
            )

            new_mapping: dict[str, str] = {}
            for phonebook_id in phonebook.phonebook_ids:
                contacts = phonebook.get_all_names(phonebook_id)
                for name, numbers in contacts.items():
                    for number in numbers:
                        new_mapping[_normalize_number(number)] = name

            # Erst hier zuweisen: bei einem Fehler oben bleibt der bisherige
            # Bestand erhalten, statt durch eine leere Zuordnung ersetzt zu werden.
            self._number_to_name = new_mapping
            self._last_loaded = datetime.now()
            _LOGGER.debug(
                "Telefonbuch geladen: %d Nummern aus %d Telefonbuch/-büchern",
                len(new_mapping),
                len(phonebook.phonebook_ids),
            )
            return True
        except Exception as err:  # bewusst breit: TR-064-Fehler variieren stark
            _LOGGER.warning(
                "Telefonbuch konnte nicht geladen werden, Namen bleiben "
                "unaufgelöst: %s",
                err,
            )
            return False

    def test_credentials(self) -> str | None:
        """Prüft (synchron!) die TR-064-Zugangsdaten gegen das Telefonbuch.

        Gibt None bei Erfolg zurück, sonst einen der ERROR_*-Schlüssel. Wird
        nur aus dem Config-Flow benutzt und muss ebenfalls über
        hass.async_add_executor_job laufen.
        """
        try:
            FritzPhonebook(
                address=self._host,
                user=self._username,
                password=self._password,
            ).phonebook_ids
        except Exception as err:
            if FritzAuthorizationError is not None and isinstance(
                err, FritzAuthorizationError
            ):
                return ERROR_INVALID_AUTH
            # Manche Firmware-/Versionskombinationen melden 401 nur als
            # generischen Fehler - dann bleibt uns der Text als Indiz.
            if "401" in str(err) or "unauthorized" in str(err).lower():
                return ERROR_INVALID_AUTH
            _LOGGER.debug("Telefonbuch-Test fehlgeschlagen: %s", err)
            return ERROR_PHONEBOOK_UNAVAILABLE
        return None

    def lookup(self, number: str | None) -> str | None:
        """Sucht einen Namen zur Nummer. None, falls kein Treffer."""
        if not number:
            return None

        normalized = _normalize_number(number)

        # 1. Versuch: exakter Treffer
        if normalized in self._number_to_name:
            return self._number_to_name[normalized]

        # 2. Versuch: Vergleich der letzten 10 Ziffern - fängt Unterschiede
        # zwischen Schreibweisen mit/ohne Landesvorwahl ab (z.B. Telefonbuch-
        # Eintrag "+49 151 234567" vs. Call-Monitor-Zeile "0151234567")
        suffix = normalized[-10:]
        for stored_number, name in self._number_to_name.items():
            if stored_number[-10:] == suffix:
                return name

        return None