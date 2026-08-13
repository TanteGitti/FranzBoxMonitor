"""Asyncio-Client für den FRITZ!Box Call-Monitor (TCP Port 1012)."""
from __future__ import annotations

import asyncio
import logging
import socket
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

# TCP-Keepalive für die stehende Verbindung: nach KEEPALIVE_IDLE Sekunden ohne
# Verkehr sendet der Kernel Prüfpakete und gibt nach KEEPALIVE_COUNT
# erfolglosen Versuchen im Abstand von KEEPALIVE_INTERVAL auf - der Socket
# schlägt dann mit einem Fehler fehl, statt stumm weiterzuwarten.
KEEPALIVE_IDLE = 60
KEEPALIVE_INTERVAL = 10
KEEPALIVE_COUNT = 3

# Zweite Absicherung neben dem Keepalive: kommt so lange keine einzige Zeile,
# gilt die Verbindung als tot und wird neu aufgebaut. Großzügig bemessen, denn
# im Normalbetrieb schweigt der Call-Monitor die meiste Zeit - er meldet nur
# Anrufe, kein Lebenszeichen.
IDLE_RECONNECT_SECONDS = 3600


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


def _enable_tcp_keepalive(writer: asyncio.StreamWriter) -> None:
    """Schaltet TCP-Keepalive für die offene Verbindung ein.

    Der Call-Monitor sendet von sich aus nur bei Anrufen etwas - zwischen zwei
    Anrufen fließt tagelang kein Byte. Bricht die Verbindung in dieser Zeit
    still weg (Fritzbox neu gestartet, Router hat den NAT-Eintrag verworfen,
    WLAN-Aussetzer), kommt weder ein FIN noch ein Fehler an: der Socket sieht
    für uns aus wie eine gesunde, ruhige Verbindung, und readline() wartet für
    immer. Mit Keepalive prüft der Kernel selbst nach und der nächste Lesezugriff
    schlägt nach rund KEEPALIVE_IDLE + KEEPALIVE_COUNT * KEEPALIVE_INTERVAL
    Sekunden fehl - damit greift die Reconnect-Schleife.

    Die TCP_KEEP*-Feineinstellungen gibt es nur auf Linux (dort läuft HA).
    Fehlen sie, bleibt es bei SO_KEEPALIVE mit der Systemvorgabe - das sind
    üblicherweise 2 Stunden, immer noch besser als nie.
    """
    sock = writer.get_extra_info("socket")
    if sock is None:
        return

    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for option_name, value in (
            ("TCP_KEEPIDLE", KEEPALIVE_IDLE),
            ("TCP_KEEPINTVL", KEEPALIVE_INTERVAL),
            ("TCP_KEEPCNT", KEEPALIVE_COUNT),
        ):
            option = getattr(socket, option_name, None)
            if option is not None:
                sock.setsockopt(socket.IPPROTO_TCP, option, value)
    except OSError as err:
        # Keepalive ist eine Absicherung, kein Muss: der Idle-Timeout in
        # _async_read_loop fängt den Fall ohnehin ab, nur später.
        _LOGGER.debug("TCP-Keepalive konnte nicht gesetzt werden: %s", err)


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
        self._connection_listeners: list[Callable[[], None]] = []
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._listen_task: asyncio.Task | None = None
        self._stopping = False

        # Verbindungen, zu denen wir ein RING/CALL, aber noch kein DISCONNECT
        # gesehen haben. Nur dafür da, den Idle-Reconnect zu verschieben,
        # solange tatsächlich ein Gespräch läuft (siehe _async_read_loop).
        self._open_connections: set[str] = set()

    def add_listener(
        self, callback: Callable[[CallEvent], None]
    ) -> Callable[[], None]:
        """Registriert einen Listener. Gibt eine Funktion zum Abmelden zurück."""
        self._listeners.append(callback)

        def remove_listener() -> None:
            self._listeners.remove(callback)

        return remove_listener

    def add_connection_listener(
        self, callback: Callable[[], None]
    ) -> Callable[[], None]:
        """Registriert einen Rückruf für "Verbindung (neu) aufgebaut".

        Getrennt von add_listener, weil es kein Ereignis der Fritzbox ist,
        sondern eines von uns: was während der Unterbrechung passierte, sendet
        der Call-Monitor nicht nach. Wer Zustand über Ereignisse hinweg führt,
        muss ihn hier verwerfen (siehe sensor.py).
        """
        self._connection_listeners.append(callback)

        def remove_listener() -> None:
            self._connection_listeners.remove(callback)

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

        await self._async_close_connection()

    async def _async_close_connection(self) -> None:
        """Schließt den Socket, falls einer offen ist.

        Läuft auch bei jedem Reconnect: ohne das bliebe pro Verbindungsverlust
        ein Writer (und damit ein Socket) liegen.
        """
        writer, self._writer = self._writer, None
        self._reader = None

        if writer is None:
            return

        writer.close()
        try:
            await writer.wait_closed()
        except OSError as err:
            # Beim Aufräumen einer ohnehin kaputten Verbindung ohne Belang
            _LOGGER.debug("Fehler beim Schließen der Verbindung: %s", err)

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
                _enable_tcp_keepalive(self._writer)
                _LOGGER.debug(
                    "Verbunden mit dem Call-Monitor %s:%s", self._host, self._port
                )
                delay_index = 0  # Verbindung stand -> Backoff zurücksetzen
                # Was während einer Unterbrechung lief, ist für uns verloren -
                # der Call-Monitor sendet nichts nach. Also mit leerem Zustand
                # weitermachen, statt Verbindungen ewig als offen zu führen.
                self._open_connections.clear()
                self._notify_connected()
                await self._async_read_loop()

            except asyncio.CancelledError:
                # Kommt von async_stop - muss durch, sonst hängt das Entladen
                raise
            except OSError as err:
                # ConnectionError und TimeoutError erben beide von OSError
                _LOGGER.warning(
                    "Verbindung zum Call-Monitor verloren oder fehlgeschlagen: %s",
                    err,
                )
            except Exception:  # noqa: BLE001
                # Ohne diesen Fangarm stirbt der Task bei jedem unerwarteten
                # Fehler lautlos, und die Integration ist bis zum nächsten
                # Reload taub - genau das Symptom, das der Reconnect vermeiden
                # soll. Lieber weiterlaufen und den Fehler protokollieren.
                _LOGGER.exception(
                    "Unerwarteter Fehler in der Call-Monitor-Verbindung"
                )
            finally:
                await self._async_close_connection()

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
            try:
                raw_line = await asyncio.wait_for(
                    self._reader.readline(), IDLE_RECONNECT_SECONDS
                )
            except asyncio.TimeoutError:
                # Ohne dieses Timeout wartet readline() bei einem stillen
                # Verbindungsabbruch (Box neu gestartet, NAT-Eintrag im Router
                # verworfen) unbegrenzt auf Daten, die nie kommen: kein EOF,
                # kein Fehler, kein Reconnect - die Integration wirkt dann
                # eingeschlafen, bis man sie von Hand neu lädt.
                if self._open_connections:
                    # Ein laufendes Gespräch erzeugt selbst keine Zeilen. Hier
                    # abzubrechen hieße, dessen DISCONNECT zu verpassen.
                    _LOGGER.debug(
                        "Keine Daten, aber %d offene Verbindung(en) - warte weiter",
                        len(self._open_connections),
                    )
                    continue

                raise ConnectionError(
                    f"Seit {IDLE_RECONNECT_SECONDS} s keine Daten vom Call-Monitor"
                ) from None

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
                self._track_connection(event)
                self._notify_listeners(event)

    def _track_connection(self, event: CallEvent) -> None:
        """Führt Buch über die gerade offenen Verbindungen.

        Bewusst unabhängig vom Zustand des Sensors: der Client darf für seine
        eigene Entscheidung (Idle-Reconnect ja/nein) nicht auf eine Entity
        angewiesen sein, die es womöglich gar nicht gibt.
        """
        if event.event_type in ("RING", "CALL"):
            self._open_connections.add(event.connection_id)
        elif event.event_type == "DISCONNECT":
            self._open_connections.discard(event.connection_id)

    def _notify_connected(self) -> None:
        """Meldet den (Neu-)Aufbau der Verbindung.

        Anders als bei den Ereignis-Listenern wird hier direkt aufgerufen und
        nicht in einen Task verpackt: das Aufräumen muss abgeschlossen sein,
        bevor die erste Zeile der neuen Verbindung verarbeitet wird - sonst
        räumte es einen gerade erst angelegten Historieneintrag mit weg.
        """
        for callback in self._connection_listeners:
            try:
                callback()
            except Exception:  # noqa: BLE001 - einer darf die anderen nicht mitreißen
                _LOGGER.exception("Fehler beim Melden der neuen Verbindung")

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