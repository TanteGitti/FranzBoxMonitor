"""Konstanten für die FRITZ!Box Call History Integration."""

# Die Domain muss exakt dem Namen des custom_components-Ordners entsprechen
DOMAIN = "franzbox_monitor"

# Konfigurationsschlüssel (werden im config_flow und in __init__ wiederverwendet)
CONF_HOST = "host"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_HISTORY_LENGTH = "history_length"

# Standardwerte
DEFAULT_PORT = 1012          # Fester Port des Fritzbox Call-Monitors, nicht änderbar
DEFAULT_HISTORY_LENGTH = 20  # Anzahl der Anrufe, die wir in der Historie vorhalten

# Welche Plattformen (Entity-Typen) diese Integration bereitstellt
PLATFORMS = ["sensor", "button"]

# Signal, mit dem ein frisch geladenes Telefonbuch den Entities eines Config-Entry
# gemeldet wird (Dispatcher-Muster). Entkoppelt den Knopf von der Sensor-Entity:
# button.py muss die Entity nicht kennen, der Sensor reichert nur seine Historie neu an.
SIGNAL_PHONEBOOK_UPDATED = f"{DOMAIN}_phonebook_updated_{{entry_id}}"

# Bus-Event, das bei jedem Call-Monitor-Ereignis gefeuert wird. Damit lassen sich
# Automationen bauen, ohne auf State-Änderungen des Sensors zu triggern (dort geht
# z.B. ein zweiter RING bei gleichem Zustand verloren).
EVENT_CALL = f"{DOMAIN}_call"

# Auslieferung der mitgelieferten Frontend-Dateien. Sie liegen im Paket selbst,
# nicht im Repo-Wurzelverzeichnis - nur so landen sie beim Deploy mit auf dem
# HA-Host (kopiert wird ausschließlich custom_components/franzbox_monitor).
FRONTEND_DIR = "frontend"
FRONTEND_SCRIPT = "franzbox-monitor-card.js"
FRONTEND_URL = f"/{DOMAIN}/{FRONTEND_SCRIPT}"
FRONTEND_ICON = "franz.png"
FRONTEND_ICON_URL = f"/{DOMAIN}/{FRONTEND_ICON}"

# Zustände, die die Fritzbox über den Call-Monitor sendet
CALL_STATE_RINGING = "RING"
CALL_STATE_CALL = "CALL"      # abgehender Anruf wird aufgebaut
CALL_STATE_CONNECT = "CONNECT"
CALL_STATE_DISCONNECT = "DISCONNECT"