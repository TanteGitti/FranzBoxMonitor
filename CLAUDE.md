# CLAUDE.md

Hinweise für Claude Code zur Arbeit an diesem Repository.

## Was das Projekt ist

`franzbox_monitor` ist eine **Home-Assistant-Custom-Integration** (installierbar über HACS),
die den **Call-Monitor der FRITZ!Box** auswertet: eingehende/abgehende Anrufe werden in
Echtzeit erkannt, über das FRITZ!Box-Telefonbuch mit Namen angereichert und als
Anrufhistorie persistiert.

Die Integration entstand als Ersatz für die offizielle `fritzbox_callmonitor`-Integration,
die für den gewünschten Zweck (dauerhafte, benannte Historie + eigene Lovelace-Darstellung)
als unzureichend empfunden wurde.

**Sprache:** Code-Kommentare, Docstrings und UI-Texte sind auf **Deutsch**. Das bitte
beibehalten — auch in neuem Code. Kommentare erklären das *Warum* (HA-Eigenheiten,
Fallstricke), nicht das Offensichtliche.

## Voraussetzung an der FRITZ!Box

Der Call-Monitor auf TCP-Port **1012** ist standardmäßig **deaktiviert**. Er wird
einmalig per Telefoncode aktiviert:

```
#96*5*   Call-Monitor einschalten
#96*4*   Call-Monitor ausschalten
```

Ohne diesen Schritt bleibt die Integration dauerhaft im Reconnect-Loop. Das ist die
häufigste Fehlerursache und gehört in jede Nutzer-Dokumentation.

## Verzeichnisstruktur

```
hacs.json                             HACS-Metadaten (Mindest-HA-Version!)
README.md                             Nutzer-Dokumentation, wird von HACS gerendert
LICENSE                               MIT
static/franz.png                      Logo-Original 1254x1254, nur Quelle - wird nicht deployt
brands/                               PR-fertige Bilder für home-assistant/brands
deploy/readme.txt                     Deploy-Ablauf, bewusst ohne Zugangsdaten
.github/workflows/validate.yml        HACS-Action + hassfest
custom_components/franzbox_monitor/   <- das ist das HACS-Artefakt
  __init__.py       Setup/Unload-Lifecycle, RuntimeData, Bus-Events, Frontend-Registrierung
  const.py          DOMAIN, Config-Keys, Defaults, Event- und Frontend-Konstanten
  config_flow.py    Einrichtungsdialog + OptionsFlow inkl. Validierung
  call_monitor.py   asyncio-Client für TCP 1012, Zeilen-Parser, Listener-Registry
  phonebook.py      TR-064-Telefonbuch (fritzconnection), Nummer -> Name, Credential-Test
  sensor.py         Sensor-Entity: aktueller Status + Historie als Attribut
  button.py         Button-Entity: Telefonbuch auf Knopfdruck neu laden
  strings.json      Quelltexte für Config-/Options-Flow (englisch)
  translations/     de.json, en.json - en.json ist die Kopie von strings.json
  frontend/         Card (JS, kein Build-Step) + franz.png (Logo, 256x256)
tests/
  fritz-connector.py  Standalone-Skript: rohe Call-Monitor-Zeilen mitlesen (Debug-Hilfe)
```

Wer `strings.json` ändert, muss `translations/en.json` mitziehen (identischer Inhalt) und
`translations/de.json` nachpflegen — HA lädt bei Custom-Integrationen nur `translations/`.

## Architektur / Datenfluss

```
FRITZ!Box :1012 ──► FranzBoxCallMonitorClient ──► Listener ──► FranzBoxCallHistorySensor
   (Rohzeilen)        (parse + Reconnect)                        │
                                                                 ├─► HA-State + Attribut "history"
FRITZ!Box TR-064 ──► FranzBoxPhonebookCache ────► lookup() ──────┤
   (Telefonbuch)      (dict: Nummer -> Name)         │           └─► Store (Persistenz)
                                                     └─► Bus-Event franzbox_monitor_call
```

