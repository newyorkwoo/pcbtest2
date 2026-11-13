"""aipcb desktop dashboard (PySide6, fully offline).

Shows the live state of a project while Claude (via MCP) or the buttons here drive the flow:
pipeline stages, schematic / PCB / 3D previews, simulations, checks, AI review verdict, BOM, log.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QProcess, Qt, QTimer
from PySide6.QtGui import QAction, QBrush, QColor, QFont, QPainter, QPixmap
from PySide6.QtSvgWidgets import QGraphicsSvgItem
from PySide6.QtWidgets import (QApplication, QComboBox, QFrame, QGraphicsScene, QGraphicsView, QHBoxLayout,
                               QHeaderView, QLabel, QListWidget, QMainWindow, QMessageBox, QPlainTextEdit,
                               QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QTextBrowser,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import config
from ..pipeline import STAGES

STAGE_LABEL = {
    "library": "元件庫 Library", "validate": "規格驗證 Validate", "schematic": "原理圖 Schematic",
    "erc": "ERC 電氣規則", "placement": "擺放 Placement", "routing": "佈線 Routing", "pcb": "PCB 產生",
    "drc": "DRC 設計規則", "simulation": "模擬 Simulation", "checks": "設計檢查 Checks",
    "previews": "預覽 Previews", "review": "AI 審核 Review", "fab": "送廠檔 Fab output",
}
STATE_COLOR = {"ok": "#1f9d55", "fail": "#d64545", "running": "#d69e2e", None: "#8a8f98"}
STATE_ICON = {"ok": "✓", "fail": "✗", "running": "…", None: "·"}
SEV_COLOR = {"error": "#d64545", "warning": "#c98a0b", "info": "#5a7bd6"}


class SvgView(QGraphicsView):
    """Zoom (wheel) / pan (drag) SVG viewer."""

    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setBackgroundBrush(QBrush(QColor("#ffffff")))
        self._path = None
        self._mtime = 0

    def load(self, path: Path | None):
        if not path or not Path(path).exists():
            self.scene().clear(); self._path = None
            self.scene().addText("尚無預覽 (no preview yet)")
            return
        mt = Path(path).stat().st_mtime
        if str(path) == str(self._path) and mt == self._mtime:
            return
        old_rect = self.scene().sceneRect() if self._path else None
        self._path, self._mtime = path, mt
        self.scene().clear()
        item = QGraphicsSvgItem(str(path))
        self.scene().addItem(item)
        self.scene().setSceneRect(item.boundingRect())
        # Keep the user's zoom on a plain refresh, but re-fit on first load or when the sheet size changed —
        # otherwise the old zoom/scroll can leave the view on empty paper.
        if old_rect is None or old_rect != item.boundingRect():
            self._fit()

    def _fit(self):
        self._zoomed = False                # follow the window size until the user zooms
        self.resetTransform()
        self.fitInView(self.scene().itemsBoundingRect(), Qt.KeepAspectRatio)

    def showEvent(self, e):
        super().showEvent(e)
        if self._path and not getattr(self, "_zoomed", False):
            self._fit()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self._path and not getattr(self, "_zoomed", False):
            self._fit()

    def wheelEvent(self, e):
        f = 1.2 if e.angleDelta().y() > 0 else 1 / 1.2
        self.scale(f, f)
        self._zoomed = True

    def mouseDoubleClickEvent(self, e):
        self._fit()


class ImageView(QLabel):
    def __init__(self):
        super().__init__("尚無 3D 渲染 (no render yet)")
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(300, 200)
        self._pix = None

    def load(self, path):
        if path and Path(path).exists():
            self._pix = QPixmap(str(path)); self._rescale()
        else:
            self._pix = None; self.setText("尚無 3D 渲染 (no render yet)")

    def resizeEvent(self, e):
        self._rescale(); super().resizeEvent(e)

    def _rescale(self):
        if self._pix:
            self.setPixmap(self._pix.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))


class Dashboard(QMainWindow):
    def __init__(self, project: str | None = None):
        super().__init__()
        self.setWindowTitle("aipcb — Claude 驅動的 EDA 儀表板")
        self.resize(1500, 950)
        self.proc: QProcess | None = None
        self._mtimes: dict[str, float] = {}

        # ---------------- left panel
        left = QWidget(); lv = QVBoxLayout(left)
        self.project_box = QComboBox()
        self.project_box.currentTextChanged.connect(self.refresh_all)
        lv.addWidget(QLabel("專案 Project")); lv.addWidget(self.project_box)
        self.verdict = QLabel(); self.verdict.setAlignment(Qt.AlignCenter)
        self.verdict.setFont(QFont("", 15, QFont.Bold)); self.verdict.setMinimumHeight(48)
        lv.addWidget(self.verdict)
        self.stages = QTreeWidget(); self.stages.setHeaderLabels(["流程階段 Stage", "狀態", "時間"])
        self.stages.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.stages.itemSelectionChanged.connect(self.show_stage_detail)
        lv.addWidget(self.stages, 3)
        self.detail = QPlainTextEdit(); self.detail.setReadOnly(True); self.detail.setFont(QFont("Menlo", 10))
        lv.addWidget(self.detail, 2)
        row = QHBoxLayout()
        for text, fn in (("▶ 全流程 Build", lambda: self.run(["build"])),
                         ("▶ 執行所選階段", self.run_selected)):
            b = QPushButton(text); b.clicked.connect(fn); row.addWidget(b)
        lv.addLayout(row)
        row = QHBoxLayout()
        for text, fn in (("KiCad 原理圖", lambda: self.open_kicad("sch")), ("KiCad PCB", lambda: self.open_kicad("pcb")),
                         ("📦 送廠檔", self.export_fab)):
            b = QPushButton(text); b.clicked.connect(fn); row.addWidget(b)
        lv.addLayout(row)

        # ---------------- tabs
        self.tabs = QTabWidget()
        self.sch_view = SvgView(); self.tabs.addTab(self.sch_view, "原理圖 Schematic")
        pcbw = QWidget(); pv = QVBoxLayout(pcbw)
        self.pcb_layer = QComboBox(); self.pcb_layer.addItems(["pcb_all", "pcb_top", "pcb_bottom"])
        self.pcb_layer.currentTextChanged.connect(self.refresh_previews)
        pv.addWidget(self.pcb_layer); self.pcb_view = SvgView(); pv.addWidget(self.pcb_view)
        self.tabs.addTab(pcbw, "PCB 佈局")
        w3 = QWidget(); v3 = QVBoxLayout(w3)
        self.view3d = QComboBox(); self.view3d.addItems(["render_iso", "render_top", "render_bottom"])
        self.view3d.currentTextChanged.connect(self.refresh_previews)
        v3.addWidget(self.view3d); self.img3d = ImageView(); v3.addWidget(self.img3d, 1)
        self.tabs.addTab(w3, "3D")
        simw = QSplitter()
        self.sim_list = QListWidget(); self.sim_list.currentTextChanged.connect(self.show_sim)
        simr = QWidget(); sv = QVBoxLayout(simr)
        self.sim_view = SvgView(); sv.addWidget(self.sim_view, 3)
        self.sim_table = QTableWidget(0, 5); self.sim_table.setHorizontalHeaderLabels(["量測", "數值", "最小", "最大", "結果"])
        self.sim_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        sv.addWidget(self.sim_table, 1)
        simw.addWidget(self.sim_list); simw.addWidget(simr); simw.setSizes([260, 1000])
        self.tabs.addTab(simw, "模擬 Simulation")
        chk = QWidget(); cv = QVBoxLayout(chk)
        self.chk_head = QLabel(); self.chk_head.setFont(QFont("", 13, QFont.Bold)); cv.addWidget(self.chk_head)
        self.chk_table = QTableWidget(0, 3); self.chk_table.setHorizontalHeaderLabels(["嚴重度", "類別", "說明"])
        self.chk_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.chk_table.setWordWrap(True)
        cv.addWidget(self.chk_table)
        self.tabs.addTab(chk, "檢查 Checks")
        self.review = QTextBrowser(); self.tabs.addTab(self.review, "AI 審核 Review")
        self.bom = QTableWidget(0, 6)
        self.bom.setHorizontalHeaderLabels(["Designator", "Qty", "Value", "Footprint", "LCSC", "MPN"])
        self.bom.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tabs.addTab(self.bom, "BOM")
        self.log = QPlainTextEdit(); self.log.setReadOnly(True); self.log.setFont(QFont("Menlo", 10))
        self.tabs.addTab(self.log, "Log")

        split = QSplitter(); split.addWidget(left); split.addWidget(self.tabs); split.setSizes([420, 1080])
        self.setCentralWidget(split)
        act = QAction("Refresh", self); act.setShortcut("Ctrl+R"); act.triggered.connect(self.refresh_all)
        self.addAction(act)

        projects = sorted(p.parent.name for p in config.PROJECTS.glob("*/design.yaml"))
        self.project_box.addItems(projects)
        if project and Path(project).name in projects:
            self.project_box.setCurrentText(Path(project).name)
        self.timer = QTimer(self); self.timer.timeout.connect(self.poll); self.timer.start(1500)
        self.refresh_all()

    # ------------------------------------------------------------- helpers
    @property
    def pd(self) -> Path:
        return config.PROJECTS / self.project_box.currentText()

    @property
    def out(self) -> Path:
        return self.pd / "out"

    def _json(self, rel, default=None):
        try:
            return json.loads((self.out / rel).read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return default

    def poll(self):
        """Refresh whatever changed on disk (Claude may be driving the flow through MCP)."""
        watch = ["status.json", "checks.json", "review.md", "preview/index.json", "sim/summary.json", "pipeline.log"]
        changed = False
        for rel in watch:
            p = self.out / rel
            mt = p.stat().st_mtime if p.exists() else 0
            if self._mtimes.get(rel) != mt:
                self._mtimes[rel] = mt; changed = True
        if changed:
            self.refresh_all()

    def refresh_all(self, *_):
        if not self.project_box.currentText():
            return
        self.refresh_stages(); self.refresh_previews(); self.refresh_sims(); self.refresh_checks()
        self.refresh_review(); self.refresh_bom(); self.refresh_log()

    def refresh_stages(self):
        st = self._json("status.json", {}) or {}
        sel = self.stages.currentItem().data(0, Qt.UserRole) if self.stages.currentItem() else None
        self.stages.clear()
        for s in STAGES:
            info = st.get(s, {})
            state = info.get("state")
            it = QTreeWidgetItem([STAGE_LABEL[s], f"{STATE_ICON[state]} {state or ''}", (info.get("time") or "")[11:]])
            it.setData(0, Qt.UserRole, s)
            it.setForeground(1, QBrush(QColor(STATE_COLOR[state])))
            self.stages.addTopLevelItem(it)
            if s == sel:
                self.stages.setCurrentItem(it)

    def show_stage_detail(self):
        it = self.stages.currentItem()
        if not it:
            return
        st = (self._json("status.json", {}) or {}).get(it.data(0, Qt.UserRole), {})
        self.detail.setPlainText(json.dumps(st.get("summary"), indent=1, ensure_ascii=False, default=str)[:20000])

    def refresh_previews(self, *_):
        idx = self._json("preview/index.json", {}) or {}
        self.sch_view.load(idx.get("schematic"))
        self.pcb_view.load(idx.get(self.pcb_layer.currentText()))
        self.img3d.load(idx.get(self.view3d.currentText()))

    def refresh_sims(self):
        sims = self._json("sim/summary.json", []) or []
        cur = self.sim_list.currentItem().text() if self.sim_list.currentItem() else None
        self.sim_list.blockSignals(True); self.sim_list.clear()
        for s in sims:
            self.sim_list.addItem(f"{'✓' if s.get('ok') else '✗'} {s['name']}")
        self.sim_list.blockSignals(False)
        if self.sim_list.count():
            items = [self.sim_list.item(i).text() for i in range(self.sim_list.count())]
            self.sim_list.setCurrentRow(items.index(cur) if cur in items else 0)
            self.show_sim(self.sim_list.currentItem().text())

    def show_sim(self, text):
        if not text:
            return
        name = text.split(" ", 1)[1]
        self.sim_view.load(self.out / "sim" / f"{name}.svg")
        sims = {s["name"]: s for s in (self._json("sim/summary.json", []) or [])}
        s = sims.get(name, {})
        checks = {c["meas"]: c for c in s.get("checks", [])}
        rows = list((s.get("measures") or {}).items())
        self.sim_table.setRowCount(len(rows))
        for r, (k, v) in enumerate(rows):
            c = checks.get(k, {})
            vals = [k, f"{v:.5g}", str(c.get("min", "")), str(c.get("max", "")),
                    ("PASS" if c.get("pass") else "FAIL") if c else ""]
            for col, txt in enumerate(vals):
                it = QTableWidgetItem(txt)
                if col == 4 and c:
                    it.setForeground(QBrush(QColor(STATE_COLOR["ok" if c.get("pass") else "fail"])))
                self.sim_table.setItem(r, col, it)

    def refresh_checks(self):
        rep = self._json("checks.json")
        if not rep:
            self.chk_head.setText("尚未執行檢查"); self.chk_table.setRowCount(0); return
        c = rep["counts"]
        drc = rep.get("drc") or {}
        self.chk_head.setText(f"{'✓ 自動檢查通過' if rep['auto_pass'] else '✗ 尚有錯誤'}   "
                              f"錯誤 {c['error']} · 警告 {c['warning']} · 資訊 {c['info']}   |   "
                              f"DRC 錯誤 {drc.get('errors', '?')} 未連接 {drc.get('unconnected', '?')}")
        f = sorted(rep["findings"], key=lambda x: ["error", "warning", "info"].index(x["severity"]))
        self.chk_table.setRowCount(len(f))
        for r, x in enumerate(f):
            for col, txt in enumerate([x["severity"], x["category"], x["message"]]):
                it = QTableWidgetItem(txt)
                if col == 0:
                    it.setForeground(QBrush(QColor(SEV_COLOR.get(x["severity"], "#333"))))
                self.chk_table.setItem(r, col, it)
        self.chk_table.resizeRowsToContents()

    def refresh_review(self):
        md = self.out / "review.md"
        rv = self._json("review.json")
        if rv:
            v = rv["verdict"]
            color = {"PASS": "#1f9d55", "CONDITIONAL": "#c98a0b", "FAIL": "#d64545"}[v]
            label = {"PASS": "可送廠 READY FOR FAB", "CONDITIONAL": "有條件通過 CONDITIONAL", "FAIL": "不可送廠 NOT READY"}[v]
            self.verdict.setText(f"AI 審核：{label}")
            self.verdict.setStyleSheet(f"background:{color};color:white;border-radius:6px;")
        else:
            self.verdict.setText("AI 審核：尚未審核"); self.verdict.setStyleSheet("background:#8a8f98;color:white;border-radius:6px;")
        self.review.setMarkdown(md.read_text(encoding="utf-8") if md.exists() else
                                "_尚未審核。請在 Claude Code 中要求 Claude 執行設計審核 (get_review_package → submit_review)。_")

    def refresh_bom(self):
        try:
            from .. import fab, spec
            d = spec.load(self.pd / "design.yaml", check=False)
            rows = fab.bom_rows(d)
        except Exception as e:  # noqa: BLE001
            rows = []; self.log.appendPlainText(f"BOM: {e}")
        self.bom.setRowCount(len(rows))
        for r, x in enumerate(rows):
            for col, k in enumerate(["Designator", "Qty", "Comment", "Footprint", "LCSC Part #", "MPN"]):
                self.bom.setItem(r, col, QTableWidgetItem(str(x.get(k, ""))))

    def refresh_log(self):
        p = self.out / "pipeline.log"
        if p.exists() and not (self.proc and self.proc.state() != QProcess.NotRunning):
            self.log.setPlainText(p.read_text(encoding="utf-8")[-30000:])
            self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    # ------------------------------------------------------------- actions
    def run(self, args: list[str]):
        if self.proc and self.proc.state() != QProcess.NotRunning:
            QMessageBox.information(self, "aipcb", "已有流程執行中"); return
        self.tabs.setCurrentWidget(self.log)
        self.log.appendPlainText(f"\n$ aipcb {' '.join(args)} {self.pd.name}\n")
        self.proc = QProcess(self)
        self.proc.setWorkingDirectory(str(config.ROOT))
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.readyReadStandardOutput.connect(
            lambda: self.log.appendPlainText(bytes(self.proc.readAllStandardOutput()).decode(errors="replace").rstrip()))
        self.proc.finished.connect(lambda *_: self.refresh_all())
        self.proc.start(sys.executable, ["-m", "aipcb", *args, self.pd.name])

    def run_selected(self):
        it = self.stages.currentItem()
        if not it:
            return
        stage = it.data(0, Qt.UserRole)
        cmd = {"placement": "place", "routing": "route", "simulation": "sim"}.get(stage, stage)
        if stage == "review":
            QMessageBox.information(self, "AI 審核", "審核由 Claude 執行：在 Claude Code 輸入「請審核 fm_radio 是否可送廠」。")
            return
        self.run([cmd])

    def export_fab(self):
        rv = self._json("review.json")
        if not rv or rv.get("verdict") != "PASS":
            r = QMessageBox.question(self, "送廠檔", "AI 審核尚未 PASS。是否仍要強制輸出（僅限打樣）？")
            if r != QMessageBox.Yes:
                return
            self.run(["fab", "--force"])
        else:
            self.run(["fab"])

    def open_kicad(self, what):
        ext = "kicad_sch" if what == "sch" else "kicad_pcb"
        files = list(self.pd.glob(f"*.{ext}"))
        if files:
            subprocess.Popen(["open", "-a", "KiCad", str(files[0])] if sys.platform == "darwin" else ["kicad", str(files[0])])


def main(project: str | None = None):
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    w = Dashboard(project)
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
