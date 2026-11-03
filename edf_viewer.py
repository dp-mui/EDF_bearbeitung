"""EDF-Viewer zum Durchsehen, Schneiden und Einteilen von EKG-Aufnahmen in Blöcke.

Start:  python edf_viewer.py [datei.edf]
"""
from __future__ import annotations

import bisect
import json
import os
import sys
from datetime import timedelta

import numpy as np
import pyqtgraph as pg
from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import Qt

from edf_io import Block, Channel, Recording, block_filename, export_blocks, fmt_time, load_edf

pg.setConfigOptions(background="w", foreground="k", antialias=False)

CHANNEL_COLORS = ["#1f4e9c", "#b3261e", "#2e7d32", "#6a1b9a", "#ef6c00", "#00838f"]
CUT_COLOR = "#ff6f00"
ANNOT_COLOR = "#2e7d32"
BLOCK_BRUSHES = [QtGui.QColor(25, 118, 210, 12), QtGui.QColor(25, 118, 210, 38)]
EXCLUDED_BRUSH = QtGui.QColor(110, 110, 110, 70)
AXIS_WIDTH = 75
MAX_VISIBLE_CUTS = 300      # mehr Schnittlinien gleichzeitig im Fenster werden nicht gezeichnet
MAX_SHADED_BLOCKS = 80      # mehr Blöcke im Fenster -> keine Hinterlegung
MAX_ANNOTATIONS = 500
MAX_OVERVIEW_LABELS = 40

HELP_TEXT = (
    "<b>Bedienung</b><br>"
    "Mausrad: Zoom &middot; Ziehen: verschieben<br>"
    "&larr;/&rarr;: blättern &middot; Bild&uarr;/&darr;: ganze Seite &middot; Pos1/Ende<br>"
    "+/&minus;: Zoom &middot; Übersicht unten: klicken/ziehen<br>"
    "<b>Doppelklick</b> in Kurve oder Taste <b>S</b>: Schnitt setzen<br>"
    "Schnittlinie ziehen: verschieben<br>"
    "<b>Rechtsklick</b> auf Linie oder <b>Entf</b>: Schnitt löschen<br>"
    "Name: Doppelklick in Spalte „Name“<br>"
    "Häkchen „Export“ aus = Block wird nicht exportiert"
)

NICE_SPACINGS = [0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30,
                 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400]


# --------------------------------------------------------------------------- #
# Hilfs-Widgets
# --------------------------------------------------------------------------- #
class TimeAxis(pg.AxisItem):
    """Zeitachse mit hh:mm:ss-Beschriftung und 'runden' Zeitabständen."""

    show_hours = True

    def tickSpacing(self, minVal, maxVal, size):
        rng = maxVal - minVal
        if rng <= 0 or size <= 0:
            return super().tickSpacing(minVal, maxVal, size)
        target = rng / max(2.0, size / 95.0)
        idx = next((i for i, s in enumerate(NICE_SPACINGS) if s >= target), len(NICE_SPACINGS) - 1)
        return [(NICE_SPACINGS[idx], 0), (NICE_SPACINGS[max(0, idx - 2)], 0)]

    def tickStrings(self, values, scale, spacing):
        dec = 0 if spacing >= 1 else 1 if spacing >= 0.1 else 2 if spacing >= 0.01 else 3
        out = []
        for v in values:
            s = fmt_time(max(0.0, v), dec)
            out.append(s if self.show_hours else s[3:])
        return out


class CutLine(pg.InfiniteLine):
    """Verschiebbare Schnittlinie; Rechtsklick fordert Löschen an."""

    sigDeleteRequested = QtCore.pyqtSignal(object)

    def __init__(self, slot: int, **kw):
        super().__init__(angle=90, movable=True, pen=pg.mkPen(CUT_COLOR, width=2),
                         hoverPen=pg.mkPen(CUT_COLOR, width=5), **kw)
        self.slot = slot
        self.cut_id = None
        self.setCursor(Qt.SizeHorCursor)

    def mouseClickEvent(self, ev):
        if ev.button() == Qt.RightButton:
            ev.accept()
            self.sigDeleteRequested.emit(self)
            return
        super().mouseClickEvent(ev)


class BlockModel(QtCore.QAbstractTableModel):
    COLS = ["Nr", "Name", "Start", "Ende", "Dauer", "Export"]
    metaChanged = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.blocks: list[Block] = []
        self.meta: dict[int, dict] = {}   # start_id -> {"name", "export"}
        self.show_hours = True

    def _t(self, seconds: float) -> str:
        s = fmt_time(seconds)
        return s if self.show_hours else s[3:]

    def set_blocks(self, blocks: list[Block]):
        self.beginResetModel()
        self.blocks = blocks
        self.endResetModel()

    def rowCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self.blocks)

    def columnCount(self, parent=QtCore.QModelIndex()):
        return len(self.COLS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.COLS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        b = self.blocks[index.row()]
        c = index.column()
        if role in (Qt.DisplayRole, Qt.EditRole):
            return [b.nr, b.name, self._t(b.start), self._t(b.end), self._t(b.duration), None][c]
        if role == Qt.CheckStateRole and c == 5:
            return Qt.Checked if b.export else Qt.Unchecked
        if role == Qt.ForegroundRole and not b.export:
            return QtGui.QBrush(QtGui.QColor("#9e9e9e"))
        if role == Qt.TextAlignmentRole and c in (0, 2, 3, 4):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        if role == Qt.ToolTipRole and c == 1:
            return "Doppelklick zum Benennen (Name erscheint im Dateinamen)"
        return None

    def flags(self, index):
        f = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if index.column() == 1:
            f |= Qt.ItemIsEditable
        if index.column() == 5:
            f |= Qt.ItemIsUserCheckable
        return f

    def setData(self, index, value, role=Qt.EditRole):
        if not index.isValid():
            return False
        b = self.blocks[index.row()]
        m = self.meta.setdefault(b.start_id, {"name": "", "export": True})
        if index.column() == 1 and role == Qt.EditRole:
            b.name = m["name"] = str(value).strip()
        elif index.column() == 5 and role == Qt.CheckStateRole:
            b.export = m["export"] = int(value) == int(Qt.Checked)
        else:
            return False
        self.dataChanged.emit(self.index(index.row(), 0), self.index(index.row(), len(self.COLS) - 1))
        self.metaChanged.emit()
        return True

    def set_all_export(self, value: bool):
        for b in self.blocks:
            b.export = value
            self.meta.setdefault(b.start_id, {"name": "", "export": True})["export"] = value
        if self.blocks:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.blocks) - 1, len(self.COLS) - 1))
        self.metaChanged.emit()