Zentrale Punkte:

- **`FranzBoxMonitorRuntimeData`** (dataclass in `__init__.py`) bündelt `call_monitor` und
  `phonebook` und liegt unter `hass.data[DOMAIN][entry.entry_id]`. Plattformen holen sich
  ihre Abhängigkeiten von dort — keine globalen Singletons.
- **Der Call-Monitor-Client ist Push, nicht Poll** (`iot_class: local_push`). Es gibt
  bewusst *keinen* DataUpdateCoordinator; ein Hintergrund-Task hält die TCP-Verbindung und
  ruft registrierte Listener auf. `add_listener()` gibt eine Abmeldefunktion zurück.
- **Reconnect** mit gestaffelten Wartezeiten (`RECONNECT_DELAYS = [5, 10, 30, 60]`), der
  letzte Wert wiederholt sich. Nach erfolgreicher Verbindung wird der Index zurückgesetzt.
- **Bus-Events werden in `__init__.py` gefeuert, nicht in der Entity.** Ein zweiter `RING`
  bei unverändertem Sensorzustand würde als State-Trigger verloren gehen; das Event
  beschreibt außerdem die Integration, nicht eine einzelne Entity.
- **Die Lovelace-Card liefert die Integration selbst aus** (`_async_register_frontend`):
  ein eigener `HomeAssistantView` (`FranzBoxMonitorAssetView`) plus `add_extra_js_url`. Die
  Funktion läuft pro Config-Entry und **muss idempotent bleiben** — ein zweiter
  `register_view` für dieselbe URL wirft in HA einen `RuntimeError`. Deshalb der Merker in
  `hass.data["franzbox_monitor_frontend"]`.
- **Der eigene View setzt Content-Type und Cache-Control fest** statt sie von aiohttps
  `mimetypes`-Erkennung ableiten zu lassen. Das ist eine Absicherung, **kein belegter
  Bugfix** — dass `async_register_static_paths` einen falschen MIME-Typ geliefert hätte, war
  eine Fehldiagnose (siehe nächster Punkt).
- **Header nie mit `curl -I` messen.** `-I` schickt HEAD, der View kennt nur GET, HA
  antwortet `405` mit einer **`text/plain`**-Fehlerseite. Das sieht genau wie ein falscher
  MIME-Typ der Card aus und hat hier schon einmal eine komplette Fehlersuche in die falsche
  Richtung geschickt. Richtig:
  ```bash
  curl -s -o /dev/null -D - http://<host>:8123/franzbox_monitor/franzbox-monitor-card.js
  ```
  Immer die **Statuszeile** mitlesen, nicht nur `grep content-type`.
- **`requires_auth = False` am View ist zwingend**: das Frontend lädt Modul und Logo ohne
  Access-Token (`<script>`-Tag bzw. `<img src>`), mit Auth käme ein 401.
- **Frontend-Dateien gehören nach `custom_components/franzbox_monitor/frontend/`**, nicht in
  ein `static/` im Repo-Wurzelverzeichnis — der Deploy kopiert ausschließlich das Paket, alles
  außerhalb erreicht den HA-Host nie. `franz.png` liegt dort als 256×256-Fassung (83 kB); das
  Original (1254×1254, 1,1 MB) bleibt unter `static/` und ist **nicht** für die Auslieferung
  gedacht.
