"""Interactive wiring view: the schematic sheet with net / part highlighting and a connection table.

Data: out/schematic_map.json (written by schematic.generate together with out/preview/schematic_sheet.svg),
in sheet millimetres. Click a wire or pin -> its whole net lights up; click a part -> its outline and pins;
pick from the lists or the table to jump there.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtSvgWidgets import QGraphicsSvgItem
from PySide6.QtWidgets import (QAbstractItemView, QGraphicsScene, QGraphicsView, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QPushButton, QSplitter, QTableWidget,
                               QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

NET_COLOR = QColor("#ff6a00")
PART_COLOR = QColor("#1e6fd9")


def _refkey(ref):
    m = re.match(r"([A-Za-z#]+)(\d*)", ref)
    return (m.group(1), int(m.group(2) or 0)) if m else (ref, 0)


def _pretty(name: str) -> str:
    """KiCad markup -> plain text: ~{SD} -> S̅D̅ (active low), _{x} / ^{x} -> x"""
    name = re.sub(r"~\{([^}]*)\}", lambda m: "".join(ch + "\u0305" for ch in m.group(1)), str(name or ""))
    return re.sub(r"[_^]\{([^}]*)\}", r"\1", name)


def _seg_dist(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    L = dx * dx + dy * dy
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / L))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


class SheetView(QGraphicsView):
    """Schematic sheet + highlight overlay. Wheel zoom, drag pan, click to pick, double-click to fit."""

    def __init__(self, on_pick):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setBackgroundBrush(QBrush(QColor("#e9e9e6")))
        self.on_pick = on_pick
        self.k = 1.0             # scene units per mm
        self.svg = None
        self.overlay = []
        self._svg_key = None
        self._press = None
        self._zoomed = False

    # ---------------------------------------------------------------- content
    def load(self, svg_path, paper):
        key = (svg_path, Path(svg_path).stat().st_mtime if svg_path and Path(svg_path).exists() else 0)
        if key == self._svg_key:
            return
        self._svg_key = key
        self.scene().clear(); self.overlay = []; self.svg = None
        if not svg_path or not Path(svg_path).exists():
            self.scene().addText("尚無原理圖 — 請先執行「原理圖 Schematic」階段")
            return
        self.svg = QGraphicsSvgItem(str(svg_path))
        self.scene().addItem(self.svg)
        br = self.svg.boundingRect()
        vb = self.svg.renderer().viewBoxF()
        self.k = br.width() / (vb.width() or paper[0])
        self.scene().setSceneRect(br)
        self.fit()

    def fit(self, rect: QRectF | None = None):
        self.resetTransform()
        self.fitInView(rect or self.scene().sceneRect(), Qt.KeepAspectRatio)
        self._zoomed = rect is not None

    def P(self, x, y):
        return QPointF(x * self.k, y * self.k)

    def clear_overlay(self):
        for it in self.overlay:
            self.scene().removeItem(it)
        self.overlay = []

    def highlight(self, wires=(), pins=(), marks=(), boxes=(), color=NET_COLOR, dim=True, extra_pins=()):
        self.clear_overlay()
        if not self.svg:
            return
        sc, k = self.scene(), self.k
        if dim:
            veil = sc.addRect(self.scene().sceneRect(), QPen(Qt.NoPen), QBrush(QColor(255, 255, 255, 150)))
            self.overlay.append(veil)
        glow = QColor(color); glow.setAlpha(110)
        for w in (QPen(glow, 1.6 * k, Qt.SolidLine, Qt.RoundCap), QPen(color, 0.45 * k, Qt.SolidLine, Qt.RoundCap)):
            for x1, y1, x2, y2 in wires:
                self.overlay.append(sc.addLine(x1 * k, y1 * k, x2 * k, y2 * k, w))
        for x0, y0, x1, y1 in boxes:
            r = QRectF(self.P(x0 - 0.8, y0 - 0.8), self.P(x1 + 0.8, y1 + 0.8))
            fill = QColor(color); fill.setAlpha(35)
            self.overlay.append(sc.addRect(r, QPen(color, 0.4 * k, Qt.DashLine), QBrush(fill)))
        for x, y in marks:
            r = 1.3 * k
            self.overlay.append(sc.addEllipse(x * k - r, y * k - r, 2 * r, 2 * r, QPen(color, 0.35 * k), QBrush(Qt.NoBrush)))
        for x, y in pins:
            r = 0.9 * k
            self.overlay.append(sc.addEllipse(x * k - r, y * k - r, 2 * r, 2 * r, QPen(color.darker(130), 0.2 * k),
                                              QBrush(color)))
        for x, y, c in extra_pins:
            r = 0.7 * k
            self.overlay.append(sc.addEllipse(x * k - r, y * k - r, 2 * r, 2 * r, QPen(Qt.NoPen), QBrush(c)))
        for it in self.overlay:
            it.setZValue(10)
        if self.overlay and dim:
            self.overlay[0].setZValue(5)

    def zoom_to(self, xs, ys, pad=12.0):
        if not xs:
            return
        r = QRectF(self.P(min(xs) - pad, min(ys) - pad), self.P(max(xs) + pad, max(ys) + pad))
        # do not zoom in absurdly far on a single point, nor out past the sheet
        min_w = 90 * self.k
        if r.width() < min_w:
            c = r.center(); r = QRectF(c.x() - min_w / 2, c.y() - r.height() / 2, min_w, r.height())
        if r.height() < min_w * 0.6:
            c = r.center(); r = QRectF(r.x(), c.y() - min_w * 0.3, r.width(), min_w * 0.6)
        self.fit(r)

    # ---------------------------------------------------------------- interaction
    def wheelEvent(self, e):
        f = 1.2 if e.angleDelta().y() > 0 else 1 / 1.2
        self.scale(f, f); self._zoomed = True

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.svg and not self._zoomed:
            self.fit()

    def mouseDoubleClickEvent(self, e):
        self.fit()

    def mousePressEvent(self, e):
        self._press = e.position()
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        super().mouseReleaseEvent(e)
        if self._press is not None and (e.position() - self._press).manhattanLength() < 5 and self.svg:
            sp = self.mapToScene(e.position().toPoint())
            self.on_pick(sp.x() / self.k, sp.y() / self.k, 6.0 / (self.transform().m11() * self.k))
        self._press = None


class WiringTab(QWidget):
    def __init__(self):
        super().__init__()
        self.map = None
        self._map_key = None
        self.sel = None          # ("net", name) | ("part", ref)

        # left: search + lists
        left = QWidget(); lv = QVBoxLayout(left); lv.setContentsMargins(0, 0, 0, 0)
        self.search = QLineEdit(); self.search.setPlaceholderText("搜尋網路或元件… (例如 I2C、U1、GND)")
        self.search.textChanged.connect(self.fill_lists)
        lv.addWidget(self.search)
        self.lists = QTabWidget()
        self.net_list = QListWidget(); self.part_list = QListWidget()
        self.net_list.currentItemChanged.connect(lambda it, _: it and self.select("net", it.data(Qt.UserRole), zoom=True))
        self.part_list.currentItemChanged.connect(lambda it, _: it and self.select("part", it.data(Qt.UserRole), zoom=True))
        self.lists.addTab(self.net_list, "網路 Nets"); self.lists.addTab(self.part_list, "元件 Parts")
        lv.addWidget(self.lists, 1)
        self.stat = QLabel(); self.stat.setWordWrap(True); self.stat.setStyleSheet("color:#555")
        lv.addWidget(self.stat)

        # centre: sheet + table
        right = QSplitter(Qt.Vertical)
        top = QWidget(); tv = QVBoxLayout(top); tv.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        self.title = QLabel("點選圖上的導線、接腳或元件，或從左側清單挑選")
        self.title.setFont(QFont("", 13, QFont.Bold))
        bar.addWidget(self.title, 1)
        for text, fn in (("全圖", self.show_all), ("清除標示", self.clear)):
            b = QPushButton(text); b.clicked.connect(fn); bar.addWidget(b)
        tv.addLayout(bar)
        self.view = SheetView(self.pick)
        tv.addWidget(self.view, 1)
        right.addWidget(top)
        self.table = QTableWidget(0, 5)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.cellDoubleClicked.connect(self.table_jump)
        self.table.currentCellChanged.connect(self.table_peek)
        right.addWidget(self.table)
        right.setSizes([640, 220])

        split = QSplitter(); split.addWidget(left); split.addWidget(right); split.setSizes([260, 1000])
        lay = QVBoxLayout(self); lay.setContentsMargins(0, 0, 0, 0); lay.addWidget(split)

    # ---------------------------------------------------------------- data
    def load(self, out_dir: Path):
        f = out_dir / "schematic_map.json"
        key = (str(f), f.stat().st_mtime if f.exists() else 0)
        if key == self._map_key:
            return
        self._map_key = key
        try:
            self.map = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            self.map = None
        if not self.map:
            self.view.load(None, (594, 420))
            self.net_list.clear(); self.part_list.clear(); self.table.setRowCount(0)
            self.stat.setText("尚無接線資料：請執行「原理圖 Schematic」階段（或全流程 Build）。")
            return
        svg = self.map.get("svg")
        if svg and not Path(svg).exists():
            svg = str(out_dir / "preview" / "schematic_sheet.svg")
        self.view.load(svg, self.map.get("paper", (594, 420)))
        self.fill_lists()
        n_pins = sum(len(p["pins"]) for p in self.map["parts"].values())
        self.stat.setText(f"{len(self.map['parts'])} 個元件 · {len(self.map['design_nets'])} 個網路 · {n_pins} 個接腳"
                          "\n滾輪縮放、拖曳平移、雙擊回到全圖")
        sel, self.sel = self.sel, None
        if sel and ((sel[0] == "net" and sel[1] in self.map["design_nets"]) or
                    (sel[0] == "part" and sel[1] in self.map["parts"])):
            self.select(*sel, zoom=False)
        else:
            self.clear()

    def fill_lists(self, *_):
        if not self.map:
            return
        q = self.search.text().strip().lower()
        self.net_list.blockSignals(True); self.part_list.blockSignals(True)
        self.net_list.clear(); self.part_list.clear()
        supply = set(self.map.get("supply", []))
        for n in sorted(self.map["design_nets"], key=lambda n: (n not in supply, n.lower())):
            pins = self.map["design_nets"][n]
            if q and q not in n.lower() and not any(q == f"{r}.{p}".lower() or q == r.lower() for r, p in pins):
                continue
            it = QListWidgetItem(f"{'⏚ ' if n in supply else ''}{n}   ({len(pins)})")
            it.setData(Qt.UserRole, n)
            self.net_list.addItem(it)
        for ref in sorted(self.map["parts"], key=_refkey):
            p = self.map["parts"][ref]
            if q and q not in ref.lower() and q not in p["value"].lower() and q not in p["block"].lower():
                continue
            it = QListWidgetItem(f"{ref}   {p['value']}")
            it.setData(Qt.UserRole, ref)
            it.setToolTip(f"區塊 {p['block']}")
            self.part_list.addItem(it)
        self.net_list.blockSignals(False); self.part_list.blockSignals(False)
        if q:
            if self.net_list.count() == 0 and self.part_list.count():
                self.lists.setCurrentIndex(1)
            elif self.part_list.count() == 0 and self.net_list.count():
                self.lists.setCurrentIndex(0)

    # ---------------------------------------------------------------- selection
    def pick(self, x, y, tol):
        """click on the sheet: nearest pin / wire / mark -> net, else the part under the cursor"""
        if not self.map:
            return
        best = (min(tol, 1.6), None)
        for n, e in self.map["nets"].items():
            for r, p, px, py in e["pins"]:
                d = math.hypot(px - x, py - y)
                if d < best[0]:
                    best = (d, ("net", n))
            for x1, y1, x2, y2 in e["wires"]:
                d = _seg_dist(x, y, x1, y1, x2, y2)
                if d < best[0]:
                    best = (d, ("net", n))
            for mx, my in e["marks"]:
                d = math.hypot(mx - x, my - y)
                if d < best[0] * 1.5:
                    best = (d, ("net", n))
        if best[1]:
            self.select(*best[1], zoom=False)
            return
        hits = [(abs((b[2] - b[0]) * (b[3] - b[1])), ref) for ref, p in self.map["parts"].items()
                for b in [p["bbox"]] if b[0] - 0.5 <= x <= b[2] + 0.5 and b[1] - 0.5 <= y <= b[3] + 0.5]
        if hits:
            self.select("part", min(hits)[1], zoom=False)
        else:
            self.clear()

    def select(self, kind, name, zoom=False):
        if not self.map:
            return
        self.sel = (kind, name)
        if kind == "net":
            self._show_net(name, zoom)
        else:
            self._show_part(name, zoom)
        self._sync_list(kind, name)

    def _sync_list(self, kind, name):
        lst = self.net_list if kind == "net" else self.part_list
        self.lists.setCurrentIndex(0 if kind == "net" else 1)
        lst.blockSignals(True)
        for i in range(lst.count()):
            if lst.item(i).data(Qt.UserRole) == name:
                lst.setCurrentRow(i); lst.scrollToItem(lst.item(i))
                break
        else:
            lst.clearSelection()
        lst.blockSignals(False)

    def _pin_name(self, ref, num):
        for n, name, *_ in self.map["parts"].get(ref, {}).get("pins", []):
            if n == num:
                return _pretty(name)
        return ""

    def _show_net(self, net, zoom):
        e = self.map["nets"].get(net, {"wires": [], "pins": [], "marks": []})
        pins = self.map["design_nets"].get(net, [])
        supply = net in self.map.get("supply", [])
        # a supply net is drawn as many separate power symbols: show every one of them
        self.view.highlight(e["wires"], [(px, py) for _, _, px, py in e["pins"]], e["marks"])
        kind = "電源網路（以電源符號連接）" if supply else "訊號網路"
        self.title.setText(f"網路 {net} — {len(pins)} 個接點 · {kind}")
        self._table(["元件", "腳位", "腳名", "數值", "區塊"],
                    [[r, p, self._pin_name(r, p), self.map["parts"].get(r, {}).get("value", ""),
                      self.map["parts"].get(r, {}).get("block", "")] for r, p in sorted(pins, key=lambda t: (_refkey(t[0]), t[1]))],
                    jump=[("part", r) for r, p in sorted(pins, key=lambda t: (_refkey(t[0]), t[1]))])
        if zoom:
            xs = [px for *_, px, py in e["pins"]] + [w[0] for w in e["wires"]] + [w[2] for w in e["wires"]]
            ys = [py for *_, px, py in e["pins"]] + [w[1] for w in e["wires"]] + [w[3] for w in e["wires"]]
            if supply or not xs:
                self.view.fit()
            else:
                self.view.zoom_to(xs, ys)

    def _show_part(self, ref, zoom):
        p = self.map["parts"][ref]
        rows, jump, extra = [], [], []
        for num, name, net, px, py in sorted(p["pins"], key=lambda t: (len(t[0]), t[0])):
            others = [f"{r}.{q}" for r, q in self.map["design_nets"].get(net, []) if (r, q) != (ref, num)] if net else []
            if net and net in self.map.get("supply", []):
                to = f"電源 {net}（{len(others)} 個接點）"
            elif net:
                to = ", ".join(others[:8]) + (" …" if len(others) > 8 else "")
            else:
                to = "未連接 (NC)"
            rows.append([num, _pretty(name), net or "—", to])
            jump.append(("net", net) if net else None)
            extra.append((px, py, NET_COLOR if net else QColor("#888888")))
        # the part's own signal wires, faintly, so its connections are visible at a glance
        wires = []
        for _, _, net, *_ in p["pins"]:
            if net and net not in self.map.get("supply", []):
                wires += self.map["nets"].get(net, {}).get("wires", [])
        self.view.highlight(wires, [], [], [p["bbox"]], color=PART_COLOR, extra_pins=extra)
        self.title.setText(f"元件 {ref} — {p['value']} · 區塊 {p['block']} · {len(p['pins'])} 腳")
        self._table(["腳位", "腳名", "網路", "連到"], rows, jump=jump)
        if zoom:
            b = p["bbox"]
            self.view.zoom_to([b[0], b[2]], [b[1], b[3]], pad=20)

    def _table(self, head, rows, jump):
        self._jump = jump
        self.table.blockSignals(True)
        self.table.clear()
        self.table.setColumnCount(len(head)); self.table.setHorizontalHeaderLabels(head)
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, v in enumerate(row):
                self.table.setItem(r, c, QTableWidgetItem(str(v)))
        hh = self.table.horizontalHeader()
        for c in range(len(head)):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(len(head) - 1, QHeaderView.Stretch)
        self.table.blockSignals(False)

    def table_peek(self, row, *_):
        """single click on a row of a part's table: preview that pin's net without leaving the part"""
        if not self.sel or self.sel[0] != "part" or row < 0 or row >= len(self._jump) or not self._jump[row]:
            return
        net = self._jump[row][1]
        p = self.map["parts"][self.sel[1]]
        e = self.map["nets"].get(net, {"wires": [], "pins": [], "marks": []})
        self.view.highlight(e["wires"], [(px, py) for _, _, px, py in e["pins"]], e["marks"], [p["bbox"]])

    def table_jump(self, row, _col):
        if 0 <= row < len(self._jump) and self._jump[row]:
            self.select(*self._jump[row], zoom=True)

    def show_all(self):
        self.view.fit()

    def clear(self):
        self.sel = None
        self.view.clear_overlay()
        self.title.setText("點選圖上的導線、接腳或元件，或從左側清單挑選")
        self._table(["元件", "腳位", "腳名", "數值", "區塊"], [], [])
        self.net_list.clearSelection(); self.part_list.clearSelection()
