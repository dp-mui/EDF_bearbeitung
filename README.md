# EDF Schnitt-Tool

Mit diesem Tool siehst du dir EDF/BDF-Aufnahmen an (z. B. ein 3-Kanal-EKG), schneidest sie und teilst sie in Blöcke ein. Jeder Block wird als eigene EDF-Datei exportiert.

## Start

```
pip install -r requirements.txt
python edf_viewer.py              # dann Datei öffnen (Strg+O) oder ins Fenster ziehen
python edf_viewer.py aufnahme.edf
```

## Bedienung

| Aktion | Wie |
|---|---|
| Durch die Zeit bewegen | Scrollleiste, ←/→ (Shift = ganze Seite), Bild↑/↓, Pos1/Ende, in den Kurven ziehen |
| Zoom | Mausrad, +/−, Feld „Fenster“ in der Werkzeugleiste |
| Übersicht | Unterer Streifen: gelben Bereich ziehen oder daneben klicken |
| Schnitt setzen | **Doppelklick** in eine Kurve oder Taste **S** (an der Mausposition) |
| Schnitt verschieben | Orange Linie ziehen |
| Schnitt löschen | **Rechtsklick** auf die Linie oder **Entf**, wenn die Maus darüber ist |
| Gleichmäßig einteilen | „Automatisch einteilen…“ (z. B. alle 60 s) |
| Block benennen | Doppelklick in die Spalte „Name“ (der Name kommt in den Dateinamen) |
| Block weglassen | Häkchen „Export“ entfernen (der Block wird grau hinterlegt) |
| Blöcke zusammenführen | Block wählen → „Mit nächstem verbinden“ |
| Arbeit sichern | „Schnitte speichern“ (Strg+S) legt `<datei>_schnitte.json` neben die EDF. Beim nächsten Öffnen wird das Laden angeboten. |
| Export | „Blöcke exportieren…“ (Strg+E) und dann einen Zielordner wählen |

## Was beim Export entsteht

- `<datei>_B001_<Name>.edf`, `<datei>_B002.edf`, … eine Datei je Block mit Export-Häkchen
- `<datei>_bloecke.csv`: Übersicht mit Start, Ende, Dauer und Uhrzeit (Trennzeichen `;`, Dezimalkomma, öffnet sich direkt in Excel)
- `<datei>_schnitte.json`: die Schnitte zum späteren Wiederladen

Die Signaldaten werden **bit-genau** übernommen: Die digitalen Rohwerte werden 1:1 kopiert, es gibt keine Umrechnung und keine Rundung. Kanalnamen, Einheiten, Kalibrierung und Patientendaten bleiben erhalten. Die Startzeit jedes Blocks wird auf die Millisekunde angepasst. Annotationen im Blockbereich werden mit verschobener Zeit übernommen.

### Hinweis zu „Schnitte auf ganze Sekunden“

EDF speichert Daten in Datensätzen fester Länge, hier 1 s. Mit der Option (Standard) rasten Schnitte auf ganze Sekunden ein, dann ist jeder Block exakt so lang wie markiert. Ohne die Option kann sekundengenau geschnitten werden. Ein Rest unter 1 s am Blockende wird dann beim Export weggelassen, nicht mit Nullen aufgefüllt. Die Export-Meldung zeigt, welche Blöcke betroffen sind.

## Dateien

- `edf_viewer.py`: Oberfläche (PyQt5 + pyqtgraph)
- `edf_io.py`: Laden und Export ohne GUI, auch aus eigenen Skripten nutzbar:

```python
from edf_io import load_edf, export_blocks, Block
rec = load_edf("aufnahme.edf")
export_blocks(rec, [Block(1, 0, 0, 300, "Ruhe"), Block(2, 1, 300, 900, "Belastung")], "ausgabe")
```