- **Der Sensor ist ein `SensorDeviceClass.ENUM`.** Nur so übersetzt HA die Zustände selbst —
  Standardkachel, Geräteseite, Verlauf und Logbuch zeigen sonst das rohe `idle`. Die
  Beschriftungen stehen unter `entity.sensor.call_status.state` in `strings.json` und beiden
  `translations/`-Dateien, verbunden über `_attr_translation_key`. Drei Dinge dazu:
  - ENUM verträgt **kein** `state_class` und keine Einheit, und HA prüft den Zustand gegen
    `_attr_options`. Ein neuer Zustand muss also in `STATE_ICONS`, `_attr_options` **und**
    allen Übersetzungsdateien nachgetragen werden.
  - Der **Rohwert bleibt** `idle`/`ringing`/… — nur die Anzeige ändert sich. Bestehende
    Automationen und die Card (`STATE_LABELS`) laufen unverändert weiter.
  - Unter `call_status` steht bewusst **kein `name`-Schlüssel**. Gäbe es ihn, bildete HA den
    Entity-Namen daraus, und eine englische Installation bekäme
    `sensor.franz_box_monitor_call_status` — eine zweite, unerwünschte Entity-ID-Variante
    neben `sensor.franz_box_monitor_anrufstatus`. Der Name kommt allein aus `_attr_name`.
- **Der Sensor hat bewusst kein `entity_picture` mehr.** Setzt eine Entity ein Bild, ignoriert
  HA das Symbol — das Logo hätte also das zustandsabhängige Telefonsymbol verdrängt. Die
  `icon`-Property leitet aus `STATE_ICONS` ab (dieselbe Zuordnung wie in der Card) und wird bei
  jedem `async_write_ha_state()` neu gelesen. Das Logo bleibt im Kopf der Lovelace-Card und
  wird weiterhin über den View ausgeliefert; `FRONTEND_ICON_URL` also nicht entfernen.
  Das **Integrations-Logo** unter *Geräte & Dienste* lässt sich davon unabhängig nicht ändern: das zieht
  HA ausschließlich von `brands.home-assistant.io`. Dafür liegen unter `brands/` fertige
  Bilder für einen PR an `home-assistant/brands` (siehe `brands/README.md`); bis der drin
  ist, bleibt dort „icon not available". Das ist **kein Bug** — nicht versuchen, es lokal zu
  überschreiben.
- **Zwei Ebenen von Konfiguration nicht verwechseln.** Der OptionsFlow der Integration
  (Zahnrad) steuert Host, Zugangsdaten und `history_length` — also wie viele Anrufe der
  Sensor *speichert*. Alles andere (`max_entries`, `visible_entries`, `max_height`, `title`)
  sind Optionen der **Lovelace-Card** und stecken in der Dashboard-Konfiguration. Sie
  werden über `FranzboxMonitorCardEditor` (basiert auf HAs `ha-form`) grafisch bearbeitbar;
  neue Card-Optionen gehören deshalb immer auch in `EDITOR_SCHEMA` und `EDITOR_LABELS`.
- **In der Card nie ungeprüft messen.** Lovelace ruft `setConfig` und setzt `hass`, **bevor**
  das Element im Dokument hängt — `offsetHeight` ist dann `0`. `_applyListHeight()` hat daraus
  einmal `max-height: 0px` gemacht, womit nur noch die Kopfzeile sichtbar war. Aus einer
  Messung von `0` darf deshalb nie eine Begrenzung werden; stattdessen unbegrenzt lassen und
  in `connectedCallback` bzw. per `requestAnimationFrame` (mit Obergrenze!) nachfassen.
  Testbar ohne Browser: DOM-Attrappen in Node setzen, die Datei per `require` laden und die
  Klasse über die `customElements`-Attrappe abgreifen — die Card meldet sich dort selbst an.
- **Die Card registriert sich mehrfach nachfassend** (`registerCard()` plus gestaffelte
  `setTimeout`-Aufrufe am Ende von `franzbox-monitor-card.js`). Grund ist ein beobachtetes
  Timing-Problem von `add_extra_js_url`: das Modul läuft als `<script type="module">` im
  `<head>`, also vor dem Frontend-Bundle, und eine dort vorgenommene Registrierung blieb
  nicht bestehen — `window.customCards` war gesetzt (die Card stand im Kartenwähler), aber
  `customElements.get("franzbox-monitor-card")` lieferte `undefined` und Lovelace meldete
  `Custom element not found`. Ein nachträgliches `import()` derselben Datei registrierte
  dagegen fehlerfrei. **Die Wiederholungen und die Idempotenz-Prüfungen also nicht
  „aufräumen".** Sie machen zusätzlich den Fall harmlos, dass die Datei zusätzlich als
  Lovelace-Ressource eingetragen ist (zweite URL -> zweite Modulinstanz).
