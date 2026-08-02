/**
 * Lovelace-Card für die FRANZ!Box-Monitor-Integration.
 *
 * Zeigt den aktuellen Leitungszustand und die Anrufhistorie aus dem Attribut
 * "history" des Sensors. Bewusst reines Vanilla-JS ohne Build-Step: die Datei
 * wird von der Integration direkt als statische Ressource ausgeliefert
 * (siehe _async_register_frontend in __init__.py).
 *
 * Konfiguration:
 *   type: custom:franzbox-monitor-card
 *   entity: sensor.franz_box_monitor_anrufstatus
 *   max_entries: 10       # optional, wie viele Anrufe geladen werden
 *   visible_entries: 5    # optional, wie viele ohne Scrollen sichtbar sind
 *   max_height: 320px     # optional, feste Höhe statt Zeilenzahl
 *   title: Anrufe         # optional
 *
 * Die Entity-ID ist NICHT fest: HA leitet sie bei der ersten Registrierung aus
 * Gerätename und Entity-Name ab. Deshalb hier nirgends eine ID hartkodieren.
 */

const CARD_TAG = "franzbox-monitor-card";
const EDITOR_TAG = "franzbox-monitor-card-editor";
const DEFAULT_MAX_ENTRIES = 10;
// Wie viele Zeilen ohne Scrollen sichtbar sind. Der Rest bleibt über die
// Bildlaufleiste erreichbar, die Karte bekommt dadurch eine feste Maximalhöhe.
const DEFAULT_VISIBLE_ENTRIES = 5;
// Wird von der Integration ausgeliefert (FranzBoxMonitorAssetView in __init__.py)
const LOGO_URL = "/franzbox_monitor/franz.png";

const STATE_LABELS = {
  idle: "Frei",
  ringing: "Klingelt",
  dialing: "Wählt",
  talking: "Im Gespräch",
};

const STATE_ICONS = {
  idle: "mdi:phone-hangup",
  ringing: "mdi:phone-ring",
  dialing: "mdi:phone-outgoing",
  talking: "mdi:phone-in-talk",
};

// Ergebnis eines Anrufs -> Beschriftung und Symbol, getrennt nach Richtung.
// Die Schlüssel entsprechen dem Attribut 'outcome' aus sensor.py.
const OUTCOMES = {
  eingehend: {
    laufend: { label: "eingehend", icon: "mdi:phone-incoming", tone: "" },
    angenommen: { label: "angenommen", icon: "mdi:phone-incoming", tone: "ok" },
    verpasst: { label: "verpasst", icon: "mdi:phone-missed", tone: "bad" },
    nicht_erreicht: { label: "verpasst", icon: "mdi:phone-missed", tone: "bad" },
    // Von der Rufnummernsperre abgewiesen. Bewusst ohne 'bad'-Ton: die Box hat
    // sich richtig verhalten, das ist kein verpasster Anruf.
    blockiert: { label: "gesperrt", icon: "mdi:phone-off", tone: "" },
  },
  ausgehend: {
    laufend: { label: "ausgehend", icon: "mdi:phone-outgoing", tone: "" },
    angenommen: { label: "erfolgreich", icon: "mdi:phone-outgoing", tone: "ok" },
    verpasst: { label: "nicht erreicht", icon: "mdi:phone-cancel", tone: "bad" },
    nicht_erreicht: {
      label: "nicht erreicht",
      icon: "mdi:phone-cancel",
      tone: "bad",
    },
  },
};

const OUTCOME_FALLBACK = { label: "", icon: "mdi:phone", tone: "" };

/** Sekunden -> "m:ss" bzw. "h:mm:ss". Null/undefined ergibt null. */
function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) {
    return null;
  }

  const total = Number(seconds);
  if (!Number.isFinite(total) || total <= 0) {
    return null;
  }

  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = Math.floor(total % 60);
  const pad = (value) => String(value).padStart(2, "0");

  return hours > 0
    ? `${hours}:${pad(minutes)}:${pad(secs)}`
    : `${minutes}:${pad(secs)}`;
}

/** ISO-Zeitstempel in lokale Darstellung; heutige Anrufe nur mit Uhrzeit. */
function formatTimestamp(isoString, language) {
  if (!isoString) {
    return "";
  }

  const date = new Date(isoString);
  if (Number.isNaN(date.getTime())) {
    return isoString;
  }

  const time = date.toLocaleTimeString(language, {
    hour: "2-digit",
    minute: "2-digit",
  });

  const isToday = date.toDateString() === new Date().toDateString();
  if (isToday) {
    return time;
  }

  const day = date.toLocaleDateString(language, {
    day: "2-digit",
    month: "2-digit",
  });
  return `${day} ${time}`;
}

