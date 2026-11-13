"""Physical wiring diagram tab: parts at their real board positions, coloured pin-to-pin wires, wire list.

Data: out/wiring.json (written by the pcb stage, see aipcb/wiring.py). Scene units: 10 per mm.
"""
from __future__ import annotations

import json
import math
import re
import subprocess
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QBrush, QColor, QFont, QIcon, QPageLayout, QPageSize, QPainter, QPainterPath, QPdfWriter,
                           QPen, QPixmap)
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QGraphicsScene, QGraphicsSimpleTextItem, QGraphicsView,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton,
                               QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

U = 10.0                       # scene units per mm
BOARD_FILL = QColor("#f4f1e8")
PART_FILL = QColor("#ffffff")
PAD_FILL = QColor("#d9a520")


def _pretty(name: str) -> str:
    name = re.sub(r"~\{([^}]*)\}", lambda m: "".join(ch + "̅" for ch in m.group(1)), str(name or ""))
    return re.sub(r"[_^]\{([^}]*)\}", r"\1", name)


def _refkey(ref):
    m = re.match(r"([A-Za-z#]+)(\d*)", ref)
    return (m.group(1), int(m.group(2) or 0)) if m else (ref, 0)


def _swatch(hexcol, w=14, h=10):
    pm = QPixmap(w, h); pm.fill(QColor(hexcol))
    return QIcon(pm)


def _seg_dist(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    L = dx * dx + dy * dy
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / L))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


class BoardView(QGraphicsView):
    def __init__(self, on_pick):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setBackgroundBrush(QBrush(QColor("#d7d9dc")))
        self.setMouseTracking(True)
        self.on_pick = on_pick
        self._press = None
        self._zoomed = False

    def fit(self, rect=None):
        self.resetTransform()
        r = rect or self.scene().itemsBoundingRect().adjusted(-40, -40, 40, 40)
        self.fitInView(r, Qt.KeepAspectRatio)
        self._zoomed = rect is not None

    def wheelEvent(self, e):
        f = 1.2 if e.angleDelta().y() > 0 else 1 / 1.2
        self.scale(f, f); self._zoomed = True

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if not self._zoomed and self.scene().items():
            self.fit()

    def mouseDoubleClickEvent(self, e):
        self.fit()

    def mousePressEvent(self, e):
        self._press = e.position(); super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        super().mouseReleaseEvent(e)
        if self._press is not None and (e.position() - self._press).manhattanLength() < 5:
            sp = self.mapToScene(e.position().toPoint())
            self.on_pick(sp.x() / U, sp.y() / U, 7.0 / (self.transform().m11() * U))
        self._press = None