- **Die Card-URL trägt einen Cache-Buster** (`?v=<sha256-Präfix des Dateiinhalts>`), damit
  der Browser nach einem Deploy automatisch die neue Version zieht. Der Inhalt wird beim
  Setup einmal in den Speicher gelesen — eine geänderte JS-Datei wird also erst nach
  Reload der Integration bzw. HA-Neustart wirksam.
- **„Custom element doesn't exist" nach einem Update kann auch der Service Worker des
  Browsers sein, nicht der Deploy.** Beobachtet nach 0.7.0: `curl -s -o /dev/null -D -`
  gegen den View lieferte `200` mit der erwarteten `Content-Length`, der HTML-Quelltext
  enthielt den korrekten `import(...)`-Aufruf mit dem neuen Cache-Buster-Hash — und
  trotzdem tauchte im Network-Tab **kein** Request für die Datei auf, `window.customCards`
  blieb `undefined`. Das Modul wurde also nie ausgeführt, obwohl Backend und Deploy
  nachweislich in Ordnung waren. Ursache war der PWA-Service-Worker von Home Assistant im
  Browser-Profil des Nutzers: ein normaler Hard-Reload (Strg+F5) umgeht dessen
  Fetch-Interception nicht zuverlässig. Ein Inkognito-Fenster (kein registrierter Service
  Worker) zeigte die Karte sofort korrekt. **Diagnose-Reihenfolge also:** erst `curl` gegen
  den View (Backend ok?), dann bei weiterhin fehlender Karte ein Inkognito-Fenster testen,
  bevor am Code gesucht wird. Der Fix liegt beim Nutzer (DevTools → Application → Service
  Workers → *Unregister*, plus *Clear site data*) — kein Integrations- oder Karten-Bug, an
  `_async_register_frontend()` also nichts ändern.
- **Das Telefonbuch wird alle 12 h neu geladen** (`PHONEBOOK_REFRESH_INTERVAL`), sonst
  bleiben neue Kontakte bis zum nächsten Reload unaufgelöst. Zusätzlich gibt es dafür eine
  **Button-Entity** (`button.py`). Bewusst ein Button und kein Sensor: ein Sensor ist ein
  Messwert, keine Aktion. `ButtonEntity` erscheint automatisch auf der Geräteseite, ist über
  `button.press` automatisierbar und braucht keinen eigenen Service — ein zusätzliches
  `franzbox_monitor.reload_phonebook` wäre nur ein zweiter Weg zum selben Ziel (plus
  `services.yaml` und Übersetzungen).
- **Knopf und Turnus laufen beide über `async_reload_phonebook()`** in `__init__.py`, das
  danach `SIGNAL_PHONEBOOK_UPDATED` über den Dispatcher schickt. Der Sensor hängt sich in
  `async_added_to_hass()` daran und frischt seine Historie auf. Über den Dispatcher statt per
  direktem Aufruf, damit `button.py` die Sensor-Entity nicht kennen muss. Das Signal trägt die
  `entry_id`, sonst würde bei zwei Fritzboxen die falsche Entity auffrischen. **Der Erstaufruf
  im Setup nutzt die Funktion absichtlich nicht** — dort gibt es die Entities noch nicht, das
  Signal ginge ins Leere.
- **`_enrich_history_with_names()` überschreibt Treffer, aber niemals mit `None`.** Ein
  Treffer im Telefonbuch gewinnt immer (sonst bliebe ein umbenannter Kontakt für immer unter
  dem alten Namen stehen), ein Fehlschlag lässt den gespeicherten Namen stehen. Andernfalls
  würde ein einziger Lauf bei nicht erreichbarer Fritzbox sämtliche Namen aus der Historie
  löschen — `phonebook.load()` wirft in dem Fall ja nicht, es behält nur den alten Bestand.