/** "Leitung 4711 · Nst. 1" - leere Bestandteile fallen weg. */
function formatLine(call) {
  const parts = [];
  if (call.line_number) {
    parts.push(`Leitung ${call.line_number}`);
  }
  if (call.extension !== null && call.extension !== undefined && call.extension !== "") {
    parts.push(`Nst. ${call.extension}`);
  }
  return parts.join(" · ");
}

function escapeHtml(value) {
  if (value === null || value === undefined) {
    return "";
  }
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

class FranzboxMonitorCard extends HTMLElement {
  setConfig(config) {
    if (!config || !config.entity) {
      throw new Error(
        "Bitte eine 'entity' angeben: den Anrufstatus-Sensor der Integration. " +
          "Die genaue ID steht unter Entwicklerwerkzeuge -> Zustände."
      );
    }

    this._config = {
      max_entries: DEFAULT_MAX_ENTRIES,
      visible_entries: DEFAULT_VISIBLE_ENTRIES,
      max_height: null,
      title: "FRANZ!Box Monitor",
      ...config,
    };
    // Erzwingt beim nächsten hass-Update ein Neuzeichnen
    this._signature = null;
  }

  set hass(hass) {
    this._hass = hass;

    if (!this._config) {
      // Lovelace ruft normalerweise setConfig zuerst - defensiv abgesichert
      return;
    }

    const entity = hass.states[this._config.entity];
    const history = entity?.attributes?.history ?? [];
    const entries = history.slice(0, this._config.max_entries);
    const activeCall = entity?.attributes?.active_call ?? null;

    // hass wird bei jeder State-Änderung im System neu gesetzt, auch für
    // fremde Entities. Ohne diesen Vergleich würde die Card dabei jedes Mal
    // komplett neu gerendert.
    const signature = JSON.stringify([entity?.state, activeCall, entries]);
    if (signature === this._signature) {
      return;
    }
    this._signature = signature;

    this._render(entity, entries, activeCall);
  }

  _render(entity, entries, activeCall) {
    // Bildlaufposition merken: Während eines Gesprächs kommen im Sekundentakt
    // State-Updates, und ohne das würde die Liste beim Durchsehen der Historie
    // ständig an den Anfang springen.
    const previousScroll = this.querySelector(".list")?.scrollTop ?? 0;

    const language = this._hass?.locale?.language || "de";
    const state = entity?.state;
    const stateLabel = state
      ? STATE_LABELS[state] ?? state
      : "Sensor nicht gefunden";
    const stateIcon = STATE_ICONS[state] ?? "mdi:phone-off";

    const rows = entries.length
      ? entries.map((call) => this._renderRow(call, language)).join("")
      : `<div class="empty">Noch keine Anrufe aufgezeichnet.</div>`;

    // Beim Wählen und im Gespräch zeigen, mit wem und über welche Leitung -
    // das steht sonst nur im Historieneintrag, der leicht zu übersehen ist.
    let activeLine = "";
    if (activeCall) {
      const partner =
        activeCall.partner_name || activeCall.partner_number || "Unbekannt";
      const via = formatLine(activeCall);
      // "im Gespräch mit X", "es klingelt von X", "es wird gewählt an X"
      const preposition =
        state === "talking"
          ? "mit"
          : activeCall.direction === "eingehend"
            ? "von"
            : "an";
      activeLine = `
        <div class="active">
          ${escapeHtml(preposition)} <strong>${escapeHtml(partner)}</strong>${
            via ? ` &middot; ${escapeHtml(via)}` : ""
          }
        </div>
      `;
    }

    this.innerHTML = `
      <ha-card>
        <style>
          .header {
            display: flex;
            align-items: center;
            gap: 12px;
            padding: 12px 16px;
          }
          .header .title {
            flex: 1;
            font-size: 1.1rem;
            font-weight: 500;
          }
          .header .status {
            color: var(--secondary-text-color);
            font-size: 0.9rem;
          }
          .header .logo {
            width: 40px;
            height: 40px;
            flex: none;
            object-fit: contain;
          }
          .header ha-icon {
            color: var(--state-icon-color, var(--paper-item-icon-color));
            flex: none;
          }
          .header.active ha-icon,
          .header.active .status {
            color: var(--state-active-color, var(--primary-color));
          }
          .list {
            border-top: 1px solid var(--divider-color);
            overflow-y: auto;
            /* Verhindert, dass am Listenende die Seite weiterscrollt */
            overscroll-behavior: contain;
            scrollbar-width: thin;
          }
          .row {
            display: flex;
            align-items: center;
            gap: 12px;
            padding: 8px 16px;
          }
          .row:not(:last-child) {
            border-bottom: 1px solid var(--divider-color);
          }
          .row ha-icon {
            color: var(--secondary-text-color);
            flex: none;
          }
          .row.bad ha-icon {
            color: var(--error-color, #db4437);
          }
          .row.ok ha-icon {
            color: var(--success-color, #43a047);
          }
          .header .active {
            font-size: 0.85rem;
            font-weight: 400;
            color: var(--secondary-text-color);
          }
          .row .details {
            color: var(--secondary-text-color);
            font-size: 0.8rem;
          }
          .row .who {
            flex: 1;
            min-width: 0;
          }
          /* Alle drei Textzeilen einzeilig halten: nur dann sind die Zeilen
             gleich hoch, und nur dann stimmt die gemessene Höhe für die
             Begrenzung auf 'visible_entries'. */
          .row .name,
          .row .meta,
          .row .details {
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
          }
          .row .meta,
          .row .duration {
            color: var(--secondary-text-color);
            font-size: 0.85rem;
          }
          .row .duration {
            flex: none;
            font-variant-numeric: tabular-nums;
          }
          .empty {
            padding: 16px;
            color: var(--secondary-text-color);
          }
        </style>

        <div class="header ${state && state !== "idle" ? "active" : ""}">
          <img class="logo" src="${LOGO_URL}" alt="" />
          <div class="title">
            ${escapeHtml(this._config.title)}
            ${activeLine}
          </div>
          <ha-icon icon="${stateIcon}"></ha-icon>
          <div class="status">${escapeHtml(stateLabel)}</div>
        </div>

        <div class="list">${rows}</div>
      </ha-card>
    `;

    this._heightRetries = 0;
    this._applyListHeight();

    const list = this.querySelector(".list");
    if (list) {
      list.scrollTop = previousScroll;
    }
  }

  connectedCallback() {
    // Lovelace erzeugt die Karte, ruft setConfig und setzt hass, BEVOR das
    // Element im Dokument hängt. Vorher ist offsetHeight 0, die Zeilenhöhe also
    // nicht messbar. Hier ist sie es - deshalb nachmessen.
    this._heightRetries = 0;
    this._applyListHeight();
  }

  /**
   * Begrenzt die Höhe der Liste, damit die Karte nicht beliebig lang wird.
   *
   * 'max_height' gewinnt, wenn gesetzt (beliebige CSS-Länge). Sonst wird aus
   * 'visible_entries' eine Höhe berechnet - dafür wird die tatsächliche Höhe
   * der ersten Zeile gemessen statt sie zu schätzen, weil sie von Theme,
   * Schriftgröße und Zeilenabstand abhängt.
   *
   * WICHTIG: Solange die Karte nicht im Layout steht, liefert offsetHeight 0.
   * Daraus darf niemals eine maxHeight von 0px werden - dann wäre nur noch die
   * Kopfzeile zu sehen. In dem Fall bleibt die Begrenzung aus und wird im
   * nächsten Frame erneut versucht.
   */
  _applyListHeight() {
    const list = this.querySelector(".list");
    if (!list || !this._config) {
      return;
    }

    if (this._config.max_height) {
      list.style.maxHeight = this._config.max_height;
      return;
    }

    const visible = Number(this._config.visible_entries);
    if (!Number.isFinite(visible) || visible <= 0) {
      // 0 oder ungültig = keine Begrenzung, Karte wächst wie bisher
      list.style.maxHeight = "";
      return;
    }

    const rowHeight = list.querySelector(".row")?.offsetHeight ?? 0;
    if (rowHeight > 0) {
      list.style.maxHeight = `${rowHeight * visible}px`;
      return;
    }

    // Noch nicht messbar: unbegrenzt lassen und es begrenzt oft neu versuchen.
    // Die Obergrenze verhindert eine Endlosschleife, falls die Karte dauerhaft
    // unsichtbar bleibt (z.B. in einem nicht aktiven Tab).
    list.style.maxHeight = "";
    if (this._heightRetries < 30) {
      this._heightRetries = (this._heightRetries ?? 0) + 1;
      requestAnimationFrame(() => this._applyListHeight());
    }
  }

  _renderRow(call, language) {
    const outcome =
      OUTCOMES[call.direction]?.[call.outcome] ?? OUTCOME_FALLBACK;

    // partner_* liefert der Sensor bereits richtungsabhängig - eingehend der
    // Anrufer, ausgehend das Ziel. Fallback auf die Rohfelder für Einträge aus
    // Versionen vor 0.4.0.
    const incoming = call.direction === "eingehend";
    const number =
      call.partner_number ?? (incoming ? call.caller_number : call.called_number);
    const name =
      call.partner_name ?? (incoming ? call.caller_name : call.called_name);

    const primary = name || number || "Unbekannt";
    // Nummer nur zusätzlich zeigen, wenn ein Name sie ersetzt hat
    const meta = [formatTimestamp(call.timestamp, language), name ? number : null]
      .filter(Boolean)
      .join(" · ");
    // Zusammengefasste Wiederholungsversuche derselben Gegenstelle. Der Zähler
    // gehört in die Detailzeile und nicht in die Dauer-Spalte: ein mehrfach
    // versuchter Anruf kann am Ende doch angenommen werden und hat dann beides.
    const repeats =
      Number(call.repeat_count) > 1 ? `${call.repeat_count}× versucht` : null;
    const details = [outcome.label, repeats, formatLine(call)]
      .filter(Boolean)
      .join(" · ");

    const duration = formatDuration(call.duration_seconds);
    const ongoing = call.outcome === "laufend";

    return `
      <div class="row ${outcome.tone}">
        <ha-icon icon="${outcome.icon}"></ha-icon>
        <div class="who">
          <div class="name">${escapeHtml(primary)}</div>
          <div class="meta">${escapeHtml(meta)}</div>
          <div class="details">${escapeHtml(details)}</div>
        </div>
        <div class="duration">${
          ongoing ? "läuft" : duration ? escapeHtml(duration) : ""
        }</div>
      </div>
    `;
  }

  getCardSize() {
    // Kopfzeile plus die sichtbaren Zeilen - nicht die insgesamt geladenen,
    // sonst plant Lovelace die Karte höher ein, als sie durch die Begrenzung
    // tatsächlich wird.
    const visible = Number(this._config?.visible_entries);
    const rows =
      Number.isFinite(visible) && visible > 0
        ? visible
        : this._config?.max_entries ?? DEFAULT_MAX_ENTRIES;
    return 1 + Math.min(rows, 10);
  }

  static getConfigElement() {
    return document.createElement(EDITOR_TAG);
  }

  static getStubConfig(hass) {
    // Die Entity-ID variiert je Installation (siehe Kopfkommentar), deshalb
    // suchen wir sie über die Attribute statt sie zu raten. 'history' allein
    // wäre zu unspezifisch - das Attribut haben auch fremde Integrationen -,
    // die Kombination mit 'active_calls' ist unsere Signatur.
    const sensor = Object.keys(hass.states).find((entityId) => {
      const attributes = hass.states[entityId].attributes;
      return (
        entityId.startsWith("sensor.") &&
        attributes?.history !== undefined &&
        attributes?.active_calls !== undefined
      );
    });
    return { entity: sensor ?? "" };
  }
}

/**
 * Grafischer Editor für die Karte.
 *
 * Baut auf 'ha-form' auf, das Home Assistant selbst mitbringt - damit sehen die
 * Felder aus wie überall sonst und wir brauchen keine eigenen Eingabeelemente.
 * Das Element wird erst erzeugt, wenn der Nutzer den Editor öffnet; zu diesem
 * Zeitpunkt ist der Frontend-Bundle mit 'ha-form' sicher geladen.
 */
const EDITOR_SCHEMA = [
  { name: "entity", required: true, selector: { entity: { domain: "sensor" } } },
  { name: "title", selector: { text: {} } },
  {
    name: "max_entries",
    selector: { number: { min: 1, max: 200, mode: "box" } },
  },
  {
    name: "visible_entries",
    selector: { number: { min: 0, max: 50, mode: "box" } },
  },
  { name: "max_height", selector: { text: {} } },
];

const EDITOR_LABELS = {
  entity: "Anrufstatus-Sensor",
  title: "Überschrift",
  max_entries: "Geladene Anrufe",
  visible_entries: "Sichtbare Zeilen (0 = alle)",
  max_height: "Maximale Höhe (z. B. 320px, leer = aus Zeilen berechnen)",
};

class FranzboxMonitorCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = config;
    this._update();
  }

  set hass(hass) {
    this._hass = hass;
    this._update();
  }

  _update() {
    if (!this._hass || !this._config) {
      return;
    }

    if (!this._form) {
      this._form = document.createElement("ha-form");
      this._form.computeLabel = (schema) =>
        EDITOR_LABELS[schema.name] ?? schema.name;
      this._form.addEventListener("value-changed", (event) =>
        this._configChanged(event.detail.value)
      );
      this.appendChild(this._form);
    }

    this._form.hass = this._hass;
    this._form.schema = EDITOR_SCHEMA;
    this._form.data = this._config;
  }

  _configChanged(value) {
    // 'type' steht nicht im Schema, muss aber erhalten bleiben - sonst weiß
    // Lovelace nach dem Speichern nicht mehr, um welche Karte es geht.
    const config = { type: `custom:${CARD_TAG}`, ...this._config, ...value };

    // Leergeräumte optionale Felder ganz entfernen statt "" zu speichern:
    // sonst stünde max_height: "" in der Konfiguration und würde als gesetzt
    // gelten (leerer String ist zwar falsy, landet aber in der YAML-Ansicht).
    for (const key of ["title", "max_height"]) {
      if (config[key] === "" || config[key] === null) {
        delete config[key];
      }
    }

    this.dispatchEvent(
      new CustomEvent("config-changed", {
        detail: { config },
        bubbles: true,
        composed: true,
      })
    );
  }
}

