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
| Export für SMARTLAB | Häkchen „Export für SMARTLAB“ setzen, dann exportieren (siehe unten) |

## Was beim Export entsteht

- `<datei>_B001_<Name>.edf`, `<datei>_B002.edf`, … eine Datei je Block mit Export-Häkchen
- `<datei>_bloecke.csv`: Übersicht mit Start, Ende, Dauer und Uhrzeit (Trennzeichen `;`, Dezimalkomma, öffnet sich direkt in Excel)
- `<datei>_schnitte.json`: die Schnitte zum späteren Wiederladen

Die Signaldaten werden **bit-genau** übernommen: Die digitalen Rohwerte werden 1:1 kopiert, es gibt keine Umrechnung und keine Rundung. Kanalnamen, Einheiten, Kalibrierung und Patientendaten bleiben erhalten. Die Startzeit jedes Blocks wird auf die Millisekunde angepasst. Annotationen im Blockbereich werden mit verschobener Zeit übernommen.

### Hinweis zu „Schnitte auf ganze Sekunden“

EDF speichert Daten in Datensätzen fester Länge, hier 1 s. Mit der Option (Standard) rasten Schnitte auf ganze Sekunden ein, dann ist jeder Block exakt so lang wie markiert. Ohne die Option kann sekundengenau geschnitten werden. Ein Rest unter 1 s am Blockende wird dann beim Export weggelassen, nicht mit Nullen aufgefüllt. Die Export-Meldung zeigt, welche Blöcke betroffen sind.

## Export für SMARTLAB

Mit dem Häkchen **„Export für SMARTLAB“** werden die Blöcke so vorbereitet, dass SMARTLAB sie ohne weitere Schritte lädt. Am SMARTLAB-Code muss nichts geändert werden. Die Einstellung bleibt beim nächsten Start erhalten.

**In SMARTLAB:** die `.dat`-Datei des Blocks öffnen oder auf `SMARTlab.exe` ziehen.

Je Block entstehen:

| Datei | Inhalt |
|---|---|
| `<datei>_B002.edf` | der Block als reines EDF, Spannung in mV |
| `<datei>_B002.hea` / `.dat` | derselbe Block im WFDB-Format (das liest SMARTLAB intern) |
| `<datei>_B002.atr1`, `.atr2`, … | fertige ecgpuwave-Annotationen (P, QRS, T) je Kanal |

Was dabei anders ist als beim normalen Export und warum:

- **Kurze Dateinamen**, höchstens 20 Zeichen, nur Buchstaben, Ziffern und `_`, z. B. `HRV_Dive_001_B002`. Die von SMARTLAB benutzte WFDB-Version 10.4.12 schneidet längere Namen still ab, dann findet SMARTLAB seine Dateien nicht. Der Blockname steht weiter in der `_bloecke.csv`.
- **Spannung in mV statt µV.** Das ecgpuwave von SMARTLAB stürzt bei Aufnahmen in µV ab. Umgerechnet wird nur die Einheitenangabe im Header, die digitalen Rohwerte bleiben bit-genau.
- **Reines EDF ohne Annotationen.** Mit EDF+ macht SMARTLABs Umwandlung (edf2mit) den Annotationskanal zu einem Signal mit 1 Hz.
- **Fertige `.atr`-Dateien.** SMARTLAB ruft ecgpuwave nur auf, wenn noch keine `.atrN` existiert. So braucht SMARTLAB keine externen Programme und funktioniert auch, wenn WFDB nicht im PATH liegt oder die Datei über einen Netzwerkpfad (`\\server\…`) geöffnet wird.
- Haben alle Kanäle denselben Namen (z. B. dreimal `ECG`), heißen sie im `.hea` `ECG_1`, `ECG_2`, `ECG_3`, damit man sie in SMARTLAB unterscheiden kann.

Die Export-Meldung und die `_bloecke.csv` zeigen je Kanal, wie viele Schläge, P- und T-Wellen ecgpuwave gefunden hat (`K1: Schläge/P/T | K2: …`). Das hilft bei der Kanalwahl in SMARTLAB: SMARTLAB öffnet standardmäßig Kanal 1, die P-Wellen werden aber oft auf einem anderen Kanal besser erkannt.

**ecgpuwave** wird automatisch gesucht: zuerst im gespeicherten Ordner, dann in der Umgebungsvariable `WFDB_BIN`, im PATH und unter `C:\wfdb-10.4.12_Win_x86_32\wfdb\bin`. Wird es nicht gefunden, fragt das Tool nach dem Ordner. Alternativ geht es ohne Annotationen weiter, dann rechnet SMARTLAB sie beim Öffnen selbst, wofür WFDB im PATH sein muss.

**Erneut exportieren:** Gibt es zu einem Block schon SMARTLAB-Dateien (`…m.mat`, `.ntr*`), fragt das Tool nach und löscht sie, sonst würde SMARTLAB die alten Daten weiter anzeigen. Korrekturen, die in SMARTLAB an diesem Block gemacht wurden, gehen dabei verloren.

Nur EDF mit 16 bit und gleicher Abtastrate in allen Kanälen. BDF wird abgelehnt, weil SMARTLAB es nicht lesen kann.

## Dateien

- `edf_viewer.py`: Oberfläche (PyQt5 + pyqtgraph)
- `edf_io.py`: Laden und Export ohne GUI, auch aus eigenen Skripten nutzbar:

```python
from edf_io import load_edf, export_blocks, find_wfdb_bin, Block
rec = load_edf("aufnahme.edf")
blocks = [Block(1, 0, 0, 300, "Ruhe"), Block(2, 1, 300, 900, "Belastung")]
export_blocks(rec, blocks, "ausgabe")
export_blocks(rec, blocks, "ausgabe_smartlab", smartlab=True, wfdb_bin=find_wfdb_bin())
```