- **Konfiguration liegt in `entry.data`, nicht in `entry.options`.** Der OptionsFlow
  schreibt per `async_update_entry(data=...)` zurück und wird nur als UI-Vehikel benutzt;
  ein `add_update_listener` löst danach einen kompletten Reload aus. Wer neue Optionen
  hinzufügt, muss dieses Muster einhalten, sonst lesen `__init__.py`/`phonebook.py` alte
  Werte.

## Regeln, die hier wehtun, wenn man sie verletzt

1. **`fritzconnection` ist synchron und blockierend.** Jeder Aufruf (z.B.
   `phonebook.load()`) muss über `hass.async_add_executor_job(...)` laufen, niemals direkt
   aus einer `async`-Funktion — sonst blockiert der komplette HA-Event-Loop.
2. **Telefonbuch-Fehler dürfen den Call-Monitor nicht mitreißen.** `phonebook.load()`
   fängt bewusst breit (`except Exception`) und loggt nur eine Warnung: Nummern bleiben
   dann unaufgelöst, statt die Integration zu killen.
3. **Attribut-Schlüssel immer setzen, auch auf `None`.** In `_enrich_history_with_names()`
   werden `caller_name`/`called_name` unbedingt geschrieben. Fehlt ein Schlüssel bei
   erfolgloser Telefonbuch-Suche, brechen Lovelace-Templates mit `UndefinedError` ab.
3a. **`extra_state_attributes` gibt ausschließlich Kopien heraus** — nie `self._history`
   selbst und nie die Eintrags-Dicts. HA bildet den Diff fürs Frontend durch Vergleich mit
   den Attributen des vorherigen States, und der alte State hält dieselben Objekte. Eine
   In-place-Änderung verändert damit auch den alten State, alt und neu sind gleich, das
   Attribut fällt aus dem Diff und erreicht das Frontend nie. Symptom war eine Card, die
   neue Anrufe erst nach Strg+F5 zeigte, und ein eingefrorenes `history: []` neben einem
   aktuellen `last_call` in den Entwicklerwerkzeugen. Das kostete eine lange Fehlersuche —
   die Kopie in Zeile 1 von `extra_state_attributes` also nicht „wegoptimieren".
4. **Listener blockieren die Leseschleife nicht.** `_notify_listeners()` verpackt jeden
   Callback in `hass.async_create_task(...)`, weil Listener selbst z.B. in den Store
   schreiben.
5. **Nummern-Matching ist unscharf.** `lookup()` vergleicht erst exakt (nur Ziffern), dann
   über die letzten 10 Ziffern — damit `+49 151 234567` aus dem Telefonbuch zu
   `0151234567` aus dem Call-Monitor passt.
6. **`DEFAULT_PORT = 1012` ist nicht konfigurierbar** und soll es auch nicht werden; der
   Port ist an der FRITZ!Box fest.
7. **Nie eine Entity-ID hartkodieren — auch nicht in Doku, Beispielen oder JS.** Die ID
   bildet HA bei der *ersten* Registrierung aus Gerätename + Entity-Name ab und friert sie
   danach in der Entity-Registry ein (Zuordnung läuft über die `unique_id`) — aktuell also
   `sensor.franz_box_monitor_anrufstatus`. Ein Umbenennen von `_attr_name` oder des
   Gerätenamens würde eine zusätzliche Entity-ID-Variante erzeugen, ohne die bestehende zu
   ersetzen. Wo eine Entity gefunden werden muss (z.B. `getStubConfig` in der Card), über
   die Attribute suchen — unsere Signatur ist `history` **und** `active_calls` — statt zu
   raten.

## Call-Monitor-Zeilenformat

Semikolon-getrennt, Zeitstempel im Format `%d.%m.%y %H:%M:%S`:

