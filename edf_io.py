"""Laden einer EDF/BDF-Datei und Export von Zeitblöcken als eigene EDF-Dateien.

Die Signale werden als *digitale* Rohwerte gehalten und auch digital wieder
geschrieben. Dadurch sind die exportierten Blöcke bit-genau identisch mit dem
Original (keine Rundungsfehler durch Umrechnen physikalisch <-> digital).
"""
from __future__ import annotations

import csv
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

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

    @property
    def trimmed(self) -> float:
        """Wie viele Sekunden am Blockende abgeschnitten wurden (Datensatzraster)."""
        return max(0.0, self.block.end - self.end)


def export_block(rec: Recording, t0: float, t1: float, out_path: str) -> tuple[float, float]:
    """Schreibt den Bereich [t0, t1) als eigene EDF-Datei.

    EDF speichert Daten in Datensätzen fester Länge (meist 1 s). Ist der Block
    kein ganzzahliges Vielfaches davon, wird der Rest am Ende weggelassen
    (statt ihn - wie pyedflib es täte - mit Nullen aufzufüllen).
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

    # Annotationen und Sekundenbruchteile der Startzeit gibt es nur in EDF+/BDF+
    filetype = rec.filetype
    if annotations or start.microsecond:
        filetype = {pyedflib.FILETYPE_EDF: pyedflib.FILETYPE_EDFPLUS,
                    pyedflib.FILETYPE_BDF: pyedflib.FILETYPE_BDFPLUS}.get(filetype, filetype)

    header = dict(rec.header)
    header["startdate"] = start.replace(microsecond=0)

    writer = pyedflib.EdfWriter(out_path, n_channels=len(rec.channels), file_type=filetype)
    try:
        writer.setSignalHeaders([ch.writer_header() for ch in rec.channels])
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


def block_filename(rec: Recording, block: Block) -> str:
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
                  progress=None) -> list[ExportResult]:
    """Exportiert alle Blöcke mit export=True und schreibt eine Übersicht (CSV)."""
    os.makedirs(out_dir, exist_ok=True)
    todo = [b for b in blocks if b.export]
    results: list[ExportResult] = []
    for k, b in enumerate(todo):
        if progress is not None and progress(k, len(todo), b) is False:
            break
        path = os.path.join(out_dir, block_filename(rec, b))
        try:
            t0, t1 = export_block(rec, b.start, b.end, path)
        except ValueError as exc:  # z.B. Block kürzer als ein Datensatz
            results.append(ExportResult(block=b, path="", start=b.start, end=b.start, error=str(exc)))
            continue
        results.append(ExportResult(block=b, path=path, start=t0, end=t1))

    csv_path = os.path.join(out_dir, f"{rec.basename}_bloecke.csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["Nr", "Name", "Datei", "Start [s]", "Ende [s]", "Dauer [s]",
                    "Start (rel.)", "Ende (rel.)", "Start (Uhrzeit)", "Ende (Uhrzeit)"])
        for r in results:
            if r.error:
                continue
            w.writerow([
                r.block.nr, r.block.name, os.path.basename(r.path),
                _de(r.start), _de(r.end), _de(r.end - r.start),
                fmt_time(r.start), fmt_time(r.end),
                (rec.start + timedelta(seconds=r.start)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                (rec.start + timedelta(seconds=r.end)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            ])
    return results
