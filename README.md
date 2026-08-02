<p align="center">
  <img src="brands/custom_integrations/franzbox_monitor/icon.png"
       alt="FRANZ! Callmonitor" width="200">
</p>

<h1 align="center">FRANZ!Box Monitor</h1>

<p align="center">
  <em>Detect FRITZ!Box calls in real time, enrich them with names, and keep a
  persistent history — including blocked numbers.</em>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/HACS-Custom-41BDF5.svg" alt="HACS Custom">
  <img src="https://img.shields.io/badge/Home%20Assistant-2024.1%2B-41BDF5.svg" alt="Home Assistant 2024.1+">
  <img src="https://img.shields.io/badge/iot__class-local__push-success.svg" alt="local push">
  <img src="https://img.shields.io/badge/license-MIT-green.svg" alt="MIT">
</p>

---

Home Assistant custom integration that reads the **FRITZ!Box call monitor** (TCP port
1012): calls are detected in real time, enriched with names from the FRITZ!Box phonebook,
and stored as a persistent call history.

## Features

| | |
| --- | --- |
| **Persistent call history** | Survives restarts, length configurable (1–200 entries). Every entry knows whether the call was answered, how long it lasted, and which of your own numbers and extensions handled it. |
| **Names from the phonebook** | Resolved against **all** FRITZ!Box phonebooks, with fuzzy number matching — `+49 151 234567` in the phonebook also matches `0151234567` from the call monitor. |
| **Blocked numbers are detected** | Calls rejected by the FRITZ!Box call blocklist are marked as `blockiert` instead of disappearing into the missed-calls pile. |
| **Repeated attempts collapsed** | A telemarketer that redials instantly after every rejection produces one entry with a counter instead of five identical rows. |
| **Phonebook reload on demand** | A dedicated button entity reloads the phonebook immediately and backfills missing names **retroactively into the stored history**. |
| **Built-in Lovelace card** | Shipped by the integration itself, finds the sensor on its own, and comes with a graphical editor. No resource to add by hand. |
| **Bus events for automations** | Every event fires `franzbox_monitor_call` — even when the sensor state itself doesn't change. |

## Prerequisite: enable the call monitor

The call monitor is **disabled** on the FRITZ!Box by default. Enable it once by dialling
from a connected phone:

| Code | Effect |
| --- | --- |
| `#96*5*` | Call monitor **on** |
| `#96*4*` | Call monitor **off** |

Without this step the integration can't connect and will keep retrying indefinitely.

## Installation via HACS

1. In HACS → *Integrations* → menu → *Add custom repository*
2. URL `https://github.com/TanteGitti/FranzBoxMonitor`, category *Integration*
3. Install, restart Home Assistant
4. *Settings → Devices & Services → Add Integration → FRANZ!Box Monitor*

Manual installation also works: copy the `custom_components/franzbox_monitor` folder into
`<config>/custom_components/` and restart HA.

## Configuration

| Field | Required | Meaning |
| --- | --- | --- |
| Host | yes | IP or hostname of the FRITZ!Box, e.g. `192.168.1.1` |
| Username | no | only needed for phonebook resolution (TR-064) |
| Password | no | only needed for phonebook resolution (TR-064) |
| History length | no | 1–200 entries, default 20 |

The call monitor keeps working without credentials — numbers just stay unresolved. All
values can be changed later via the integration's gear icon; the integration reloads
automatically afterwards.

## Entities

The integration creates two entities on a shared *FRANZ!Box Monitor* device.

### Sensor "Call status"

The current line state; the icon changes with the state. The call history is attached as
an attribute.

| State | Display | Meaning |
| --- | --- | --- |
| `idle` | Idle | no active connection |
| `ringing` | Ringing | an incoming call is ringing |
| `dialing` | Dialing | an outgoing call is being set up |
| `talking` | In call | call connected |

Home Assistant shows the *Display* column in the UI. The **raw state value stays in
English** (`idle`, `ringing`, …) — automations and templates keep comparing against `idle`
and friends, not against the translated label.

> **What is the sensor called?** Home Assistant derives the entity ID from the device and
> entity name at first registration and never changes it afterwards — new installations get
> `sensor.franz_box_monitor_anrufstatus`. Your own ID is under *Developer tools → States*
> (filter for `anrufstatus`) and must be substituted into all examples below.

### Button "Reload phonebook"

The integration reads the phonebook on its own at startup and then every 12 hours. Anyone
who just created a contact in the FRITZ!Box doesn't want to wait for that — hence this
button.

- New contacts appear **immediately and retroactively** in the already-stored history: a
  bare number from yesterday turns into a name afterwards.