```
datum;RING;ConnectionID;AnruferNr;AngerufeneNr;SIPx;
datum;CALL;ConnectionID;Nebenstelle;GenutzteNr;ZielNr;SIPx;
datum;CONNECT;ConnectionID;Nebenstelle;Nummer;
datum;DISCONNECT;ConnectionID;Dauer;
```

**Die Feldpositionen unterscheiden sich je Ereignistyp** — das ist die fehleranfälligste
Stelle im Projekt, siehe `parse_call_monitor_line()`:

| Feld 3 | Feld 4 | Feld 5 |
| --- | --- | --- |
| `RING`: Anrufer | angerufene eigene MSN | — |
| `CALL`: Nebenstelle | genutzte eigene MSN | Zielnummer |
| `CONNECT`: Nebenstelle | Nummer | — |
| `DISCONNECT`: Dauer (s) | — | — |

Bei `CALL` steht die eigene Nummer also in `caller_number`, bei `RING` in `called_number`.
Die Nebenstelle kommt bei abgehenden Anrufen schon im `CALL`, bei eingehenden erst im
`CONNECT` (vorher steht nicht fest, wo abgehoben wird). Unbekannte oder zu kurze Zeilen
liefern `None` und werden verworfen, nie geworfen.

Zum Nachprüfen ohne HA-Installation lassen sich die `homeassistant.*`-Importe stubben und
`parse_call_monitor_line()` direkt gegen Beispielzeilen laufen — schneller als ein Deploy.

Ein Historieneintrag entsteht nur bei `RING`/`CALL` (Anrufbeginn), neueste Einträge stehen
vorne (`insert(0, ...)`), abgeschnitten auf `history_length`. `CONNECT` und `DISCONNECT`
**ergänzen** den passenden Eintrag (`answered`, `extension`, `duration_seconds`, `outcome`)
statt einen neuen anzulegen — zugeordnet über die `connection_id`.

Zwei Fallstricke dabei:

- **Connection-IDs werden von der Fritzbox recycelt.** `_find_open_entry()` trifft deshalb
  nur Einträge mit `outcome == "laufend"`.
- **Ein `DISCONNECT` schließt den Eintrag immer**, notfalls mit `duration_seconds = 0` —
  ein offen gelassener Eintrag würde später von einer wiederverwendeten ID getroffen.

Der Eintrag trägt neben den Rohfeldern zwei **abgeleitete** Ebenen, damit Card und Templates
keine Richtungs-Fallunterscheidung brauchen:

- `partner_number`/`partner_name` — die Gegenstelle (eingehend der Anrufer, ausgehend das
  Ziel), `line_number` — die eigene MSN, `extension` — die Nebenstelle.
- `outcome` — `laufend` → beim `DISCONNECT` zu `angenommen` (wenn `CONNECT` kam), sonst
  `verpasst` (eingehend) bzw. `nicht_erreicht` (ausgehend). Damit sind erfolglose Anrufe in
  **beide** Richtungen protokolliert, nicht nur verpasste eingehende.

`_normalize_entry()` leitet diese Felder für Altbestände beim Laden nach. Wichtig: Ein
Alteintrag mit gesetzter `duration_seconds` bekommt dort sein Ergebnis nachträglich
zugewiesen — bliebe er auf `laufend`, würde ihn `_find_open_entry()` später fälschlich
abgreifen.

### Rufnummernsperre: `blockiert` und `repeat_count`

**Die Rufnummernsperre greift in der Box erst nach der Meldung auf Port 1012** — gesperrte
Anrufe kommen als ganz normales `RING` an, es gibt kein Feld dafür. Erkennbar sind sie nur
am Zeitverhalten: die Box beendet den Ruf selbst und sofort. Gemessene Zeilen (30.07.2026):

```
12:21:56;RING;0;<Nummer>;<eigene MSN>;SIP1;
12:21:56;DISCONNECT;0;0;
```

