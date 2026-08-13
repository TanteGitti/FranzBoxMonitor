"""Sensor-Plattform für FranzBoxMonitor – aktueller Status + Anrufhistorie."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.storage import Store

from .call_monitor import CallEvent, FranzBoxCallMonitorClient
from .const import (
    CONF_HISTORY_LENGTH,
    CONF_HOST,
    DEFAULT_HISTORY_LENGTH,
    DOMAIN,
    SIGNAL_PHONEBOOK_UPDATED,
)
from .phonebook import FranzBoxPhonebookCache

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY_TEMPLATE = f"{DOMAIN}_history_{{entry_id}}"

STATE_IDLE = "idle"
STATE_RINGING = "ringing"
STATE_DIALING = "dialing"
STATE_TALKING = "talking"

EVENT_TO_STATE = {
    "RING": STATE_RINGING,
    "CALL": STATE_DIALING,
    "CONNECT": STATE_TALKING,
}

# Reihenfolge der Dringlichkeit: laufen mehrere Verbindungen parallel, gewinnt der
# weiter fortgeschrittene Zustand (ein aktives Gespräch schlägt ein Klingeln).
STATE_PRIORITY = [STATE_TALKING, STATE_RINGING, STATE_DIALING]

# Symbol je Zustand - dieselbe Zuordnung wie STATE_ICONS in der Lovelace-Card.
# Damit wechselt auch die HA-Standardkachel das Symbol mit dem Zustand.
STATE_ICONS = {
    STATE_IDLE: "mdi:phone-hangup",
    STATE_RINGING: "mdi:phone-ring",
    STATE_DIALING: "mdi:phone-outgoing",
    STATE_TALKING: "mdi:phone-in-talk",
}

DIRECTION_IN = "eingehend"
DIRECTION_OUT = "ausgehend"

# Ergebnis eines Anrufs. Solange er läuft, steht OUTCOME_ONGOING drin; beim
# DISCONNECT wird daraus einer der drei Endzustände.
OUTCOME_ONGOING = "laufend"
OUTCOME_ANSWERED = "angenommen"      # Gespräch kam zustande (CONNECT gesehen)
OUTCOME_MISSED = "verpasst"          # eingehend, nie angenommen
OUTCOME_UNREACHED = "nicht_erreicht"  # ausgehend, Gegenseite ging nicht ran
OUTCOME_BLOCKED = "blockiert"        # eingehend, von der Rufnummernsperre abgewiesen

# Ergebnisse, die zu einer Wiederholungsserie zusammengefasst werden dürfen.
# 'angenommen' fehlt hier bewusst: ein zustande gekommenes Gespräch darf nie
# hinter einem Zähler verschwinden. Zusammengefasst wird ohnehin nur, wenn beide
# Anrufe dasselbe Ergebnis haben (siehe _collapse_repeat).
MERGEABLE_OUTCOMES = frozenset({OUTCOME_MISSED, OUTCOME_UNREACHED, OUTCOME_BLOCKED})

# Höchstabstand zwischen RING und DISCONNECT, bis zu dem ein nicht angenommener
# eingehender Anruf als von der Fritzbox abgewiesen gilt (siehe _was_blocked).
BLOCKED_MAX_SECONDS = 2

# Zeitfenster, in dem ein erneuter Anruf derselben Gegenstelle als unmittelbare
# Wiederholung gilt und den bestehenden Eintrag hochzählt, statt einen neuen
# anzulegen (siehe _collapse_repeat).
REPEAT_WINDOW_SECONDS = 60

# Schlüssel, die jeder Historieneintrag garantiert besitzt (siehe _normalize_entry)
HISTORY_KEYS: dict[str, Any] = {
    "timestamp": None,
    "direction": None,
    # Rohdaten aus der Call-Monitor-Zeile
    "caller_number": None,
    "caller_name": None,
    "called_number": None,
    "called_name": None,
    # Abgeleitet: die Gegenstelle - das, was man in einer Anrufliste sehen will,
    # unabhängig von der Richtung. Erspart Templates und Card die Fallunterscheidung.
    "partner_number": None,
    "partner_name": None,
    # Über welche eigene Rufnummer (MSN) der Anruf lief und an welcher
    # Nebenstelle / welchem Endgerät er hing
    "line_number": None,
    "extension": None,
    "connection_id": None,
    "answered": False,
    "duration_seconds": None,
    "outcome": OUTCOME_ONGOING,
    # Wie oft dieselbe Gegenstelle unmittelbar hintereinander angerufen hat.
    # 1 = ein einzelner Anruf; größere Werte entstehen durch Zusammenfassen.
    "repeat_count": 1,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Richtet die Sensor-Entity für diesen Config-Entry ein."""
    runtime_data = hass.data[DOMAIN][entry.entry_id]
    history_length: int = entry.data.get(
        CONF_HISTORY_LENGTH, DEFAULT_HISTORY_LENGTH
    )

    store: Store = Store(
        hass, STORAGE_VERSION, STORAGE_KEY_TEMPLATE.format(entry_id=entry.entry_id)
    )

    async_add_entities(
        [
            FranzBoxCallHistorySensor(
                runtime_data.call_monitor,
                runtime_data.phonebook,
                store,
                entry,
                history_length,
            )
        ]
    )