/**
 * Registriert Element, Editor und Eintrag im Kartenwähler. Muss mehrfach
 * aufrufbar sein.
 *
 * Hintergrund: Die Integration hängt dieses Modul über add_extra_js_url ein,
 * also als <script type="module"> im <head> - es läuft damit VOR dem
 * Frontend-Bundle von Home Assistant. Beobachtet wurde, dass eine dort
 * vorgenommene Registrierung nicht bestehen bleibt: window.customCards war
 * gesetzt (die Card erschien im Kartenwähler), customElements.get() lieferte
 * aber undefined, und Lovelace meldete
 * "Custom element not found: franzbox-monitor-card". Ein nachträgliches
 * import() derselben Datei registrierte dagegen fehlerfrei.
 *
 * Deshalb wird unten mehrfach nachgesetzt. 'customElements' wird bei jedem
 * Aufruf frisch über den globalen Namen aufgelöst - eine zwischenzeitlich
 * ausgetauschte Registry wird also mitgenommen.
 */
function registerCard() {
  if (!customElements.get(CARD_TAG)) {
    customElements.define(CARD_TAG, FranzboxMonitorCard);
  }
  if (!customElements.get(EDITOR_TAG)) {
    customElements.define(EDITOR_TAG, FranzboxMonitorCardEditor);
  }

  // Vorhandene Einträge entfernen statt nur zu überspringen: Wird die Datei aus
  // zwei Quellen geladen (z.B. zusätzlich als Lovelace-Ressource eingetragen),
  // stünde die Karte sonst doppelt im Kartenwähler. In-place bearbeiten, nicht
  // neu zuweisen - das Frontend hält eine Referenz auf das Array.
  window.customCards = window.customCards || [];
  for (let index = window.customCards.length - 1; index >= 0; index--) {
    if (window.customCards[index].type === CARD_TAG) {
      window.customCards.splice(index, 1);
    }
  }
  window.customCards.push({
    type: CARD_TAG,
    name: "FRANZ!Box Monitor",
    description: "Leitungsstatus und Anrufhistorie der FRITZ!Box",
    preview: false,
  });
}

registerCard();

// Nachfassen, bis das Frontend geladen ist. Lovelace wartet beim Erzeugen einer
// Card rund 2 Sekunden auf customElements.whenDefined(), die Staffelung deckt
// dieses Fenster ab. Jeder Aufruf ist dank der Prüfungen oben ein No-Op, sobald
// die Registrierung sitzt.
for (const delay of [0, 100, 500, 1500, 3000]) {
  setTimeout(registerCard, delay);
}