Derselbe Boxzeitstempel, kein `CONNECT`, Dauer 0 — ein echter verpasster Anruf klingelt
20–30 s. `_was_blocked()` macht daraus `outcome = blockiert` (`BLOCKED_MAX_SECONDS = 2`, die
Toleranz fängt den Sekundenübertrag ab). Das ist eine **Heuristik**, kein hartes Signal: wer
binnen zwei Sekunden selbst auflegt, landet ebenfalls dort. Eine belastbare Auskunft gäbe nur
die Anrufliste über TR-064 (`X_AVM-DE_OnTel`, Feld `<Type>`) — das wäre aber Polling neben
dem Push-Client und ist bewusst **nicht** eingebaut. **Ausgehende Anrufe sind absichtlich
ausgenommen**, dort ist sofortiges Auflegen der Normalfall (Vertipper).

Gesperrte Anrufer bauen nach jeder Abweisung sofort neu auf — beobachtet waren **fünf
vollständige RING/DISCONNECT-Zyklen in drei Sekunden**, also fünf Historieneinträge für einen
Anrufversuch. `_collapse_repeat()` fasst das zusammen: gleiche `partner_number`, gleiche
Richtung, **gleiches `outcome`**, innerhalb `REPEAT_WINDOW_SECONDS = 60` → der ältere Eintrag
übernimmt die Daten des jüngeren, `repeat_count` addiert sich, der jüngere fällt weg.
Verglichen wird nur `self._history[0]` mit `self._history[1]`; kam zwischendurch ein anderer
Anruf, ist es keine Wiederholungsserie mehr.

**Zusammengefasst wird beim `DISCONNECT`, nicht beim `RING`** — und das ist der Kern. Ein
erster Anlauf, der beim `RING` zusammenfasste, hatte einen Fehler, der erst am echten Gerät
auffiel: nimmt man die Sperre an der Box heraus und ruft erneut an, sieht das `RING` des
durchgestellten Anrufs **exakt** aus wie ein weiterer abgewiesener — der Anruf wurde der
gesperrten Serie zugeschlagen. Zum Zeitpunkt des `RING` ist das Ergebnis schlicht noch nicht
bekannt. Erst der Vergleich der fertigen `outcome`-Werte trennt sauber: `blockiert` nur mit
`blockiert`, `verpasst` nur mit `verpasst`. **Diese Reihenfolge also nicht umdrehen.**

`MERGEABLE_OUTCOMES` enthält bewusst **kein** `angenommen`: ein zustande gekommenes Gespräch
darf nie hinter einem Zähler verschwinden. Ruft jemand dreimal an und wird beim dritten Mal
angenommen, stehen zwei Einträge in der Historie — das Gespräch und darunter „2× versucht".

Beim Zusammenfassen wird `connection_id` auf die des jüngeren Anrufs gezogen. Das ist kein
Detail: bliebe die alte stehen, träfe `_find_open_entry()` bei einer recycelten ID (die Box
vergibt fast immer `0`) den falschen Eintrag.

Das Bus-Event in `__init__.py` wird **nicht** dedupliziert: es bildet die Rohzeile ab, nicht
den Historieneintrag. Automationen, die auf `franzbox_monitor_call` hören, sehen bei einer
Wiederholungsserie also weiterhin jedes `RING`.

Der Sensorzustand leitet sich aus `_connection_states` ab (`connection_id -> Zustand`), nicht
direkt aus dem letzten Ereignis: bei parallelen Anrufen gewinnt der weiter fortgeschrittene
Zustand (`STATE_PRIORITY`), `idle` gilt erst, wenn keine Verbindung mehr offen ist.

## Persistenz

`homeassistant.helpers.storage.Store` mit `STORAGE_VERSION = 1` und Key
`franzbox_monitor_history_{entry_id}` — eine Datei je Config-Entry unter `.storage/`.