class FranzBoxCallHistorySensor(SensorEntity):
    """Zeigt den aktuellen Anruf-Status; die Historie liegt im Attribut 'history'."""

    _attr_has_entity_name = True
    _attr_name = "Anrufstatus"
    # Rückfall, falls _current_state() je einen Zustand außerhalb von STATE_ICONS
    # liefert - die icon-Property unten hat Vorrang.
    _attr_icon = "mdi:phone-log"

    # Mit ENUM übersetzt HA die Zustände selbst: Kachel, Geräteseite, Verlauf und
    # Logbuch zeigen "Frei"/"Klingelt"/... statt der Rohwerte. Die Beschriftungen
    # stehen unter entity.sensor.call_status.state in translations/*.json.
    # ACHTUNG: ENUM verträgt sich nicht mit state_class oder einer Einheit, und
    # HA prüft den Zustand gegen _attr_options - ein neuer Zustand muss also
    # immer auch dort und in allen Übersetzungsdateien eingetragen werden.
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [STATE_IDLE, STATE_RINGING, STATE_DIALING, STATE_TALKING]
    _attr_translation_key = "call_status"

    # Kein _attr_entity_picture: setzt eine Entity ein Bild, ignoriert HA das
    # Symbol. Das zustandsabhängige Telefonsymbol sagt auf der Kachel mehr aus
    # als das unveränderliche Logo - im Kopf unserer Card bleibt das Logo ohnehin.

    @property
    def icon(self) -> str:
        """Symbol passend zum aktuellen Zustand.

        Als Property und nicht als _attr_icon: HA liest sie bei jedem
        async_write_ha_state() neu, das Symbol folgt dem Zustand also von selbst.
        """
        return STATE_ICONS.get(self._attr_native_value, self._attr_icon)

    def __init__(
        self,
        client: FranzBoxCallMonitorClient,
        phonebook: FranzBoxPhonebookCache,
        store: Store,
        entry: ConfigEntry,
        history_length: int,
    ) -> None:
        self._client = client
        self._phonebook = phonebook
        self._store = store
        self._history_length = history_length
        # Für das Dispatcher-Signal: es ist pro Config-Entry eigen, damit bei
        # mehreren Fritzboxen nicht die falsche Entity auffrischt.
        self._entry_id = entry.entry_id

        self._attr_unique_id = f"{entry.entry_id}_call_status"
        self._attr_native_value: str = STATE_IDLE
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="FRANZ!Box Monitor",
            manufacturer="PolliProgs",
            configuration_url=f"http://{entry.data[CONF_HOST]}",
        )
        self._history: list[dict[str, Any]] = []

        # Laufende Verbindungen: connection_id -> Zustand. Erst wenn dieses Dict
        # leer ist, gilt die Leitung als 'idle' - sonst würde ein DISCONNECT eines
        # von zwei parallelen Anrufen den noch laufenden zweiten verschlucken.
        self._connection_states: dict[str, str] = {}

        self._remove_listener: Any = None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Attribute für den HA-State.

        KOPIEN herausgeben, niemals die internen Objekte! Home Assistant bildet
        beim Schreiben eines States den Diff fürs Frontend, indem es die neuen
        Attribute mit denen des vorherigen States vergleicht. Der alte State
        hält dieselben Objekte, die wir hier zurückgeben - eine spätere
        In-place-Änderung (insert in die Historie, 'outcome' an einem Eintrag)
        würde also auch den alten State mitverändern. Alt und neu wären beim
        Vergleich gleich, 'history' fiele aus dem Diff heraus und käme nie beim
        Frontend an: die Card zeigte neue Anrufe erst nach einem vollständigen
        Reload (Strg+F5), und in den Entwicklerwerkzeugen stand ein
        eingefrorenes 'history: []' neben einem aktuellen 'last_call'.
        """
        history = [dict(entry_dict) for entry_dict in self._history]

        return {
            "history": history,
            "last_call": history[0] if history else None,
            # Der gerade laufende Anruf - damit man bei "Wählt"/"Im Gespräch"
            # sieht, mit wem und über welche Leitung
            "active_call": self._active_call(history),
            "active_calls": len(self._connection_states),
        }

    def _active_call(
        self, history: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """Der jüngste Eintrag aus 'history', dessen Verbindung noch steht."""
        for entry_dict in history:
            if (
                entry_dict.get("outcome") == OUTCOME_ONGOING
                and entry_dict.get("connection_id") in self._connection_states
            ):
                return entry_dict
        return None

    async def async_added_to_hass(self) -> None:
        """Beim Hinzufügen: Historie laden, mit Namen anreichern, dann anmelden."""
        stored_data = await self._store.async_load()
        if stored_data is not None:
            self._history = [
                self._normalize_entry(entry_dict)
                for entry_dict in stored_data.get("history", [])
            ]

        # Rückwirkende Anreicherung bereits gespeicherter Einträge,
        # z.B. aus der Zeit vor Anbindung des Telefonbuchs
        changed = self._enrich_history_with_names()
        # Wurde HA mitten im Gespräch beendet, steht dessen Eintrag noch auf
        # 'laufend'. Das DISCONNECT dazu kommt nie mehr - der Eintrag muss also
        # jetzt geschlossen werden, sonst greift ihn _find_open_entry() beim
        # nächsten Anruf mit derselben (recycelten) Connection-ID ab.
        changed = self._close_orphaned_entries() or changed
        if changed:
            self._async_save_history()

        self._remove_listener = self._client.add_listener(self._handle_call_event)

        # Nach jedem Neuaufbau der Verbindung dasselbe Aufräumen: was während
        # der Unterbrechung passiert ist, erfahren wir nie.
        self.async_on_remove(
            self._client.add_connection_listener(self._handle_reconnected)
        )

        # Auf ein neu geladenes Telefonbuch reagieren (Knopfdruck oder der
        # 12-Stunden-Turnus), um die Namen in der gespeicherten Historie
        # nachzuziehen. Über den Dispatcher, damit button.py diese Entity nicht
        # kennen muss; async_on_remove meldet das Abo beim Entladen wieder ab.
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_PHONEBOOK_UPDATED.format(entry_id=self._entry_id),
                self._handle_phonebook_updated,
            )
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_listener is not None:
            self._remove_listener()

    @callback
    def _handle_reconnected(self) -> None:
        """Die Verbindung zum Call-Monitor wurde (neu) aufgebaut.

        Der Call-Monitor sendet nichts nach: ein DISCONNECT, das während der
        Unterbrechung gefallen ist, ist für uns verloren. Ohne Aufräumen bliebe
        die Verbindung in _connection_states stehen, der Sensor stünde für
        immer auf 'talking' und würde nie wieder 'idle' - und der offene
        Historieneintrag würde beim nächsten Anruf mit derselben, von der
        Fritzbox wiederverwendeten Connection-ID (meist '0') als Ziel des
        DISCONNECT missverstanden.
        """
        if not self._connection_states and not any(
            entry_dict.get("outcome") == OUTCOME_ONGOING
            for entry_dict in self._history
        ):
            return

        _LOGGER.debug(
            "Verbindung neu aufgebaut - %d laufende Verbindung(en) verworfen",
            len(self._connection_states),
        )
        self._connection_states.clear()
        self._close_orphaned_entries()
        self._attr_native_value = self._current_state()

        self._async_save_history()
        self.async_write_ha_state()

    def _close_orphaned_entries(self) -> bool:
        """Schließt Historieneinträge, deren DISCONNECT nie ankommen wird.

        Betrifft zwei Fälle: einen HA-Neustart mitten im Gespräch und einen
        Reconnect des Call-Monitors. In beiden ist die Dauer unbekannt - der
        Eintrag bekommt deshalb 0 Sekunden und das Ergebnis, das sich aus dem
        bereits Bekannten ergibt. Ein 'laufend' stehenzulassen wäre die
        schlechtere Wahl: _find_open_entry() würde ihn später fälschlich
        treffen.

        Gibt True zurück, wenn etwas geändert wurde (dann speichern).
        """
        changed = False
        for entry_dict in self._history:
            if entry_dict.get("outcome") != OUTCOME_ONGOING:
                continue

            if entry_dict.get("duration_seconds") is None:
                entry_dict["duration_seconds"] = 0
            entry_dict["outcome"] = self._final_outcome(entry_dict)
            changed = True

        return changed

    @staticmethod
    def _final_outcome(entry_dict: dict[str, Any]) -> str:
        """Ergebnis eines beendeten Anrufs aus dem, was am Eintrag steht.

        Ohne die 'blockiert'-Heuristik: die braucht den Zeitpunkt des
        DISCONNECT, und genau der fehlt hier (siehe _handle_call_disconnect).
        """
        if entry_dict.get("answered"):
            return OUTCOME_ANSWERED
        if entry_dict.get("direction") == DIRECTION_IN:
            return OUTCOME_MISSED
        return OUTCOME_UNREACHED

    @callback
    def _handle_phonebook_updated(self) -> None:
        """Das Telefonbuch wurde neu geladen: Namen in der Historie auffrischen."""
        if not self._enrich_history_with_names():
            return

        self._async_save_history()
        self.async_write_ha_state()

    # classmethod statt staticmethod, damit _final_outcome() mitbenutzt werden
    # kann - die Regel "wie endete dieser Anruf" soll nur an einer Stelle stehen.
    @classmethod
    def _normalize_entry(cls, entry_dict: dict[str, Any]) -> dict[str, Any]:
        """Ergänzt fehlende Schlüssel an einem gespeicherten Historieneintrag.

        Einträge aus älteren Versionen kennen z.B. 'partner_number' oder
        'outcome' noch nicht. Statt die STORAGE_VERSION zu erhöhen füllen wir
        rein additive Felder beim Laden auf - damit bleibt das Attribut-Schema
        für Lovelace-Templates stabil (siehe Regel: Schlüssel immer setzen,
        notfalls auf None).

        Wo es geht, werden die neuen Felder aus den alten abgeleitet, statt sie
        auf dem Default stehen zu lassen: ein abgeschlossener Altanruf darf
        nicht als 'laufend' gelten, sonst greift ihn _find_open_entry später ab.
        """
        normalized = {**HISTORY_KEYS, **entry_dict}
        incoming = normalized["direction"] == DIRECTION_IN

        if normalized["partner_number"] is None:
            normalized["partner_number"] = (
                normalized["caller_number"] if incoming else normalized["called_number"]
            )
            normalized["partner_name"] = (
                normalized["caller_name"] if incoming else normalized["called_name"]
            )

        if normalized["line_number"] is None:
            normalized["line_number"] = (
                normalized["called_number"] if incoming else normalized["caller_number"]
            )

        # Ein Altanruf mit Dauer ist beendet - Ergebnis nachträglich bestimmen
        if (
            normalized["outcome"] == OUTCOME_ONGOING
            and normalized["duration_seconds"] is not None
        ):
            normalized["outcome"] = cls._final_outcome(normalized)

        return normalized

    def _async_save_history(self) -> None:
        """Schreibt die Historie in den Store (feuert und wartet nicht)."""
        self.hass.async_create_task(self._store.async_save({"history": self._history}))

    def _enrich_history_with_names(self) -> bool:
        """Ergänzt caller_name/called_name für alle Einträge.

        Läuft beim Laden der Historie und nach jedem Neuladen des Telefonbuchs.
        Zwei Regeln, die auseinandergehalten werden müssen:

        1. Ein Treffer im Telefonbuch gewinnt IMMER, auch wenn schon ein Name
           gespeichert war - sonst bliebe ein in der Fritzbox umbenannter Kontakt
           in der Historie für immer unter dem alten Namen stehen.
        2. Ein Fehlschlag überschreibt NICHTS. Wäre das anders, würde ein einziger
           Lauf bei nicht erreichbarer Fritzbox (phonebook.load() lässt den
           Bestand dann leer bzw. unverändert) sämtliche Namen aus der Historie
           löschen.

        WICHTIG: Die Schlüssel werden immer gesetzt (auch auf None), nie nur
        bedingt - sonst fehlt der Schlüssel bei erfolgloser Telefonbuch-Suche
        komplett und Lovelace-Templates schlagen mit UndefinedError fehl.
        """
        changed = False
        for entry_dict in self._history:
            for number_key, name_key in (
                ("caller_number", "caller_name"),
                ("called_number", "called_name"),
                ("partner_number", "partner_name"),
            ):
                resolved = self._phonebook.lookup(entry_dict.get(number_key))

                if resolved is None:
                    # Kein Treffer: vorhandenen Namen behalten, aber sicherstellen,
                    # dass der Schlüssel überhaupt existiert.
                    if name_key not in entry_dict:
                        entry_dict[name_key] = None
                        changed = True
                    continue

                if entry_dict.get(name_key) != resolved:
                    entry_dict[name_key] = resolved
                    changed = True
        return changed

    def _find_open_entry(self, connection_id: str) -> dict[str, Any] | None:
        """Sucht den jüngsten noch offenen Historieneintrag zu einer Verbindung.

        'Offen' heißt: Ergebnis steht noch aus. Die Fritzbox recycelt
        Connection-IDs, deshalb darf ein bereits abgeschlossener Eintrag nicht
        noch einmal getroffen werden.
        """
        for entry_dict in self._history:
            if (
                entry_dict.get("connection_id") == connection_id
                and entry_dict.get("outcome") == OUTCOME_ONGOING
            ):
                return entry_dict
        return None

    def _handle_call_event(self, event: CallEvent) -> None:
        if event.event_type in ("RING", "CALL"):
            self._handle_call_start(event)
        elif event.event_type == "CONNECT":
            self._handle_call_connect(event)
        elif event.event_type == "DISCONNECT":
            self._handle_call_disconnect(event)

        self._attr_native_value = self._current_state()

        # Erfolgspfad protokollieren, sonst ist bei einer Fehlersuche nicht zu
        # sehen, ob ein Ereignis überhaupt in der Historie gelandet ist.
        _LOGGER.debug(
            "%s (Verbindung %s) verarbeitet -> Zustand %s, %d Historieneinträge, "
            "%d laufende Verbindungen",
            event.event_type,
            event.connection_id,
            self._attr_native_value,
            len(self._history),
            len(self._connection_states),
        )

        self.async_write_ha_state()

    def _handle_call_start(self, event: CallEvent) -> None:
        """RING/CALL: neuen Historieneintrag anlegen.

        Hier wird bewusst NICHT mit einem vorherigen Anruf zusammengefasst: zum
        Zeitpunkt des RING steht das Ergebnis noch nicht fest, ein
        durchgestellter Anruf sieht exakt aus wie ein weiterer abgewiesener. Das
        Zusammenfassen passiert deshalb erst beim DISCONNECT (_collapse_repeat).
        """
        self._connection_states[event.connection_id] = EVENT_TO_STATE[event.event_type]

        incoming = event.event_type == "RING"
        # Gegenstelle und eigene Leitung hängen an der Richtung: eingehend ist
        # der Anrufer die Gegenstelle und die angerufene MSN die eigene Leitung,
        # ausgehend genau andersherum.
        partner_number = event.caller_number if incoming else event.called_number
        line_number = event.called_number if incoming else event.caller_number

        entry_dict = self._normalize_entry(
            {
                "timestamp": event.timestamp.isoformat(),
                "direction": DIRECTION_IN if incoming else DIRECTION_OUT,
                "caller_number": event.caller_number,
                "caller_name": self._phonebook.lookup(event.caller_number),
                "called_number": event.called_number,
                "called_name": self._phonebook.lookup(event.called_number),
                "partner_number": partner_number,
                "partner_name": self._phonebook.lookup(partner_number),
                "line_number": line_number,
                "extension": event.extension,
                "connection_id": event.connection_id,
                "outcome": OUTCOME_ONGOING,
            }
        )
        self._history.insert(0, entry_dict)
        self._history = self._history[: self._history_length]
        self._async_save_history()

    def _collapse_repeat(self, entry_dict: dict[str, Any]) -> None:
        """Fasst einen gerade abgeschlossenen Anruf mit dem davor zusammen, wenn
        es derselbe Anrufversuch derselben Gegenstelle mit demselben Ergebnis war.

        Auslöser war eine gesperrte Nummer: die Rufnummernsperre greift in der
        Fritzbox erst NACH der Meldung auf Port 1012, die Gegenstelle baut nach
        jeder Abweisung sofort neu auf. Gemessen waren das fünf vollständige
        RING/DISCONNECT-Zyklen in drei Sekunden - und damit fünf Einträge für
        einen einzigen Anrufversuch.

        WARUM ERST HIER und nicht schon beim RING: vorher steht das Ergebnis
        nicht fest. Nimmt man die Sperre heraus und ruft erneut an, sieht der
        durchgestellte Anruf beim RING exakt aus wie ein weiterer abgewiesener
        und würde der gesperrten Serie zugeschlagen. Erst der Vergleich der
        fertigen 'outcome'-Werte trennt beides sauber - 'blockiert' wird nur mit
        'blockiert' zusammengefasst, 'verpasst' nur mit 'verpasst'.

        Zusammengefasst wird nur mit dem unmittelbar davorliegenden Eintrag: kam
        zwischendurch ein anderer Anruf, ist es keine Wiederholungsserie mehr.
        """
        # Nur der eben geschlossene Anruf darf einklappen; ein CONNECT/DISCONNECT
        # zu einem älteren, noch offenen Eintrag lässt die Reihenfolge sonst
        # durcheinandergeraten.
        if len(self._history) < 2 or self._history[0] is not entry_dict:
            return

        previous = self._history[1]
        outcome = entry_dict.get("outcome")

        # Angenommene Gespräche bleiben immer eigenständig - eines darf nie
        # hinter einem Zähler verschwinden.
        if outcome not in MERGEABLE_OUTCOMES:
            return
        if previous.get("outcome") != outcome:
            return
        if not entry_dict.get("partner_number"):
            return
        if previous.get("partner_number") != entry_dict.get("partner_number"):
            return
        if previous.get("direction") != entry_dict.get("direction"):
            return

        try:
            started = datetime.fromisoformat(entry_dict["timestamp"])
            previous_started = datetime.fromisoformat(previous["timestamp"])
        except (TypeError, ValueError):
            return

        # Negatives Delta hieße, die Zeitstempel passen nicht zusammen (z.B.
        # Zeitumstellung an der Box). Dann lieber zwei Einträge als eine falsche
        # Zusammenfassung.
        delta = (started - previous_started).total_seconds()
        if not 0 <= delta <= REPEAT_WINDOW_SECONDS:
            return

        # Der ältere Eintrag überlebt und übernimmt die Daten des jüngeren -
        # so bleibt der Zähler auch über mehr als zwei Versuche erhalten.
        previous["repeat_count"] = int(previous.get("repeat_count") or 1) + int(
            entry_dict.get("repeat_count") or 1
        )
        previous["timestamp"] = entry_dict["timestamp"]
        previous["connection_id"] = entry_dict["connection_id"]
        previous["duration_seconds"] = entry_dict["duration_seconds"]
        if entry_dict.get("extension") is not None:
            previous["extension"] = entry_dict["extension"]

        del self._history[0]

        _LOGGER.debug(
            "Wiederholter Anruf von %s zusammengefasst (%s, jetzt %d Versuche)",
            previous.get("partner_number"),
            outcome,
            previous["repeat_count"],
        )

    def _handle_call_connect(self, event: CallEvent) -> None:
        """CONNECT: Anruf wurde angenommen - Flag am bestehenden Eintrag setzen."""
        self._connection_states[event.connection_id] = STATE_TALKING

        entry_dict = self._find_open_entry(event.connection_id)
        if entry_dict is None:
            # Kann passieren, wenn HA mitten im Gespräch neu gestartet wurde
            _LOGGER.debug(
                "CONNECT ohne passenden Historieneintrag (Verbindung %s)",
                event.connection_id,
            )
            return

        entry_dict["answered"] = True
        # Bei eingehenden Anrufen steht erst im CONNECT, an welchem Endgerät
        # abgehoben wurde - vorher kennen wir die Nebenstelle nicht.
        if event.extension is not None:
            entry_dict["extension"] = event.extension
        self._async_save_history()

    def _handle_call_disconnect(self, event: CallEvent) -> None:
        """DISCONNECT: Verbindung abmelden, Dauer und Ergebnis nachtragen."""
        self._connection_states.pop(event.connection_id, None)

        entry_dict = self._find_open_entry(event.connection_id)
        if entry_dict is None:
            _LOGGER.debug(
                "DISCONNECT ohne passenden Historieneintrag (Verbindung %s)",
                event.connection_id,
            )
            return

        # Fehlt die Dauer in der Zeile, tragen wir 0 ein: der Eintrag muss
        # geschlossen werden, sonst trifft ihn eine recycelte Connection-ID erneut.
        entry_dict["duration_seconds"] = (
            event.duration_seconds if event.duration_seconds is not None else 0
        )

        if entry_dict["answered"]:
            entry_dict["outcome"] = OUTCOME_ANSWERED
        elif entry_dict["direction"] == DIRECTION_IN:
            entry_dict["outcome"] = (
                OUTCOME_BLOCKED
                if self._was_blocked(entry_dict, event)
                else OUTCOME_MISSED
            )
        else:
            entry_dict["outcome"] = OUTCOME_UNREACHED

        # Erst jetzt, mit fertigem Ergebnis, kann entschieden werden, ob das
        # nur ein weiterer Versuch desselben Anrufs war.
        self._collapse_repeat(entry_dict)

        self._async_save_history()

    @staticmethod
    def _was_blocked(entry_dict: dict[str, Any], event: CallEvent) -> bool:
        """Heuristik: wurde dieser eingehende Anruf von der Fritzbox abgewiesen?

        Der Call-Monitor kennt kein Feld dafür. Die Rufnummernsperre greift in
        der Box erst nach der Meldung auf Port 1012, wir sehen also ein ganz
        normales RING. Unterscheidbar ist es nur am Zeitverhalten: die Box
        beendet einen gesperrten Ruf selbst und sofort. In den gemessenen
        Zeilen tragen RING und DISCONNECT denselben Boxzeitstempel:

            12:21:56;RING;0;<Nummer>;<eigene MSN>;SIP1;
            12:21:56;DISCONNECT;0;0;

        Ein wirklich verpasster Anruf klingelt dagegen 20-30 s, bevor die
        Gegenseite auflegt. Die zwei Sekunden Toleranz fangen den Fall ab, dass
        die beiden Zeilen über eine Sekundengrenze fallen.

        Restrisiko: ein Anrufer, der binnen zwei Sekunden selbst wieder auflegt,
        wird ebenfalls als 'blockiert' geführt. Eine harte Auskunft gäbe nur die
        Anrufliste der Box über TR-064 (X_AVM-DE_OnTel) - das wäre Polling neben
        dem Push-Client und ist bewusst nicht eingebaut.

        Ausgehende Anrufe bleiben absichtlich außen vor: dort ist sofortiges
        Auflegen der Normalfall (Vertipper), nicht eine Sperre.
        """
        try:
            started = datetime.fromisoformat(entry_dict["timestamp"])
        except (TypeError, ValueError):
            return False

        return 0 <= (event.timestamp - started).total_seconds() <= BLOCKED_MAX_SECONDS

    def _current_state(self) -> str:
        """Leitet den Sensorzustand aus allen laufenden Verbindungen ab."""
        active = set(self._connection_states.values())
        for state in STATE_PRIORITY:
            if state in active:
                return state
        return STATE_IDLE
