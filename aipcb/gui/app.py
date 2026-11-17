"""aipcb desktop dashboard (PySide6, fully offline).

Shows the live state of a project while Claude (via MCP) or the buttons here drive the flow:
pipeline stages, schematic / interactive wiring view / PCB / 3D previews, simulations, checks, AI review verdict, BOM, log.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QProcess, QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QBrush, QColor, QDesktopServices, QFont, QKeySequence, QPainter, QPixmap
from PySide6.QtSvgWidgets import QGraphicsSvgItem
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QFrame, QLineEdit, QListWidgetItem, QStackedWidget, QGraphicsScene, QGraphicsView, QHBoxLayout,
                               QHeaderView, QInputDialog, QLabel, QListWidget, QMainWindow, QMessageBox, QPlainTextEdit,
                               QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QTextBrowser,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import config
from ..pipeline import STAGES
from .design_info import DesignInfoPanel
from .wiring import WiringTab
from .zoom import ZoomableView, ZoomBar
from .wiring_diagram import WiringDiagramTab

STAGE_LABEL = {
    "library": "元件庫 Library", "validate": "規格驗證 Validate", "schematic": "原理圖 Schematic",
    "erc": "ERC 電氣規則", "placement": "擺放 Placement", "routing": "佈線 Routing", "pcb": "PCB 產生",
    "drc": "DRC 設計規則", "simulation": "模擬 Simulation", "checks": "設計檢查 Checks",
    "previews": "預覽 Previews", "review": "AI 審核 Review", "fab": "送廠檔 Fab output",
}
STATE_COLOR = {"ok": "#1f9d55", "fail": "#d64545", "running": "#d69e2e", None: "#8a8f98"}
STATE_ICON = {"ok": "✓", "fail": "✗", "running": "…", None: "·"}
SEV_COLOR = {"error": "#d64545", "warning": "#c98a0b", "info": "#5a7bd6"}


class SvgView(ZoomableView):
    """Zoom (wheel / ZoomBar) / pan (drag) SVG viewer."""

    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setBackgroundBrush(QBrush(QColor("#ffffff")))
        self._path = None
        self._mtime = 0
        self._zoomed = False

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
        self.fitInView(self.scene().sceneRect(), Qt.KeepAspectRatio)
        self._emit_zoom()

    def showEvent(self, e):
        super().showEvent(e)
        if self._path and not getattr(self, "_zoomed", False):
            self._fit()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self._path and not getattr(self, "_zoomed", False):
            self._fit()
        else:
            self._emit_zoom()

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
        self.project: str | None = None          # the one open project (name under projects/)
        self._proj_actions: list[QAction] = []    # menu items that need an open project

        # ---------------- left panel
        left = QWidget(); lv = QVBoxLayout(left)
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
        sch = QSplitter()
        schl = QWidget(); sl = QVBoxLayout(schl); sl.setContentsMargins(0, 0, 0, 0)
        self.sch_view = SvgView(); sl.addWidget(self.sch_view, 1)
        sl.addWidget(ZoomBar(self.sch_view, self.sch_view._fit))
        self.sch_info = DesignInfoPanel()
        sch.addWidget(schl); sch.addWidget(self.sch_info); sch.setSizes([760, 420])
        self.tabs.addTab(sch, "原理圖 Schematic")
        self.wdiag = WiringDiagramTab(); self.tabs.addTab(self.wdiag, "實體接線圖 Wiring Diagram")
        self.wiring = WiringTab(); self.tabs.addTab(self.wiring, "網路追蹤 Nets")
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
        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_welcome()); self.stack.addWidget(split)
        self.setCentralWidget(self.stack)
        self.settings = QSettings("aipcb", "dashboard")
        self._build_menus()

        # start-up: the project given on the command line, else the one open (or last closed) when the app quit
        start = Path(project).name if project else self.settings.value("last_project", "")
        if not (start and (config.PROJECTS / start / "design.yaml").exists() and self.set_project(start, ask=False)):
            self._show_welcome()
        self.timer = QTimer(self); self.timer.timeout.connect(self.poll); self.timer.start(1500)

    # ------------------------------------------------------------- project open / close
    def _build_welcome(self) -> QWidget:
        w = QWidget(); v = QVBoxLayout(w); v.setContentsMargins(80, 60, 80, 60)
        t = QLabel("aipcb"); t.setFont(QFont("", 30, QFont.Bold)); v.addWidget(t)
        sub = QLabel("目前沒有開啟的專案。從「檔案 File」選單或下方開始。"); sub.setStyleSheet("color:#888"); v.addWidget(sub)
        row = QHBoxLayout()
        for text, fn in (("＋ 新增專案", self.new_project), ("開啟專案…", self.open_project), ("匯入專案…", self.import_project)):
            b = QPushButton(text); b.setMinimumHeight(36); b.clicked.connect(fn); row.addWidget(b)
        row.addStretch(1); v.addLayout(row)
        v.addSpacing(16)
        h = QLabel("最近開啟 / 所有專案（點兩下開啟）"); h.setFont(QFont("", 13, QFont.Bold)); v.addWidget(h)
        self.welcome_list = QListWidget(); self.welcome_list.setFont(QFont("", 13))
        self.welcome_list.itemActivated.connect(lambda it: self.open_by_name(it.data(Qt.UserRole)))
        v.addWidget(self.welcome_list, 1)
        return w

    def _show_welcome(self):
        self.welcome_list.clear()
        recent = [n for n in (self.settings.value("recent", []) or [])]
        allp = sorted(p.parent.name for p in config.PROJECTS.glob("*/design.yaml"))
        for n in [n for n in recent if n in allp] + [n for n in allp if n not in recent]:
            raw = {}
            try:
                import yaml
                raw = yaml.safe_load((config.PROJECTS / n / "design.yaml").read_text(encoding="utf-8")) or {}
            except Exception:  # noqa: BLE001
                pass
            it = QListWidgetItem(f"{n}    —  {raw.get('title', '')}  (rev {raw.get('rev', 'A')})"
                                 + ("    · 最近開啟" if n in recent else ""))
            it.setData(Qt.UserRole, n)
            self.welcome_list.addItem(it)
        if self.welcome_list.count():
            self.welcome_list.setCurrentRow(0)
        self.stack.setCurrentIndex(0)
        self.setWindowTitle("aipcb — Claude 驅動的 EDA 儀表板")
        for a in self._proj_actions:
            a.setEnabled(False)

    def set_project(self, name: str, ask: bool = True) -> bool:
        """open `name`; the currently open project is closed first (asking to save when `ask`)"""
        if not (config.PROJECTS / name / "design.yaml").exists():
            QMessageBox.warning(self, "aipcb", f"找不到專案 {name}"); return False
        if self.project and self.project != name and not self.close_project(ask=ask):
            return False
        self.project = name
        self._mark_saved()
        self.settings.setValue("last_project", name)
        self._remember_recent(name)
        try:
            import yaml
            title = (yaml.safe_load((self.pd / "design.yaml").read_text(encoding="utf-8")) or {}).get("title", "")
        except Exception:  # noqa: BLE001
            title = ""
        self.setWindowTitle(f"{name} — {title} · aipcb" if title else f"{name} · aipcb")
        self.stack.setCurrentIndex(1)
        for a in self._proj_actions:
            a.setEnabled(True)
        self._mtimes.clear()
        self.refresh_all()
        return True

    def _mark_saved(self):
        """remember the project's state; closing it unchanged later needs no save prompt"""
        from .. import archive
        self._baseline = archive.fingerprint(self.pd) if self.project else None

    def is_modified(self) -> bool:
        from .. import archive
        return bool(self.project) and archive.fingerprint(self.pd) != getattr(self, "_baseline", None)

    def open_by_name(self, name: str):
        if name and name != self.project:
            self.set_project(name)

    def close_project(self, ask: bool = True) -> bool:
        """-> False if the user cancelled. Asks Save / Don't Save / Cancel; Save = snapshot to exports/."""
        if not self.project:
            return True
        if self.proc and self.proc.state() != QProcess.NotRunning:
            QMessageBox.warning(self, "aipcb", "流程正在執行中，請等它完成後再關閉專案。"); return False
        if ask and self.is_modified():           # unchanged since it was opened / last saved: close straight away
            box = QMessageBox(QMessageBox.Question, "關閉專案",
                              f"專案「{self.project}」自開啟或上次儲存後有變更。關閉前要儲存嗎？\n\n"
                              "儲存會把整個專案與所有產出存成一份快照（資料夾 + .zip）到 exports/。", parent=self)
            b_save = box.addButton("儲存", QMessageBox.AcceptRole)
            b_no = box.addButton("不儲存", QMessageBox.DestructiveRole)
            box.addButton("取消", QMessageBox.RejectRole)
            box.setDefaultButton(b_save)
            box.exec()
            if box.clickedButton() is b_save:
                if not self.save_snapshot():
                    return False
            elif box.clickedButton() is not b_no:
                return False
        self.settings.setValue("last_project", self.project)
        self.project = None
        self._show_welcome()
        return True

    def closeEvent(self, e):
        last = self.project
        if not self.close_project():
            e.ignore(); return
        if last:
            self.settings.setValue("last_project", last)     # reopened on next start
        self.settings.sync()
        e.accept()

    # ------------------------------------------------------------- menu bar
    def _act(self, menu, text, fn, shortcut=None, tip=None, role=None, needs=False):
        a = QAction(text, self)
        if needs:
            self._proj_actions.append(a)
        if shortcut is not None:
            a.setShortcut(shortcut if isinstance(shortcut, QKeySequence) else QKeySequence(shortcut))
        if tip:
            a.setStatusTip(tip)
        if role is not None:
            a.setMenuRole(role)
        a.triggered.connect(lambda *_: fn())
        menu.addAction(a)
        return a

    def _build_menus(self):
        mb = self.menuBar()
        K = QKeySequence
        # ---- File
        f = mb.addMenu("檔案 File")
        self._act(f, "新增專案… New Project", self.new_project, K.New, "建立新的空白專案（design.yaml 骨架）")
        self._act(f, "開啟專案… Open Project", self.open_project, K.Open, "選擇專案的 design.yaml 開啟")
        self.recent_menu = f.addMenu("最近開啟 Open Recent")
        self.recent_menu.aboutToShow.connect(self._fill_recent)
        f.addSeparator()
        self._act(f, "關閉專案 Close Project", self.close_project, K.Close, "關閉目前專案（會詢問是否儲存）", needs=True)
        f.addSeparator()
        self._act(f, "儲存存檔 Save Snapshot", self.save_snapshot, K.Save,
                  "design.yaml 是唯一來源、隨時都已存檔；這會把目前專案的完整快照存到 exports/", needs=True)
        self._act(f, "另存新專案… Save As", self.save_as, K.SaveAs, "以新名稱複製整個專案（含所有產出）", needs=True)
        f.addSeparator()
        self._act(f, "匯入專案… Import", self.import_project, "Ctrl+Shift+I", "匯入先前匯出的資料夾 / .zip")
        self._act(f, "匯出專案… Export", self.export_project, "Ctrl+Shift+E",
                  "把整個專案與所有產出整理成一個資料夾 + .zip", needs=True)
        self._act(f, "輸出送廠檔 Export Fab Output", self.export_fab, None, "Gerber / 鑽孔 / BOM / 座標檔（需 AI 審核 PASS）",
                  needs=True)
        f.addSeparator()
        k = f.addMenu("以 KiCad 開啟 Open in KiCad")
        self._act(k, "原理圖 Schematic", lambda: self.open_kicad("sch"), needs=True)
        self._act(k, "電路板 PCB", lambda: self.open_kicad("pcb"), needs=True)
        self._act(f, "編輯 design.yaml", self.edit_design, None, "用系統預設編輯器開啟 design.yaml", needs=True)
        self._act(f, "在 Finder 中顯示專案 Reveal in Finder", self.reveal_project, needs=True)
        self._act(f, "開啟匯出資料夾 Open Exports Folder", self.open_exports)
        f.addSeparator()
        self._act(f, "關閉視窗 Close Window", self.close, "Ctrl+Shift+W")
        self._act(f, "結束 Quit aipcb", self.close, K.Quit, role=QAction.QuitRole)   # close() -> asks to save
        # ---- View
        v = mb.addMenu("檢視 View")
        self._act(v, "重新整理 Refresh", self.refresh_all, "Ctrl+R", needs=True)
        v.addSeparator()
        for i in range(min(9, self.tabs.count())):
            self._act(v, self.tabs.tabText(i), lambda i=i: self.tabs.setCurrentIndex(i), f"Ctrl+{i + 1}", needs=True)
        v.addSeparator()
        self._act(v, "全螢幕 Full Screen", lambda: self.showNormal() if self.isFullScreen() else self.showFullScreen(),
                  K.FullScreen)
        # ---- Build
        b = mb.addMenu("流程 Build")
        self._act(b, "全流程 Build", lambda: self.run(["build"]), "Ctrl+B", needs=True)
        self._act(b, "執行所選階段 Run Selected Stage", self.run_selected, "Ctrl+Shift+B", needs=True)
        self._act(b, "保留擺放重建 Build --keep", lambda: self.run(["build", "--keep"]), None,
                  "保留已擺放元件位置，只擺新元件並重新佈線", needs=True)
        # ---- Window / Help
        w = mb.addMenu("視窗 Window")
        self._act(w, "最小化 Minimize", self.showMinimized, "Ctrl+M")
        self._act(w, "縮放 Zoom", lambda: self.showNormal() if self.isMaximized() else self.showMaximized())
        h = mb.addMenu("說明 Help")
        self._act(h, "使用說明 (CLAUDE.md)", lambda: self._open_path(config.ROOT / "CLAUDE.md"))
        self._act(h, "專案 README", lambda: self._open_path(self.pd / "README.md"), needs=True)
        self._act(h, "關於 aipcb About", self.about, role=QAction.AboutRole)
        self.statusBar()

    def _open_path(self, p: Path):
        if not Path(p).exists():
            QMessageBox.information(self, "aipcb", f"找不到 {p}"); return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))

    def _remember_recent(self, name):
        if not name:
            return
        rec = [n for n in (self.settings.value("recent", []) or []) if n != name]
        self.settings.setValue("recent", [name] + rec[:9])

    def _fill_recent(self):
        self.recent_menu.clear()
        names = [n for n in (self.settings.value("recent", []) or []) if (config.PROJECTS / n / "design.yaml").exists()]
        for n in names:
            self._act(self.recent_menu, n, lambda n=n: self.open_by_name(n))
        if not names:
            a = self.recent_menu.addAction("（無）"); a.setEnabled(False)
        self.recent_menu.addSeparator()
        self._act(self.recent_menu, "清除清單 Clear", lambda: self.settings.setValue("recent", []))

    def new_project(self):
        from .. import archive
        dlg = QDialog(self); dlg.setWindowTitle("新增專案 New Project")
        form = QFormLayout(dlg)
        name = QLineEdit(); name.setPlaceholderText("英數字、底線，例如 my_board")
        title = QLineEdit(); title.setPlaceholderText("顯示用標題，例如 USB 溫度計")
        w = QDoubleSpinBox(); w.setRange(5, 400); w.setValue(60); w.setSuffix(" mm")
        hgt = QDoubleSpinBox(); hgt.setRange(5, 500); hgt.setValue(40); hgt.setSuffix(" mm")
        form.addRow("專案名稱", name); form.addRow("標題", title); form.addRow("板寬", w); form.addRow("板高", hgt)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept); bb.rejected.connect(dlg.reject); form.addRow(bb)
        if dlg.exec() != QDialog.Accepted:
            return
        if not self.close_project():
            return
        try:
            pd = archive.new_project(name.text().strip(), title.text().strip(), w.value(), hgt.value())
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "無法建立專案", str(e)); return
        self.set_project(pd.name)
        QMessageBox.information(self, "已建立", f"已建立 {pd}/design.yaml\n\n接著在 Claude Code 請 Claude 設計電路，"
                                "或用「檔案 → 編輯 design.yaml」自行填寫，再按「流程 → 全流程 Build」。")

    def open_project(self):
        from .. import archive
        src, _ = QFileDialog.getOpenFileName(self, "開啟專案：選擇 design.yaml", str(config.PROJECTS),
                                             "aipcb 專案 (design.yaml);;所有檔案 (*)")
        if not src:
            return
        pd = Path(src).resolve().parent
        if pd.parent == config.PROJECTS.resolve():
            self.open_by_name(pd.name); return
        if QMessageBox.question(self, "開啟專案", f"{pd}\n不在專案資料夾 projects/ 內。\n\n要把它複製進來並開啟嗎？"
                                ) != QMessageBox.Yes:
            return
        self._do_import(pd)

    def save_snapshot(self) -> bool:
        from .. import archive
        if not self.project:
            return False
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            res = archive.export_project(self.pd)
        except Exception as e:  # noqa: BLE001
            QApplication.restoreOverrideCursor(); QMessageBox.critical(self, "存檔失敗", str(e)); return False
        QApplication.restoreOverrideCursor()
        self._mark_saved()
        self.statusBar().showMessage(f"已存檔：{res['zip'] or res['folder']}", 10000)
        return True

    def save_as(self):
        from .. import archive
        cur = self.project
        if not cur:
            return
        new, ok = QInputDialog.getText(self, "另存新專案 Save As", "新專案名稱（英數字、底線、減號）：", text=f"{cur}_copy")
        if not ok or not new.strip():
            return
        try:
            res = archive.duplicate_project(self.pd, new.strip())
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "另存失敗", str(e)); return
        self.set_project(res["project"], ask=False)        # the copy is identical: nothing to save
        self.statusBar().showMessage(f"已另存為 projects/{res['project']}", 10000)

    def edit_design(self):
        self._open_path(self.pd / "design.yaml")

    def reveal_project(self):
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(self.pd / "design.yaml")])
        else:
            self._open_path(self.pd)

    def open_exports(self):
        from .. import archive
        archive.EXPORTS.mkdir(parents=True, exist_ok=True)
        self._open_path(archive.EXPORTS)

    def about(self):
        QMessageBox.about(self, "關於 aipcb", "<b>aipcb</b> — Claude 驅動的本機 EDA<br>"
                          "KiCad 10 檔案格式 / ERC / DRC / Gerber，物理擺放、A* 佈線、ngspice 模擬，全部離線執行。")

    # ------------------------------------------------------------- helpers
    @property
    def pd(self) -> Path:
        return config.PROJECTS / (self.project or "")

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
        if not self.project:
            return
        watch = ["status.json", "checks.json", "review.md", "preview/index.json", "sim/summary.json", "pipeline.log",
                 "schematic_map.json", "wiring.json", "review.json", "../design.yaml"]
        changed = False
        for rel in watch:
            p = self.out / rel
            mt = p.stat().st_mtime if p.exists() else 0
            if self._mtimes.get(rel) != mt:
                self._mtimes[rel] = mt; changed = True
        if changed:
            self.refresh_all()

    def refresh_all(self, *_):
        if not self.project:
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
        self.sch_info.load(self.pd)
        self.wiring.load(self.out)
        self.wdiag.load(self.out)
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

    def export_project(self):
        from .. import archive
        name = self.project
        if not name:
            return
        archive.EXPORTS.mkdir(parents=True, exist_ok=True)
        dest = QFileDialog.getExistingDirectory(self, f"選擇「{name}」的匯出位置", str(archive.EXPORTS))
        if not dest:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            res = archive.export_project(self.pd, dest)
        except Exception as e:  # noqa: BLE001
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "匯出失敗", str(e)); return
        QApplication.restoreOverrideCursor()
        msg = (f"已匯出 {res['files']} 個檔案（{res['bytes'] / 1e6:.1f} MB）\n\n資料夾：{res['folder']}\n"
               f"存檔：{res['zip']}\n\n資料夾內 README.md 說明內容與匯入方式。")
        if res["notes"]:
            msg += "\n\n備註：\n" + "\n".join(res["notes"])
        box = QMessageBox(QMessageBox.Information, "匯出完成", msg, QMessageBox.Ok, self)
        show = box.addButton("在 Finder 顯示", QMessageBox.ActionRole)
        box.exec()
        if box.clickedButton() is show:
            subprocess.Popen(["open", "-R", res["zip"] or res["folder"]] if sys.platform == "darwin"
                             else ["xdg-open", str(Path(res["folder"]).parent)])

    def import_project(self):
        from .. import archive
        src, _ = QFileDialog.getOpenFileName(
            self, "匯入專案：選擇 .zip，或匯出資料夾中的 manifest.json（也可選專案的 design.yaml）",
            str(archive.EXPORTS if archive.EXPORTS.exists() else config.ROOT),
            "aipcb 匯出 (*.zip manifest.json design.yaml);;所有檔案 (*)")
        if not src:
            return
        self._do_import(Path(src))

    def _do_import(self, src: Path):
        from .. import archive
        try:
            info = archive.inspect(Path(src))
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "無法匯入", str(e)); return
        name, overwrite = info["name"], False
        desc = f"專案：{name}\n標題：{info.get('title') or ''}  rev {info.get('rev') or ''}\n匯出時間：{info.get('exported') or '—'}"
        if info["exists"]:
            box = QMessageBox(QMessageBox.Question, "專案已存在",
                              f"{desc}\n\n本機已有同名專案「{name}」。要怎麼處理？\n"
                              "（覆蓋前會先把現有專案備份到 exports/，不會刪除）", parent=self)
            b_new = box.addButton("另存新名稱…", QMessageBox.AcceptRole)
            b_over = box.addButton("覆蓋（先備份）", QMessageBox.DestructiveRole)
            box.addButton("取消", QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() is b_over:
                overwrite = True
            elif box.clickedButton() is b_new:
                new, ok = QInputDialog.getText(self, "另存新名稱", "新專案名稱（英數字、底線、減號）：", text=f"{name}_2")
                if not ok or not new.strip():
                    return
                name = new.strip()
            else:
                return
        elif QMessageBox.question(self, "匯入專案", f"{desc}\n\n匯入到 projects/{name}？") != QMessageBox.Yes:
            return
        if not self.close_project():            # the open project is closed (asking to save) before the import
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            res = archive.import_project(Path(src), name=name, overwrite=overwrite)
        except Exception as e:  # noqa: BLE001
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "匯入失敗", str(e)); return
        QApplication.restoreOverrideCursor()
        self.set_project(res["project"], ask=False)
        extra = f"\n舊專案已備份到：{res['backup_of_previous']}" if res["backup_of_previous"] else ""
        QMessageBox.information(self, "匯入完成", f"已匯入 projects/{res['project']}"
                                f"{'（檔案完整性已驗證）' if res['verified'] else ''}{extra}\n\n"
                                "如需在本機重新產生所有檔案，按「▶ 全流程 Build」。")

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
