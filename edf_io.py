"""Laden einer EDF/BDF-Datei und Export von Zeitblöcken als eigene EDF-Dateien.

Die Signale werden als *digitale* Rohwerte gehalten und auch digital wieder
geschrieben. Dadurch sind die exportierten Blöcke bit-genau identisch mit dem
Original (keine Rundungsfehler durch Umrechnen physikalisch <-> digital).

Optional ("für SMARTLAB") wird jeder Block zusätzlich so vorbereitet, dass
SMARTLAB ihn ohne eigene Aufrufe externer Programme laden kann: WFDB-Record
(.hea/.dat) in mV, kurze Namen und fertige ecgpuwave-Annotationen (.atr1, .atr2, …).
"""
from __future__ import annotations

import csv
import glob
import math
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

import numpy as np
import pyedflib

try:  # private Hilfsfunktion von pyedflib -> gleiche Datensatzlänge wie der Writer
    from pyedflib.edfwriter import _calculate_record_duration
except Exception:  # pragma: no cover
    _calculate_record_duration = None

# pyedflib rechnet Sekundenbruchteile der Startzeit falsch um (Faktor 100 statt 10
# bei edflib-Einheiten von 100 ns): beim Schreiben werden sie verworfen, beim Lesen
# um Faktor 10 zu klein. Deshalb wird hier direkt die edflib-Funktion benutzt.
from pyedflib._extensions._pyedflib import set_starttime_subsecond


# --------------------------------------------------------------------------- #
# Datenstrukturen
# --------------------------------------------------------------------------- #
@dataclass
class Channel:
    label: str
    unit: str
    fs: float
    digital: np.ndarray
    physical_min: float
    physical_max: float
    digital_min: int
    digital_max: int
    prefilter: str = ""
    transducer: str = ""

    @property
    def n_samples(self) -> int:
        return int(self.digital.shape[0])

    @property
    def gain(self) -> float:
        return (self.physical_max - self.physical_min) / (self.digital_max - self.digital_min)

    def to_physical(self, digital: np.ndarray) -> np.ndarray:
        return ((digital.astype(np.float32) - self.digital_min) * self.gain
                + self.physical_min).astype(np.float32)

    def physical(self, i0: int, i1: int) -> np.ndarray:
        return self.to_physical(self.digital[i0:i1])

    def writer_header(self) -> dict:
        return {
            "label": self.label,
            "dimension": self.unit,
            "sample_frequency": self.fs,
            "physical_max": self.physical_max,
            "physical_min": self.physical_min,
            "digital_max": self.digital_max,
            "digital_min": self.digital_min,
            "prefilter": self.prefilter,
            "transducer": self.transducer,
        }


@dataclass
class Recording:
    path: str
    header: dict
    channels: list[Channel]
    duration: float
    annotations: list[tuple[float, float, str]] = field(default_factory=list)
    filetype: int = pyedflib.FILETYPE_EDFPLUS

    @property
    def start(self) -> datetime:
        return self.header["startdate"]

    @property
    def basename(self) -> str:
        return os.path.splitext(os.path.basename(self.path))[0]

    @property
    def extension(self) -> str:
        return os.path.splitext(self.path)[1] or ".edf"

    @property
    def max_fs(self) -> float:
        return max(ch.fs for ch in self.channels)

    def record_duration(self) -> float:
        """Datensatzlänge (s), die der EDF-Writer für diese Abtastraten wählt."""
        freqs = [ch.fs for ch in self.channels]
        if _calculate_record_duration is not None:
            try:
                return float(_calculate_record_duration(freqs))
            except Exception:
                pass
        return 1.0


@dataclass
class Block:
    nr: int
    start_id: int          # 0 = Dateianfang, sonst ID des Schnitts am Blockanfang
    start: float           # Sekunden ab Aufnahmebeginn
    end: float
    name: str = ""
    export: bool = True

    @property
    def duration(self) -> float:
        return self.end - self.start