# --------------------------------------------------------------------------- #
# Hauptfenster
# --------------------------------------------------------------------------- #
class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, path: str | None = None):
        super().__init__()
        self.setWindowTitle("EDF Schnitt-Tool")
        self.resize(1450, 880)
        self.setAcceptDrops(True)

        self.rec: Recording | None = None
        self.cut_times: list[float] = []   # sortiert
        self.cut_ids: list[int] = []       # parallel zu cut_times
        self._next_cut_id = 1
        self._block_starts: list[float] = []
        self._dirty = False
        self._dragging = False
        self._silent_select = False
        self._mouse_t: float | None = None
        self._last_split = 60.0

        self.plots: list[pg.PlotItem] = []
        self.curves: list[pg.PlotDataItem] = []
        self.cut_pool: list[list[CutLine]] = []
        self.region_pool: list[list[pg.LinearRegionItem]] = []
        self.label_pool: list[pg.TextItem] = []
        self.ov_labels: list[pg.TextItem] = []

        self.model = BlockModel(self)
        self.model.metaChanged.connect(self._on_meta_changed)

        self._render_timer = QtCore.QTimer(self, singleShot=True, interval=0)
        self._render_timer.timeout.connect(self._render)

        self._build_ui()
        self._build_shortcuts()
        self._set_loaded(False)
        if path:
            QtCore.QTimer.singleShot(0, lambda: self.open_file(path))

    # ------------------------------------------------------------------ UI --
    def _build_ui(self):
        tb = self.addToolBar("Werkzeuge")
        tb.setMovable(False)
        self.act_open = tb.addAction("Öffnen…", self.open_dialog)
        self.act_open.setShortcut("Ctrl+O")
        self.act_export = tb.addAction("Blöcke exportieren…", self.export)
        self.act_export.setShortcut("Ctrl+E")
        tb.addSeparator()
        self.act_save = tb.addAction("Schnitte speichern", self.save_session)
        self.act_save.setShortcut("Ctrl+S")
        self.act_load = tb.addAction("Schnitte laden…", self.load_session_dialog)
        tb.addSeparator()
        tb.addWidget(QtWidgets.QLabel(" Fenster: "))
        self.spin_window = QtWidgets.QDoubleSpinBox()
        self.spin_window.setRange(0.05, 1e7)
        self.spin_window.setDecimals(2)
        self.spin_window.setSuffix(" s")
        self.spin_window.setValue(10.0)
        self.spin_window.setKeyboardTracking(False)
        self.spin_window.valueChanged.connect(self._on_window_spin)
        tb.addWidget(self.spin_window)
        tb.addWidget(QtWidgets.QLabel("  "))
        self.chk_yauto = QtWidgets.QCheckBox("Y automatisch")
        self.chk_yauto.setChecked(True)
        self.chk_yauto.setToolTip("Amplitude automatisch an den sichtbaren Ausschnitt anpassen.\n"
                                  "Aus: Y-Achse mit Mausrad/Ziehen auf der Y-Achse selbst einstellen.")
        self.chk_yauto.toggled.connect(self._on_yauto_toggled)
        tb.addWidget(self.chk_yauto)
        tb.addWidget(QtWidgets.QLabel("  "))
        self.chk_snap = QtWidgets.QCheckBox("Schnitte auf ganze Sekunden")
        self.chk_snap.setChecked(True)
        self.chk_snap.setToolTip(
            "EDF speichert Daten in Datensätzen (hier meist 1 s).\n"
            "Mit Einrasten sind exportierte Blöcke exakt so lang wie markiert.\n"
            "Ohne Einrasten wird beim Export der unvollständige Rest am Blockende (< 1 Datensatz) weggelassen.")
        tb.addWidget(self.chk_snap)
        tb.addSeparator()
        self.act_split = tb.addAction("Automatisch einteilen…", self.auto_split)
        self.act_clear = tb.addAction("Alle Schnitte löschen", self.clear_cuts)

        # --- Signalbereich
        self.stack = QtWidgets.QStackedWidget()
        placeholder = QtWidgets.QLabel("EDF-Datei öffnen (Strg+O)\noder hierher ziehen")
        placeholder.setAlignment(Qt.AlignCenter)
        placeholder.setStyleSheet("color:#888; font-size:18pt;")
        self.stack.addWidget(placeholder)

        viewer = QtWidgets.QWidget()
        vl = QtWidgets.QVBoxLayout(viewer)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(2)
        self.glw = pg.GraphicsLayoutWidget()
        self.glw.scene().sigMouseClicked.connect(self._on_plot_clicked)
        self.glw.scene().sigMouseMoved.connect(self._on_mouse_moved)
        vl.addWidget(self.glw, 1)

        self.ov_axis = TimeAxis(orientation="bottom")
        self.ov_widget = pg.PlotWidget(axisItems={"bottom": self.ov_axis})
        self.ov_widget.setFixedHeight(125)
        self.ov_plot: pg.PlotItem = self.ov_widget.getPlotItem()
        self.ov_plot.setMenuEnabled(False)
        self.ov_plot.hideButtons()
        self.ov_plot.setMouseEnabled(x=False, y=False)
        self.ov_plot.getAxis("left").setWidth(AXIS_WIDTH)
        self.ov_plot.getAxis("left").setStyle(showValues=False)
        self.ov_plot.setLabel("left", "Übersicht")
        self.ov_widget.scene().sigMouseClicked.connect(self._on_overview_clicked)
        self.ov_plot.vb.sigResized.connect(lambda *_: self._update_overview())
        vl.addWidget(self.ov_widget)

        self.scroll = QtWidgets.QScrollBar(Qt.Horizontal)
        self.scroll.valueChanged.connect(self._on_scroll)
        vl.addWidget(self.scroll)

        # --- Blockliste
        right = QtWidgets.QWidget()
        rl = QtWidgets.QVBoxLayout(right)
        rl.setContentsMargins(6, 0, 0, 0)
        rl.addWidget(QtWidgets.QLabel("<b>Blöcke</b>"))
        self.table = QtWidgets.QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.setAlternatingRowColors(True)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QtWidgets.QHeaderView.Interactive)
        hh.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        hh.setMinimumSectionSize(30)
        self._set_table_widths(hours=False)
        self.table.selectionModel().currentRowChanged.connect(self._on_block_selected)
        rl.addWidget(self.table, 1)

        self.lbl_blocks = QtWidgets.QLabel()
        rl.addWidget(self.lbl_blocks)
        row1 = QtWidgets.QHBoxLayout()
        self.btn_zoom_block = QtWidgets.QPushButton("Block ganz anzeigen")
        self.btn_zoom_block.clicked.connect(self.zoom_to_selected_block)
        self.btn_merge = QtWidgets.QPushButton("Mit nächstem verbinden")
        self.btn_merge.setToolTip("Entfernt den Schnitt am Ende des gewählten Blocks")
        self.btn_merge.clicked.connect(self.merge_with_next)
        row1.addWidget(self.btn_zoom_block)
        row1.addWidget(self.btn_merge)
        rl.addLayout(row1)
        row2 = QtWidgets.QHBoxLayout()
        self.btn_all = QtWidgets.QPushButton("Alle exportieren")
        self.btn_all.clicked.connect(lambda: self.model.set_all_export(True))
        self.btn_none = QtWidgets.QPushButton("Keinen exportieren")
        self.btn_none.clicked.connect(lambda: self.model.set_all_export(False))
        row2.addWidget(self.btn_all)
        row2.addWidget(self.btn_none)
        rl.addLayout(row2)
        help_lbl = QtWidgets.QLabel(HELP_TEXT)
        help_lbl.setWordWrap(True)
        help_lbl.setStyleSheet("color:#555; font-size:8.5pt; padding-top:8px;")
        rl.addWidget(help_lbl)

        splitter = QtWidgets.QSplitter(Qt.Horizontal)
        splitter.addWidget(viewer)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([1020, 430])
        self.stack.addWidget(splitter)
        self.setCentralWidget(self.stack)

        self.lbl_file = QtWidgets.QLabel()
        self.lbl_cursor = QtWidgets.QLabel()
        self.statusBar().addWidget(self.lbl_file)
        self.statusBar().addPermanentWidget(self.lbl_cursor)

    def _build_shortcuts(self):
        def sc(key, fn):
            s = QtWidgets.QShortcut(QtGui.QKeySequence(key), self)
            s.activated.connect(fn)

        sc(Qt.Key_Left, lambda: self.step(-0.2))
        sc(Qt.Key_Right, lambda: self.step(0.2))
        sc(Qt.SHIFT + Qt.Key_Left, lambda: self.step(-1.0))
        sc(Qt.SHIFT + Qt.Key_Right, lambda: self.step(1.0))
        sc(Qt.Key_PageUp, lambda: self.step(-1.0))
        sc(Qt.Key_PageDown, lambda: self.step(1.0))
        sc(Qt.Key_Home, lambda: self.step(-1e12))
        sc(Qt.Key_End, lambda: self.step(1e12))
        sc(Qt.Key_Plus, lambda: self.zoom(0.5))
        sc(Qt.Key_Minus, lambda: self.zoom(2.0))
        sc(Qt.Key_S, self.cut_at_mouse)
        sc(Qt.Key_Delete, self.delete_cut_near_mouse)

    def _set_table_widths(self, hours: bool):
        fm = self.table.fontMetrics()
        tw = fm.horizontalAdvance("00:00:00.000" if hours else "00:00.000") + 14
        for col, w in [(0, fm.horizontalAdvance("0000") + 12), (2, tw), (3, tw), (4, tw),
                       (5, fm.horizontalAdvance("Export") + 16)]:
            self.table.setColumnWidth(col, w)

    def _set_loaded(self, loaded: bool):
        for w in (self.act_export, self.act_save, self.act_load, self.act_split, self.act_clear,
                  self.spin_window, self.btn_zoom_block, self.btn_merge, self.btn_all, self.btn_none):
            w.setEnabled(loaded)
        self.stack.setCurrentIndex(1 if loaded else 0)

    # ------------------------------------------------------------ Datei --
    def open_dialog(self):
        start = os.path.dirname(self.rec.path) if self.rec else os.getcwd()
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "EDF-Datei öffnen", start, "EDF/BDF (*.edf *.EDF *.bdf *.BDF);;Alle Dateien (*)")
        if path:
            self.open_file(path)

    def open_file(self, path: str):
        if not self._confirm_discard():
            return
        QtWidgets.QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            rec = load_edf(path)
        except Exception as exc:
            QtWidgets.QApplication.restoreOverrideCursor()
            QtWidgets.QMessageBox.critical(self, "Fehler beim Öffnen", f"{path}\n\n{exc}")
            return
        try:
            self.rec = rec
            self.cut_times, self.cut_ids, self._next_cut_id = [], [], 1
            self.model.meta = {0: {"name": "", "export": True}}
            self.model.show_hours = rec.duration >= 3600
            self._set_table_widths(self.model.show_hours)
            self._setup_overview()
            self._setup_plots()
            self._set_loaded(True)
            self._rebuild_blocks()
            self.set_view(0, min(10.0, rec.duration))
            self._dirty = False
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()

        fs = sorted({f"{ch.fs:g}" for ch in rec.channels})
        self.lbl_file.setText(
            f"{os.path.basename(path)}  ·  {len(rec.channels)} Kanäle  ·  {'/'.join(fs)} Hz  ·  "
            f"Dauer {fmt_time(rec.duration, 0)}  ·  Start {rec.start:%d.%m.%Y %H:%M:%S}")
        self.setWindowTitle(f"EDF Schnitt-Tool – {os.path.basename(path)}")
        if len(rec.annotations) > MAX_ANNOTATIONS:
            self.statusBar().showMessage(
                f"{len(rec.annotations)} Annotationen – zu viele zum Anzeigen, werden aber mit exportiert.", 8000)

        session = self._default_session_path()
        if os.path.exists(session):
            r = QtWidgets.QMessageBox.question(
                self, "Gespeicherte Schnitte", f"Zu dieser Datei gibt es gespeicherte Schnitte:\n{session}\n\nLaden?")
            if r == QtWidgets.QMessageBox.Yes:
                self.load_session(session)

    def _confirm_discard(self) -> bool:
        if not (self.rec and self._dirty):
            return True
        r = QtWidgets.QMessageBox.question(
            self, "Ungespeicherte Schnitte", "Die Schnitte wurden geändert. Speichern?",
            QtWidgets.QMessageBox.Save | QtWidgets.QMessageBox.Discard | QtWidgets.QMessageBox.Cancel)
        if r == QtWidgets.QMessageBox.Save:
            return self.save_session()
        return r == QtWidgets.QMessageBox.Discard

    def closeEvent(self, ev):
        if self._confirm_discard():
            ev.accept()
        else:
            ev.ignore()

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls() and any(u.toLocalFile().lower().endswith((".edf", ".bdf"))
                                           for u in ev.mimeData().urls()):
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        for u in ev.mimeData().urls():
            p = u.toLocalFile()
            if p.lower().endswith((".edf", ".bdf")):
                self.open_file(p)
                break

    # -------------------------------------------------------- Plot-Setup --
    def _setup_plots(self):
        rec = self.rec
        self.glw.clear()
        self.plots, self.curves = [], []
        self.cut_pool, self.region_pool, self.label_pool = [], [], []
        TimeAxis.show_hours = rec.duration >= 3600
        n = len(rec.channels)
        for i, ch in enumerate(rec.channels):
            p = self.glw.addPlot(row=i, col=0, axisItems={"bottom": TimeAxis(orientation="bottom")})
            p.setMenuEnabled(False)
            p.hideButtons()
            p.showGrid(x=True, y=True, alpha=0.25)
            p.enableAutoRange(x=False, y=False)
            p.setMouseEnabled(x=True, y=not self.chk_yauto.isChecked())
            p.getAxis("left").setWidth(AXIS_WIDTH)
            p.getAxis("left").enableAutoSIPrefix(False)
            p.setLabel("left", f"{ch.label} [{ch.unit}]" if ch.unit else ch.label)
            p.setLimits(xMin=0, xMax=rec.duration, minXRange=min(0.05, rec.duration),
                        maxXRange=rec.duration)
            if i < n - 1:
                p.getAxis("bottom").setStyle(showValues=False)
            if i > 0:
                p.setXLink(self.plots[0])
            curve = p.plot(pen=pg.mkPen(CHANNEL_COLORS[i % len(CHANNEL_COLORS)], width=1))
            self.plots.append(p)
            self.curves.append(curve)
        self.plots[-1].setLabel("bottom", "Zeit ab Aufnahmebeginn")
        self.plots[0].sigXRangeChanged.connect(self._on_xrange_changed)
        self.plots[0].vb.sigResized.connect(lambda *_: self._render_timer.start())

        if len(rec.annotations) <= MAX_ANNOTATIONS:
            pen = pg.mkPen(ANNOT_COLOR, width=1, style=Qt.DashLine)
            for onset, _dur, text in rec.annotations:
                for i, p in enumerate(self.plots):
                    kw = {}
                    if i == 0:
                        kw = dict(label=text, labelOpts=dict(position=0.82, color=ANNOT_COLOR,
                                                             fill=(255, 255, 255, 200), movable=True))
                    p.addItem(pg.InfiniteLine(pos=onset, angle=90, movable=False, pen=pen, **kw),
                              ignoreBounds=True)

    def _setup_overview(self):
        rec = self.rec
        ov = self.ov_plot
        ov.clear()
        self.ov_labels = []
        self.ov_axis.show_hours = rec.duration >= 3600
        ch = rec.channels[0]
        t, y = self._envelope(ch, 0, ch.n_samples, 3000)
        if y.size:
            lo, hi = np.percentile(y, [0.5, 99.5])
        else:
            lo, hi = -1.0, 1.0
        if hi - lo < 1e-9:
            lo, hi = lo - 1, hi + 1
        pad = 0.08 * (hi - lo)
        self.ov_y0, self.ov_y1 = float(lo - pad), float(hi + pad)
        ov.setXRange(0, rec.duration, padding=0)
        ov.setYRange(self.ov_y0, self.ov_y1, padding=0)

        self.ov_bars = pg.BarGraphItem(x0=[0], width=[rec.duration], y0=self.ov_y0,
                                       height=self.ov_y1 - self.ov_y0, brush=BLOCK_BRUSHES[0],
                                       pen=pg.mkPen(None))
        self.ov_bars.setZValue(-10)
        ov.addItem(self.ov_bars)
        ov.plot(t, y, pen=pg.mkPen("#607d8b", width=1))
        self.ov_cuts = ov.plot(pen=pg.mkPen(CUT_COLOR, width=1.5), connect="pairs")
        self.ov_region = pg.LinearRegionItem(values=(0, min(10.0, rec.duration)), bounds=(0, rec.duration),
                                             brush=QtGui.QColor(255, 193, 7, 70),
                                             pen=pg.mkPen("#ff8f00", width=1.5))
        self.ov_region.setZValue(10)
        self.ov_region.sigRegionChanged.connect(self._on_region_changed)
        ov.addItem(self.ov_region)

    # ------------------------------------------------------ Darstellung --
    @staticmethod
    def _envelope(ch: Channel, i0: int, i1: int, max_bins: int):
        """Min/Max-Hüllkurve (Peak-Decimation) -> sieht bei jeder Zoomstufe korrekt aus."""
        n = i1 - i0
        if n <= 0:
            return np.empty(0), np.empty(0, dtype=np.float32)
        if n <= 2 * max_bins:
            return np.arange(i0, i1) / ch.fs, ch.physical(i0, i1)
        size = int(np.ceil(n / max_bins))
        nb = n // size
        seg = ch.digital[i0:i0 + nb * size].reshape(nb, size)
        d = np.empty(nb * 2, dtype=seg.dtype)
        d[0::2] = seg.min(axis=1)
        d[1::2] = seg.max(axis=1)
        t = np.repeat((i0 + np.arange(nb) * size + size / 2) / ch.fs, 2)
        return t, ch.to_physical(d)

    def view_range(self) -> tuple[float, float]:
        x0, x1 = self.plots[0].viewRange()[0]
        return float(x0), float(x1)

    def set_view(self, x0: float, x1: float):
        if not self.rec:
            return
        dur = self.rec.duration
        w = min(max(x1 - x0, min(0.05, dur)), dur)
        x0 = min(max(0.0, x0), dur - w)
        self.plots[0].setXRange(x0, x0 + w, padding=0)
        self._on_xrange_changed()

    def _on_xrange_changed(self, *_):
        if not self.rec:
            return
        x0, x1 = self.view_range()
        w = x1 - x0
        dur = self.rec.duration
        self.scroll.blockSignals(True)
        self.scroll.setMaximum(max(0, int(round((dur - w) * 1000))))
        self.scroll.setPageStep(max(1, int(w * 1000)))
        self.scroll.setSingleStep(max(1, int(w * 100)))
        self.scroll.setValue(int(round(x0 * 1000)))
        self.scroll.blockSignals(False)
        self.spin_window.blockSignals(True)
        self.spin_window.setValue(w)
        self.spin_window.blockSignals(False)
        self.ov_region.blockSignals(True)
        self.ov_region.setRegion((x0, x1))
        self.ov_region.blockSignals(False)
        self._render_timer.start()

    def _render(self):
        if not self.rec or not self.plots:
            return
        x0, x1 = self.view_range()
        px = max(300, int(self.plots[0].vb.width()))
        for ch, curve, p in zip(self.rec.channels, self.curves, self.plots):
            i0 = max(0, int(np.floor(x0 * ch.fs)) - 1)
            i1 = min(ch.n_samples, int(np.ceil(x1 * ch.fs)) + 2)
            t, y = self._envelope(ch, i0, i1, px)
            curve.setData(t, y)
            if self.chk_yauto.isChecked() and y.size:
                lo, hi = float(y.min()), float(y.max())
                if hi - lo < 1e-9:
                    lo, hi = lo - 0.5, hi + 0.5
                p.setYRange(lo, hi, padding=0.08)
        self._refresh_overlays()

    def _refresh_overlays(self):
        if not self.plots:
            return
        x0, x1 = self.view_range()
        self._sync_cut_lines(x0, x1)
        self._sync_block_shading(x0, x1)

    def _sync_cut_lines(self, x0, x1):
        if self._dragging:
            return
        lo = bisect.bisect_left(self.cut_times, x0)
        hi = min(bisect.bisect_right(self.cut_times, x1), lo + MAX_VISIBLE_CUTS)
        while len(self.cut_pool) < hi - lo:
            self._add_cut_slot()
        px = self.plots[0].vb.viewPixelSize()[0] or 1.0
        last_label = -1e18
        for slot, lines in enumerate(self.cut_pool):
            k = lo + slot
            if k < hi:
                t = self.cut_times[k]
                for ln in lines:
                    ln.cut_id = self.cut_ids[k]
                    ln.blockSignals(True)
                    ln.setValue(t)
                    ln.blockSignals(False)
                    ln.show()
                lines[-1].label.setFormat(fmt_time(t, 2))
                show_label = (t - last_label) / px >= 90   # Zeitbeschriftungen nicht überlappen lassen
                lines[-1].label.setVisible(show_label)
                if show_label:
                    last_label = t
            else:
                for ln in lines:
                    ln.hide()

    def _add_cut_slot(self):
        slot = len(self.cut_pool)
        lines = []
        for i, p in enumerate(self.plots):
            kw = {}
            if i == len(self.plots) - 1:
                kw = dict(label=" ", labelOpts=dict(position=0.06, color=CUT_COLOR,
                                                    fill=(255, 255, 255, 220), movable=False))
            ln = CutLine(slot, bounds=(0, self.rec.duration), **kw)
            ln.setZValue(20)
            ln.sigPositionChanged.connect(self._on_cut_dragging)
            ln.sigPositionChangeFinished.connect(self._on_cut_drag_finished)
            ln.sigDeleteRequested.connect(self._on_cut_delete_requested)
            ln.hide()
            p.addItem(ln, ignoreBounds=True)
            lines.append(ln)
        self.cut_pool.append(lines)

    def _sync_block_shading(self, x0, x1):
        blocks = self.model.blocks
        lo = max(0, bisect.bisect_right(self._block_starts, x0) - 1)
        hi = bisect.bisect_left(self._block_starts, x1)
        visible = blocks[lo:hi] if hi - lo <= MAX_SHADED_BLOCKS else []
        while len(self.region_pool) < len(visible):
            regs = []
            for p in self.plots:
                r = pg.LinearRegionItem(movable=False, pen=pg.mkPen(None))
                r.setZValue(-100)
                p.addItem(r, ignoreBounds=True)
                regs.append(r)
            self.region_pool.append(regs)
            lbl = pg.TextItem(anchor=(0, 0), fill=(255, 255, 255, 190))
            lbl.setZValue(30)
            self.plots[0].addItem(lbl, ignoreBounds=True)
            self.label_pool.append(lbl)
        ytop = self.plots[0].viewRange()[1][1]
        px = self.plots[0].vb.viewPixelSize()[0] or 1.0
        for k, (regs, lbl) in enumerate(zip(self.region_pool, self.label_pool)):
            if k < len(visible):
                b = visible[k]
                brush = BLOCK_BRUSHES[b.nr % 2] if b.export else EXCLUDED_BRUSH
                for r in regs:
                    r.setRegion((b.start, b.end))
                    r.setBrush(brush)
                    r.show()
                text = f"B{b.nr}" + (f"  {b.name}" if b.name else "")
                if not b.export:
                    text += "  (kein Export)"
                lbl.setText(text, color="#0d47a1" if b.export else "#616161")
                lbl.setPos(max(b.start, x0), ytop)
                lbl.setVisible((min(b.end, x1) - max(b.start, x0)) / px >= 60)
            else:
                for r in regs:
                    r.hide()
                lbl.hide()

    def _update_overview(self):
        if not self.rec or not hasattr(self, "ov_bars"):
            return
        blocks = self.model.blocks
        self.ov_bars.setOpts(
            x0=np.array([b.start for b in blocks]), width=np.array([b.duration for b in blocks]),
            y0=self.ov_y0, height=self.ov_y1 - self.ov_y0,
            brushes=[BLOCK_BRUSHES[b.nr % 2] if b.export else EXCLUDED_BRUSH for b in blocks])
        ov_px = max(1, self.ov_plot.vb.width())
        if self.cut_times and ov_px / (len(self.cut_times) + 1) >= 5:   # zu dicht -> nur Hinterlegung
            ct = np.repeat(np.asarray(self.cut_times), 2)
            cy = np.tile([self.ov_y0, self.ov_y1], len(self.cut_times))
            self.ov_cuts.setData(ct, cy)
        else:
            self.ov_cuts.setData([], [])
        for lbl in self.ov_labels:
            self.ov_plot.removeItem(lbl)
        self.ov_labels = []
        if len(blocks) <= MAX_OVERVIEW_LABELS:
            px_per_s = ov_px / self.rec.duration
            for b in blocks:
                if b.duration * px_per_s < 30:
                    continue
                lbl = pg.TextItem(f"B{b.nr}", color="#0d47a1" if b.export else "#757575", anchor=(0, 0))
                lbl.setPos(b.start, self.ov_y1)
                self.ov_plot.addItem(lbl, ignoreBounds=True)
                self.ov_labels.append(lbl)

    # ---------------------------------------------------- Navigation --
    def step(self, frac: float):
        if not self.rec:
            return
        x0, x1 = self.view_range()
        w = x1 - x0
        self.set_view(x0 + frac * w, x1 + frac * w)

    def zoom(self, factor: float):
        if not self.rec:
            return
        x0, x1 = self.view_range()
        c, w = (x0 + x1) / 2, (x1 - x0) * factor
        self.set_view(c - w / 2, c + w / 2)

    def _on_scroll(self, value: int):
        x0, x1 = self.view_range()
        start = value / 1000.0
        self.set_view(start, start + (x1 - x0))

    def _on_window_spin(self, w: float):
        if not self.rec:
            return
        x0, _ = self.view_range()
        self.set_view(x0, x0 + w)

    def _on_region_changed(self):
        x0, x1 = self.ov_region.getRegion()
        self.plots[0].setXRange(x0, x1, padding=0)

    def _on_overview_clicked(self, ev):
        if not self.rec or ev.button() != Qt.LeftButton:
            return
        vb = self.ov_plot.vb
        if not vb.sceneBoundingRect().contains(ev.scenePos()):
            return
        t = vb.mapSceneToView(ev.scenePos()).x()
        x0, x1 = self.view_range()
        if x0 <= t <= x1:
            return  # Klick in den markierten Bereich -> Ziehen übernimmt die Region
        w = x1 - x0
        self.set_view(t - w / 2, t + w / 2)

    def _on_yauto_toggled(self, on: bool):
        for p in self.plots:
            p.setMouseEnabled(x=True, y=not on)
        self._render_timer.start()

    # ----------------------------------------------------- Maus/Plots --
    def _plot_at(self, scene_pos):
        for p in self.plots:
            if p.vb.sceneBoundingRect().contains(scene_pos):
                return p
        return None

    def _on_mouse_moved(self, pos):
        if not self.rec:
            return
        p = self._plot_at(pos)
        if p is None:
            self._mouse_t = None
            return
        t = float(p.vb.mapSceneToView(pos).x())
        self._mouse_t = t
        if not 0 <= t <= self.rec.duration:
            return
        clock = self.rec.start + timedelta(seconds=t)
        b = self._block_at(t)
        btxt = f"  ·  Block B{b.nr}" if b else ""
        self.lbl_cursor.setText(f"Cursor {fmt_time(t)}  ·  Uhrzeit {clock:%H:%M:%S}.{clock.microsecond // 1000:03d}{btxt}")

    def _on_plot_clicked(self, ev):
        if not self.rec or ev.button() != Qt.LeftButton:
            return
        p = self._plot_at(ev.scenePos())
        if p is None:
            return
        t = float(p.vb.mapSceneToView(ev.scenePos()).x())
        if ev.double():
            self.add_cut(t)
        else:
            b = self._block_at(t)
            if b:
                self._select_row(b.nr - 1, silent=True)

    # ---------------------------------------------------------- Schnitte --
    def _min_gap(self) -> float:
        return 1.0 if self.chk_snap.isChecked() else 10.0 / self.rec.max_fs

    def _snap(self, t: float) -> float:
        if self.chk_snap.isChecked():
            return float(round(t))
        fs = self.rec.max_fs
        return round(t * fs) / fs

    def _cut_ok(self, t: float) -> bool:
        g = self._min_gap() * 0.999
        if t < g or t > self.rec.duration - g:
            return False
        k = bisect.bisect_left(self.cut_times, t)
        for j in (k - 1, k):
            if 0 <= j < len(self.cut_times) and abs(self.cut_times[j] - t) < g:
                return False
        return True

    def _insert_cut(self, t: float, cid: int | None = None) -> int:
        """Fügt einen Schnitt ein; der neue Block übernimmt den Export-Status des geteilten Blocks."""
        k = bisect.bisect_left(self.cut_times, t)
        parent_id = self.cut_ids[k - 1] if k > 0 else 0
        if cid is None:
            cid = self._next_cut_id
            self._next_cut_id += 1
            parent = self.model.meta.get(parent_id, {"export": True})
            self.model.meta[cid] = {"name": "", "export": parent.get("export", True)}
        self.cut_times.insert(k, t)
        self.cut_ids.insert(k, cid)
        return cid

    def _remove_cut_at(self, k: int):
        del self.cut_times[k]
        del self.cut_ids[k]

    def add_cut(self, t: float) -> bool:
        if not self.rec:
            return False
        t = self._snap(t)
        if not self._cut_ok(t):
            self.statusBar().showMessage("Kein Schnitt gesetzt: zu nah am Dateirand oder an einem anderen Schnitt.", 4000)
            return False
        self._insert_cut(t)
        self._cuts_changed()
        self.statusBar().showMessage(f"Schnitt gesetzt bei {fmt_time(t)}", 3000)
        return True

    def cut_at_mouse(self):
        if not self.rec:
            return
        if self._mouse_t is not None and 0 <= self._mouse_t <= self.rec.duration:
            self.add_cut(self._mouse_t)
        else:
            x0, x1 = self.view_range()
            self.add_cut((x0 + x1) / 2)

    def delete_cut_near_mouse(self):
        if not self.rec or self._mouse_t is None or not self.cut_times:
            return
        k = bisect.bisect_left(self.cut_times, self._mouse_t)
        cands = [j for j in (k - 1, k) if 0 <= j < len(self.cut_times)]
        j = min(cands, key=lambda j: abs(self.cut_times[j] - self._mouse_t))
        tol = 10 * self.plots[0].vb.viewPixelSize()[0]
        if abs(self.cut_times[j] - self._mouse_t) <= tol:
            t = self.cut_times[j]
            self._remove_cut_at(j)
            self._cuts_changed()
            self.statusBar().showMessage(f"Schnitt bei {fmt_time(t)} gelöscht", 3000)

    def _on_cut_delete_requested(self, line: CutLine):
        if line.cut_id in self.cut_ids:
            k = self.cut_ids.index(line.cut_id)
            t = self.cut_times[k]
            self._remove_cut_at(k)
            self._cuts_changed()
            self.statusBar().showMessage(f"Schnitt bei {fmt_time(t)} gelöscht", 3000)

    def _on_cut_dragging(self, line: CutLine):
        self._dragging = True
        t = line.value()
        for other in self.cut_pool[line.slot]:
            if other is not line:
                other.blockSignals(True)
                other.setValue(t)
                other.blockSignals(False)
        st = self._snap(t)
        self.cut_pool[line.slot][-1].label.setFormat(fmt_time(st, 2))
        self.statusBar().showMessage(f"Schnitt verschieben: {fmt_time(st)}")

    def _on_cut_drag_finished(self, line: CutLine):
        self._dragging = False
        self.statusBar().clearMessage()
        if line.cut_id not in self.cut_ids:
            self._refresh_overlays()
            return
        k = self.cut_ids.index(line.cut_id)
        old = self.cut_times[k]
        self._remove_cut_at(k)
        t = self._snap(line.value())
        if not self._cut_ok(t):
            self.statusBar().showMessage("Verschieben nicht möglich: zu nah an einem anderen Schnitt.", 4000)
            t = old
        self._insert_cut(t, cid=line.cut_id)
        self._cuts_changed()

    def clear_cuts(self):
        if not self.cut_times:
            return
        r = QtWidgets.QMessageBox.question(self, "Alle Schnitte löschen",
                                           f"Alle {len(self.cut_times)} Schnitte löschen?")
        if r != QtWidgets.QMessageBox.Yes:
            return
        self.cut_times, self.cut_ids = [], []
        self._cuts_changed()

    def auto_split(self):
        if not self.rec:
            return
        dur = self.rec.duration
        sec, ok = QtWidgets.QInputDialog.getDouble(
            self, "Automatisch einteilen", "Blocklänge in Sekunden:", self._last_split, 1.0, dur, 1)
        if not ok:
            return
        n_new = int(dur // sec)
        if n_new > 2000:
            r = QtWidgets.QMessageBox.question(self, "Viele Blöcke", f"Das ergibt ca. {n_new} Blöcke. Fortfahren?")
            if r != QtWidgets.QMessageBox.Yes:
                return
        if self.cut_times:
            r = QtWidgets.QMessageBox.question(
                self, "Bestehende Schnitte",
                "Es gibt bereits Schnitte.\n\nJa = ersetzen\nNein = zusätzlich einfügen",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No | QtWidgets.QMessageBox.Cancel)
            if r == QtWidgets.QMessageBox.Cancel:
                return
            if r == QtWidgets.QMessageBox.Yes:
                self.cut_times, self.cut_ids = [], []
        self._last_split = sec
        k = 1
        while k * sec < dur - 1e-9:
            t = self._snap(k * sec)
            if self._cut_ok(t):
                self._insert_cut(t)
            k += 1
        self._cuts_changed()

    def merge_with_next(self):
        b = self._current_block()
        if b is None or b.nr >= len(self.model.blocks):
            return
        nxt = self.model.blocks[b.nr]  # nr ist 1-basiert -> Index des nächsten Blocks
        if nxt.start_id in self.cut_ids:
            self._remove_cut_at(self.cut_ids.index(nxt.start_id))
            self._cuts_changed()

    def _cuts_changed(self):
        self._dirty = True
        self._rebuild_blocks()

    def _rebuild_blocks(self):
        rec = self.rec
        bounds = [0.0] + self.cut_times + [rec.duration]
        ids = [0] + self.cut_ids
        meta = self.model.meta
        blocks = []
        for k, sid in enumerate(ids):
            m = meta.setdefault(sid, {"name": "", "export": True})
            blocks.append(Block(nr=k + 1, start_id=sid, start=bounds[k], end=bounds[k + 1],
                                name=m["name"], export=m["export"]))
        for key in [key for key in meta if key not in set(ids)]:
            del meta[key]
        self._block_starts = bounds[:-1]

        cur = self._current_block()
        cur_id = cur.start_id if cur else None
        self.model.set_blocks(blocks)
        if cur_id is not None and cur_id in ids:
            self._select_row(ids.index(cur_id), silent=True)
        self._update_overview()
        self._update_block_info()
        self._refresh_overlays()

    def _on_meta_changed(self):
        self._dirty = True
        self._update_overview()
        self._update_block_info()
        self._refresh_overlays()

    def _update_block_info(self):
        blocks = self.model.blocks
        exp = [b for b in blocks if b.export]
        self.lbl_blocks.setText(f"{len(blocks)} Blöcke  ·  {len(exp)} für Export  "
                                f"({fmt_time(sum(b.duration for b in exp), 0)})")

    # ----------------------------------------------------- Blockliste --
    def _block_at(self, t: float) -> Block | None:
        if not self.model.blocks:
            return None
        k = max(0, bisect.bisect_right(self._block_starts, t) - 1)
        return self.model.blocks[min(k, len(self.model.blocks) - 1)]

    def _current_block(self) -> Block | None:
        idx = self.table.selectionModel().currentIndex()
        if idx.isValid() and idx.row() < len(self.model.blocks):
            return self.model.blocks[idx.row()]
        return None

    def _select_row(self, row: int, silent: bool):
        self._silent_select = silent
        try:
            idx = self.model.index(row, 0)
            self.table.selectionModel().setCurrentIndex(
                idx, QtCore.QItemSelectionModel.ClearAndSelect | QtCore.QItemSelectionModel.Rows)
            self.table.scrollTo(idx)
        finally:
            self._silent_select = False

    def _on_block_selected(self, current, _previous):
        if self._silent_select or not current.isValid() or not self.rec:
            return
        b = self.model.blocks[current.row()]
        x0, x1 = self.view_range()
        w = x1 - x0
        self.set_view(b.start - 0.05 * w, b.start + 0.95 * w)

    def zoom_to_selected_block(self):
        b = self._current_block()
        if b:
            pad = 0.02 * b.duration
            self.set_view(b.start - pad, b.end + pad)

    # -------------------------------------------- Speichern / Laden --
    def _default_session_path(self) -> str:
        return os.path.splitext(self.rec.path)[0] + "_schnitte.json"

    def _session_data(self) -> dict:
        return {
            "edf_datei": os.path.basename(self.rec.path),
            "dauer_s": self.rec.duration,
            "schnitte_s": list(self.cut_times),
            "bloecke": [{"nr": b.nr, "name": b.name, "start_s": b.start, "ende_s": b.end,
                         "export": b.export} for b in self.model.blocks],
        }

    def save_session(self, path: str | None = None) -> bool:
        if not self.rec:
            return False
        path = path or self._default_session_path()
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(self._session_data(), fh, indent=2, ensure_ascii=False)
        except OSError:
            path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Schnitte speichern", path, "JSON (*.json)")
            if not path:
                return False
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(self._session_data(), fh, indent=2, ensure_ascii=False)
        self._dirty = False
        self.statusBar().showMessage(f"Schnitte gespeichert: {path}", 5000)
        return True

    def load_session_dialog(self):
        if not self.rec:
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Schnitte laden", os.path.dirname(self.rec.path), "JSON (*.json)")
        if path:
            self.load_session(path)

    def load_session(self, path: str):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            QtWidgets.QMessageBox.critical(self, "Fehler", f"Schnitte konnten nicht geladen werden:\n{exc}")
            return
        dur = self.rec.duration
        self.cut_times, self.cut_ids = [], []
        self.model.meta = {0: {"name": "", "export": True}}
        for t in sorted(float(x) for x in data.get("schnitte_s", [])):
            if 0 < t < dur:
                self._insert_cut(t)
        starts = [0.0] + self.cut_times
        ids = [0] + self.cut_ids
        for bd in data.get("bloecke", []):
            k = bisect.bisect_left(starts, float(bd.get("start_s", -1)) - 1e-6)
            if k < len(starts) and abs(starts[k] - float(bd["start_s"])) < 1e-6:
                self.model.meta[ids[k]] = {"name": str(bd.get("name", "")), "export": bool(bd.get("export", True))}
        self._rebuild_blocks()
        self._dirty = False
        self.statusBar().showMessage(f"{len(self.cut_times)} Schnitte geladen aus {path}", 5000)

    # ------------------------------------------------------- Export --
    def export(self):
        if not self.rec:
            return
        todo = [b for b in self.model.blocks if b.export]
        if not todo:
            QtWidgets.QMessageBox.information(self, "Export", "Kein Block ist zum Export markiert.")
            return
        out_dir = QtWidgets.QFileDialog.getExistingDirectory(
            self, f"Zielordner für {len(todo)} Blöcke wählen", os.path.dirname(self.rec.path))
        if not out_dir:
            return
        existing = [n for n in (block_filename(self.rec, b) for b in todo)
                    if os.path.exists(os.path.join(out_dir, n))]
        if existing:
            r = QtWidgets.QMessageBox.question(
                self, "Dateien überschreiben?",
                f"{len(existing)} Datei(en) existieren bereits, z.B.\n{existing[0]}\n\nÜberschreiben?")
            if r != QtWidgets.QMessageBox.Yes:
                return

        dlg = QtWidgets.QProgressDialog("Exportiere Blöcke…", "Abbrechen", 0, len(todo), self)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(300)

        def progress(k, n, b):
            dlg.setValue(k)
            dlg.setLabelText(f"Block B{b.nr} ({k + 1}/{n})")
            QtWidgets.QApplication.processEvents()
            return not dlg.wasCanceled()

        QtWidgets.QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            results = export_blocks(self.rec, self.model.blocks, out_dir, progress)
            with open(os.path.join(out_dir, f"{self.rec.basename}_schnitte.json"), "w", encoding="utf-8") as fh:
                json.dump(self._session_data(), fh, indent=2, ensure_ascii=False)
        except Exception as exc:
            QtWidgets.QApplication.restoreOverrideCursor()
            dlg.close()
            QtWidgets.QMessageBox.critical(self, "Export fehlgeschlagen", str(exc))
            return
        QtWidgets.QApplication.restoreOverrideCursor()
        dlg.setValue(len(todo))

        ok = [r for r in results if not r.error]
        failed = [r for r in results if r.error]
        trimmed = [r for r in ok if r.trimmed > 1e-6]
        msg = f"{len(ok)} Block-Datei(en) exportiert nach:\n{out_dir}\n\nÜbersicht: {self.rec.basename}_bloecke.csv"
        if len(results) < len(todo):
            msg += f"\n\nAbgebrochen nach {len(results)} von {len(todo)} Blöcken."
        if trimmed:
            msg += "\n\nAm Ende gekürzt (EDF-Datensatzraster):\n" + "\n".join(
                f"  B{r.block.nr}: {r.trimmed:.3f} s" for r in trimmed[:15])
        if failed:
            msg += "\n\nNicht exportiert:\n" + "\n".join(f"  B{r.block.nr}: {r.error}" for r in failed[:15])
        QtWidgets.QMessageBox.information(self, "Export abgeschlossen", msg)


def main():
    QtWidgets.QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QtWidgets.QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QtWidgets.QApplication(sys.argv)
    win = MainWindow(sys.argv[1] if len(sys.argv) > 1 else None)
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
