#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
zeitverlauf_plot.py
===================
Trägt die SMART-lab-Ergebnisse der gleitenden Fenster über die Zeit auf, mit
Tauchphasen und Tiefenprofil, und erstellt eine Tabelle "Phase pro Fenster".

Einfachster Aufruf (phasen.csv, tauchabschnitte.csv und tiefenprofil_digitalisiert.csv
liegen im selben Ordner wie dieses Skript):

  python zeitverlauf_plot.py --ergebnisse "PFAD\\smartlab_ergebnisse.csv"

Ergebnis (im Ordner der Ergebnis-CSV, wenn --out nicht angegeben):
  zeitverlauf.png / zeitverlauf.pdf     Abbildung
  zeitverlauf_fenster_phasen.csv        Ergebnisse + Phase/Phasenanteile pro Fenster
                                        (Trennzeichen ;  Dezimalkomma -> Excel bzw. R: read.csv2)

Optionen:
  --parameter PRD,DC,MHR,SDNN,RMSSD,HF,LF/HF   welche Spalten gezeichnet werden
  --fenster-s 300                              Fensterlänge in s
  --tauch-start 22                             Minute der Aufzeichnung = 0:00 am Tauchcomputer
  --fenster PFAD\\fenster_uebersicht.csv        nur für die Gesamtlänge der Zeitachse
Ohne --ergebnisse entsteht nur die Zeitleiste (Phasen + Tiefe).

