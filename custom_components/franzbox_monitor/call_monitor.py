"""Asyncio-Client für den FRITZ!Box Call-Monitor (TCP Port 1012)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_HOST, DEFAULT_PORT

_LOGGER = logging.getLogger(__name__)

# Wartezeiten für den Reconnect-Versuch, in Sekunden. Nach dem letzten
# Wert wiederholen wir uns (kein ewiges Hochzählen).
RECONNECT_DELAYS = [5, 10, 30, 60]

# Timeout für den Erreichbarkeitstest im Config-Flow. Kurz halten: der Nutzer
# wartet im Einrichtungsdialog darauf.
TEST_CONNECT_TIMEOUT = 5


async def async_test_connection(host: str, port: int = DEFAULT_PORT) -> None:
    """Prüft, ob der Call-Monitor erreichbar ist.

    Wirft OSError (inkl. TimeoutError, das davon erbt), wenn nicht. Ein
    geschlossener Port bedeutet in der Praxis fast immer: der Call-Monitor
    wurde an der Fritzbox nicht per '#96*5*' freigeschaltet.
    """
    writer: asyncio.StreamWriter | None = None
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), TEST_CONNECT_TIMEOUT
        )
    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()


class CallEvent:
    """Ein einzelnes, geparstes Ereignis vom Call-Monitor."""

    def __init__(
        self,
        raw_line: str,
        timestamp: datetime,
        event_type: str,
        connection_id: str,
        caller_number: str | None = None,
        called_number: str | None = None,
        duration_seconds: int | None = None,
        extension: str | None = None,
    ) -> None:
        self.raw_line = raw_line
        self.timestamp = timestamp
        self.event_type = event_type          # RING, CALL, CONNECT, DISCONNECT
        self.connection_id = connection_id
        self.caller_number = caller_number
        self.called_number = called_number
        # Nur bei DISCONNECT gefüllt: Gesprächsdauer in Sekunden, wie sie die
        # Fritzbox selbst mitschickt (0 bei nicht angenommenen Anrufen)
        self.duration_seconds = duration_seconds
        # Nebenstelle laut Fritzbox (bei CALL und CONNECT): welches Endgerät
        # bzw. welcher interne Anschluss beteiligt ist, z.B. "0" für FON 1
        self.extension = extension


def parse_call_monitor_line(line: str) -> CallEvent | None:
    """Parst eine einzelne Zeile des Call-Monitors in ein CallEvent.

    Gibt None zurück, wenn die Zeile nicht dem erwarteten Format entspricht
    (z.B. leere Zeilen oder unbekannte Ereignistypen).
    """
    parts = line.strip().split(";")
    if len(parts) < 3:
        return None

    raw_timestamp, event_type, connection_id = parts[0], parts[1], parts[2]

    try:
        timestamp = datetime.strptime(raw_timestamp, "%d.%m.%y %H:%M:%S")
    except ValueError:
        _LOGGER.debug("Konnte Zeitstempel nicht parsen: %s", raw_timestamp)
        return None

    caller_number: str | None = None
    called_number: str | None = None
    duration_seconds: int | None = None
    extension: str | None = None

    # Die Feldpositionen unterscheiden sich je Ereignistyp - siehe CLAUDE.md:
    #   RING;ID;Anrufer;AngerufeneNummer
    #   CALL;ID;Nebenstelle;GenutzteNummer;Zielnummer
    #   CONNECT;ID;Nebenstelle;Nummer
    #   DISCONNECT;ID;Dauer
    if event_type == "RING" and len(parts) >= 5:
        caller_number = parts[3]
        called_number = parts[4]
    elif event_type == "CALL" and len(parts) >= 6:
        extension = parts[3]
        caller_number = parts[4]
        called_number = parts[5]
    elif event_type == "CONNECT":
        # Nebenstelle nachtragen: bei eingehenden Anrufen steht erst hier, an
        # welchem Endgerät tatsächlich abgehoben wurde.
        if len(parts) >= 4:
            extension = parts[3]
    elif event_type == "DISCONNECT":
        # Feld 3 ist die Gesprächsdauer in Sekunden. Fehlt oder ist sie unlesbar,
        # bleibt duration_seconds None statt den Parse abzubrechen.
        if len(parts) >= 4:
            try:
                duration_seconds = int(parts[3])
            except ValueError:
                _LOGGER.debug("Konnte Gesprächsdauer nicht parsen: %s", parts[3])
    else:
        _LOGGER.debug("Unbekannter oder unvollständiger Ereignistyp: %s", line)
        return None

    return CallEvent(
        raw_line=line,
        timestamp=timestamp,
        event_type=event_type,
        connection_id=connection_id,
        caller_number=caller_number,
        called_number=called_number,
        duration_seconds=duration_seconds,
        extension=extension,
    )


class FranzBoxCallMonitorClient:
    """Hält eine dauerhafte Verbindung zum Call-Monitor-Port der Fritzbox
    und benachrichtigt registrierte Listener über neue Ereignisse."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._host: str = entry.data[CONF_HOST]
        self._port: int = DEFAULT_PORT

        self._listeners: list[Callable[[CallEvent], None]] = []
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._listen_task: asyncio.Task | None = None
        self._stopping = False

    def add_listener(
        self, callback: Callable[[CallEvent], None]
    ) -> Callable[[], None]:
        """Registriert einen Listener. Gibt eine Funktion zum Abmelden zurück."""
        self._listeners.append(callback)

        def remove_listener() -> None:
            self._listeners.remove(callback)

        return remove_listener

    async def async_start(self) -> None:
        """Startet den Hintergrund-Task, der die Verbindung hält."""
        self._stopping = False
        self._listen_task = self._hass.loop.create_task(
            self._async_connect_loop()
        )

    async def async_stop(self) -> None:
        """Beendet den Hintergrund-Task und schließt die Verbindung sauber."""
        self._stopping = True

        if self._listen_task is not None:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass

        if self._writer is not None:
            self._writer.close()
            await self._writer.wait_closed()

    async def _async_connect_loop(self) -> None:
        """Verbindet sich mit dem Call-Monitor und versucht bei Verbindungs-
        abbruch automatisch einen Reconnect (mit steigender Wartezeit)."""
        delay_index = 0

        while not self._stopping:
            try:
                _LOGGER.debug(
                    "Verbinde zu FRITZ!Box Call-Monitor %s:%s",
                    self._host,
                    self._port,
                )
                self._reader, self._writer = await asyncio.open_connection(
                    self._host, self._port
                )
                _LOGGER.debug(
                    "Verbunden mit dem Call-Monitor %s:%s", self._host, self._port
                )
                delay_index = 0  # Verbindung stand -> Backoff zurücksetzen
                await self._async_read_loop()

            except (ConnectionError, OSError) as err:
                _LOGGER.warning(
                    "Verbindung zum Call-Monitor verloren oder fehlgeschlagen: %s",
                    err,
                )

            if self._stopping:
                break

            delay = RECONNECT_DELAYS[
                min(delay_index, len(RECONNECT_DELAYS) - 1)
            ]
            delay_index += 1
            _LOGGER.debug("Warte %s Sekunden vor erneutem Verbindungsversuch", delay)
            await asyncio.sleep(delay)

    async def _async_read_loop(self) -> None:
        """Liest Zeilen vom offenen Socket, solange die Verbindung steht."""
        assert self._reader is not None

        while not self._stopping:
            raw_line = await self._reader.readline()

            if not raw_line:
                # Leere Antwort = Verbindung wurde von der Gegenseite beendet
                raise ConnectionError("Fritzbox hat die Verbindung beendet")

            line = raw_line.decode("utf-8", errors="ignore")
            # Jede empfangene Zeile protokollieren: ohne das ist bei einer
            # Fehlersuche nicht unterscheidbar, ob die Fritzbox nichts sendet
            # oder ob wir das Gesendete falsch verarbeiten.
            _LOGGER.debug("Zeile vom Call-Monitor: %s", line.strip())

            event = parse_call_monitor_line(line)

            if event is not None:
                self._notify_listeners(event)

    def _notify_listeners(self, event: CallEvent) -> None:
        """Benachrichtigt alle registrierten Listener über ein neues Ereignis."""
        for callback in self._listeners:
            # async_create_task statt direktem Aufruf: die Listener sollen
            # nicht die Leseschleife blockieren, falls sie selbst z.B. Storage
            # schreiben (siehe sensor.py im nächsten Schritt)
            self._hass.async_create_task(self._async_call_listener(callback, event))

    @staticmethod
    async def _async_call_listener(
        callback: Callable[[CallEvent], None], event: CallEvent
    ) -> None:
        try:
            callback(event)
        except Exception:  # noqa: BLE001 - ein Listener darf die anderen nicht mitreißen
            # Ohne dieses except landet der Fehler nur als "Task exception was
            # never retrieved" im Log, ohne Bezug zur auslösenden Zeile - und
            # der Rest der Verarbeitung (z.B. das Schreiben des HA-States)
            # bricht kommentarlos ab.
            _LOGGER.exception(
                "Listener hat das Ereignis nicht verarbeitet, Rohzeile: %r",
                event.raw_line,
            )