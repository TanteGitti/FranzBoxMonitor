"""FranzBoxMonitor Integration – Setup und Lifecycle-Verwaltung."""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval

from .call_monitor import CallEvent, FranzBoxCallMonitorClient
from .const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_USERNAME,
    DOMAIN,
    EVENT_CALL,
    FRONTEND_DIR,
    FRONTEND_ICON,
    FRONTEND_ICON_URL,
    FRONTEND_SCRIPT,
    FRONTEND_URL,
    PLATFORMS,
    SIGNAL_PHONEBOOK_UPDATED,
)
from .phonebook import FranzBoxPhonebookCache

_LOGGER = logging.getLogger(__name__)

# Wie oft das Telefonbuch neu geladen wird. Ohne das würden neue Kontakte erst
# nach einem Reload der Integration aufgelöst.
PHONEBOOK_REFRESH_INTERVAL = timedelta(hours=12)


@dataclass
class FranzBoxMonitorRuntimeData:
    """Bündelt die Laufzeit-Objekte dieser Integration für einen Config-Entry."""

    call_monitor: FranzBoxCallMonitorClient
    phonebook: FranzBoxPhonebookCache


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Wird beim Laden eines Config-Entry aufgerufen (Start/Reload/Erstanlage)."""

    phonebook = FranzBoxPhonebookCache(
        host=entry.data[CONF_HOST],
        username=entry.data.get(CONF_USERNAME, ""),
        password=entry.data.get(CONF_PASSWORD, ""),
    )
    # fritzconnection ist synchron/blockierend -> zwingend im Executor-Thread,
    # sonst blockiert das kurzzeitig den kompletten HA-Event-Loop.
    # Hier bewusst OHNE async_reload_phonebook: die Entities existieren noch
    # nicht, das Signal ginge ins Leere - der Sensor reichert seine Historie
    # ohnehin in async_added_to_hass() an.
    await hass.async_add_executor_job(phonebook.load)

    call_monitor = FranzBoxCallMonitorClient(hass, entry)
    await call_monitor.async_start()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = FranzBoxMonitorRuntimeData(
        call_monitor=call_monitor, phonebook=phonebook
    )

    await _async_register_frontend(hass)

    # Bus-Events für Automationen. Bewusst hier und nicht in der Sensor-Entity:
    # das Event beschreibt die Integration, nicht eine einzelne Entity - und es
    # muss auch feuern, wenn sich der Sensorzustand gar nicht ändert
    # (z.B. zweiter RING während schon eine Verbindung klingelt).
    entry.async_on_unload(
        call_monitor.add_listener(
            lambda event: _fire_call_event(hass, entry, phonebook, event)
        )
    )

    async def _async_refresh_phonebook(_now) -> None:
        await async_reload_phonebook(hass, entry, phonebook)

    entry.async_on_unload(
        async_track_time_interval(
            hass, _async_refresh_phonebook, PHONEBOOK_REFRESH_INTERVAL
        )
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

      # Reagiert auf Änderungen aus dem OptionsFlow: lädt die Integration mit
    # den neuen Werten aus entry.data komplett neu (unload + setup)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def async_reload_phonebook(
    hass: HomeAssistant, entry: ConfigEntry, phonebook: FranzBoxPhonebookCache
) -> bool:
    """Lädt das Telefonbuch neu und meldet das den Entities dieses Config-Entry.

    Gemeinsamer Weg für den 12-Stunden-Turnus und den Knopf. fritzconnection ist
    synchron, der Executor-Umweg ist also auch beim Knopfdruck zwingend.

    Das Signal geht über den Dispatcher statt über einen direkten Aufruf: so
    muss button.py die Sensor-Entity nicht kennen, und weitere Entities können
    sich später ohne Änderung hier anhängen.
    """
    loaded = await hass.async_add_executor_job(phonebook.load)
    async_dispatcher_send(
        hass, SIGNAL_PHONEBOOK_UPDATED.format(entry_id=entry.entry_id)
    )
    return loaded


class FranzBoxMonitorAssetView(HomeAssistantView):
    """Liefert eine mitgelieferte Frontend-Datei mit festem MIME-Typ aus.

    Warum kein statischer Pfad (hass.http.async_register_static_paths)?
    Der reicht Einzeldateien an aiohttps web.FileResponse weiter, das den
    Content-Type per mimetypes errät - abhängig von der mimetypes-Datenbank im
    Container. Hier wird er stattdessen fest gesetzt, ebenso Cache-Control.
    Das ist eine Absicherung, kein belegter Bugfix: dass die statische Route
    einen falschen Typ geliefert hätte, wurde nie nachgewiesen.

    ACHTUNG beim Nachmessen: 'curl -I' schickt HEAD, und dieser View kennt nur
    GET -> HA antwortet 405 mit einer text/plain-Fehlerseite. Das sieht wie ein
    falscher MIME-Typ aus, ist aber keiner. Richtig messen mit
    'curl -s -o /dev/null -D - <url>' (GET, Header ausgeben).
    """

    # Das Frontend lädt Card und Bild ohne Access-Token (Script-Tag bzw.
    # <img src>) - mit requires_auth käme hier ein 401.
    requires_auth = False

    def __init__(
        self, url: str, name: str, content: bytes, content_type: str
    ) -> None:
        # url/name als Instanzattribute: HomeAssistantView liest sie beim
        # Registrieren von der Instanz, so genügt eine Klasse für alle Dateien.
        self.url = url
        self.name = name
        self._content = content
        self._content_type = content_type

    async def get(self, request: web.Request) -> web.Response:
        # Bewusst 'body' (Bytes) plus Content-Type als Header, nicht 'text' mit
        # content_type/charset: so ist aiohttps eigene Content-Type-Logik gar
        # nicht beteiligt und der Header steht garantiert so, wie er hier steht.
        return web.Response(
            body=self._content,
            headers={
                "Content-Type": self._content_type,
                "Cache-Control": "no-cache",
            },
        )


async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Liefert Lovelace-Card und Logo aus und hängt die Card ins Frontend ein.

    Läuft pro Config-Entry, muss also idempotent sein: ein zweiter
    register_view für dieselbe URL wirft in HA einen RuntimeError, und
    add_extra_js_url würde das Modul doppelt einhängen.
    """
    # Import erst hier: 'frontend' ist eine Abhängigkeit, die zum Modul-Import-
    # Zeitpunkt von __init__.py noch nicht zwingend geladen ist.
    from homeassistant.components.frontend import add_extra_js_url

    registered: set[str] = hass.data.setdefault(f"{DOMAIN}_frontend", set())
    if FRONTEND_URL in registered:
        return
    registered.add(FRONTEND_URL)

    frontend_dir = Path(__file__).parent / FRONTEND_DIR
    # Einmal einlesen und im Speicher halten (zusammen ~100 kB): Dateizugriff
    # pro Request wäre blockierend, und ein Deploy erfordert ohnehin einen
    # HA-Neustart.
    script, icon = await hass.async_add_executor_job(
        _read_frontend_files, frontend_dir
    )

    hass.http.register_view(
        FranzBoxMonitorAssetView(
            FRONTEND_URL, f"{DOMAIN}:card", script, "text/javascript; charset=utf-8"
        )
    )
    hass.http.register_view(
        FranzBoxMonitorAssetView(
            FRONTEND_ICON_URL, f"{DOMAIN}:icon", icon, "image/png"
        )
    )

    # Cache-Buster aus dem Dateiinhalt: nach einem Deploy holt der Browser die
    # Card automatisch neu, ohne dass man ihn hart neu laden muss.
    revision = hashlib.sha256(script).hexdigest()[:8]
    add_extra_js_url(hass, f"{FRONTEND_URL}?v={revision}")