Benötigt: Python 3 mit numpy, pandas, matplotlib.
"""
import argparse
import os

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

HERE = os.path.dirname(os.path.abspath(__file__))
INK, INK2, MUTED = '#0b0b0b', '#52514e', '#8a8984'
LINE = '#2a78d6'
CAT_FILL = {'Ruhe': '#ecebe7', 'Aktivität an Land': '#f8e3d6', 'Wasser': '#dbe8f8'}
UNITS = {'PRD': 'PRD (deg²)', 'PRD_PRSA': 'PRD-PRSA (deg²)', 'DC': 'DC (ms)', 'MHR': 'Herzfrequenz (1/min)',
         'SDNN': 'SDNN (ms)', 'RMSSD': 'RMSSD (ms)', 'HF': 'HF-Power (ms²)', 'LF': 'LF-Power (ms²)',
         'VLF': 'VLF-Power (ms²)', 'ULF': 'ULF-Power (ms²)', 'LF/HF': 'LF/HF', 'HRVi': 'HRV-Index'}

plt.rcParams.update({'font.size': 9, 'axes.edgecolor': MUTED, 'axes.labelcolor': INK2,
                     'xtick.color': INK2, 'ytick.color': INK2, 'axes.linewidth': 0.8,
                     'pdf.fonttype': 42})


# --------------------------------------------------------------------------
def read_semicolon(path):
    return pd.read_csv(path, sep=';', encoding='utf-8-sig')


def read_results(path):
    df = pd.read_csv(path, sep=None, engine='python', encoding='utf-8-sig')
    if df.shape[1] == 1:
        df = pd.read_csv(path, sep=';', encoding='utf-8-sig')
    df.columns = [c.strip() for c in df.columns]
    for c in df.columns:
        if c != 'RecordName' and not pd.api.types.is_numeric_dtype(df[c]):
            conv = pd.to_numeric(df[c].astype(str).str.strip().str.replace(',', '.'), errors='coerce')
            if df[c].notna().any() and conv.notna().sum() >= 0.8 * df[c].notna().sum():
                df[c] = conv
    if 'RecordName' not in df.columns:
        raise SystemExit('Spalte "RecordName" fehlt in %s (gefunden: %s)' % (path, list(df.columns)))
    m = df['RecordName'].astype(str).str.extract(r'_w(\d+)_t(\d+)m(\d+)')
    if m[0].isna().all():
        raise SystemExit('Keine Fensternamen der Form ..._w001_t000m00 in RecordName gefunden, z. B.: %s'
                         % df['RecordName'].iloc[0])
    df.insert(1, 'Fenster', pd.to_numeric(m[0]))
    df['_start_min'] = (pd.to_numeric(m[1]) * 60 + pd.to_numeric(m[2])) / 60
    return df.sort_values('_start_min').reset_index(drop=True)


def hms(minutes):
    s = int(round(minutes * 60))
    return '%d:%02d:%02d' % (s // 3600, s % 3600 // 60, s % 60)


def hm(x, _=None):
    x = int(round(x))
    return '%d:%02d' % (x // 60, x % 60)


def overlaps(a, b, iv, label):
    """Überlappung (min) des Fensters [a,b) mit jedem Intervall."""
    out = []
    for _, r in iv.iterrows():
        o = max(0.0, min(b, r['Ende_min']) - max(a, r['Start_min']))
        if o > 1e-9:
            out.append((r[label], o))
    return out


def phase_table(res, win_min, ph, ab):
    rows = []
    for _, r in res.iterrows():
        a = r['_start_min']
        b = a + win_min
        row = {'Fenster': r['Fenster'], 'RecordName': r['RecordName'],
               'Start': hms(a), 'Ende': hms(b), 'Mitte': hms(a + win_min / 2),
               'Mitte_min': round(a + win_min / 2, 3)}
        po = overlaps(a, b, ph, 'Phase')
        if po:
            main = max(po, key=lambda x: x[1])
            row['Phase'] = main[0]
            row['Anteil_Phase_%'] = round(100 * main[1] / win_min, 1)
            row['Phasen_Anteile'] = ' | '.join('%s %.0f %%' % (p, 100 * o / win_min) for p, o in po)
            row['Fenster_ganz_in_Phase'] = 'ja' if main[1] >= win_min - 1e-6 else 'nein'
        ao = overlaps(a, b, ab, 'Abschnitt') if ab is not None else []
        if ao:
            main = max(ao, key=lambda x: x[1])
            row['Tauchabschnitt'] = main[0]
            row['Anteil_Abschnitt_%'] = round(100 * main[1] / win_min, 1)
            row['Abschnitte_Anteile'] = ' | '.join('%s %.0f %%' % (p, 100 * o / win_min) for p, o in ao)
            row['Fenster_ganz_in_Abschnitt'] = 'ja' if main[1] >= win_min - 1e-6 else 'nein'
        rows.append(row)
    t = pd.DataFrame(rows)
    rest = res.drop(columns=['Fenster', 'RecordName', '_start_min'])
    return pd.concat([t, rest], axis=1)


def phase_track(ax, ph, t_end):
    ax.set_xlim(0, t_end)
    ax.set_ylim(0, 1)
    ax.axis('off')
    level = 0
    r = ax.figure.canvas.get_renderer()
    for _, row in ph.iterrows():
        a, b = row['Start_min'], row['Ende_min']
        ax.add_patch(Rectangle((a, 0), b - a, 0.42, facecolor=CAT_FILL.get(row['Kategorie'], '#eee'),
                               edgecolor='white', linewidth=1.5))
        label = row['Kurz'] if 'Kurz' in row and isinstance(row['Kurz'], str) else row['Phase']
        txt = ax.text((a + b) / 2, 0.21, label, ha='center', va='center', fontsize=8, color=INK)
        bb = txt.get_window_extent(r).transformed(ax.transData.inverted())
        if bb.width > (b - a) * 0.95:          # passt nicht in den Block -> darüber, versetzt
            txt.set_position(((a + b) / 2, 0.6 + 0.28 * level))
            txt.set_va('bottom')
            ax.plot([(a + b) / 2] * 2, [0.42, 0.58 + 0.28 * level], color=MUTED, lw=0.6)
            level = 1 - level


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description='Zeitverlauf der SMART-lab-Parameter mit Tauchphasen.')
    ap.add_argument('--ergebnisse', help='Ergebnis-CSV aus SMART-lab')
    ap.add_argument('--phasen', default=os.path.join(HERE, 'phasen.csv'))
    ap.add_argument('--tauchabschnitte', default=os.path.join(HERE, 'tauchabschnitte.csv'))
    ap.add_argument('--tiefe', default=os.path.join(HERE, 'tiefenprofil_digitalisiert.csv'))
    ap.add_argument('--tauch-start', type=float, default=22,
                    help='Minute der Aufzeichnung, die 0:00 am Tauchcomputer entspricht (Standard 22)')
    ap.add_argument('--fenster', help='fenster_uebersicht.csv (optional, für die Länge der Zeitachse)')
    ap.add_argument('--fenster-s', type=float, default=300)
    ap.add_argument('--parameter', default='PRD,DC,MHR,SDNN,RMSSD,HF,LF/HF')
    ap.add_argument('--ende-min', type=float, default=None, help='Ende der Zeitachse in min')
    ap.add_argument('--out', help='Ausgabename ohne Endung (Standard: zeitverlauf neben der Ergebnis-CSV)')
    a = ap.parse_args()

    out = a.out or os.path.join(os.path.dirname(os.path.abspath(a.ergebnisse)) if a.ergebnisse else os.getcwd(),
                                'zeitverlauf')
    win_min = a.fenster_s / 60
    res = read_results(a.ergebnisse) if a.ergebnisse else None
    t_end = a.ende_min
    if t_end is None:
        t_end = 120.0
        if res is not None:
            t_end = max(t_end, res['_start_min'].max() + win_min)
        if a.fenster:
            t_end = max(t_end, read_semicolon(a.fenster)['Ende_s'].max() / 60)
    ph = read_semicolon(a.phasen)
    ph['Ende_min'] = ph['Ende_min'].fillna(t_end)
    ab = read_semicolon(a.tauchabschnitte) if os.path.exists(a.tauchabschnitte) else None
    z = read_semicolon(a.tiefe)
    zt = a.tauch_start + z['Tauchzeit_s'] / 60

    params = []
    if res is not None:
        for p in a.parameter.split(','):
            p = p.strip()
            if p in res.columns and pd.api.types.is_numeric_dtype(res[p]):
                params.append(p)
            else:
                print('Hinweis: Spalte "%s" nicht gefunden oder nicht numerisch - wird ausgelassen' % p)
    n = len(params)
    heights = [0.55, 1.5] + [1.05] * n
    fig, axes = plt.subplots(len(heights), 1, figsize=(10, sum(heights) + 1.1), sharex=True,
                             gridspec_kw={'height_ratios': heights, 'hspace': 0.12})
    track, dax, pax = axes[0], axes[1], axes[2:]

    for ax in axes[1:]:
        for _, row in ph.iterrows():
            ax.axvspan(row['Start_min'], row['Ende_min'], color=CAT_FILL.get(row['Kategorie'], '#eee'),
                       lw=0, zorder=0)
        for x in ph['Start_min'].iloc[1:]:
            ax.axvline(x, color='white', lw=1.5, zorder=1)
        ax.grid(axis='y', color='white', lw=0.8, zorder=1)
        for s in ('top', 'right'):
            ax.spines[s].set_visible(False)

    # Tiefenprofil
    dax.plot(np.r_[a.tauch_start, zt], np.r_[0, z['Tiefe_m']], color=LINE, lw=1.6, zorder=3)
    dax.set_ylim(max(48, z['Tiefe_m'].max() + 4), -2)
    dax.set_yticks(np.arange(0, z['Tiefe_m'].max() + 1, 10))
    if ab is not None and 'Beschriftung' in ab.columns:
        for _, r in ab.dropna(subset=['Beschriftung']).iterrows():
            dax.text((r['Start_min'] + r['Ende_min']) / 2, r['Text_Tiefe_m'], r['Beschriftung'],
                     ha='center', va='center', fontsize=7.5, color=INK2, zorder=4)

    # Parameter
    if res is not None:
        flag = pd.Series(False, index=res.index)
        if 'Error' in res.columns:
            e = res['Error']
            flag = ~(e.isna() | e.astype(str).str.strip().isin(['0', '0.0', '', 'nan', 'NaN']))
        x = (res['_start_min'] + win_min / 2).values
        f = flag.values
        for ax, p in zip(pax, params):
            y = res[p].values.astype(float)
            ax.plot(x, y, color=LINE, lw=1.3, zorder=3)
            ax.scatter(x[~f], y[~f], s=9, color=LINE, zorder=4, linewidths=0)
            ax.scatter(x[f], y[f], s=14, facecolors='white', edgecolors=LINE, linewidths=1, zorder=5)
            ax.set_ylabel(UNITS.get(p, p), rotation=0, ha='right', va='center')
            ax.yaxis.set_label_coords(-0.045, 0.5)
    dax.set_ylabel('Tiefe (m)', rotation=0, ha='right', va='center')
    dax.yaxis.set_label_coords(-0.045, 0.5)

    phase_track(track, ph, t_end)
    last = axes[-1]
    last.set_xlim(0, t_end)
    last.set_xticks(np.arange(0, t_end + 0.1, 10))
    last.xaxis.set_major_formatter(plt.FuncFormatter(hm))
    last.set_xlabel('Zeit seit Aufzeichnungsbeginn (h:mm)')
    note = ('Tiefenprofil aus Screenshot des Tauchcomputers digitalisiert; 0:00 Tauchzeit = Minute %g der Aufzeichnung.'
            % a.tauch_start)
    if res is not None:
        note += ('\nJeder Punkt = %g-min-Fenster, aufgetragen in der Fenstermitte; offene Kreise = Fehlermeldung von SMART-lab (Spalte Error).'
                 % win_min)
    fig.text(0.01, 0.004, note, fontsize=7, color=MUTED, ha='left', va='bottom')
    fig.subplots_adjust(left=0.17, right=0.985, top=0.97, bottom=(0.75 / fig.get_figheight()))
    for ext in ('png', 'pdf'):
        fig.savefig('%s.%s' % (out, ext), dpi=200)
    print('Abbildung:', out + '.png / .pdf')

    if res is not None:
        tab = phase_table(res, win_min, ph, ab)
        tab.to_csv(out + '_fenster_phasen.csv', sep=';', decimal=',', index=False, encoding='utf-8-sig')
        print('Tabelle:  ', out + '_fenster_phasen.csv  (%d Fenster, %d ganz in einer Phase)'
              % (len(tab), (tab.get('Fenster_ganz_in_Phase') == 'ja').sum()))


if __name__ == '__main__':
    main()