- A contact **renamed** in the FRITZ!Box is corrected as well.
- If the phonebook doesn't find a number, an already-stored name is left untouched — a
  temporarily unreachable FRITZ!Box doesn't erase names from the history.
- If the reload fails, an error message appears in the UI; call recording keeps running
  unaffected.

The button sits on the device page under *Settings → Devices & Services* and can be placed
on any dashboard with a regular tile. In automations it's reachable via the `button.press`
action.

### The call history

It hangs off the sensor as the `history` attribute, newest entries first:

```yaml
history:
  - timestamp: "2026-07-30T18:42:10"
    direction: eingehend      # or "ausgehend" (incoming / outgoing)
    outcome: angenommen       # laufend | angenommen | verpasst | nicht_erreicht | blockiert
    partner_number: "015112345678"    # the other party, direction-independent
    partner_name: "Max Mustermann"    # null if not in the phonebook
    line_number: "4711"       # your own number (MSN) the call came in/went out on
    extension: "2"            # extension / device, null if unknown
    caller_number: "015112345678"     # raw fields from the call monitor line
    caller_name: "Max Mustermann"
    called_number: "4711"
    called_name: null
    connection_id: "1"
    answered: true            # was the call connected?
    duration_seconds: 143     # null while the call is ongoing
    repeat_count: 1           # number of immediately consecutive attempts
```

`outcome` answers how the call ended — for both directions:

| Value | Meaning |
| --- | --- |
| `laufend` | call is still active |
| `angenommen` | call was connected (incoming answered, or outgoing succeeded) |
| `verpasst` | incoming call, never answered |
| `nicht_erreicht` | outgoing call, the other side didn't pick up |
| `blockiert` | incoming call, rejected by the FRITZ!Box call blocklist |

For most purposes `partner_name`/`partner_number` are enough — they always show the other
party, regardless of whether the call was incoming or outgoing. `line_number` and
`extension` say which of your own numbers and which device handled it.

Further attributes: `last_call` (newest entry), `active_call` (the currently ongoing call
with all the fields above, otherwise `null`), and `active_calls` (number of active
connections).

## Blocked numbers and repeated attempts

The FRITZ!Box call blocklist only kicks in *after* the call monitor has already reported
the call — blocked calls show up there just like any other. Without further handling
they'd sit in the history as "missed", indistinguishable from a call you simply didn't
answer in time.

**Detection.** The integration recognizes them by timing: the box ends a blocked call
itself, practically instantly. In the raw lines, `RING` and `DISCONNECT` carry the same
timestamp, there's no `CONNECT`, and the duration is 0:

```
12:21:56;RING;0;<number>;<own MSN>;SIP1;
12:21:56;DISCONNECT;0;0;
```

A genuinely missed call rings for 20–30 seconds instead. If less than two seconds pass
between the two lines, the entry is marked `blockiert`.

> **This is a heuristic, not a hard signal.** A caller who hangs up within two seconds on
> their own also ends up there. Outgoing calls are deliberately excluded — there, hanging
> up immediately is the normal case (a misdial), not a block.

**Collapsing.** Many blocked callers redial instantly after every rejection; five complete
call attempts in three seconds have been observed. If the same party calls again within 60
seconds and the call ends **exactly the same way** as the one before, both are collapsed
into a single entry and `repeat_count` is incremented. The card shows this as "5× versucht"
(5× attempted).

What matters is the outcome, not just the number:

- A call that gets through is **never** folded into a series of rejected ones — take the
  block off and call again, and it gets its own entry.
- An answered call is **always** kept separate. Someone who calls three times and gets
  through on the third attempt ends up with two entries in the history: the call itself and
  the two failed attempts before it.

## Automations

A `franzbox_monitor_call` bus event fires on every event:

```yaml
automation:
  - alias: Announce incoming call
    triggers:
      - trigger: event
        event_type: franzbox_monitor_call
        event_data:
          event_type: RING
    actions:
      - action: tts.speak
        data:
          message: "Call from {{ trigger.event.data.caller_name or trigger.event.data.caller_number }}"
```

The event data matches the fields of a history entry, plus `event_type` (`RING`, `CALL`,
`CONNECT`, `DISCONNECT`) and `entry_id`.

> **Bus events mirror the raw lines, not the history.** A series of repeated attempts gets
> collapsed for the history — as an event, every single `RING` still fires. If you don't
> want that, check the time gap in the automation, or trigger on the sensor state instead.

## Lovelace card

The integration ships its own card that renders the history. It's registered
automatically during setup — **no** manual entry needed under *Settings → Dashboards →
Resources*; adding one would load the file twice.

The easiest way is via *Add card* → "FRANZ!Box Monitor". The card finds the sensor on its
own and comes with a graphical editor covering all options — no YAML required. For anyone
who prefers to write it anyway:

```yaml
type: custom:franzbox-monitor-card
entity: sensor.franz_box_monitor_anrufstatus   # use your own ID, see above
max_entries: 10       # optional, default 10
visible_entries: 5    # optional, default 5
max_height: 320px     # optional, instead of visible_entries
title: Anrufe         # optional
```

| Option | Effect |
| --- | --- |
| `max_entries` | how many calls the card loads at all |
| `visible_entries` | how many of those are visible without scrolling; the rest stays reachable via the scrollbar. `0` disables the limit, the card then grows with the history |
| `max_height` | fixed maximum height of the list as a CSS length (e.g. `320px`, `40vh`); takes precedence over `visible_entries` |

Each row shows the other party, the time, and the outcome — blocked calls as "gesperrt"
(blocked), collapsed series with the "5× versucht" (5× attempted) suffix. The header with
status and the ongoing call always stays visible; only the list scrolls. While scrolling
through the history, the scroll position is preserved even if new events arrive in the
meantime.

`max_entries` only limits what's displayed — how many calls get stored is controlled by
the *History length* setting on the integration.

## Troubleshooting

### Enabling and viewing the log

Quickest without YAML and without a restart: *Settings → Devices & Services →
FRANZ!Box Monitor → three-dot menu → **Enable debug logging***. Disable it again in the
same menu after reproducing the issue — Home Assistant will then automatically download a
log file with all lines from this integration.

For a permanent setting, use `configuration.yaml` (restart required):

```yaml
logger:
  default: warning
  logs:
    custom_components.franzbox_monitor: debug
```

View the log via *Settings → System → Logs* (button *Load full logs*, then search for
`franzbox`) or from the shell:

```bash
ha core logs | grep -i franzbox        # current state
ha core logs -f | grep -i franzbox     # live
```

`ha core logs` reads via the Supervisor and works even without a
`/homeassistant/home-assistant.log` file. For targeted narrowing there's a logger per
module: `…franzbox_monitor.call_monitor` (connection, parser, listener errors), `.sensor`
(history, matching `CONNECT`/`DISCONNECT`), `.phonebook` (TR-064).

### Common cases

- **Persistent reconnect warnings** → enable the call monitor with `#96*5*`.
- **Events only arrive sporadically** → check whether the official `fritzbox_callmonitor`
  integration is also set up. Both connect on port 1012, and the FRITZ!Box doesn't reliably
  serve two clients at once. In the debug log this shows up as missing
  `Zeile vom Call-Monitor:` entries despite an open connection.
- **Names aren't resolved** → check username/password; the user needs the *"Settings"*
  permission in the FRITZ!Box for TR-064 access. Then press the *Reload phonebook* button
  and look for `Telefonbuch geladen:` in the debug log — it shows how many numbers actually
  arrived.
- **A blocked call shows as "verpasst" (missed) in the history** → then more than two
  seconds passed between `RING` and `DISCONNECT`. The raw lines are in the debug log;
  entries from before version 0.6 are never reinterpreted retroactively.
- **Watch the raw lines live** → `python tests/fritz-connector.py` (adjust the host in the
  script).
- **"Custom element doesn't exist: franzbox-monitor-card"** → first check the browser
  console (F12): the message *before* it names the cause. Then check whether the card is
  actually being served:

  ```bash
  curl -s -o /dev/null -D - http://<ha-host>:8123/franzbox_monitor/franzbox-monitor-card.js
  ```

  Expected: `200` and `Content-Type: text/javascript; charset=utf-8`. A `404` means the
  integration isn't loaded, or the `frontend/` folder is missing from the installation.
  Don't use `curl -I` — that sends HEAD and returns `405` with a misleading `text/plain`
  error page.

  If the file is served correctly and the error persists, it helps to additionally add the
  card by hand as a resource: *Settings → Dashboards → menu → Resources → Add resource*,
  URL `/franzbox_monitor/franzbox-monitor-card.js`, type *JavaScript module*. The frontend
  then loads it after startup, which works around the registration timing issue. Loading it
  twice is harmless — the card registers itself idempotently.

  **If `curl` already reports `200` with the correct content, the browser's service worker
  is the more likely culprit, not the integration.** Home Assistant registers a PWA service
  worker in the browser that can cache frontend resources independently of server restarts
  and cache-busting parameters — a regular hard reload (Ctrl+F5) often isn't enough to get
  past it. Quick way to test: open an incognito/private window. If the card shows up
  correctly there, the integration is fine. Fix in the regular browser window: DevTools
  (F12) → *Application* tab (Chrome/Edge) or *Storage* (Firefox) → *Service Workers* →
  *Unregister*, then *Storage* → *Clear site data*, and reload the page.

## License

MIT