class WiringDiagramTab(QWidget):
    def __init__(self):
        super().__init__()
        self.data = None
        self._key = None
        self.out_dir = None
        self.sel = None              # ("net", name) | ("part", ref) | ("wire", no)
        self.wire_items = {}         # no -> (casing, line)

        # ---- left
        left = QWidget(); lv = QVBoxLayout(left); lv.setContentsMargins(0, 0, 0, 0)
        self.search = QLineEdit(); self.search.setPlaceholderText("搜尋網路 / 元件 / 腳位 (例 SPK、U8、J4.1)")
        self.search.textChanged.connect(self.fill_list)
        lv.addWidget(self.search)
        self.chk = {}
        for key, text, on in (("signal", "訊號線", True), ("power", "電源線（紅/橙）", True),
                              ("ground", "GND 線（黑）", False), ("pins", "腳位編號", True)):
            c = QCheckBox(text); c.setChecked(on); c.toggled.connect(self.redraw)
            self.chk[key] = c; lv.addWidget(c)
        self.net_list = QListWidget()
        self.net_list.currentItemChanged.connect(lambda it, _: it and self.select(*it.data(Qt.UserRole), zoom=True))
        lv.addWidget(self.net_list, 1)
        self.stat = QLabel(); self.stat.setWordWrap(True); self.stat.setStyleSheet("color:#555")
        lv.addWidget(self.stat)

        # ---- right
        right = QSplitter(Qt.Vertical)
        top = QWidget(); tv = QVBoxLayout(top); tv.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        self.title = QLabel("實體接線圖：元件依板上實際位置，彩色線繞過元件、腳對腳接線")
        self.title.setFont(QFont("", 13, QFont.Bold))
        bar.addWidget(self.title, 1)
        for text, fn in (("全圖", lambda: self.view.fit()), ("清除標示", self.clear),
                         ("匯出接線表 CSV", self.export_csv), ("匯出 PDF", self.export_pdf)):
            b = QPushButton(text); b.clicked.connect(fn); bar.addWidget(b)
        tv.addLayout(bar)
        self.view = BoardView(self.pick)
        tv.addWidget(self.view, 1)
        right.addWidget(top)
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(["線號", "線色", "網路", "起點", "起點腳名", "終點", "終點腳名", "走線長度 mm"])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.currentCellChanged.connect(self.table_pick)
        right.addWidget(self.table)
        right.setSizes([620, 240])

        split = QSplitter(); split.addWidget(left); split.addWidget(right); split.setSizes([250, 1000])
        lay = QVBoxLayout(self); lay.setContentsMargins(0, 0, 0, 0); lay.addWidget(split)

    # ------------------------------------------------------------------ data
    def load(self, out_dir: Path):
        self.out_dir = out_dir
        f = out_dir / "wiring.json"
        key = (str(f), f.stat().st_mtime if f.exists() else 0)
        if key == self._key:
            return
        self._key = key
        try:
            self.data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            self.data = None
        if not self.data:
            self.view.scene().clear()
            self.view.scene().addText("尚無接線資料：請執行「PCB 產生」階段（或全流程 Build）")
            self.net_list.clear(); self.table.setRowCount(0); self.stat.setText("")
            return
        self.by_no = {w["no"]: w for w in self.data["wires"]}
        self.fill_list()
        self.redraw()
        self.view.fit()
        w = self.data["wires"]
        self.stat.setText(f"{len(self.data['parts'])} 個元件 · {len(self.data['nets'])} 個網路 · {len(w)} 條線，"
                          f"總長 {sum(x['length'] for x in w) / 10:.1f} cm\n點線 / 腳位 / 元件可標示；滾輪縮放、雙擊回全圖")
        if self.sel:
            self.select(*self.sel, zoom=False)
        else:
            self.fill_table(self.data["wires"])

    def _visible(self, w):
        n = self.data["nets"][w["net"]]
        if n["ground"]:
            return self.chk["ground"].isChecked()
        if n["supply"]:
            return self.chk["power"].isChecked()
        return self.chk["signal"].isChecked()

    def fill_list(self, *_):
        if not self.data:
            return
        q = self.search.text().strip().lower()
        self.net_list.blockSignals(True); self.net_list.clear()
        nets = self.data["nets"]
        for n in sorted(nets, key=lambda n: (not nets[n]["supply"], n.lower())):
            e = nets[n]
            if q and q not in n.lower() and not any(q == p.lower() or p.lower().startswith(q + ".") for p in e["pins"]):
                continue
            it = QListWidgetItem(_swatch(e["color"]), f"{n}  ·  {e['color_name']}  ({len(e['pins'])} 腳)")
            it.setData(Qt.UserRole, ("net", n))
            self.net_list.addItem(it)
        if q:
            for ref in sorted(self.data["parts"], key=_refkey):
                p = self.data["parts"][ref]
                if q in ref.lower() or q in p["value"].lower():
                    it = QListWidgetItem(f"▣ {ref}  {p['value']}")
                    it.setData(Qt.UserRole, ("part", ref))
                    self.net_list.addItem(it)
        self.net_list.blockSignals(False)

    # ------------------------------------------------------------------ drawing
    def redraw(self, *_):
        if not self.data:
            return
        sc = self.view.scene()
        sc.clear()
        self.wire_items = {}
        b = self.data.get("board")
        if b:
            r = QRectF(b[0] * U, b[1] * U, (b[2] - b[0]) * U, (b[3] - b[1]) * U)
            sc.addRect(r, QPen(QColor("#556"), 0.25 * U), QBrush(BOARD_FILL))
        hi = self._highlight_set()
        dim = bool(hi)
        show_pins = self.chk["pins"].isChecked()
        f_ref = QFont(); f_ref.setPixelSize(int(1.3 * U)); f_ref.setBold(True)
        f_pin = QFont(); f_pin.setPixelSize(max(1, int(0.55 * U)))
        sel_parts = hi.get("parts", set())
        for ref, p in self.data["parts"].items():
            x0, y0, x1, y1 = p.get("body") or p["bbox"]
            on = ref in sel_parts
            pen = QPen(QColor("#1e6fd9") if on else QColor("#8a8f98"), (0.3 if on else 0.12) * U,
                       Qt.SolidLine if p["side"] == "F" else Qt.DashLine)
            fill = QColor("#e6f0ff") if on else PART_FILL
            rect = sc.addRect(QRectF(x0 * U, y0 * U, (x1 - x0) * U, (y1 - y0) * U), pen, QBrush(fill))
            rect.setOpacity(0.55 if dim and not on else 1.0)
            rect.setToolTip(f"{ref}  {p['value']}\n區塊 {p['block']} · {'背面' if p['side'] == 'B' else '正面'}")
            for pad in p["pads"]:
                pw, ph = max(pad["w"], 0.25), max(pad["h"], 0.25)
                col = PAD_FILL if pad["net"] else QColor("#bbbbbb")
                it = sc.addRect(QRectF((pad["x"] - pw / 2) * U, (pad["y"] - ph / 2) * U, pw * U, ph * U),
                                QPen(Qt.NoPen), QBrush(col))
                it.setOpacity(0.5 if dim and not on else 1.0)
                it.setToolTip(f"{ref}.{pad['num']}  {_pretty(pad['name'])}\n網路 {pad['net'] or '未連接'}")
                if show_pins and pad["num"]:
                    t = QGraphicsSimpleTextItem(pad["num"]); t.setFont(f_pin)
                    t.setBrush(QBrush(QColor("#3a2a00")))
                    br = t.boundingRect()
                    t.setPos(pad["x"] * U - br.width() / 2, pad["y"] * U - br.height() / 2)
                    t.setZValue(3); sc.addItem(t)
            t = QGraphicsSimpleTextItem(ref); t.setFont(f_ref)
            t.setBrush(QBrush(QColor("#1e6fd9") if on else QColor("#2b2f36")))
            br = t.boundingRect()
            t.setPos((x0 + x1) / 2 * U - br.width() / 2, y0 * U - br.height() - 0.2 * U)
            t.setZValue(4); t.setOpacity(0.5 if dim and not on else 1.0); sc.addItem(t)
        # wires: ground under rails under signals; highlighted ones on top
        order = sorted(self.data["wires"], key=lambda w: (self.data["nets"][w["net"]]["ground"] is False,
                                                           self.data["nets"][w["net"]]["supply"] is False))
        hw, hn = hi.get("wires", set()), hi.get("nets", set())
        for w in order:
            lit = w["no"] in hw or w["net"] in hn
            if not lit and not self._visible(w):
                continue
            col = QColor(w["color"])
            width = 0.55 if lit else 0.32
            pts = w.get("path") or [[w["x1"], w["y1"]], [w["x2"], w["y2"]]]
            path = QPainterPath(QPointF(pts[0][0] * U, pts[0][1] * U))
            for x, y in pts[1:]:
                path.lineTo(x * U, y * U)
            casing = sc.addPath(path, QPen(QColor(255, 255, 255, 230), (width + 0.22) * U, Qt.SolidLine, Qt.RoundCap,
                                           Qt.RoundJoin))
            line = sc.addPath(path, QPen(col, width * U, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            tip = (f"線 #{w['no']} · {w['color_name']} · 網路 {w['net']}\n"
                   f"{w['from']} {_pretty(w['from_name'])}  →  {w['to']} {_pretty(w['to_name'])}\n"
                   f"走線長度 {w['length']} mm（直線 {w.get('straight', w['length'])} mm）")
            line.setToolTip(tip); casing.setToolTip(tip)
            z = 20 if lit else 10
            casing.setZValue(z); line.setZValue(z + 1)
            if dim and not lit:
                casing.setOpacity(0.25); line.setOpacity(0.25)
            for x, y in ((w["x1"], w["y1"]), (w["x2"], w["y2"])):
                r = (0.45 if lit else 0.3) * U
                dot = sc.addEllipse(x * U - r, y * U - r, 2 * r, 2 * r, QPen(QColor("white"), 0.08 * U), QBrush(col))
                dot.setZValue(z + 2); dot.setOpacity(line.opacity())
            self.wire_items[w["no"]] = (casing, line)
        self._legend(sc)

    def _legend(self, sc):
        b = self.data.get("board") or [0, 0, 0, 0]
        x, y = b[2] + 4, b[1]
        f = QFont(); f.setPixelSize(int(1.4 * U))
        t = QGraphicsSimpleTextItem("線色圖例"); f2 = QFont(f); f2.setBold(True); t.setFont(f2)
        t.setPos(x * U, y * U); sc.addItem(t)
        y += 2.6
        nets = self.data["nets"]
        for n in sorted(nets, key=lambda n: (not nets[n]["ground"], not nets[n]["supply"], n)):
            e = nets[n]
            if not (e["supply"] or e["ground"]):
                continue
            sc.addRect(QRectF(x * U, (y - 0.5) * U, 3 * U, 1.0 * U), QPen(Qt.NoPen), QBrush(QColor(e["color"])))
            t = QGraphicsSimpleTextItem(f"{n}（{e['color_name']}）"); t.setFont(f)
            t.setPos((x + 4) * U, (y - 1.0) * U); sc.addItem(t)
            y += 2.2
        t = QGraphicsSimpleTextItem("訊號線：各網路不同顏色\n虛線框 = 背面元件"); t.setFont(f)
        t.setBrush(QBrush(QColor("#555"))); t.setPos(x * U, y * U); sc.addItem(t)

    # ------------------------------------------------------------------ selection
    def _highlight_set(self):
        if not self.sel or not self.data:
            return {}
        kind, name = self.sel
        if kind == "net":
            pins = {p.split(".")[0] for p in self.data["nets"].get(name, {}).get("pins", [])}
            return {"nets": {name}, "parts": pins}
        if kind == "wire":
            w = self.by_no.get(name)
            return {"wires": {name}, "parts": {w["from"].split(".")[0], w["to"].split(".")[0]}} if w else {}
        if kind == "part":
            nets = {pd["net"] for pd in self.data["parts"][name]["pads"] if pd["net"]}
            nets = {n for n in nets if not self.data["nets"][n]["ground"]}
            return {"nets": nets, "parts": {name}}
        return {}

    def select(self, kind, name, zoom=False):
        if not self.data:
            return
        self.sel = (kind, name)
        self.redraw()
        if kind == "net":
            e = self.data["nets"][name]
            ws = [w for w in self.data["wires"] if w["net"] == name]
            self.title.setText(f"網路 {name} · 線色 {e['color_name']} · {len(e['pins'])} 個腳位 · {len(ws)} 條線")
            self.fill_table(ws)
        elif kind == "part":
            p = self.data["parts"][name]
            ws = [w for w in self.data["wires"] if w["from"].split(".")[0] == name or w["to"].split(".")[0] == name]
            self.title.setText(f"元件 {name} · {p['value']} · {len(p['pads'])} 個焊點 · 接出 {len(ws)} 條線")
            self.fill_table(ws)
        elif kind == "wire":
            w = self.by_no[name]
            self.title.setText(f"線 #{name} · {w['color_name']} · {w['net']}：{w['from']} → {w['to']}（{w['length']} mm）")
        if zoom:
            self.zoom_to_sel()

    def zoom_to_sel(self):
        kind, name = self.sel
        if kind == "net":
            ws = [w for w in self.data["wires"] if w["net"] == name]
            xs = [q[0] for w in ws for q in w.get("path", [])] or [w["x1"] for w in ws]
            ys = [q[1] for w in ws for q in w.get("path", [])] or [w["y1"] for w in ws]
        elif kind == "part":
            b = self.data["parts"][name]["bbox"]; xs, ys = [b[0], b[2]], [b[1], b[3]]
        else:
            w = self.by_no[name]
            xs = [q[0] for q in w.get("path", [[w["x1"], w["y1"]], [w["x2"], w["y2"]]])]
            ys = [q[1] for q in w.get("path", [[w["x1"], w["y1"]], [w["x2"], w["y2"]]])]
        if not xs:
            return
        pad = 6
        cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2      # at least a 36 x 24 mm window
        xs += [cx - 18 + pad, cx + 18 - pad]; ys += [cy - 12 + pad, cy + 12 - pad]
        r = QRectF((min(xs) - pad) * U, (min(ys) - pad) * U, (max(xs) - min(xs) + 2 * pad) * U,
                   (max(ys) - min(ys) + 2 * pad) * U)
        self.view.fit(r)

    def pick(self, x, y, tol):
        if not self.data:
            return
        tol = min(tol, 1.5)
        for ref, p in self.data["parts"].items():          # a pad first
            for pad in p["pads"]:
                if abs(pad["x"] - x) <= pad["w"] / 2 + 0.1 and abs(pad["y"] - y) <= pad["h"] / 2 + 0.1 and pad["net"]:
                    self.select("net", pad["net"])
                    self.title.setText(self.title.text() + f"　｜　點選 {ref}.{pad['num']} {_pretty(pad['name'])}")
                    return
        best = (tol, None)
        for w in self.data["wires"]:
            if not self._visible(w) and not (self.sel and self.sel == ("net", w["net"])):
                continue
            pts = w.get("path") or [[w["x1"], w["y1"]], [w["x2"], w["y2"]]]
            d = min(_seg_dist(x, y, *pts[i], *pts[i + 1]) for i in range(len(pts) - 1))
            if d < best[0]:
                best = (d, w)
        if best[1]:
            self.select("net", best[1]["net"])
            self._select_row(best[1]["no"])
            return
        hits = [((b[2] - b[0]) * (b[3] - b[1]), ref) for ref, p in self.data["parts"].items()
                for b in [p["bbox"]] if b[0] <= x <= b[2] and b[1] <= y <= b[3]]
        if hits:
            self.select("part", min(hits)[1])
        else:
            self.clear()

    def _select_row(self, no):
        for r in range(self.table.rowCount()):
            if self.table.item(r, 0) and self.table.item(r, 0).data(Qt.UserRole) == no:
                self.table.blockSignals(True); self.table.selectRow(r); self.table.blockSignals(False)
                self.table.scrollToItem(self.table.item(r, 0))
                return

    def fill_table(self, wires):
        self.table.blockSignals(True)
        self.table.setRowCount(len(wires))
        for r, w in enumerate(wires):
            vals = [w["no"], w["color_name"], w["net"], w["from"], _pretty(w["from_name"]), w["to"],
                    _pretty(w["to_name"]), w["length"]]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(str(v))
                if c == 0:
                    it.setData(Qt.UserRole, w["no"])
                if c == 1:
                    it.setIcon(_swatch(w["color"]))
                self.table.setItem(r, c, it)
        hh = self.table.horizontalHeader()
        for c in range(8):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(6, QHeaderView.Stretch)
        self.table.blockSignals(False)

    def table_pick(self, row, *_):
        it = self.table.item(row, 0) if row >= 0 else None
        if not it:
            return
        no = it.data(Qt.UserRole)
        self.sel = ("wire", no)          # light this wire; the table keeps its current list
        self.redraw()
        w = self.by_no[no]
        self.title.setText(f"線 #{no} · {w['color_name']} · {w['net']}：{w['from']} {_pretty(w['from_name'])} → "
                           f"{w['to']} {_pretty(w['to_name'])}（{w['length']} mm）")
        self.zoom_to_sel()

    def clear(self):
        self.sel = None
        self.title.setText("實體接線圖：元件依板上實際位置，彩色線繞過元件、腳對腳接線")
        self.net_list.clearSelection()
        if self.data:
            self.redraw()
            self.fill_table(self.data["wires"])

    # ------------------------------------------------------------------ export
    def export_csv(self):
        if not self.data or not self.out_dir:
            return
        import csv
        path = self.out_dir / "wiring_list.csv"
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            wr = csv.writer(f)
            wr.writerow(["線號", "線色", "網路", "起點 (元件.腳)", "起點腳名", "終點 (元件.腳)", "終點腳名", "走線長度 mm"])
            for w in self.data["wires"]:
                wr.writerow([w["no"], w["color_name"], w["net"], w["from"], _pretty(w["from_name"]), w["to"],
                             _pretty(w["to_name"]), w["length"]])
        subprocess.run(["open", "-R", str(path)])
        self.title.setText(f"已匯出接線表：{path}")

    def export_pdf(self):
        if not self.data or not self.out_dir:
            return
        path = self.out_dir / "wiring_diagram.pdf"
        pdf = QPdfWriter(str(path))
        pdf.setPageSize(QPageSize(QPageSize.A3))
        pdf.setPageOrientation(QPageLayout.Landscape)
        pdf.setResolution(300)
        painter = QPainter(pdf)
        sc = self.view.scene()
        src = sc.itemsBoundingRect().adjusted(-20, -20, 20, 20)
        sc.render(painter, QRectF(), src, Qt.KeepAspectRatio)
        painter.end()
        subprocess.run(["open", "-R", str(path)])
        self.title.setText(f"已匯出 PDF：{path}（目前顯示的線與標示）")