def _read_frontend_files(frontend_dir: Path) -> tuple[bytes, bytes]:
    """Liest Card und Logo (synchron, gehört in den Executor)."""
    return (
        (frontend_dir / FRONTEND_SCRIPT).read_bytes(),
        (frontend_dir / FRONTEND_ICON).read_bytes(),
    )


def _fire_call_event(
    hass: HomeAssistant,
    entry: ConfigEntry,
    phonebook: FranzBoxPhonebookCache,
    event: CallEvent,
) -> None:
    """Feuert ein Bus-Event mit denselben Feldern wie ein Historieneintrag."""
    direction = _direction_for(event.event_type)
    # Gegenstelle wie im Historieneintrag: eingehend der Anrufer, ausgehend das
    # Ziel. Bei CONNECT/DISCONNECT ist die Richtung unbekannt -> None.
    if direction == "eingehend":
        partner_number = event.caller_number
    elif direction == "ausgehend":
        partner_number = event.called_number
    else:
        partner_number = None

    hass.bus.async_fire(
        EVENT_CALL,
        {
            "entry_id": entry.entry_id,
            "event_type": event.event_type,
            "timestamp": event.timestamp.isoformat(),
            "connection_id": event.connection_id,
            "direction": direction,
            "caller_number": event.caller_number,
            "caller_name": phonebook.lookup(event.caller_number),
            "called_number": event.called_number,
            "called_name": phonebook.lookup(event.called_number),
            "partner_number": partner_number,
            "partner_name": phonebook.lookup(partner_number),
            "extension": event.extension,
            "duration_seconds": event.duration_seconds,
        },
    )


def _direction_for(event_type: str) -> str | None:
    """Richtung des Anrufs, soweit sie sich aus dem Ereignistyp ergibt."""
    if event_type == "RING":
        return "eingehend"
    if event_type == "CALL":
        return "ausgehend"
    # CONNECT/DISCONNECT tragen die Richtung nicht, sie gehört zum Anrufbeginn
    return None


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Wird vom OptionsFlow ausgelöst, wenn entry.data geändert wurde."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Wird beim Entladen/Entfernen des Config-Entry aufgerufen."""

    unload_ok = await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS
    )

    if unload_ok:
        runtime_data: FranzBoxMonitorRuntimeData = hass.data[DOMAIN].pop(
            entry.entry_id
        )
        await runtime_data.call_monitor.async_stop()

    return unload_ok
