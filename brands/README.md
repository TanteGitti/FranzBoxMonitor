# Brand-Bilder für home-assistant/brands

Diese Dateien sind **nicht** Teil der Integration und werden nicht mit deployt. Sie liegen
hier fertig zugeschnitten, um sie per Pull Request in das Repository
[home-assistant/brands](https://github.com/home-assistant/brands) einzureichen.

Erst danach zeigt Home Assistant unter *Einstellungen → Geräte & Dienste* statt
„icon not available" unser Logo — die Oberfläche lädt die Bilder ausschließlich von
`brands.home-assistant.io`, ein lokaler Ersatz ist nicht vorgesehen.

## Inhalt

| Datei | Größe | Anforderung laut brands-Repo |
| --- | --- | --- |
| `custom_integrations/franzbox_monitor/icon.png` | 256×256 | exakt 256×256 |
| `custom_integrations/franzbox_monitor/icon@2x.png` | 512×512 | exakt 512×512 |
| `custom_integrations/franzbox_monitor/logo.png` | 256×256 | kürzeste Seite 128–256 |
| `custom_integrations/franzbox_monitor/logo@2x.png` | 512×512 | kürzeste Seite 256–512 |

Der Verzeichnisname `franzbox_monitor` muss der `domain` aus der `manifest.json`
entsprechen. Die Bilder sind transparent freigestellt und auf den Bildinhalt getrimmt, wie
das Repo es verlangt.

## Einreichen

1. `home-assistant/brands` forken und klonen
2. `custom_integrations/franzbox_monitor/` aus diesem Ordner dorthin kopieren
3. Branch, Commit, Push, Pull Request

Quelle der Bilder ist `static/franz.png` (1254×1254). Erzeugt wurden sie durch
Freistellen des Hintergrunds per Flood-Fill von den Bildrändern — ein simpler
Weiß-Schwellwert hätte auch das weiße FRITZ!Box-Gehäuse im Logo durchsichtig gemacht.