# --------------------------------------------------------------------------- #
# Laden
# --------------------------------------------------------------------------- #
def load_edf(path: str) -> Recording:
    f = pyedflib.EdfReader(path)
    try:
        header = f.getHeader()
        header.pop("gender", None)  # veralteter Schlüssel, Writer will "sex"
        # Startzeit inkl. korrekter Sekundenbruchteile (edflib: Einheiten von 100 ns)
        header["startdate"] = header["startdate"].replace(
            microsecond=int(getattr(f, "starttime_subsecond", 0)) // 10)
        channels: list[Channel] = []
        for i in range(f.signals_in_file):
            sh = f.getSignalHeader(i)
            dig = f.readSignal(i, digital=True)
            dmin, dmax = int(sh["digital_min"]), int(sh["digital_max"])
            # EDF = 16 bit -> int16 spart die Hälfte Speicher, BDF bleibt int32
            if dmin >= -32768 and dmax <= 32767 and (dig.size == 0 or
                                                     (dig.min() >= -32768 and dig.max() <= 32767)):
                dig = dig.astype(np.int16)
            channels.append(Channel(
                label=sh["label"], unit=sh["dimension"], fs=float(sh["sample_frequency"]),
                digital=dig, physical_min=float(sh["physical_min"]),
                physical_max=float(sh["physical_max"]), digital_min=dmin, digital_max=dmax,
                prefilter=sh.get("prefilter", ""), transducer=sh.get("transducer", ""),
            ))
        onsets, durations, texts = f.readAnnotations()
        annotations = [(float(o), float(d), str(t)) for o, d, t in zip(onsets, durations, texts)]
        duration = float(f.file_duration)
        filetype = int(getattr(f, "filetype", pyedflib.FILETYPE_EDFPLUS))
    finally:
        f.close()
    return Recording(path=path, header=header, channels=channels, duration=duration,
                     annotations=annotations, filetype=filetype)


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #
@dataclass
class ExportResult:
    block: Block
    path: str
    start: float          # tatsächlich exportierter Bereich
    end: float
    error: str | None = None
    smartlab: "SmartlabResult | None" = None

    @property
    def trimmed(self) -> float:
        """Wie viele Sekunden am Blockende abgeschnitten wurden (Datensatzraster)."""
        return max(0.0, self.block.end - self.end)


def export_block(rec: Recording, t0: float, t1: float, out_path: str,
                 smartlab: bool = False) -> tuple[float, float]:
    """Schreibt den Bereich [t0, t1) als eigene EDF-Datei.

    EDF speichert Daten in Datensätzen fester Länge (meist 1 s). Ist der Block
    kein ganzzahliges Vielfaches davon, wird der Rest am Ende weggelassen
    (statt ihn - wie pyedflib es täte - mit Nullen aufzufüllen).
    smartlab=True: reines EDF ohne Annotationen, Spannungskanäle in mV
    (die digitalen Rohwerte bleiben unverändert, nur die Einheitenangabe wird umgerechnet).
    Rückgabe: tatsächlich geschriebener Bereich (start, ende) in Sekunden.
    """
    rd = rec.record_duration()
    n_records = math.floor((t1 - t0) / rd + 1e-6)
    starts = []
    for ch in rec.channels:
        i0 = int(round(t0 * ch.fs))
        per_rec = int(round(rd * ch.fs))
        n_records = min(n_records, (ch.n_samples - i0) // per_rec)
        starts.append((i0, per_rec))
    if n_records < 1:
        raise ValueError(f"Block {t0:.3f}-{t1:.3f} s ist kürzer als ein EDF-Datensatz ({rd:g} s).")

    actual_t0 = starts[0][0] / rec.channels[0].fs
    actual_t1 = actual_t0 + n_records * rd
    signals = [np.ascontiguousarray(ch.digital[i0:i0 + n_records * per_rec], dtype=np.int32)
               for ch, (i0, per_rec) in zip(rec.channels, starts)]

    annotations = [(on - actual_t0, du, txt) for on, du, txt in rec.annotations
                   if actual_t0 <= on < actual_t1]

    start = rec.start + timedelta(seconds=actual_t0)

    if smartlab:
        # SMARTLAB wandelt EDF mit edf2mit um; das kommt nur mit reinem EDF zurecht
        # (EDF+ -> Annotationskanal wird zum Signal, falsche Abtastrate).
        if rec.filetype not in (pyedflib.FILETYPE_EDF, pyedflib.FILETYPE_EDFPLUS):
            raise ValueError("SMARTLAB-Export geht nur mit EDF (16 bit), nicht mit BDF.")
        annotations = []
        start = start.replace(microsecond=0)
        filetype = pyedflib.FILETYPE_EDF
        signal_headers = [smartlab_signal_header(ch) for ch in rec.channels]
    else:
        # Annotationen und Sekundenbruchteile der Startzeit gibt es nur in EDF+/BDF+
        filetype = rec.filetype
        if annotations or start.microsecond:
            filetype = {pyedflib.FILETYPE_EDF: pyedflib.FILETYPE_EDFPLUS,
                        pyedflib.FILETYPE_BDF: pyedflib.FILETYPE_BDFPLUS}.get(filetype, filetype)
        signal_headers = [ch.writer_header() for ch in rec.channels]

    header = dict(rec.header)
    header["startdate"] = start.replace(microsecond=0)

    writer = pyedflib.EdfWriter(out_path, n_channels=len(rec.channels), file_type=filetype)
    try:
        writer.setSignalHeaders(signal_headers)
        writer.setHeader(header)
        if start.microsecond:  # muss nach setHeader und vor dem ersten Schreiben passieren
            set_starttime_subsecond(writer.handle, start.microsecond * 10)
        writer.writeSamples(signals, digital=True)
        for onset, dur, txt in annotations:
            writer.writeAnnotation(onset, dur, txt)
    finally:
        writer.close()
    return actual_t0, actual_t1


def safe_filename(text: str) -> str:
    text = re.sub(r"[^\w\-]+", "_", text.strip(), flags=re.UNICODE)
    return text.strip("_")[:60]


def block_filename(rec: Recording, block: Block, smartlab: bool = False) -> str:
    if smartlab:
        return smartlab_record_name(rec, block) + ".edf"
    name = f"{rec.basename}_B{block.nr:03d}"
    if block.name.strip():
        name += "_" + safe_filename(block.name)
    return name + rec.extension


def fmt_time(seconds: float, decimals: int = 3) -> str:
    """Sekunden -> 'hh:mm:ss.mmm'."""
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    total_ms = int(round(seconds * 10 ** decimals))
    frac = total_ms % 10 ** decimals
    s = total_ms // 10 ** decimals
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    out = f"{sign}{h:02d}:{m:02d}:{s:02d}"
    if decimals > 0:
        out += f".{frac:0{decimals}d}"
    return out


def _de(x: float) -> str:
    """Zahl mit Dezimalkomma (für deutsches Excel)."""
    return f"{x:.3f}".replace(".", ",")


def export_blocks(rec: Recording, blocks: list[Block], out_dir: str,
                  progress=None, smartlab: bool = False, wfdb_bin: str | None = None,
                  tick: Callable[[str], bool] | None = None) -> list[ExportResult]:
    """Exportiert alle Blöcke mit export=True und schreibt eine Übersicht (CSV).

    smartlab=True: kurze Namen, EDF in mV und zusätzlich WFDB-Record mit
    ecgpuwave-Annotationen (siehe prepare_smartlab). wfdb_bin=None -> ohne ecgpuwave.
    tick(text) wird während langer Schritte aufgerufen; gibt es False zurück, wird abgebrochen.
    """
    os.makedirs(out_dir, exist_ok=True)
    todo = [b for b in blocks if b.export]
    results: list[ExportResult] = []
    for k, b in enumerate(todo):
        if progress is not None and progress(k, len(todo), b) is False:
            break
        path = os.path.join(out_dir, block_filename(rec, b, smartlab))
        try:
            t0, t1 = export_block(rec, b.start, b.end, path, smartlab=smartlab)
        except ValueError as exc:  # z.B. Block kürzer als ein Datensatz
            results.append(ExportResult(block=b, path="", start=b.start, end=b.start, error=str(exc)))
            continue
        res = ExportResult(block=b, path=path, start=t0, end=t1)
        results.append(res)
        if smartlab:
            note = f"Quelle: {os.path.basename(rec.path)}, Block B{b.nr}"
            if b.name.strip():
                note += f" ({b.name.strip()})"
            note += f", {fmt_time(t0)} - {fmt_time(t1)}"
            sub = None
            if tick is not None:
                sub = lambda text, _b=b: tick(f"Block B{_b.nr}: {text}")
            try:
                res.smartlab = prepare_smartlab(path, out_dir, smartlab_record_name(rec, b),
                                                wfdb_bin, comments=[note], tick=sub)
            except ExportCancelled:
                res.error = "abgebrochen (SMARTLAB-Vorbereitung unvollständig)"
                break
            except (ValueError, OSError) as exc:
                res.error = f"SMARTLAB-Vorbereitung fehlgeschlagen: {exc}"

    csv_path = os.path.join(out_dir, f"{rec.basename}_bloecke.csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        head = ["Nr", "Name", "Datei", "Start [s]", "Ende [s]", "Dauer [s]",
                "Start (rel.)", "Ende (rel.)", "Start (Uhrzeit)", "Ende (Uhrzeit)"]
        if smartlab:
            head += ["SMARTLAB-Datei", "ecgpuwave je Kanal (Schläge/P/T)", "Hinweis"]
        w.writerow(head)
        for r in results:
            if not r.path:
                continue
            row = [
                r.block.nr, r.block.name, os.path.basename(r.path),
                _de(r.start), _de(r.end), _de(r.end - r.start),
                fmt_time(r.start), fmt_time(r.end),
                (rec.start + timedelta(seconds=r.start)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                (rec.start + timedelta(seconds=r.end)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            ]
            if smartlab:
                s = r.smartlab
                row += [f"{s.name}.dat" if s else "", s.wave_summary() if s else "",
                        "; ".join(x for x in ([r.error] if r.error else []) + (s.problems() if s else []))]
            w.writerow(row)
    return results


# --------------------------------------------------------------------------- #
# SMARTLAB-Vorbereitung (WFDB-Record + ecgpuwave)
# --------------------------------------------------------------------------- #
# SMARTLAB ruft für EDF "edf2mit" und für die Wellenerkennung "ecgpuwave" (WFDB 10.4.12)
# über die Windows-Kommandozeile auf. Damit das unabhängig von PATH und Netzwerkpfaden
# klappt, legt der Export Record und Annotationen gleich selbst an; SMARTLAB überspringt
# ecgpuwave, wenn <name>.atrN schon existiert.

WFDB_MAX_NAME = 20   # WFDB 10.4.x: längere Record-Namen werden still abgeschnitten
DEFAULT_WFDB_BIN = r"C:\wfdb-10.4.12_Win_x86_32\wfdb\bin"
ECGPUWAVE_TIMEOUT = 3600.0   # s je Kanal
_TO_MV = {"uv": 1e-3, "nv": 1e-6, "mv": 1.0, "v": 1e3}
# Codes im MIT-Annotationsformat
_ANN_NORMAL, _ANN_PWAVE, _ANN_TWAVE = 1, 24, 27
_ANN_SKIP, _ANN_NUM, _ANN_SUB, _ANN_CHN, _ANN_AUX = 59, 60, 61, 62, 63


class ExportCancelled(Exception):
    pass


@dataclass
class SmartlabResult:
    name: str                                    # WFDB-Record-Name (= Dateiname ohne Endung)
    channels: list[str]
    waves: list[dict | None]                     # je Kanal {"beats", "p", "t"} oder None
    errors: list[str | None]                     # je Kanal Fehlermeldung von ecgpuwave
    skipped_reason: str | None = None            # ecgpuwave gar nicht gelaufen

    @property
    def atr_ok(self) -> int:
        return sum(w is not None for w in self.waves)

    def wave_summary(self) -> str:
        parts = []
        for i, w in enumerate(self.waves):
            parts.append(f"K{i + 1}: " + (f"{w['beats']}/{w['p']}/{w['t']}" if w else "Fehler"))
        return " | ".join(parts)

    def problems(self) -> list[str]:
        out = [self.skipped_reason] if self.skipped_reason else []
        out += [f"Kanal {i + 1}: {e}" for i, e in enumerate(self.errors) if e]
        return out


def find_wfdb_bin(hint: str | None = None) -> str | None:
    """Ordner mit ecgpuwave: `hint`, Umgebungsvariable WFDB_BIN, PATH oder Standardordner."""
    found = shutil.which("ecgpuwave")
    for d in (hint, os.environ.get("WFDB_BIN"), os.path.dirname(found) if found else None,
              DEFAULT_WFDB_BIN):
        if d and _wfdb_exe(d, "ecgpuwave"):
            return d
    return None


def _wfdb_exe(folder: str, name: str) -> str | None:
    for n in (name + ".exe", name):
        p = os.path.join(folder, n)
        if os.path.isfile(p):
            return p
    return None


def smartlab_record_name(rec: Recording, block: Block) -> str:
    """Kurzer, WFDB-tauglicher Name (nur A-Z, 0-9, _; höchstens 20 Zeichen)."""
    suffix = f"_B{block.nr:03d}"
    base = re.sub(r"[^A-Za-z0-9_]+", "_", rec.basename).strip("_") or "rec"
    return base[:WFDB_MAX_NAME - len(suffix)].rstrip("_") + suffix


def _mv_factor(unit: str) -> float | None:
    u = unit.strip().replace("\u00b5", "u").replace("\u03bc", "u").lower()
    return _TO_MV.get(u)


def smartlab_signal_header(ch: Channel) -> dict:
    """EDF-Signalheader mit Spannung in mV (nur die physikalischen Grenzen werden umgerechnet)."""
    h = ch.writer_header()
    f = _mv_factor(ch.unit)
    if f is not None and f != 1.0:
        h["physical_min"] = float(f"{ch.physical_min * f:.10g}")
        h["physical_max"] = float(f"{ch.physical_max * f:.10g}")
        h["dimension"] = "mV"
    return h


def _ascii(text: str) -> str:
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("Ä", "Ae"), ("Ö", "Oe"), ("Ü", "Ue"), ("ß", "ss")):
        text = text.replace(a, b)
    return text.encode("ascii", "replace").decode("ascii")


def write_wfdb_record(edf_path: str, out_dir: str, name: str, comments=()) -> int:
    """Schreibt <name>.hea/<name>.dat (Format 16, Spannung in mV) aus einer EDF-Datei.

    Die digitalen Rohwerte werden unverändert übernommen. Rückgabe: Samples je Kanal.
    """
    rec = load_edf(edf_path)
    fss = {ch.fs for ch in rec.channels}
    if len(fss) != 1:
        raise ValueError("SMARTLAB braucht in allen Kanälen dieselbe Abtastrate.")
    fs = fss.pop()
    n = min(ch.n_samples for ch in rec.channels)
    labels = [re.sub(r"\s+", "_", _ascii(ch.label).strip()) or f"Kanal{i + 1}"
              for i, ch in enumerate(rec.channels)]
    if len(set(labels)) < len(labels):   # z.B. dreimal "ECG" -> in SMARTLAB unterscheidbar machen
        labels = [f"{lab}_{i + 1}" for i, lab in enumerate(labels)]

    lines = [f"{name} {len(rec.channels)} {fs:.12g} {n} {rec.start:%H:%M:%S} {rec.start:%d/%m/%Y}"]
    data = np.empty((n, len(rec.channels)), dtype="<i2")
    for i, ch in enumerate(rec.channels):
        x = ch.digital[:n]
        if x.size and (int(x.min()) < -32768 or int(x.max()) > 32767):
            raise ValueError("Werte passen nicht in 16 bit (BDF?).")
        data[:, i] = x
        f = _mv_factor(ch.unit)
        unit = "mV" if f is not None else (re.sub(r"\s+", "_", _ascii(ch.unit)) or "NU")
        f = f if f is not None else 1.0
        prange = (ch.physical_max - ch.physical_min) * f
        if prange == 0 or ch.digital_max == ch.digital_min:
            raise ValueError(f"Kanal {ch.label}: ungültige Kalibrierung im EDF-Header.")
        gain = (ch.digital_max - ch.digital_min) / prange           # ADU je mV
        baseline = int(round(ch.digital_min - ch.physical_min * f * gain))
        adcres = max(1, math.ceil(math.log2(ch.digital_max - ch.digital_min + 1)))
        adczero = int((ch.digital_max + ch.digital_min) / 2)
        init = int(x[0]) if x.size else 0
        checksum = int(np.sum(x, dtype=np.int64)) & 0xFFFF
        checksum -= 65536 if checksum >= 32768 else 0
        lines.append(f"{name}.dat 16 {gain:.10g}({baseline})/{unit} {adcres} {adczero} "
                     f"{init} {checksum} 0 {labels[i]}")
    lines += ["#" + _ascii(c) for c in comments]

    data.tofile(os.path.join(out_dir, name + ".dat"))
    with open(os.path.join(out_dir, name + ".hea"), "w", encoding="ascii", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    return n


def count_waves(atr_path: str) -> dict:
    """Zählt Schläge (N), P- und T-Wellen in einer MIT-Annotationsdatei."""
    words = np.fromfile(atr_path, dtype="<u2")
    counts: Counter = Counter()
    i = 0
    while i < len(words):
        w = int(words[i])
        code, val = w >> 10, w & 0x3FF
        if code == 0 and val == 0:
            break
        if code == _ANN_SKIP:
            i += 3
            continue
        if code == _ANN_AUX:
            i += 1 + (val + 1) // 2
            continue
        if code not in (_ANN_NUM, _ANN_SUB, _ANN_CHN):
            counts[code] += 1
        i += 1
    return {"beats": counts[_ANN_NORMAL], "p": counts[_ANN_PWAVE], "t": counts[_ANN_TWAVE]}


def _run_ecgpuwave(exe: str, work_dir: str, name: str, signal: int,
                   tick: Callable[[], bool] | None) -> str | None:
    """Startet ecgpuwave für einen Kanal. Rückgabe: None oder Fehlermeldung."""
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(exe) + os.pathsep + env.get("PATH", "")
    env["WFDB"] = "."            # Record nur im Arbeitsordner suchen, nie im Netz
    ext = f"atr{signal + 1}"
    log_path = os.path.join(work_dir, f"ecgpuwave_{ext}.log")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with open(log_path, "wb") as log:
        p = subprocess.Popen([exe, "-r", name, "-a", ext, "-s", str(signal)], cwd=work_dir, env=env,
                             stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                             creationflags=flags)
        waited = 0.0
        while True:
            try:
                p.wait(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                waited += 0.25
                if (tick is not None and tick() is False) or waited > ECGPUWAVE_TIMEOUT:
                    p.kill()
                    p.wait()
                    if waited > ECGPUWAVE_TIMEOUT:
                        return f"ecgpuwave nach {ECGPUWAVE_TIMEOUT:.0f} s abgebrochen"
                    raise ExportCancelled()
    atr = os.path.join(work_dir, f"{name}.{ext}")
    if p.returncode != 0:
        return f"ecgpuwave abgestürzt (Code {p.returncode})"
    if not os.path.isfile(atr) or os.path.getsize(atr) == 0:
        return "ecgpuwave hat keine Annotationen erzeugt"
    return None


def prepare_smartlab(edf_path: str, out_dir: str, name: str, wfdb_bin: str | None,
                     comments=(), tick: Callable[[str], bool] | None = None) -> SmartlabResult:
    """Legt <name>.hea/.dat und <name>.atr1… (ecgpuwave, je Kanal) in out_dir an.

    Gerechnet wird in einem lokalen Temp-Ordner (ecgpuwave hinterlässt dort Hilfsdateien),
    danach werden die Ergebnisse verschoben. Veraltete SMARTLAB-Dateien desselben Namens
    (<name>m.mat/.hea, .ntr*, .atr*) werden entfernt, sonst würde SMARTLAB sie statt der
    neuen Daten laden.
    """
    work = tempfile.mkdtemp(prefix="smartlab_")
    try:
        if tick is not None and tick("WFDB-Record schreiben…") is False:
            raise ExportCancelled()
        write_wfdb_record(edf_path, work, name, comments)
        labels = load_edf_header_labels(edf_path)
        nsig = len(labels)
        res = SmartlabResult(name=name, channels=labels, waves=[None] * nsig, errors=[None] * nsig)
        exe = _wfdb_exe(wfdb_bin, "ecgpuwave") if wfdb_bin else None
        if exe is None:
            res.skipped_reason = "ecgpuwave nicht gefunden – SMARTLAB rechnet beim Öffnen selbst"
        else:
            for s in range(nsig):
                sub = None
                if tick is not None:
                    sub = lambda _s=s: tick(f"ecgpuwave Kanal {_s + 1}/{nsig}…")
                    if sub() is False:
                        raise ExportCancelled()
                res.errors[s] = _run_ecgpuwave(exe, work, name, s, sub)
                if res.errors[s] is None:
                    res.waves[s] = count_waves(os.path.join(work, f"{name}.atr{s + 1}"))

        pattern = glob.escape(os.path.join(out_dir, name))
        for old in glob.glob(pattern + "m.mat") + glob.glob(pattern + "m.hea") + \
                glob.glob(pattern + ".ntr*") + glob.glob(pattern + ".atr*"):
            os.remove(old)
        keep = [f"{name}.hea", f"{name}.dat"] + [f"{name}.atr{s + 1}" for s in range(nsig)
                                                 if res.errors[s] is None and exe is not None]
        for fn in keep:
            dst = os.path.join(out_dir, fn)
            if os.path.exists(dst):
                os.remove(dst)
            shutil.move(os.path.join(work, fn), dst)
        return res
    finally:
        shutil.rmtree(work, ignore_errors=True)


def load_edf_header_labels(path: str) -> list[str]:
    f = pyedflib.EdfReader(path)
    try:
        return list(f.getSignalLabels())
    finally:
        f.close()
