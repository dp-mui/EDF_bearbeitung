#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prd_fenster_schneiden.py
========================
Schneidet eine in SMART-lab annotierte Aufzeichnung samplegenau in
überlappende Fenster (Standard: 5 min Fensterlänge, 1 min Schrittweite),
damit jedes Fenster einzeln in SMART-lab ausgewertet werden kann.

Alle Dateien mit Zeitpositionen werden mitgeschnitten und verschoben:
  <rec>.hea / <rec>.dat     WFDB-Header + Signal (Format 16, Startzeit + Prüfsummen neu)
  <rec>.atrN                WFDB-Annotationen (automatische Delineation, 0-basiert)
  <rec>.ntrN                SMART-lab-Schlagliste inkl. manueller Korrekturen (1-basiert)
  <rec>.rri                 libRASCH-RR-Liste (1-basiert)
  <rec>m.hea / <rec>m.mat   wfdb2mat-Kopie des Signals
  <rec>.lead                Ableitung/Algorithmus-Einstellung (unverändert kopiert)
Nicht übernommen werden fort.20/fort.21 (Zwischenausgaben von ecgpuwave, die bei
jeder Auswertung neu geschrieben werden).

Regeln an den Fenstergrenzen
  * Ein Schlag gehört zum Fenster, wenn sein R-Zacken im Fenster liegt.
  * Eine P- oder T-Welle, die über den Fensterrand hinausragt, wird im Fenster
    auf 0 gesetzt (= "nicht vorhanden", so wie SMART-lab es selbst kodiert).
  * QRS-Beginn bzw. -Ende außerhalb des Fensters -> 0, der R-Zacken bleibt.
  * Werte, die schon im Original außerhalb der Aufzeichnung lagen, werden nur
    verschoben, sonst unverändert übernommen.
  * Ein unvollständiges letztes Fenster wird verworfen (--keep-partial behält es).

Aufruf
  python prd_fenster_schneiden.py  PFAD/HRV_Dive_001  --out PFAD/fenster
  Optionen:  --window 300  --step 60   (Sekunden)   --keep-partial

