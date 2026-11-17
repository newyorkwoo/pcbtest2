"""Zoomable graphics view + a centre-zero zoom slider shared by the schematic-type tabs.

Zoom 1.0 (= 100 %) means "the whole sheet fits the window". The slider sits at 0 in the middle for that;
dragging left zooms out (down to ZMIN), right zooms in (up to ZMAX), on a log scale so each half feels even.
"""
from __future__ import annotations

import math

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QGraphicsView, QHBoxLayout, QLabel, QPushButton, QSlider, QWidget

ZMIN, ZMAX = 0.2, 20.0       # 20 % .. 2000 % of the fitted sheet
STEPS = 1000                 # slider range -STEPS .. +STEPS, 0 = fit


def zoom_to_slider(z: float) -> int:
    z = max(ZMIN, min(ZMAX, z))
    if z >= 1:
        return round(STEPS * math.log(z) / math.log(ZMAX))
    return -round(STEPS * math.log(z) / math.log(ZMIN))


def slider_to_zoom(v: int) -> float:
    return ZMAX ** (v / STEPS) if v >= 0 else ZMIN ** (-v / STEPS)


class ZoomableView(QGraphicsView):
    """QGraphicsView with a zoom level relative to the fitted scene, wheel zoom and a zoomChanged signal.

    Subclasses call `self._emit_zoom()` after they re-fit."""

    zoomChanged = Signal(float)

    def base_scale(self) -> float:
        """view scale at which the whole scene fits (fitInView keeps a 2 px margin per side). Uses the size
        without scrollbars, so the value does not jump when scrollbars appear while zoomed in."""
        r, vp = self.scene().sceneRect(), self.maximumViewportSize()
        if r.isEmpty() or vp.width() <= 4 or vp.height() <= 4:
            return 1.0
        return min((vp.width() - 4) / r.width(), (vp.height() - 4) / r.height())

    def zoom(self) -> float:
        return self.transform().m11() / self.base_scale()

    def set_zoom(self, z: float, anchor=QGraphicsView.AnchorViewCenter):
        z = max(ZMIN, min(ZMAX, z))
        f = z / (self.zoom() or 1.0)
        if abs(f - 1) > 1e-4:
            old = self.transformationAnchor()
            self.setTransformationAnchor(anchor)
            self.scale(f, f)
            self.setTransformationAnchor(old)
            self._zoomed = True
        self._emit_zoom()

    def _emit_zoom(self):
        self.zoomChanged.emit(self.zoom())

    def wheelEvent(self, e):
        f = 1.2 if e.angleDelta().y() > 0 else 1 / 1.2
        self.set_zoom(self.zoom() * f, QGraphicsView.AnchorUnderMouse)


class ZoomBar(QWidget):
    """縮小 [－] ━━━━●━━━━ [＋] 放大   100%   [適合視窗 Fit]  — the knob rests in the middle at 'fit'."""

    def __init__(self, view: ZoomableView, on_fit):
        super().__init__()
        self.view = view
        lay = QHBoxLayout(self); lay.setContentsMargins(4, 2, 4, 2)
        minus = QPushButton("－"); plus = QPushButton("＋")
        for b, f, tip in ((minus, 1 / 1.25, "縮小 Zoom out"), (plus, 1.25, "放大 Zoom in")):
            b.setFixedWidth(34); b.setToolTip(tip); b.setAutoRepeat(True)
            b.clicked.connect(lambda _=False, f=f: self.view.set_zoom(self.view.zoom() * f))
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(-STEPS, STEPS)
        self.slider.setPageStep(STEPS // 10)
        self.slider.setTickPosition(QSlider.TicksBelow)
        self.slider.setTickInterval(STEPS)              # ticks at both ends and at the 0 (fit) centre
        self.slider.setToolTip("中間 0 = 整張圖剛好塞滿視窗；向左縮小、向右放大")
        self.slider.setValue(0)
        self.slider.valueChanged.connect(lambda v: self.view.set_zoom(slider_to_zoom(v)))
        self.label = QLabel("100%"); self.label.setFixedWidth(52)
        self.label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        fit = QPushButton("適合視窗 Fit"); fit.setToolTip("回到中間 0：整張圖剛好塞滿視窗（也可雙擊圖面）")
        fit.clicked.connect(on_fit)
        lay.addWidget(QLabel("縮小")); lay.addWidget(minus); lay.addWidget(self.slider, 1); lay.addWidget(plus)
        lay.addWidget(QLabel("放大")); lay.addWidget(self.label); lay.addWidget(fit)
        view.zoomChanged.connect(self._on_zoom)

    def _on_zoom(self, z):
        self.slider.blockSignals(True)
        self.slider.setValue(zoom_to_slider(z))
        self.slider.blockSignals(False)
        self.label.setText(f"{z * 100:.0f}%")