Rein **additive** Felder am Eintragsschema brauchen keine Versionserhöhung: `HISTORY_KEYS`
plus `_normalize_entry()` füllen fehlende Schlüssel beim Laden auf. Das ist auch der Grund,
warum neue Felder immer in `HISTORY_KEYS` eingetragen werden müssen — sonst fehlt der
Schlüssel bei alten Einträgen und Lovelace-Templates brechen ab. Erst bei **umbenannten oder
umgedeuteten** Feldern die `STORAGE_VERSION` erhöhen und einen Migrator ergänzen.

## Entwicklung / Test

Es gibt **keine automatisierten Tests und keine lokale HA-Instanz** in diesem Repo. Das
`.venv` enthält absichtlich nur `pip`; Home Assistant wird nicht lokal installiert.
Verifikation läuft daher über:

- Syntaxcheck: `python -m compileall custom_components/franzbox_monitor`
  (Die IDE meldet zu allen `homeassistant.*`- und `fritzconnection`-Imports
  „could not be resolved“ — das ist erwartet, die Pakete fehlen lokal absichtlich.)
- `tests/fritz-connector.py` als Standalone-Debughilfe, um rohe Zeilen der FRITZ!Box zu
  sehen (HOST dort ggf. anpassen).
- Deploy auf die echte HA-Instanz und Prüfung des Logs (`custom_components.franzbox_monitor`).

Wenn Verhalten nur an echter Hardware prüfbar ist: das offen sagen, nicht behaupten, es
sei getestet.

## Deploy

Der Ablauf steht in [deploy/readme.txt](deploy/readme.txt) — **ohne** Hostnamen und
Zugangsdaten, die stehen im Passwortsafe. Das Repository ist öffentlich, also gehören dort
keine Betriebsinterna hinein.

Zwei Dinge, an denen es hier schon gescheitert ist:

- **Branch prüfen, bevor man dem `git pull` glaubt** (`git branch --show-current`). Ein Pull
  auf dem falschen Branch holt nichts, und der Fehler sieht danach wie ein Code-Problem aus.
- **HA komplett neu starten**, ein Reload der Integration genügt nicht — Python importiert
  geänderte Module nur beim Start neu. Gegenprobe:
  `grep '"version"' /homeassistant/custom_components/franzbox_monitor/manifest.json`.

Kopiert wird mit `rsync -a --delete`, nicht mit `cp -r`: `cp` löscht nichts, entfernte
Dateien blieben als Leichen im Zielverzeichnis liegen.

## Veröffentlichung über HACS

`main` ist Default-Branch und enthält den vollständigen Stand; HACS zieht ohne Release den
Default-Branch, mit Releases bevorzugt die getaggte Fassung.

- **`manifest.json` -> `version` bei funktionalen Änderungen hochziehen**, sonst erkennt HACS
  das Update nicht.
- **Tags werden OHNE `v`-Präfix vergeben** (`0.5.1`, nicht `v0.5.1`) — bewusste
  Festlegung. HACS setzt laut Doku „the tag name from the latest release" als Remote-Version,
  die Schreibweise muss deshalb dauerhaft gleich bleiben; ein späterer Wechsel bringt den
  Versionsvergleich durcheinander. Tag und `manifest.json` sollen denselben Wert tragen.
- **`.github/workflows/validate.yml`** lässt die HACS-Action und hassfest laufen. Die
  HACS-Action prüft unter anderem `brands`, `description` und `topics` — Beschreibung und
  Topics sind Einstellungen am GitHub-Repository, keine Dateien. `ignore: brands` steht drin,
  solange der PR an `home-assistant/brands` nicht gemerged ist; danach entfernen.

## Git-Konventionen

- Arbeitsbranch ist `develop`, Zielbranch `main`.
- Commit-Messages sind kurz und auf Deutsch/Englisch gemischt gehalten, ohne festes
  Präfix-Schema (`Phonebook fixes`, `Basic implementations`).
- Nur committen/pushen, wenn ausdrücklich gewünscht.