Benötigt: Python 3.8+ und numpy.
"""
import argparse
import csv
import datetime as dt
import glob
import os
import re
import struct
import sys
from collections import Counter

import numpy as np

# WFDB-Annotationscodes
SKIP, NUM, SUB, CHN, AUX = 59, 60, 61, 62, 63
WFON, WFOFF = 39, 40                     # '(' und ')'
BEAT_CODES = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 25, 30, 34, 35, 38, 41}
BEAT_CHARS = set("NLRaVFJASEj/QBfen?r")  # dieselben Typen als Zeichen (für .ntr/.rri)


# --------------------------------------------------------------------------
# WFDB-Header
# --------------------------------------------------------------------------
class Header:
    pass


def read_header(path):
    raw = open(path, 'rb').read().decode('latin-1')
    h = Header()
    h.eol = '\r\n' if '\r\n' in raw else '\n'
    lines = raw.split(h.eol)
    h.trailing_eol = lines[-1] == ''
    if h.trailing_eol:
        lines = lines[:-1]
    i = 0
    h.pre = []
    while lines[i].startswith('#') or not lines[i].strip():
        h.pre.append(lines[i])
        i += 1
    h.rec_tokens = lines[i].split()
    h.name = h.rec_tokens[0]
    h.nsig = int(h.rec_tokens[1])
    h.fs = float(re.split(r'[/(]', h.rec_tokens[2])[0])
    h.n = int(h.rec_tokens[3])
    h.time = h.rec_tokens[4] if len(h.rec_tokens) > 4 else None
    h.date = h.rec_tokens[5] if len(h.rec_tokens) > 5 else None
    h.sig = [lines[i + 1 + k].split(' ', 8) for k in range(h.nsig)]
    h.info = lines[i + 1 + h.nsig:]
    h.start = parse_start(h.time, h.date)
    return h


def parse_start(time_s, date_s):
    if not time_s:
        return None
    parts = [float(p) for p in time_s.split(':')]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    if date_s:
        d, m, y = (int(x) for x in date_s.split('/'))
        base = dt.datetime(y, m, d)
    else:
        base = dt.datetime(1900, 1, 1)
    return base + dt.timedelta(hours=parts[0], minutes=parts[1], seconds=parts[2])


def fmt_time(t, force_ms=False):
    s = t.strftime('%H:%M:%S')
    ms = int(round(t.microsecond / 1000.0))
    if ms or force_ms:
        s += '.%03d' % ms
    return s


def checksum16(x):
    v = int(np.asarray(x, dtype=np.int64).sum())
    return ((v + 32768) % 65536) - 32768


def build_header(h, name, datfile, frames, start, info_lines=None):
    tok = list(h.rec_tokens)
    tok[0] = name
    tok[3] = str(len(frames))
    if start is not None and len(tok) > 4:
        tok[4] = fmt_time(start)
        if len(tok) > 5:
            tok[5] = start.strftime('%d/%m/%Y')
    lines = list(h.pre) + [' '.join(tok)]
    for k, s in enumerate(h.sig):
        s = list(s)
        s[0] = datfile
        if len(s) > 5:
            s[5] = str(int(frames[0, k])) if len(frames) else '0'
        if len(s) > 6:
            s[6] = str(checksum16(frames[:, k]))
        lines.append(' '.join(s))
    lines += list(h.info if info_lines is None else info_lines)
    txt = h.eol.join(lines) + (h.eol if h.trailing_eol else '')
    return txt.encode('latin-1')


def load_frames(h, folder):
    files = {s[0] for s in h.sig}
    fmts = {s[1] for s in h.sig}
    if len(files) != 1:
        raise ValueError('Signale in mehreren Dateien werden nicht unterstützt: %s' % files)
    fmt = fmts.pop() if len(fmts) == 1 else None
    m = re.fullmatch(r'16(?:\+(\d+))?', fmt or '')
    if not m:
        raise ValueError('Nur WFDB-Format 16 wird unterstützt (gefunden: %s)' % fmts)
    offset = int(m.group(1) or 0)
    path = os.path.join(folder, files.pop())
    need = offset + h.n * h.nsig * 2
    if os.path.getsize(path) < need:
        raise ValueError('%s ist kürzer als im Header angegeben' % path)
    data = np.memmap(path, dtype='<i2', mode='r', offset=offset, shape=(h.n, h.nsig))
    return path, offset, data


# --------------------------------------------------------------------------
# WFDB-Annotationen (.atrN) – eigener Codec, byte-genau zum Original
# --------------------------------------------------------------------------
def read_ann(path):
    b = open(path, 'rb').read()
    w = np.frombuffer(b[:len(b) // 2 * 2], dtype='<u2').astype(np.int64)
    anns, t, chan, num, i = [], 0, 0, 0, 0
    cur = None
    while i < len(w):
        A, I = int(w[i]) >> 10, int(w[i]) & 1023
        if A == 0 and I == 0:
            break
        if A == SKIP:
            val = (int(w[i + 1]) << 16) | int(w[i + 2])
            if val >= 2 ** 31:
                val -= 2 ** 32
            t += val
            i += 3
            continue
        if A == NUM and cur is not None:
            cur['num'] = num = I
        elif A == SUB and cur is not None:
            cur['sub'] = I
        elif A == CHN and cur is not None:
            cur['chan'] = chan = I
        elif A == AUX and cur is not None:
            nb = I
            raw = b[2 * (i + 1): 2 * (i + 1) + nb]
            cur['aux'] = raw
            i += (nb + 1) // 2
        else:
            t += I
            cur = {'t': t, 'type': A, 'sub': 0, 'chan': chan, 'num': num, 'aux': None}
            anns.append(cur)
        i += 1
    return anns


def encode_ann(anns):
    out = bytearray()
    prev_t, pchan, pnum = 0, 0, 0
    for a in anns:
        d = a['t'] - prev_t
        if d > 1023 or d < 0:
            out += struct.pack('<H', SKIP << 10)
            dd = d & 0xFFFFFFFF
            out += struct.pack('<HH', dd >> 16, dd & 0xFFFF)
            d = 0
        out += struct.pack('<H', (a['type'] << 10) | d)
        if a['sub']:
            out += struct.pack('<H', (SUB << 10) | (a['sub'] & 1023))
        if a['chan'] != pchan:
            out += struct.pack('<H', (CHN << 10) | (a['chan'] & 1023))
            pchan = a['chan']
        if a['num'] != pnum:
            out += struct.pack('<H', (NUM << 10) | (a['num'] & 1023))
            pnum = a['num']
        if a['aux']:
            aux = a['aux']
            out += struct.pack('<H', (AUX << 10) | len(aux)) + aux
            if len(aux) % 2:
                out += b'\x00'
        prev_t = a['t']
    out += b'\x00\x00'
    return bytes(out)


def cut_ann(anns, s0, s1, stats):
    """Behält Wellen-Gruppen '(' ... ')', die ganz im Fenster liegen.
    Ragt eine QRS-Gruppe über den Rand, bleibt nur der Schlag selbst."""
    head = [a for a in anns if a['t'] == 0 and a['aux'] and a['aux'].startswith(b'##')]
    groups, cur = [], None
    for a in anns:
        if any(a is x for x in head):
            continue
        if a['type'] == WFON:
            if cur:
                groups.append(cur)
            cur = [a]
        elif cur is not None:
            cur.append(a)
            if a['type'] == WFOFF:
                groups.append(cur)
                cur = None
        else:
            groups.append([a])
    if cur:
        groups.append(cur)
    kept = [dict(a) for a in head]
    for g in groups:
        inside = [s0 <= a['t'] < s1 for a in g]
        if all(inside):
            sel = g
        else:
            sel = [a for a, ok in zip(g, inside) if ok and a['type'] in BEAT_CODES]
            if any(inside):
                stats['atr: Welle am Rand entfernt' if not sel else 'atr: QRS-Klammer am Rand entfernt'] += 1
        for a in sel:
            b = dict(a)
            b['t'] = a['t'] - s0
            kept.append(b)
    return kept


# --------------------------------------------------------------------------
# SMART-lab .ntrN  (28-Byte-Kopf, je Schlag 39 Byte:
#   P(on,peak,off) Flag | QRS(on,R,off) Label | T(on,peak,off) Flag ; int32, 1-basiert)
# --------------------------------------------------------------------------
def read_ntr(path):
    b = open(path, 'rb').read()
    hsize = struct.unpack('<i', b[16:20])[0]
    if hsize < 20 or (len(b) - hsize) % 39:
        raise ValueError('%s: unbekanntes .ntr-Format' % path)
    recs = []
    for off in range(hsize, len(b), 39):
        r = b[off:off + 39]
        recs.append([list(struct.unpack('<3i', r[0:12])), r[12],
                     list(struct.unpack('<3i', r[13:25])), r[25],
                     list(struct.unpack('<3i', r[26:38])), r[38]])
    return b[:hsize], recs


def encode_ntr(hdr, recs):
    n_on = sum(1 for r in recs if r[2][0] != 0)   # reproduziert Kopffeld (z. B. 551 bzw. 563)
    out = bytearray(hdr[:8] + struct.pack('<ii', 12 * n_on, n_on) + hdr[16:])
    for P, pf, Q, ql, T, tf in recs:
        out += struct.pack('<3i', *P) + bytes([pf]) + struct.pack('<3i', *Q) + bytes([ql]) \
            + struct.pack('<3i', *T) + bytes([tf])
    return bytes(out)


def ntr_r(Q):
    return Q[1] if Q[1] else next((v for v in Q if v), 0)


def cut_ntr(recs, s0, s1, n_total, stats):
    n = s1 - s0
    out = []

    def in_orig(v):
        return 1 <= v <= n_total

    def in_win(v):
        return 1 <= v <= n

    for P, pf, Q, ql, T, tf in recs:
        R = ntr_r(Q)
        if not (s0 < R <= s1):
            continue
        nP = [v - s0 if v else 0 for v in P]
        nQ = [v - s0 if v else 0 for v in Q]
        nT = [v - s0 if v else 0 for v in T]
        if any(o and in_orig(o) and not in_win(v) for o, v in zip(P, nP)):
            nP, pf = [0, 0, 0], 0
            stats['ntr: P-Welle am Rand auf 0'] += 1
        if any(o and in_orig(o) and not in_win(v) for o, v in zip(T, nT)):
            nT, tf = [0, 0, 0], 0
            stats['ntr: T-Welle am Rand auf 0'] += 1
        for j in (0, 2):
            if Q[j] and in_orig(Q[j]) and not in_win(nQ[j]):
                nQ[j] = 0
                stats['ntr: QRS-Grenze am Rand auf 0'] += 1
        out.append([nP, pf, nQ, ql, nT, tf])
    return out


# --------------------------------------------------------------------------
# libRASCH .rri (Text, 1-basiert)
# --------------------------------------------------------------------------
def read_rri(path):
    txt = open(path, 'rb').read().decode('latin-1')
    eol = '\r\n' if '\r\n' in txt else '\n'
    lines = txt.split(eol)
    trailing = lines[-1] == ''
    if trailing:
        lines = lines[:-1]
    k = next(i for i, l in enumerate(lines) if l and set(l) == {'-'})
    beats = []
    for l in lines[k + 1:]:
        m = re.match(r'\s*(\d+)(.*)$', l)
        if m:
            beats.append((int(m.group(1)), m.group(2)))
    return {'eol': eol, 'trailing': trailing, 'head': lines[:k + 1], 'beats': beats}


def encode_rri(r, beats):
    head = [re.sub(r'^(number_qrs\s*=\s*)\d+', lambda m: m.group(1) + str(len(beats)), l)
            for l in r['head']]
    lines = head + ['%d%s' % (p, rest) for p, rest in beats]
    return (r['eol'].join(lines) + (r['eol'] if r['trailing'] else '')).encode('latin-1')


# --------------------------------------------------------------------------
# Ein Segment schreiben
# --------------------------------------------------------------------------
def write_file(path, data, src):
    with open(path, 'wb') as f:
        f.write(data)
    try:
        st = os.stat(src)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    except OSError:
        pass


def companion_files(rec_path):
    folder, rec = os.path.split(rec_path)
    found = {}
    for p in glob.glob(glob.escape(rec_path) + '*'):
        base = os.path.basename(p)
        if base.startswith(rec + '.'):
            found[base[len(rec) + 1:]] = p
        elif base in (rec + 'm.hea', rec + 'm.mat'):
            found[base[len(rec):]] = p
    return found


class Record:
    def __init__(self, rec_path):
        self.path = rec_path
        self.folder, self.name = os.path.split(rec_path)
        self.files = companion_files(rec_path)
        if 'hea' not in self.files:
            raise FileNotFoundError(rec_path + '.hea nicht gefunden')
        self.h = read_header(self.files['hea'])
        self.dat_path, _, self.frames = load_frames(self.h, self.folder or '.')
        self.atr, self.ntr, self.rri, self.copy, self.skipped = {}, {}, None, {}, []
        self.mh = None
        for ext, p in sorted(self.files.items()):
            if ext in ('hea', 'm.hea', 'm.mat') or os.path.basename(p) == os.path.basename(self.dat_path):
                continue
            if re.fullmatch(r'atr\d*', ext):
                self.atr[ext] = (p, read_ann(p))
            elif re.fullmatch(r'ntr\d*', ext):
                self.ntr[ext] = (p, *read_ntr(p))
            elif ext == 'rri':
                self.rri = (p, read_rri(p))
            elif ext == 'lead':
                self.copy[ext] = p
            else:
                self.skipped.append(os.path.basename(p))
        if 'm.hea' in self.files and 'm.mat' in self.files:
            self.mh = read_header(self.files['m.hea'])
            self.mat_hdr = open(self.files['m.mat'], 'rb').read(24)
            # wfdb2mat: MAT-v4, int16, nsig Zeilen x n Spalten, Daten identisch zu .dat
            typ, mrows, ncols, imagf, namlen = struct.unpack('<5i', self.mat_hdr[:20])
            if (typ, mrows, ncols, namlen) != (30, self.h.nsig, self.h.n, 4):
                raise ValueError('m.mat hat ein unerwartetes Format')

    def write_segment(self, out_dir, s0, s1, seg_name):
        stats = Counter()
        fr = np.asarray(self.frames[s0:s1])
        start = self.h.start + dt.timedelta(seconds=s0 / self.h.fs) if self.h.start else None
        P = lambda ext: os.path.join(out_dir, seg_name + ext)
        write_file(P('.dat'), fr.tobytes(), self.dat_path)
        write_file(P('.hea'), build_header(self.h, seg_name, seg_name + '.dat', fr, start),
                   self.files['hea'])
        for ext, (p, anns) in self.atr.items():
            write_file(P('.' + ext), encode_ann(cut_ann(anns, s0, s1, stats)), p)
        counts = {}
        for ext, (p, hdr, recs) in self.ntr.items():
            seg = cut_ntr(recs, s0, s1, self.h.n, stats)
            write_file(P('.' + ext), encode_ntr(hdr, seg), p)
            counts[ext] = (len(seg), sum(1 for r in seg if chr(r[3]) not in BEAT_CHARS),
                           sum(1 for r in seg if r[4][0] and r[4][2]))
        if self.rri:
            p, r = self.rri
            beats = [(q - s0, rest) for q, rest in r['beats'] if s0 < q <= s1]
            write_file(P('.rri'), encode_rri(r, beats), p)
            counts['rri'] = (len(beats),)
        for ext, p in self.copy.items():
            write_file(P('.' + ext), open(p, 'rb').read(), p)
        if self.mh is not None:
            mname = seg_name + 'm'
            info = []
            for l in self.mh.info:
                if l.startswith('#Source:') and start is not None:
                    l = re.sub(r'record \S+', 'record ' + seg_name, l)
                    l = re.sub(r'Start: \[[^\]]*\]',
                               'Start: [%s %s]' % (fmt_time(start, True), start.strftime('%d/%m/%Y')), l)
                info.append(l)
            write_file(P('m.hea'), build_header(self.mh, mname, mname + '.mat', fr, start, info),
                       self.files['m.hea'])
            mh = self.mat_hdr[:8] + struct.pack('<i', len(fr)) + self.mat_hdr[12:24]
            write_file(P('m.mat'), mh + fr.tobytes(), self.files['m.mat'])
        return start, counts, stats


# --------------------------------------------------------------------------
# Hauptprogramm
# --------------------------------------------------------------------------
def hms(sec):
    sec = int(round(sec))
    return '%d:%02d:%02d' % (sec // 3600, sec % 3600 // 60, sec % 60)


def window_name(rec, i, s0, fs):
    sec = int(round(s0 / fs))
    return '%s_w%03d_t%03dm%02d' % (rec, i, sec // 60, sec % 60)


def main(argv=None):
    ap = argparse.ArgumentParser(description='Aufzeichnung in überlappende Fenster schneiden (SMART-lab/WFDB).')
    ap.add_argument('record', help='Pfad zur Aufzeichnung ohne Endung (z. B. daten/HRV_Dive_001)')
    ap.add_argument('--out', required=True, help='Ausgabeordner')
    ap.add_argument('--window', type=float, default=300, help='Fensterlänge in s (Standard 300)')
    ap.add_argument('--step', type=float, default=60, help='Schrittweite in s (Standard 60)')
    ap.add_argument('--keep-partial', action='store_true', help='unvollständiges letztes Fenster behalten')
    a = ap.parse_args(argv)

    rec_path = re.sub(r'\.(hea|dat)$', '', a.record)
    R = Record(rec_path)
    h = R.h
    win, step = int(round(a.window * h.fs)), int(round(a.step * h.fs))
    starts = list(range(0, max(h.n - win, 0) + 1, step)) if h.n >= win else []
    if a.keep_partial:
        nxt = (starts[-1] + step) if starts else 0
        if nxt < h.n and (not starts or starts[-1] + win < h.n):
            starts.append(nxt)
    os.makedirs(a.out, exist_ok=True)

    print('Aufzeichnung: %s  (%d Kanäle, %g Hz, %s, Start %s)' % (
        R.name, h.nsig, h.fs, hms(h.n / h.fs), h.start))
    print('Annotationen: atr=%s ntr=%s rri=%s  kopiert=%s' % (
        list(R.atr), list(R.ntr), bool(R.rri), list(R.copy)))
    if R.skipped:
        print('ACHTUNG, nicht übernommen (unbekannter Inhalt): %s' % R.skipped)
    print('Fenster: %g s, Schritt %g s -> %d Fenster' % (a.window, a.step, len(starts)))

    rows, log = [], []
    tot = Counter()
    for i, s0 in enumerate(starts, 1):
        s1 = min(s0 + win, h.n)
        name = window_name(R.name, i, s0, h.fs)
        start, counts, stats = R.write_segment(a.out, s0, s1, name)
        tot.update(stats)
        row = {'Fenster': i, 'Record': name,
               'Start_Sample': s0, 'Ende_Sample': s1,
               'Start_s': round(s0 / h.fs, 3), 'Ende_s': round(s1 / h.fs, 3),
               'Mitte_s': round((s0 + s1) / 2 / h.fs, 3),
               'Start_rel': hms(s0 / h.fs), 'Ende_rel': hms(s1 / h.fs)}
        if start:
            row['Start_Uhrzeit'] = fmt_time(start)
            row['Mitte_Uhrzeit'] = fmt_time(start + dt.timedelta(seconds=(s1 - s0) / 2 / h.fs))
            row['Ende_Uhrzeit'] = fmt_time(start + dt.timedelta(seconds=(s1 - s0) / h.fs))
        for ext, c in counts.items():
            row['Schläge_' + ext] = c[0]
            if len(c) > 1:
                row['Nicht-N_' + ext] = c[1]
                row['mit_T_' + ext] = c[2]
        rows.append(row)
        log.append('%s  %s-%s  %s' % (name, hms(s0 / h.fs), hms(s1 / h.fs),
                                      ', '.join('%s: %d' % kv for kv in sorted(stats.items())) or 'keine Randanpassung'))

    tail = h.n - (starts[-1] + win if starts else 0)
    with open(os.path.join(a.out, 'fenster_uebersicht.csv'), 'w', newline='', encoding='utf-8-sig') as f:
        cols = list(dict.fromkeys(k for r in rows for k in r))
        w = csv.DictWriter(f, fieldnames=cols, delimiter=';')
        w.writeheader()
        w.writerows(rows)
    with open(os.path.join(a.out, 'schnitt_protokoll.txt'), 'w', encoding='utf-8') as f:
        f.write('Quelle: %s\nFenster %g s, Schritt %g s, %d Fenster\n' % (rec_path, a.window, a.step, len(starts)))
        if tail > 0 and not a.keep_partial:
            f.write('Rest am Ende ohne vollständiges Fenster: %.1f s (verworfen)\n' % (tail / h.fs))
        if R.skipped:
            f.write('Nicht übernommen: %s\n' % ', '.join(R.skipped))
        f.write('\n' + '\n'.join(log) + '\n')
    print('Randanpassungen gesamt: %s' % (dict(tot) or 'keine'))
    if tail > 0 and not a.keep_partial:
        print('Rest am Ende ohne vollständiges Fenster: %.1f s (verworfen)' % (tail / h.fs))
    print('Fertig: %s' % a.out)


if __name__ == '__main__':
    main()
