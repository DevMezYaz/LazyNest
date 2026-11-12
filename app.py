"""
LazyNest — CNC Laser Nesting Application
PyQt6 UI: light / dark themes, true-shape viewer, live optimisation.
"""
import os
import sys
import math
import logging
import tempfile
import threading
from datetime import datetime
from typing import List, Optional, Dict

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QSplitter, QVBoxLayout, QHBoxLayout,
    QScrollArea, QLayout,
    QGridLayout, QLabel, QPushButton, QToolButton, QLineEdit, QComboBox,
    QTreeWidget, QTreeWidgetItem, QHeaderView, QFrame, QFileDialog,
    QInputDialog, QMessageBox, QProgressBar, QStatusBar, QDoubleSpinBox,
    QSizePolicy, QDialog, QSpinBox, QAbstractItemView, QCheckBox,
    QButtonGroup, QListWidget, QListWidgetItem,
    QToolTip,
)
from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, QTimer, QSize, QSettings, QDir, QPointF, QRectF,
    QRect, QPoint,
)
from PyQt6.QtGui import (
    QColor, QPainter, QPen, QPixmap, QIcon, QPainterPath, QTransform,
    QKeySequence, QShortcut, QPalette,
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import (
    LibraryManager, JobManager, JobItem, ConfigManager,
    run_nesting, estimate_platforms, NestingJob, NestRequest, GroupRequest,
    PlacementResult, export_job, combined_filename, export_cam, output_folders,
    ROTATION_MODES, QUALITY_MODES, native_path, write_report, colour_map_for,
    REPORT_FILENAME, daily_report_path, master_report_path,
)

log = logging.getLogger(__name__)

APP_NAME    = "LazyNest"
APP_VERSION = "2.4"
APP_DIR     = os.path.dirname(os.path.abspath(sys.argv[0]))
RES_DIR     = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR  = os.path.join(APP_DIR, "lazynest_data")


# ══════════════════════════════════════════════════════════════════════════════
# THEME
# ══════════════════════════════════════════════════════════════════════════════
THEMES = {
    "light": dict(
        window="#F3F2F5", card="#FFFFFF", field="#FFFFFF", sunken="#F1EFF4",
        hover="#F5EFF3", border="#E3E0E7", border_strong="#D3CFD8",
        text="#1C1B1F", muted="#6E6A75", faint="#A19DA8",
        accent="#E0206A", accent_hover="#C8145C", accent_press="#AE0F50",
        accent_soft="#FDE8F0", accent_text="#FFFFFF",
        success="#138A62", warn="#B86E00", danger="#C0392B",
        canvas="#ECEAEF", plate="#FFFFFF", grid="#EFEDF2", overflow="#C9A227",
        selection="#FDE8F0", selection_text="#1C1B1F",
    ),
    "dark": dict(
        window="#111014", card="#1A191E", field="#222127", sunken="#16151A",
        hover="#26242C", border="#2C2A32", border_strong="#3A3842",
        text="#ECEAF0", muted="#A09CA8", faint="#6D6975",
        accent="#FF3D8B", accent_hover="#FF5C9E", accent_press="#E62A77",
        accent_soft="#3A1D2A", accent_text="#FFFFFF",
        success="#3FC495", warn="#E5A13A", danger="#FF6B5E",
        canvas="#0D0C10", plate="#1B1A20", grid="#24222A", overflow="#D9B23A",
        selection="#3A1D2A", selection_text="#ECEAF0",
    ),
}


_TOGGLE_DIR = tempfile.mkdtemp(prefix="lazynest_ui_")


def _toggle_images(t: dict) -> tuple:
    """Render on/off switch images for QCheckBox indicators (QSS needs files)."""
    out = []
    for on in (False, True):
        path = os.path.join(_TOGGLE_DIR, f"toggle_{'on' if on else 'off'}_{t['accent'][1:]}_{t['card'][1:]}.png")
        if not os.path.isfile(path):
            w, h, dpr = 34, 20, 3
            pm = QPixmap(w * dpr, h * dpr)
            pm.fill(Qt.GlobalColor.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.scale(dpr, dpr)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(t['accent'] if on else t['border_strong']))
            p.drawRoundedRect(QRectF(0, 0, w, h), h / 2, h / 2)
            p.setBrush(QColor("#FFFFFF"))
            knob = h - 6
            p.drawEllipse(QRectF((w - knob - 3) if on else 3, 3, knob, knob))
            p.end()
            pm.save(path, "PNG")
        out.append(path.replace("\\", "/"))
    return tuple(out)


QUALITY_SHORT = {"quick": "Quick 5s", "balanced": "Balanced 30s",
                 "thorough": "Thorough 2m", "nonstop": "Nonstop ∞"}


def build_stylesheet(t: dict) -> str:
    toggle_off, toggle_on = _toggle_images(t)
    return f"""
    * {{ outline: none; }}
    QWidget {{
        color: {t['text']};
        font-size: 13px;
    }}
    QMainWindow, QDialog {{ background: {t['window']}; }}
    QWidget#root {{ background: {t['window']}; }}

    /* ── Top bar ── */
    QFrame#topbar {{
        background: {t['card']};
        border-bottom: 1px solid {t['border']};
    }}
    QLabel#brand {{ font-size: 16px; font-weight: 700; letter-spacing: 0.3px; }}
    QLabel#brandDot {{ color: {t['accent']}; font-size: 16px; font-weight: 800; }}
    QLabel#version {{ color: {t['faint']}; font-size: 11px; }}

    /* ── Cards ── */
    QFrame#card {{
        background: {t['card']};
        border: 1px solid {t['border']};
        border-radius: 12px;
    }}
    QLabel#cardTitle {{
        color: {t['muted']};
        font-size: 11px;
        font-weight: 700;
        letter-spacing: 1.2px;
    }}
    QLabel#muted  {{ color: {t['muted']}; font-size: 12px; }}
    QLabel#faint  {{ color: {t['faint']}; font-size: 11px; }}
    QLabel#formLabel {{ color: {t['muted']}; font-size: 12px; }}
    QLabel#danger {{ color: {t['danger']}; font-size: 12px; font-weight: 600; }}
    QLabel#kpiValue {{ font-size: 20px; font-weight: 700; }}
    QLabel#kpiAccent {{ font-size: 20px; font-weight: 700; color: {t['accent']}; }}
    QLabel#kpiLabel {{ color: {t['muted']}; font-size: 10px; font-weight: 700; letter-spacing: 1px; }}
    QLabel#platTitle {{ font-size: 14px; font-weight: 700; }}
    QFrame#kpi {{
        background: {t['sunken']};
        border: 1px solid {t['border']};
        border-radius: 10px;
    }}
    QLabel#chip {{
        background: {t['accent_soft']};
        color: {t['accent']};
        border: 1px solid {t['accent']};
        border-radius: 11px;
        padding: 2px 10px;
        font-size: 12px;
        font-weight: 700;
    }}
    QLabel#chipDone {{
        background: {t['sunken']};
        color: {t['muted']};
        border: 1px solid {t['border']};
        border-radius: 11px;
        padding: 2px 10px;
        font-size: 12px;
        font-weight: 600;
    }}
    QPushButton#chipStop {{
        background: {t['accent']};
        color: {t['accent_text']};
        border: none;
        border-radius: 11px;
        padding: 3px 12px;
        font-size: 12px;
        font-weight: 700;
    }}
    QPushButton#chipStop:hover {{ background: {t['accent_hover']}; }}
    QPushButton#chipStop:disabled {{ background: {t['accent_soft']}; color: {t['faint']}; }}
    QToolButton#stepper {{
        background: transparent;
        color: {t['faint']};
        border: none;
        min-width: 14px;
        max-width: 14px;
        min-height: 20px;
        font-size: 13px;
        font-weight: 600;
        padding: 0;
    }}
    QToolButton#stepper:hover {{ color: {t['accent']}; }}
    QToolButton#stepper:pressed {{ color: {t['accent_press']}; }}
    QToolButton#stepper:disabled {{ color: {t['border']}; }}
    QLabel#statusStat {{ color: {t['muted']}; font-size: 12px; }}
    QLabel#statusTime {{ color: {t['text']}; font-size: 12px; font-weight: 700; }}
    QLabel#statusHint {{ color: {t['success']}; font-size: 12px; font-weight: 700; }}
    QFrame#divider {{ background: {t['border']}; max-height: 1px; min-height: 1px; border: none; }}

    /* ── Splitter ── */
    QSplitter::handle {{ background: transparent; }}
    QSplitter::handle:horizontal {{ width: 10px; }}
    QSplitter::handle:vertical {{ height: 10px; }}

    /* ── Buttons ── */
    QPushButton {{
        background: {t['sunken']};
        color: {t['text']};
        border: 1px solid {t['border']};
        border-radius: 8px;
        padding: 6px 14px;
        font-weight: 600;
    }}
    QPushButton:hover {{ background: {t['hover']}; border-color: {t['border_strong']}; }}
    QPushButton:pressed {{ background: {t['border']}; }}
    QPushButton:disabled {{ color: {t['faint']}; }}
    QPushButton[primary="true"] {{
        background: {t['accent']};
        color: {t['accent_text']};
        border: 1px solid {t['accent']};
    }}
    QPushButton[primary="true"]:hover {{ background: {t['accent_hover']}; border-color: {t['accent_hover']}; }}
    QPushButton[primary="true"]:pressed {{ background: {t['accent_press']}; }}
    QPushButton[primary="true"]:disabled {{ background: {t['accent_soft']}; border-color: {t['accent_soft']}; color: {t['faint']}; }}
    QPushButton[big="true"] {{ padding: 10px 18px; font-size: 14px; border-radius: 10px; }}
    QPushButton[danger="true"] {{ color: {t['danger']}; }}

    QToolButton#icon {{
        background: transparent;
        border: 1px solid transparent;
        border-radius: 7px;
        padding: 4px;
    }}
    QToolButton#icon:hover {{ background: {t['hover']}; border-color: {t['border']}; }}
    QToolButton#icon:pressed {{ background: {t['border']}; }}
    QToolButton#icon:disabled {{ background: transparent; }}

    QToolButton#seg {{
        background: {t['sunken']};
        border: 1px solid {t['border']};
        padding: 5px 10px;
        color: {t['muted']};
        font-weight: 600;
    }}
    QToolButton#seg:hover {{ color: {t['text']}; }}
    QToolButton#seg:checked {{ background: {t['accent_soft']}; color: {t['accent']}; border-color: {t['accent']}; }}
    QToolButton#seg[pos="first"] {{ border-top-left-radius: 8px; border-bottom-left-radius: 8px; }}
    QToolButton#seg[pos="last"] {{ border-top-right-radius: 8px; border-bottom-right-radius: 8px; }}

    /* ── Inputs ── */
    QLineEdit, QDoubleSpinBox, QSpinBox, QComboBox {{
        background: {t['field']};
        border: 1px solid {t['border']};
        border-radius: 8px;
        padding: 5px 8px;
        min-height: 20px;
        selection-background-color: {t['accent']};
        selection-color: {t['accent_text']};
    }}
    QLineEdit:hover, QDoubleSpinBox:hover, QSpinBox:hover, QComboBox:hover {{ border-color: {t['border_strong']}; }}
    QLineEdit:focus, QDoubleSpinBox:focus, QSpinBox:focus, QComboBox:focus {{ border-color: {t['accent']}; }}
    QLineEdit:disabled, QDoubleSpinBox:disabled, QSpinBox:disabled {{ color: {t['faint']}; background: {t['sunken']}; }}
    QLineEdit#search {{ padding-left: 10px; background: {t['sunken']}; }}
    QDoubleSpinBox::up-button, QSpinBox::up-button,
    QDoubleSpinBox::down-button, QSpinBox::down-button {{ width: 0; border: none; }}
    QComboBox::drop-down {{ border: none; width: 22px; }}
    QComboBox QAbstractItemView {{
        background: {t['card']};
        border: 1px solid {t['border']};
        selection-background-color: {t['accent_soft']};
        selection-color: {t['text']};
        padding: 4px;
    }}
    QComboBox#jobCombo {{ font-weight: 600; }}

    QCheckBox {{ spacing: 8px; }}
    QCheckBox::indicator {{ width: 34px; height: 20px; }}
    QCheckBox::indicator:unchecked {{ image: url("{toggle_off}"); }}
    QCheckBox::indicator:checked {{ image: url("{toggle_on}"); }}
    QCheckBox:disabled {{ color: {t['faint']}; }}

    /* ── Lists / tables ── */
    QTreeWidget, QTableWidget, QListWidget {{
        background: {t['card']};
        border: none;
        alternate-background-color: {t['sunken']};
    }}
    QTreeWidget::item, QTableWidget::item {{ padding: 4px 2px; border: none; }}
    QTreeWidget::item:hover, QTableWidget::item:hover {{ background: {t['hover']}; }}
    QTreeWidget::item:selected, QTableWidget::item:selected {{
        background: {t['selection']}; color: {t['selection_text']};
    }}
    QListWidget#strip {{ background: transparent; }}
    QListWidget#strip::item {{ border: 2px solid transparent; border-radius: 8px; padding: 2px; margin: 2px; }}
    QListWidget#strip::item:selected {{ border-color: {t['accent']}; background: transparent; }}
    QListWidget#strip::item:hover {{ background: {t['hover']}; }}
    QHeaderView::section {{
        background: {t['card']};
        color: {t['faint']};
        border: none;
        border-bottom: 1px solid {t['border']};
        padding: 6px 6px;
        font-size: 11px;
        font-weight: 700;
    }}
    QTableCornerButton::section {{ background: {t['card']}; border: none; }}
    /* column labels sit over the text of their column, not over the thumbnails */
    QTreeWidget#queueTree QHeaderView::section:first {{ padding-left: 50px; }}
    QTreeWidget#libTree QHeaderView::section:first {{ padding-left: 40px; }}
    QTreeWidget#memberTree QHeaderView::section:first {{ padding-left: 38px; }}

    /* ── Progress / status ── */
    QProgressBar {{
        background: {t['sunken']};
        border: none;
        border-radius: 3px;
        max-height: 6px;
        min-height: 6px;
    }}
    QProgressBar::chunk {{ background: {t['accent']}; border-radius: 3px; }}
    QStatusBar {{
        background: {t['card']};
        border-top: 1px solid {t['border']};
        color: {t['muted']};
        font-size: 12px;
    }}
    QStatusBar::item {{ border: none; }}

    /* ── Scrollbars ── */
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {t['border_strong']}; border-radius: 3px; min-height: 30px; }}
    QScrollBar::handle:vertical:hover {{ background: {t['faint']}; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
    QScrollBar::handle:horizontal {{ background: {t['border_strong']}; border-radius: 3px; min-width: 30px; }}
    QScrollBar::handle:horizontal:hover {{ background: {t['faint']}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

    QToolTip {{
        background: {t['text']};
        color: {t['card']};
        border: none;
        padding: 6px 8px;
        border-radius: 6px;
    }}
    QMessageBox QLabel {{ font-size: 13px; }}
    """


# ══════════════════════════════════════════════════════════════════════════════
# ICONS  (drawn at runtime so they are crisp and follow the theme colour)
# ══════════════════════════════════════════════════════════════════════════════
def make_icon(name: str, colour: str, size: int = 18) -> QIcon:
    dpr = 3
    pm = QPixmap(size * dpr, size * dpr)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(size * dpr / 24.0, size * dpr / 24.0)
    pen = QPen(QColor(colour), 1.9)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    path = QPainterPath()

    def line(x1, y1, x2, y2):
        path.moveTo(x1, y1); path.lineTo(x2, y2)

    if name == "plus":
        line(12, 5, 12, 19); line(5, 12, 19, 12)
    elif name == "minus":
        line(5, 12, 19, 12)
    elif name == "close":
        line(6, 6, 18, 18); line(18, 6, 6, 18)
    elif name == "copy":
        path.addRoundedRect(QRectF(8, 8, 11, 11), 2.5, 2.5)
        path.moveTo(16, 8); path.lineTo(16, 6.5); path.quadTo(16, 5, 14.5, 5)
        path.lineTo(6.5, 5); path.quadTo(5, 5, 5, 6.5); path.lineTo(5, 14.5)
        path.quadTo(5, 16, 6.5, 16); path.lineTo(8, 16)
    elif name == "trash":
        line(5, 7, 19, 7); line(10, 4.5, 14, 4.5)
        path.moveTo(7, 7); path.lineTo(8, 19); path.quadTo(8.2, 20, 9.2, 20)
        path.lineTo(14.8, 20); path.quadTo(15.8, 20, 16, 19); path.lineTo(17, 7)
        line(10.5, 10.5, 10.5, 16.5); line(13.5, 10.5, 13.5, 16.5)
    elif name == "refresh":
        path.arcMoveTo(QRectF(5, 5, 14, 14), 60)
        path.arcTo(QRectF(5, 5, 14, 14), 60, 280)
        line(15.5, 4.5, 15.5, 8.5); line(15.5, 8.5, 19.5, 8.5)
    elif name == "folder":
        path.moveTo(4, 7); path.quadTo(4, 5.5, 5.5, 5.5); path.lineTo(9.5, 5.5)
        path.lineTo(11.5, 7.5); path.lineTo(18.5, 7.5); path.quadTo(20, 7.5, 20, 9)
        path.lineTo(20, 17); path.quadTo(20, 18.5, 18.5, 18.5); path.lineTo(5.5, 18.5)
        path.quadTo(4, 18.5, 4, 17); path.closeSubpath()
    elif name == "search":
        path.addEllipse(QRectF(5, 5, 10, 10)); line(13.5, 13.5, 19, 19)
    elif name == "sun":
        path.addEllipse(QRectF(8.5, 8.5, 7, 7))
        for k in range(8):
            a = k * math.pi / 4
            line(12 + 5.5 * math.cos(a), 12 + 5.5 * math.sin(a),
                 12 + 8 * math.cos(a), 12 + 8 * math.sin(a))
    elif name == "moon":
        path.moveTo(18.5, 14.5)
        path.cubicTo(15, 19.5, 6, 18, 5.5, 11.5)
        path.cubicTo(5.2, 7.5, 8, 5, 11, 4.8)
        path.cubicTo(8.5, 8.5, 11.5, 15.5, 18.5, 14.5)
    elif name == "play":
        p.setBrush(QColor(colour))
        path.moveTo(8, 5.5); path.lineTo(18.5, 12); path.lineTo(8, 18.5); path.closeSubpath()
    elif name == "stop":
        p.setBrush(QColor(colour))
        path.addRoundedRect(QRectF(6.5, 6.5, 11, 11), 2, 2)
    elif name == "export":
        line(12, 4, 12, 14.5); line(8, 10.5, 12, 14.5); line(16, 10.5, 12, 14.5)
        path.moveTo(5, 15); path.lineTo(5, 18.5); path.quadTo(5, 19.5, 6, 19.5)
        path.lineTo(18, 19.5); path.quadTo(19, 19.5, 19, 18.5); path.lineTo(19, 15)
    elif name == "left":
        line(14.5, 6, 8.5, 12); line(8.5, 12, 14.5, 18)
    elif name == "right":
        line(9.5, 6, 15.5, 12); line(15.5, 12, 9.5, 18)
    elif name == "fit":
        for (x, y, dx, dy) in ((5, 9, 0, -4), (5, 5, 4, 0), (19, 9, 0, -4), (19, 5, -4, 0),
                               (5, 15, 0, 4), (5, 19, 4, 0), (19, 15, 0, 4), (19, 19, -4, 0)):
            line(x, y, x + dx, y + dy)
    elif name == "edit":
        path.moveTo(5, 19); path.lineTo(5.8, 15.5); path.lineTo(15.5, 5.8)
        path.lineTo(18.2, 8.5); path.lineTo(8.5, 18.2); path.closeSubpath()
    elif name == "group":
        path.addRoundedRect(QRectF(4, 4, 7, 7), 1.8, 1.8)
        path.addRoundedRect(QRectF(13, 4, 7, 7), 1.8, 1.8)
        path.addRoundedRect(QRectF(4, 13, 7, 7), 1.8, 1.8)
        path.addRoundedRect(QRectF(13, 13, 7, 7), 1.8, 1.8)
    elif name == "rhino":
        path.addEllipse(QRectF(4.5, 4.5, 15, 15)); line(9, 15, 15, 9); line(11, 9, 15, 9); line(15, 9, 15, 13)
    elif name == "sheet":
        path.addRoundedRect(QRectF(5, 4, 14, 16), 2, 2)
        line(5, 9, 19, 9); line(5, 14, 19, 14); line(10.5, 9, 10.5, 20)
    elif name == "settings":
        path.addEllipse(QRectF(9, 9, 6, 6))
        for k in range(6):
            a = k * math.pi / 3
            line(12 + 6 * math.cos(a), 12 + 6 * math.sin(a),
                 12 + 8.5 * math.cos(a), 12 + 8.5 * math.sin(a))
        path.addEllipse(QRectF(6, 6, 12, 12))
    p.drawPath(path)
    p.end()
    pm.setDevicePixelRatio(dpr)
    return QIcon(pm)


# ══════════════════════════════════════════════════════════════════════════════
# SMALL WIDGET HELPERS
# ══════════════════════════════════════════════════════════════════════════════
class _NoWheel:
    """
    The mouse wheel must never change a value — too many accidental edits while
    scrolling a list. Ignoring the event lets the panel underneath scroll
    instead. Values are set by typing, the arrow keys or the ± buttons.
    """
    def wheelEvent(self, e):
        e.ignore()


class QueueTree(QTreeWidget):
    """Queue list. Rows keep the order they were added in and can be dragged."""
    reordered = pyqtSignal()

    def dropEvent(self, e):
        super().dropEvent(e)
        self.reordered.emit()


class SpinBox(_NoWheel, QSpinBox):
    pass


class DoubleSpinBox(_NoWheel, QDoubleSpinBox):
    pass


class ComboBox(_NoWheel, QComboBox):
    pass


class FlowLayout(QLayout):
    """Lays widgets out in a row, wrapping onto the next line when short of width."""

    def __init__(self, parent=None, spacing=8):
        super().__init__(parent)
        self._items = []
        self.setSpacing(spacing)
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do(QRect(0, 0, width, 0), test=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do(rect, test=False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        w = h = 0
        for it in self._items:
            s = it.minimumSize()
            w = max(w, s.width())
            h = max(h, s.height())
        return QSize(w, h)

    def _do(self, rect, test):
        x, y, line = rect.x(), rect.y(), 0
        gap = self.spacing()
        for it in self._items:
            s = it.sizeHint()
            if line and x + s.width() > rect.right() + 1:
                x = rect.x()
                y += line + gap
                line = 0
            if not test:
                it.setGeometry(QRect(QPoint(x, y), s))
            x += s.width() + gap
            line = max(line, s.height())
        return y + line - rect.y()


def scrollable(widget: QWidget) -> QScrollArea:
    """
    Wrap a column so a short window scrolls instead of squashing the cards
    into each other.
    """
    area = QScrollArea()
    area.setWidget(widget)
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    area.viewport().setAutoFillBackground(False)
    area.setStyleSheet("QScrollArea { background: transparent; } "
                       "QScrollArea > QWidget > QWidget { background: transparent; }")
    return area


def _copy_item(it: JobItem) -> JobItem:
    d = dict(vars(it))
    d["member_qty"] = dict(it.member_qty)
    return JobItem(**d)


def make_card(title: str = "", right: List[QWidget] = None, margins=(14, 12, 14, 14)):
    """Rounded card. Returns (frame, body_layout)."""
    frame = QFrame()
    frame.setObjectName("card")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(*margins)
    lay.setSpacing(10)
    if title or right:
        head = QHBoxLayout()
        head.setSpacing(4)
        if title:
            lbl = QLabel(title.upper())
            lbl.setObjectName("cardTitle")
            head.addWidget(lbl)
        head.addStretch(1)
        for w in right or []:
            head.addWidget(w)
        lay.addLayout(head)
    return frame, lay


def icon_button(tip: str, name: str = "", parent=None) -> QToolButton:
    b = QToolButton(parent)
    b.setObjectName("icon")
    b.setFixedSize(QSize(30, 30))
    b.setToolTip(tip)
    b.setIconSize(QSize(18, 18))
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setProperty("iconName", name)
    return b


def button(text: str, primary=False, big=False, danger=False) -> QPushButton:
    b = QPushButton(text)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    if primary:
        b.setProperty("primary", True)
    if big:
        b.setProperty("big", True)
    if danger:
        b.setProperty("danger", True)
    return b


def label(text: str, obj: str = "") -> QLabel:
    l = QLabel(text)
    if obj:
        l.setObjectName(obj)
    return l


def divider() -> QFrame:
    f = QFrame()
    f.setObjectName("divider")
    return f


def spin(lo, hi, val, dec=1, step=0.1, suffix=" mm") -> QDoubleSpinBox:
    s = DoubleSpinBox()
    s.setMinimumWidth(64)
    s.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
    s.setRange(lo, hi)
    s.setDecimals(dec)
    s.setSingleStep(step)
    s.setValue(val)
    s.setSuffix(suffix)
    s.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
    s.setKeyboardTracking(False)
    return s


def polygon_path(geom, flip_h: float = None) -> QPainterPath:
    """QPainterPath (odd-even, so holes are holes) for a shapely (Multi)Polygon."""
    path = QPainterPath()
    path.setFillRule(Qt.FillRule.OddEvenFill)
    polys = [geom] if geom.geom_type == 'Polygon' else list(getattr(geom, 'geoms', []))
    for poly in polys:
        for ring in [poly.exterior] + list(poly.interiors):
            pts = list(ring.coords)
            if not pts:
                continue
            path.moveTo(pts[0][0], pts[0][1])
            for x, y in pts[1:]:
                path.lineTo(x, y)
            path.closeSubpath()
    return path


def design_colours(theme: dict, pair: tuple, dark: bool):
    fill, stroke = QColor(pair[0]), QColor(pair[1])
    if dark:
        fill = QColor(pair[1]); fill.setAlpha(95)
        stroke = QColor(pair[1]).lighter(135)
    return fill, stroke


def outline_pixmap(geom, size: int, theme: dict, colour: str = None) -> QPixmap:
    dpr = 2
    pm = QPixmap(size * dpr, size * dpr)
    pm.fill(Qt.GlobalColor.transparent)
    if geom is None or geom.is_empty:
        return pm
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    b = geom.bounds
    w, h = max(b[2] - b[0], 1e-6), max(b[3] - b[1], 1e-6)
    s = (size * dpr - 4) / max(w, h)
    tr = QTransform(s, 0, 0, -s, (size * dpr - w * s) / 2 - b[0] * s,
                    (size * dpr + h * s) / 2 + b[1] * s)
    p.setTransform(tr)
    c = QColor(colour or theme['accent'])
    fill = QColor(c); fill.setAlpha(45)
    pen = QPen(c, 1.3); pen.setCosmetic(True)
    p.setPen(pen)
    p.setBrush(fill)
    p.drawPath(polygon_path(geom))
    p.end()
    pm.setDevicePixelRatio(dpr)
    return pm


# ══════════════════════════════════════════════════════════════════════════════
# WORKER THREADS
# ══════════════════════════════════════════════════════════════════════════════
class ScanWorker(QThread):
    progress = pyqtSignal(int, int, str)
    done     = pyqtSignal()

    def __init__(self, lib_mgr):
        super().__init__()
        self.lib_mgr = lib_mgr

    def run(self):
        self.lib_mgr.scan(lambda c, t, m: self.progress.emit(c, t, m))
        self.done.emit()


class NestWorker(QThread):
    progress = pyqtSignal(int, int, str)
    best     = pyqtSignal(object)          # List[PlacementResult] (live)
    stats    = pyqtSignal(object)          # dict — search progress
    done     = pyqtSignal(object)          # NestResults

    def __init__(self, nest_job):
        super().__init__()
        self.nest_job = nest_job
        self.stop_event = threading.Event()

    def run(self):
        try:
            res = run_nesting(self.nest_job,
                              progress_callback=lambda c, t, m: self.progress.emit(c, t, m),
                              best_callback=lambda r: self.best.emit(r),
                              stop_event=self.stop_event,
                              stats_callback=lambda d: self.stats.emit(d))
        except Exception as ex:
            log.exception("Nesting failed")
            res = ex
        self.done.emit(res)


class ExportWorker(QThread):
    done = pyqtSignal(object)   # dict

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            self.done.emit(self.fn())
        except Exception as ex:
            log.exception("Export failed")
            self.done.emit({"error": str(ex)})


# ══════════════════════════════════════════════════════════════════════════════
# PLATFORM CANVAS (zoom / pan / hover)
# ══════════════════════════════════════════════════════════════════════════════
class PlatformCanvas(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumHeight(160)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.theme = THEMES["light"]
        self.dark = False
        self.result: Optional[PlacementResult] = None
        self.cmap: Dict[str, tuple] = {}
        self.overflow = (0.0, 0.0)
        self.message = "Add designs to the queue and run nesting"
        self.zoom = 1.0
        self.pan = QPointF(0, 0)
        self._drag = None
        self._paths = []            # [(item, QPainterPath world)]
        self._hover = None

    # public
    def set_theme(self, theme, dark):
        self.theme, self.dark = theme, dark
        self.update()

    def set_result(self, result, cmap, overflow=(0.0, 0.0), keep_view=True):
        same_size = (self.result is not None and result is not None and
                     (self.result.platform_w, self.result.platform_h) ==
                     (result.platform_w, result.platform_h) and self.overflow == overflow)
        self.result, self.cmap, self.overflow = result, cmap, overflow
        self._paths = [(it, polygon_path(it.placed_polygon)) for it in result.placements] \
            if result else []
        self._hover = None
        if not (keep_view and same_size):
            self.fit()
        self.update()

    def clear(self, message=""):
        self.result = None
        self._paths = []
        if message:
            self.message = message
        self.update()

    def fit(self):
        self.zoom = 1.0
        self.pan = QPointF(0, 0)
        self.update()

    # geometry
    def _view(self):
        """(scale, screen x of platform x=0, screen y of platform y=0)."""
        W, H = self.result.platform_w, self.result.platform_h
        fx, fy = self.overflow
        TW, TH = W + 2 * fx, H + 2 * fy
        pad = 28
        fit = min((self.width() - 2 * pad) / TW, (self.height() - 2 * pad) / TH)
        s = max(fit, 0.01) * self.zoom
        ox = (self.width() - TW * s) / 2 + self.pan.x() + fx * s
        oy = (self.height() + TH * s) / 2 + self.pan.y() - fy * s
        return s, ox, oy, TW, TH

    def _to_world(self, pt: QPointF):
        s, ox, oy, TW, TH = self._view()
        return QPointF((pt.x() - ox) / s, (oy - pt.y()) / s)

    # events
    def wheelEvent(self, e):
        if not self.result:
            return
        pos = e.position()
        before = self._to_world(pos)
        factor = 1.15 ** (e.angleDelta().y() / 120.0)
        self.zoom = min(40.0, max(0.5, self.zoom * factor))
        s, ox, oy, TW, TH = self._view()
        sx, sy = ox + before.x() * s, oy - before.y() * s
        self.pan += QPointF(pos.x() - sx, pos.y() - sy)
        self.update()

    def mousePressEvent(self, e):
        if e.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.MiddleButton):
            self._drag = (e.position(), QPointF(self.pan))
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseReleaseEvent(self, e):
        self._drag = None
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def mouseDoubleClickEvent(self, e):
        self.fit()

    def mouseMoveEvent(self, e):
        if not self.result:
            return
        if self._drag:
            start, pan0 = self._drag
            self.pan = pan0 + (e.position() - start)
            self.update()
            return
        w = self._to_world(e.position())
        hit = None
        for it, path in reversed(self._paths):
            if path.contains(w):
                hit = it
                break
        if hit is not self._hover:
            self._hover = hit
            self.update()
            if hit:
                extra = f"\nGroup: {hit.group_name} (set {hit.set_index + 1})" if hit.group_name else ""
                QToolTip.showText(e.globalPosition().toPoint(),
                                  f"{hit.design_name}\nRotation {hit.rotation:g}°   "
                                  f"at {hit.x:.2f}, {hit.y:.2f} mm{extra}", self)
            else:
                QToolTip.hideText()

    def leaveEvent(self, e):
        if self._hover is not None:
            self._hover = None
            self.update()

    # paint
    def paintEvent(self, e):
        t = self.theme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(t['canvas']))
        if not self.result:
            p.setPen(QColor(t['faint']))
            f = p.font(); f.setPointSize(12); p.setFont(f)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.message)
            return

        r = self.result
        W, H = r.platform_w, r.platform_h
        s, ox, oy, TW, TH = self._view()
        world = QTransform(s, 0, 0, -s, ox, oy)

        # overflow zone (reaches out on every side)
        fx, fy = self.overflow
        if fx > 0 or fy > 0:
            p.setTransform(world)
            pen = QPen(QColor(t['overflow']), 1.4, Qt.PenStyle.DashLine); pen.setCosmetic(True)
            p.setPen(pen)
            oc = QColor(t['overflow']); oc.setAlpha(18)
            p.setBrush(oc)
            p.drawRect(QRectF(-fx, -fy, TW, TH))

        # plate + grid
        p.setTransform(world)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(t['plate']))
        p.drawRect(QRectF(0, 0, W, H))
        gpen = QPen(QColor(t['grid']), 1); gpen.setCosmetic(True)
        p.setPen(gpen)
        step = 10 if s * 10 > 18 else 20
        gx = step
        while gx < W:
            p.drawLine(QPointF(gx, 0), QPointF(gx, H)); gx += step
        gy = step
        while gy < H:
            p.drawLine(QPointF(0, gy), QPointF(W, gy)); gy += step

        # pieces
        for it, path in self._paths:
            fill, stroke = design_colours(t, self.cmap.get(it.design_id, ("#EEEEEE", "#888888")),
                                          self.dark)
            if it is self._hover:
                fill = QColor(fill); fill.setAlpha(min(255, fill.alpha() + 90))
                pen = QPen(QColor(t['text']), 2.0)
            else:
                pen = QPen(stroke, 1.3)
            pen.setCosmetic(True)
            p.setPen(pen)
            p.setBrush(fill)
            p.drawPath(path)

        # plate border
        bpen = QPen(QColor(t['accent']), 2); bpen.setCosmetic(True)
        p.setPen(bpen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(QRectF(0, 0, W, H))

        # labels (screen space)
        p.resetTransform()
        f = p.font(); f.setPointSize(9); p.setFont(f)
        for it, path in self._paths:
            b = path.boundingRect()
            if b.width() * s < 110 or b.height() * s < 40:
                continue
            c = world.map(b.center())
            fill, stroke = design_colours(t, self.cmap.get(it.design_id, ("#EEEEEE", "#888888")),
                                          self.dark)
            p.setPen(stroke.darker(125) if not self.dark else QColor(t['text']))
            name = it.design_name if len(it.design_name) <= 14 else it.design_name[:13] + "…"
            p.drawText(QRectF(c.x() - 60, c.y() - 9, 120, 18), Qt.AlignmentFlag.AlignCenter, name)

        # dimensions
        p.setPen(QColor(t['muted']))
        tl = world.map(QPointF(0, 0))
        tr_ = world.map(QPointF(W, 0))
        top = world.map(QPointF(0, H))
        p.drawText(QRectF(tl.x(), tl.y() + 4, tr_.x() - tl.x(), 16),
                   Qt.AlignmentFlag.AlignCenter, f"{W:g} mm")
        p.save()
        p.translate(tl.x() - 8, (tl.y() + top.y()) / 2)
        p.rotate(-90)
        p.drawText(QRectF(-60, -14, 120, 16), Qt.AlignmentFlag.AlignCenter, f"{H:g} mm")
        p.restore()


def render_thumb(result, cmap, theme, dark, overflow, w=150, h=80) -> QPixmap:
    dpr = 2
    pm = QPixmap(w * dpr, h * dpr)
    pm.fill(QColor(theme['canvas']))
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    W, H = result.platform_w, result.platform_h
    fx, fy = overflow
    TW, TH = W + 2 * fx, H + 2 * fy
    pad = 6 * dpr
    s = min((w * dpr - 2 * pad) / TW, (h * dpr - 2 * pad) / TH)
    ox = (w * dpr - TW * s) / 2 + fx * s
    oy = (h * dpr + TH * s) / 2 - fy * s
    world = QTransform(s, 0, 0, -s, ox, oy)
    p.setTransform(world)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(theme['plate']))
    p.drawRect(QRectF(0, 0, W, H))
    for it in result.placements:
        fill, stroke = design_colours(theme, cmap.get(it.design_id, ("#EEEEEE", "#888888")), dark)
        pen = QPen(stroke, 1); pen.setCosmetic(True)
        p.setPen(pen); p.setBrush(fill)
        p.drawPath(polygon_path(it.placed_polygon))
    bpen = QPen(QColor(theme['accent']), 1.5); bpen.setCosmetic(True)
    p.setPen(bpen); p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRect(QRectF(0, 0, W, H))
    p.end()
    pm.setDevicePixelRatio(dpr)
    return pm


# ══════════════════════════════════════════════════════════════════════════════
# DIALOGS
# ══════════════════════════════════════════════════════════════════════════════
class GroupsDialog(QDialog):
    """
    Group editor: a group is a recipe — which designs, and how many of each in
    one set (e.g. a size series A×1, B×6, C×2, D×2). One set always nests on
    one platform.
    """
    def __init__(self, app: "LazyNestApp", focus_group: str = None):
        super().__init__(app)
        self.app = app
        self.setWindowTitle("Groups")
        self.resize(940, 560)
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)
        root.addWidget(label("A group is a recipe: which designs go together and how many of "
                             "each in one set. One set always lands on the same platform; the "
                             "queue holds the number of sets.", "muted"))

        cols = QHBoxLayout()
        cols.setSpacing(12)

        # ── groups ──
        newb = icon_button("New group", "plus")
        newb.setIcon(make_icon("plus", app.theme['muted']))
        newb.clicked.connect(self._new_group)
        delb = icon_button("Delete group", "trash")
        delb.setIcon(make_icon("trash", app.theme['muted']))
        delb.clicked.connect(self._delete)
        gcard, glay = make_card("Groups", [newb, delb])
        gcard.setFixedWidth(230)
        self.glist = QListWidget()
        self.glist.currentRowChanged.connect(lambda *_: self._show_members())
        self.glist.itemDoubleClicked.connect(lambda *_: self._rename())
        glay.addWidget(self.glist, 1)
        ren = button("Rename")
        ren.clicked.connect(self._rename)
        glay.addWidget(ren)
        cols.addWidget(gcard)

        # ── members ──
        remb = icon_button("Remove selected design from the group", "minus")
        remb.setIcon(make_icon("minus", app.theme['muted']))
        remb.clicked.connect(self._remove_member)
        mcard, mlay = make_card("Designs in one set", [remb])
        self.mtree = QTreeWidget()
        self.mtree.setObjectName("memberTree")
        self.mtree.setColumnCount(2)
        self.mtree.setHeaderLabels(["Design", "Per set"])
        self.mtree.setRootIsDecorated(False)
        self.mtree.setIconSize(QSize(26, 26))
        self.mtree.header().setStretchLastSection(False)
        self.mtree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.mtree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.mtree.setColumnWidth(1, 84)
        self.mtree.headerItem().setTextAlignment(1, Qt.AlignmentFlag.AlignCenter)
        mlay.addWidget(self.mtree, 1)
        self.sum_lbl = label("", "muted")
        mlay.addWidget(self.sum_lbl)
        cols.addWidget(mcard, 1)

        # ── library picker ──
        pcard, play = make_card("Add a design")
        pcard.setFixedWidth(250)
        self.search = QLineEdit()
        self.search.setObjectName("search")
        self.search.setPlaceholderText("Search designs")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._fill_picker)
        play.addWidget(self.search)
        self.picker = QTreeWidget()
        self.picker.setHeaderHidden(True)
        self.picker.setRootIsDecorated(False)
        self.picker.setIconSize(QSize(24, 24))
        self.picker.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.picker.itemDoubleClicked.connect(lambda *_: self._add_selected())
        play.addWidget(self.picker, 1)
        addb = button("Add to group", primary=True)
        addb.clicked.connect(self._add_selected)
        play.addWidget(addb)
        cols.addWidget(pcard)
        root.addLayout(cols, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        close = button("Done", primary=True)
        close.clicked.connect(self.accept)
        row.addWidget(close)
        root.addLayout(row)

        self._fill_groups(focus_group)
        self._fill_picker()

    # ── data ──
    def _gid(self):
        i = self.glist.currentRow()
        ids = [g.group_id for g in self.app.lib_mgr.all_groups()]
        return ids[i] if 0 <= i < len(ids) else None

    def _fill_groups(self, focus=None):
        self.glist.blockSignals(True)
        self.glist.clear()
        groups = self.app.lib_mgr.all_groups()
        icon = make_icon("group", self.app.theme['accent'], 20)
        for g in groups:
            it = QListWidgetItem(icon, f"{g.name}\n{g.pieces_per_set} pieces per set")
            self.glist.addItem(it)
        self.glist.blockSignals(False)
        if groups:
            idx = next((i for i, g in enumerate(groups) if g.group_id == focus), 0)
            self.glist.setCurrentRow(idx)
        self._show_members()

    def _show_members(self):
        self.mtree.clear()
        gid = self._gid()
        g = self.app.lib_mgr.groups.get(gid) if gid else None
        if not g:
            self.sum_lbl.setText("No group selected")
            return
        for did, count in g.members():
            e = self.app.lib_mgr.designs.get(did)
            it = QTreeWidgetItem([e.display_name if e else f"{did}  (missing)"])
            it.setData(0, Qt.ItemDataRole.UserRole, did)
            it.setIcon(0, self.app._thumb(did, 26))
            if not e:
                it.setForeground(0, QColor(self.app.theme['danger']))
            self.mtree.addTopLevelItem(it)
            sp = SpinBox()
            sp.setRange(1, 999)
            sp.setValue(count)
            sp.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
            sp.setAlignment(Qt.AlignmentFlag.AlignCenter)
            sp.setKeyboardTracking(False)
            sp.valueChanged.connect(lambda v, did=did: self._set_count(did, v))
            holder = QWidget()
            hl = QHBoxLayout(holder)
            hl.setContentsMargins(4, 3, 4, 3)
            hl.addWidget(sp)
            self.mtree.setItemWidget(it, 1, holder)
        self.sum_lbl.setText(f"{g.pieces_per_set} pieces in one set  ·  "
                             f"{len(g.design_ids)} design{'s' if len(g.design_ids) != 1 else ''}")

    def _fill_picker(self):
        q = self.search.text().strip().lower()
        self.picker.clear()
        for e in self.app.lib_mgr.all_designs():
            if q and q not in e.display_name.lower():
                continue
            it = QTreeWidgetItem([e.display_name])
            it.setData(0, Qt.ItemDataRole.UserRole, e.design_id)
            it.setIcon(0, self.app._thumb(e.design_id, 24))
            self.picker.addTopLevelItem(it)

    # ── actions ──
    def _set_count(self, design_id, value):
        gid = self._gid()
        g = self.app.lib_mgr.groups.get(gid) if gid else None
        if not g:
            return
        counts = dict(g.counts)
        counts[design_id] = value
        self.app.lib_mgr.update_group(gid, counts=counts)
        self.sum_lbl.setText(f"{g.pieces_per_set} pieces in one set  ·  "
                             f"{len(g.design_ids)} design{'s' if len(g.design_ids) != 1 else ''}")
        self._refresh_app(keep=gid)

    def _add_selected(self):
        gid = self._gid()
        g = self.app.lib_mgr.groups.get(gid) if gid else None
        if not g:
            QMessageBox.information(self, "No group", "Create a group first.")
            return
        dids = [i.data(0, Qt.ItemDataRole.UserRole) for i in self.picker.selectedItems()]
        if not dids:
            return
        ids = list(g.design_ids)
        counts = dict(g.counts)
        for did in dids:
            if did in counts:
                counts[did] += 1
            else:
                ids.append(did)
                counts[did] = 1
        self.app.lib_mgr.update_group(gid, design_ids=ids, counts=counts)
        self._show_members()
        self._fill_groups(gid)
        self._refresh_app(keep=gid)

    def _remove_member(self):
        gid = self._gid()
        g = self.app.lib_mgr.groups.get(gid) if gid else None
        sel = self.mtree.selectedItems()
        if not g or not sel:
            return
        drop = {i.data(0, Qt.ItemDataRole.UserRole) for i in sel}
        ids = [d for d in g.design_ids if d not in drop]
        counts = {d: c for d, c in g.counts.items() if d not in drop}
        self.app.lib_mgr.update_group(gid, design_ids=ids, counts=counts)
        self._show_members()
        self._fill_groups(gid)
        self._refresh_app(keep=gid)

    def _new_group(self):
        name, ok = QInputDialog.getText(self, "New group", "Group name:")
        if not ok or not name.strip():
            return
        dids = [i.data(0, Qt.ItemDataRole.UserRole)
                for i in self.app._lib_tree.selectedItems()]
        g = self.app.lib_mgr.create_group(name.strip(), dids)
        self._fill_groups(g.group_id)
        self._refresh_app()

    def _rename(self):
        gid = self._gid()
        g = self.app.lib_mgr.groups.get(gid) if gid else None
        if not g:
            return
        name, ok = QInputDialog.getText(self, "Rename group", "New name:", text=g.name)
        if ok and name.strip():
            self.app.lib_mgr.update_group(gid, name=name.strip())
            for it in self.app._queue_items:
                if it.group_id == gid:
                    it.design_name = name.strip()
            self._fill_groups(gid)
            self._refresh_app()

    def _delete(self):
        gid = self._gid()
        g = self.app.lib_mgr.groups.get(gid) if gid else None
        if not g:
            return
        if QMessageBox.question(self, "Delete group",
                                f"Delete '{g.name}'? The designs themselves are not affected."
                                ) != QMessageBox.StandardButton.Yes:
            return
        self.app.lib_mgr.delete_group(gid)
        self.app._queue_items = [x for x in self.app._queue_items if x.group_id != gid]
        self._fill_groups()
        self._refresh_app()

    def _refresh_app(self, keep=None):
        self.app._refresh_groups()
        self.app._refresh_queue()


# ══════════════════════════════════════════════════════════════════════════════
# MAIN WINDOW
# ══════════════════════════════════════════════════════════════════════════════
class LazyNestApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(900, 560)
        icon_path = os.path.join(RES_DIR, "icon.png")
        if os.path.isfile(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        self.cfg_mgr = ConfigManager(CONFIG_DIR)
        self.lib_mgr = LibraryManager(CONFIG_DIR)
        self.job_mgr = JobManager(CONFIG_DIR)

        self.theme_name = self.cfg_mgr.cfg.theme if self.cfg_mgr.cfg.theme in THEMES else "light"
        self.theme = THEMES[self.theme_name]

        self.current_job_id: Optional[str] = self.cfg_mgr.cfg.last_job_id or None
        self.nesting_results: List[PlacementResult] = []
        self.results_meta = {}            # snapshot of settings used for current results
        self._plat_idx = 0
        self._queue_items: List[JobItem] = []
        self._colour_map: Dict[str, tuple] = {}
        self._expanded_groups: set = set()
        self._row_for: Dict[int, QTreeWidgetItem] = {}
        self._thumb_cache: Dict[tuple, QPixmap] = {}
        self._loading = False
        self._scan_worker: Optional[ScanWorker] = None
        self._nest_worker: Optional[NestWorker] = None
        self._export_worker: Optional[ExportWorker] = None
        self._last_export = None          # dict with paths of the last export
        self._last_nest_results = None    # NestResults of the last finished run

        self._estimate_timer = QTimer(self)
        self._estimate_timer.setSingleShot(True)
        self._estimate_timer.timeout.connect(self._run_estimate)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._save_job)

        self._build_ui()
        self._apply_theme()
        self._restore_geometry()
        self._shortcuts()

        # library.json can be deleted to start fresh — fall back to the folder
        # remembered in config.json so the library comes straight back
        if not self.lib_mgr.folder and self.cfg_mgr.cfg.library_folder \
                and os.path.isdir(self.cfg_mgr.cfg.library_folder):
            self.lib_mgr.set_folder(self.cfg_mgr.cfg.library_folder)
        if self.lib_mgr.folder:
            self._async_scan(silent=True)
        self._refresh_lib()
        self._refresh_job_combo()
        self._load_job(self.current_job_id)

    # ── UI construction ───────────────────────────────────────────────────────
    def _build_ui(self):
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_topbar())

        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(12, 12, 12, 12)
        self._main_split = QSplitter(Qt.Orientation.Horizontal)
        self._main_split.setChildrenCollapsible(False)
        for col, min_w in ((self._build_library_col(), 230),
                           (self._build_job_col(), 300),
                           (self._build_viewer_col(), 300)):
            area = scrollable(col)
            area.setMinimumWidth(min_w)
            self._main_split.addWidget(area)
        self._main_split.setSizes([300, 380, 760])
        self._main_split.setStretchFactor(0, 0)
        self._main_split.setStretchFactor(1, 0)
        self._main_split.setStretchFactor(2, 1)
        bl.addWidget(self._main_split)
        outer.addWidget(body, 1)

        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_lbl = QLabel("Ready")
        self._status_lbl.setContentsMargins(10, 0, 0, 0)
        self._status_bar.addWidget(self._status_lbl, 1)
        self._status_hint = label("", "statusHint")
        self._status_bar.addPermanentWidget(self._status_hint)
        self._status_stat = label("", "statusStat")
        self._status_bar.addPermanentWidget(self._status_stat)
        self._status_time = label("", "statusTime")
        self._status_bar.addPermanentWidget(self._status_time)
        self._prog = QProgressBar()
        self._prog.setFixedWidth(220)
        self._prog.setTextVisible(False)
        self._prog.setRange(0, 100)
        self._prog.setValue(0)
        self._status_bar.addPermanentWidget(self._prog)

    def _build_topbar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("topbar")
        bar.setFixedHeight(58)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(18, 0, 14, 0)
        lay.setSpacing(8)

        logo = QLabel()
        icon_path = os.path.join(RES_DIR, "icon.png")
        if os.path.isfile(icon_path):
            pm = QPixmap(icon_path)
            if not pm.isNull():
                logo.setPixmap(pm.scaled(28, 28, Qt.AspectRatioMode.KeepAspectRatio,
                                         Qt.TransformationMode.SmoothTransformation))
                lay.addWidget(logo)
        brand = QHBoxLayout(); brand.setSpacing(0)
        brand.addWidget(label("LazyNest", "brand"))
        brand.addWidget(label(".", "brandDot"))
        lay.addLayout(brand)
        lay.addWidget(label(f"v{APP_VERSION}", "version"))
        lay.addSpacing(26)

        lay.addWidget(label("Job", "formLabel"))
        self._job_combo = ComboBox()
        self._job_combo.setObjectName("jobCombo")
        self._job_combo.setMinimumWidth(220)
        self._job_combo.setMaximumWidth(380)
        self._job_combo.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._job_combo.currentIndexChanged.connect(self._on_job_selected)
        lay.addWidget(self._job_combo)
        self._btn_new_job = icon_button("New job", "plus")
        self._btn_new_job.clicked.connect(self._new_job)
        self._btn_dup_job = icon_button("Duplicate job", "copy")
        self._btn_dup_job.clicked.connect(self._dup_job)
        self._btn_ren_job = icon_button("Rename job", "edit")
        self._btn_ren_job.clicked.connect(self._rename_job)
        self._btn_del_job = icon_button("Delete job", "trash")
        self._btn_del_job.clicked.connect(self._del_job)
        for b in (self._btn_new_job, self._btn_dup_job, self._btn_ren_job, self._btn_del_job):
            lay.addWidget(b)

        lay.addStretch(1)
        self._btn_theme = icon_button("Switch light / dark", "moon")
        self._btn_theme.clicked.connect(self._toggle_theme)
        lay.addWidget(self._btn_theme)
        return bar

    # LEFT ─ library + groups
    def _build_library_col(self) -> QWidget:
        col = QSplitter(Qt.Orientation.Vertical)
        col.setChildrenCollapsible(False)

        self._btn_folder = icon_button("Choose library folder", "folder")
        self._btn_folder.clicked.connect(self._set_library_folder)
        self._btn_rescan = icon_button("Re-scan library", "refresh")
        self._btn_rescan.clicked.connect(lambda: self._async_scan())
        card, lay = make_card("Library", [self._btn_folder, self._btn_rescan])
        self._folder_lbl = label("", "faint")
        self._folder_lbl.setMinimumWidth(40)
        lay.addWidget(self._folder_lbl)

        self._search_edit = QLineEdit()
        self._search_edit.setObjectName("search")
        self._search_edit.setPlaceholderText("Search designs")
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.textChanged.connect(self._refresh_lib)
        lay.addWidget(self._search_edit)

        self._lib_tree = QTreeWidget()
        self._lib_tree.setObjectName("libTree")
        self._lib_tree.setHeaderLabels(["Design", "Size mm"])
        self._lib_tree.setRootIsDecorated(False)
        self._lib_tree.setIconSize(QSize(30, 30))
        self._lib_tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._lib_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._lib_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._lib_tree.header().setStretchLastSection(False)
        self._lib_tree.itemDoubleClicked.connect(lambda *_: self._add_to_job())
        self._lib_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)
        lay.addWidget(self._lib_tree, 1)

        row = QHBoxLayout()
        add = button("Add to job", primary=True)
        add.clicked.connect(self._add_to_job)
        ren = button("Rename")
        ren.clicked.connect(self._rename_design)
        row.addWidget(add, 1)
        row.addWidget(ren)
        lay.addLayout(row)
        self._lib_count = label("", "faint")
        lay.addWidget(self._lib_count)
        col.addWidget(card)

        self._btn_new_group = icon_button("New group from selected designs", "plus")
        self._btn_new_group.clicked.connect(self._create_group)
        self._btn_manage_groups = icon_button("Manage groups", "settings")
        self._btn_manage_groups.clicked.connect(self._manage_groups)
        gcard, glay = make_card("Groups", [self._btn_new_group, self._btn_manage_groups])
        self._grp_tree = QTreeWidget()
        self._grp_tree.setHeaderHidden(True)
        self._grp_tree.setRootIsDecorated(False)
        self._grp_tree.setIconSize(QSize(18, 18))
        self._grp_tree.itemDoubleClicked.connect(lambda *_: self._add_group_to_job())
        glay.addWidget(self._grp_tree, 1)
        gadd = button("Add group to job")
        gadd.clicked.connect(self._add_group_to_job)
        glay.addWidget(gadd)
        col.addWidget(gcard)
        col.setSizes([520, 200])
        return col

    # MIDDLE ─ queue + settings
    def _build_job_col(self) -> QWidget:
        wrap = QWidget()
        v = QVBoxLayout(wrap)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)

        self._btn_q_remove = icon_button("Remove selected rows (Delete)", "trash")
        self._btn_q_remove.clicked.connect(self._remove_from_queue)
        self._btn_q_clear = icon_button("Clear queue", "close")
        self._btn_q_clear.clicked.connect(self._clear_queue)
        qcard, qlay = make_card("Queue", [self._btn_q_remove, self._btn_q_clear])
        self._q_tree = QueueTree()
        self._q_tree.setObjectName("queueTree")
        self._q_tree.setColumnCount(3)
        self._q_tree.setHeaderLabels(["Design", "Qty", "Priority"])
        self._q_tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._q_tree.setIconSize(QSize(26, 26))
        self._q_tree.setUniformRowHeights(True)   # keeps the scrollbar honest
        self._q_tree.setIndentation(16)
        self._q_tree.setExpandsOnDoubleClick(False)
        self._q_tree.setRootIsDecorated(True)
        hh = self._q_tree.header()
        hh.setStretchLastSection(False)
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self._q_tree.setMinimumHeight(150)
        self._q_tree.setColumnWidth(1, 96)
        self._q_tree.setColumnWidth(2, 68)
        head = self._q_tree.headerItem()
        head.setTextAlignment(1, Qt.AlignmentFlag.AlignCenter)
        head.setTextAlignment(2, Qt.AlignmentFlag.AlignCenter)
        self._q_tree.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._q_tree.setDefaultDropAction(Qt.DropAction.MoveAction)
        self._q_tree.setDragDropOverwriteMode(False)
        self._q_tree.setDropIndicatorShown(True)
        self._q_tree.setToolTip("Drag rows to change their order")
        self._q_tree.reordered.connect(self._queue_rows_moved)
        self._q_tree.model().rowsMoved.connect(self._queue_rows_moved)
        self._q_tree.itemExpanded.connect(lambda i: self._remember_expanded(i, True))
        self._q_tree.itemCollapsed.connect(lambda i: self._remember_expanded(i, False))
        qlay.addWidget(self._q_tree, 1)
        self._q_summary = label("Queue is empty — double-click designs in the library to add them",
                                "faint")
        self._q_summary.setWordWrap(True)
        qlay.addWidget(self._q_summary)
        v.addWidget(qcard, 1)

        scard, slay = make_card("Settings")
        cfg = self.cfg_mgr.cfg
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        self._pw_spin = spin(1, 5000, cfg.last_platform_w)
        self._ph_spin = spin(1, 5000, cfg.last_platform_h)
        self._mg_spin = spin(0, 20, cfg.last_margin_mm, dec=2, step=0.05)
        self._ec_spin = spin(0, 50, 0.0, dec=2, step=0.1)
        self._mg_spin.setToolTip("Minimum distance between the cut outlines of two pieces")
        self._ec_spin.setToolTip("Minimum distance between a piece and the platform edge")
        grid.addWidget(label("Platform", "formLabel"), 0, 0)
        grid.addWidget(self._pw_spin, 0, 1)
        grid.addWidget(label("×", "formLabel"), 0, 2, Qt.AlignmentFlag.AlignCenter)
        grid.addWidget(self._ph_spin, 0, 3)
        grid.addWidget(label("Gap", "formLabel"), 1, 0)
        grid.addWidget(self._mg_spin, 1, 1)
        grid.addWidget(label("Edge", "formLabel"), 1, 2)
        grid.addWidget(self._ec_spin, 1, 3)
        grid.addWidget(label("Rotation", "formLabel"), 2, 0)
        self._rot_combo = ComboBox()
        self._rot_combo.setMinimumWidth(90)
        self._rot_combo.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        for key, (lbl, _) in ROTATION_MODES.items():
            self._rot_combo.addItem(lbl, key)
        ri = self._rot_combo.findData(cfg.last_rotation)
        self._rot_combo.setCurrentIndex(ri if ri >= 0 else 1)
        grid.addWidget(self._rot_combo, 2, 1, 1, 3)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        slay.addLayout(grid)

        slay.addWidget(divider())
        self._ovf_check = QCheckBox("Allow overflow past the platform edge")
        self._ovf_check.setToolTip("Pieces may hang over the platform edge by up to the set "
                                   "amount on EACH side — used only when it saves space")
        slay.addWidget(self._ovf_check)
        self._ovf_row = QWidget()
        orow = QGridLayout(self._ovf_row)
        orow.setContentsMargins(0, 0, 0, 0)
        orow.setHorizontalSpacing(10)
        orow.setVerticalSpacing(4)
        self._ox_spin = spin(0, 100, 0.0, dec=1, step=0.5)
        self._oy_spin = spin(0, 100, 0.0, dec=1, step=0.5)
        orow.addWidget(label("Sides ↔", "formLabel"), 0, 0)
        orow.addWidget(self._ox_spin, 0, 1)
        orow.addWidget(label("Top/bottom ↕", "formLabel"), 0, 2)
        orow.addWidget(self._oy_spin, 0, 3)
        orow.setColumnStretch(1, 1)
        orow.setColumnStretch(3, 1)
        self._ox_spin.setToolTip("How far pieces may reach past the left edge AND the right "
                                 "edge (e.g. 2 mm = 2 mm on each side, 4 mm extra width)")
        self._oy_spin.setToolTip("How far pieces may reach past the bottom edge AND the top "
                                 "edge (e.g. 2 mm = 2 mm on each side, 4 mm extra height)")
        slay.addWidget(self._ovf_row)
        self._holes_check = QCheckBox("Nest small pieces inside holes")
        self._holes_check.setToolTip("Use large through-holes (e.g. ring centres) for small pieces")
        slay.addWidget(self._holes_check)
        self._keep_groups_check = QCheckBox("Keep each group's set on one platform")
        self._keep_groups_check.setChecked(cfg.last_keep_groups)
        self._keep_groups_check.setToolTip(
            "On — every set of a group is nested together on the same platform.\n"
            "Off — the group is only a quick way to add designs: its pieces are "
            "nested like any others, wherever they fit best.")
        slay.addWidget(self._keep_groups_check)

        slay.addWidget(divider())
        seg = QHBoxLayout()
        seg.setSpacing(0)
        self._quality_group = QButtonGroup(self)
        self._quality_group.setExclusive(True)
        self._quality_btns = {}
        for i, (key, (lbl, _)) in enumerate(QUALITY_MODES.items()):
            b = QToolButton()
            b.setObjectName("seg")
            b.setText(QUALITY_SHORT.get(key, lbl))
            b.setToolTip(lbl)
            b.setCheckable(True)
            b.setProperty("pos", "first" if i == 0 else
                          ("last" if i == len(QUALITY_MODES) - 1 else "mid"))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            b.setMinimumWidth(56)
            self._quality_group.addButton(b)
            self._quality_btns[key] = b
            seg.addWidget(b)
        slay.addWidget(label("Search effort — longer finds tighter layouts", "formLabel"))
        slay.addLayout(seg)

        self._est_lbl = label("", "faint")
        slay.addWidget(self._est_lbl)
        self._run_btn = button("Run nesting", primary=True, big=True)
        self._run_btn.clicked.connect(self._run_or_stop)
        slay.addWidget(self._run_btn)
        v.addWidget(scard)

        for w in (self._pw_spin, self._ph_spin, self._mg_spin, self._ec_spin,
                  self._ox_spin, self._oy_spin):
            w.valueChanged.connect(self._settings_changed)
        self._rot_combo.currentIndexChanged.connect(self._settings_changed)
        self._ovf_check.toggled.connect(self._settings_changed)
        self._holes_check.toggled.connect(self._settings_changed)
        self._keep_groups_check.toggled.connect(self._settings_changed)
        self._quality_group.buttonClicked.connect(lambda *_: self._settings_changed())
        return wrap

    # RIGHT ─ viewer + output
    def _build_viewer_col(self) -> QWidget:
        wrap = QWidget()
        v = QVBoxLayout(wrap)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)

        self._btn_prev = icon_button("Previous platform", "left")
        self._btn_prev.clicked.connect(self._prev_plat)
        self._btn_next = icon_button("Next platform", "right")
        self._btn_next.clicked.connect(self._next_plat)
        self._btn_fit = icon_button("Fit to view (double-click canvas)", "fit")
        self._btn_fit.clicked.connect(lambda: self._canvas.fit())
        vcard, vlay = make_card("Result", [self._btn_prev, self._btn_next, self._btn_fit],
                                margins=(14, 12, 14, 12))
        head = QHBoxLayout()
        head.setSpacing(8)
        self._plat_lbl = label("No results yet", "platTitle")
        head.addWidget(self._plat_lbl)
        self._search_chip = label("", "chip")
        self._search_chip.setSizePolicy(QSizePolicy.Policy.Ignored,
                                        QSizePolicy.Policy.Preferred)
        self._search_chip.hide()
        head.addWidget(self._search_chip)
        self._search_stop = QPushButton("Stop")
        self._search_stop.setObjectName("chipStop")
        self._search_stop.setCursor(Qt.CursorShape.PointingHandCursor)
        self._search_stop.clicked.connect(self._stop_nesting)
        self._search_stop.hide()
        head.addWidget(self._search_stop)
        head.addStretch(1)
        self._plat_sub = label("", "muted")
        self._plat_sub.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._plat_sub.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        head.addWidget(self._plat_sub, 1)
        vlay.addLayout(head)

        self._pulse = QTimer(self)
        self._pulse.timeout.connect(self._pulse_tick)
        self._pulse_on = True

        self._canvas = PlatformCanvas()
        vlay.addWidget(self._canvas, 1)
        self._legend = QLabel()
        self._legend.setObjectName("muted")
        self._legend.setWordWrap(True)
        self._legend.setTextFormat(Qt.TextFormat.RichText)
        vlay.addWidget(self._legend)

        self._strip = QListWidget()
        self._strip.setObjectName("strip")
        self._strip.setViewMode(QListWidget.ViewMode.IconMode)
        self._strip.setFlow(QListWidget.Flow.LeftToRight)
        self._strip.setWrapping(False)
        self._strip.setMovement(QListWidget.Movement.Static)
        self._strip.setIconSize(QSize(150, 80))
        self._strip.setFixedHeight(122)
        self._strip.setSpacing(2)
        self._strip.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._strip.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._strip.currentRowChanged.connect(self._on_strip)
        vlay.addWidget(self._strip)

        kpis = FlowLayout(spacing=8)
        self._k_plat = self._kpi(kpis, "PLATFORMS")
        self._k_pieces = self._kpi(kpis, "PIECES PLACED")
        self._k_metal = self._kpi(kpis, "METAL USED", accent=True)
        self._k_this = self._kpi(kpis, "THIS PLATFORM")
        vlay.addLayout(kpis)
        self._unplaced_lbl = label("", "danger")
        self._unplaced_lbl.setWordWrap(True)
        self._unplaced_lbl.hide()
        vlay.addWidget(self._unplaced_lbl)
        v.addWidget(vcard, 1)

        ocard, olay = make_card("Output")
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        grid.addWidget(label("DXF folder", "formLabel"), 0, 0)
        self._out_edit = QLineEdit(self.cfg_mgr.cfg.output_folder or "")
        self._out_edit.setMinimumWidth(120)
        self._out_edit.setPlaceholderText("Base folder — files go to <date>/Rhino "
                                          "and <date>/Prisma & Solanki")
        self._out_edit.editingFinished.connect(self._out_edited)
        grid.addWidget(self._out_edit, 0, 1)
        b1 = icon_button("Choose DXF folder", "folder"); b1.clicked.connect(self._browse_output)
        grid.addWidget(b1, 0, 2)
        grid.addWidget(label("Report folder", "formLabel"), 1, 0)
        self._xlsx_edit = QLineEdit(self.cfg_mgr.cfg.report_folder or "")
        self._xlsx_edit.setMinimumWidth(120)
        self._xlsx_edit.setPlaceholderText("Excel report is updated after every export")
        self._xlsx_edit.editingFinished.connect(self._report_edited)
        grid.addWidget(self._xlsx_edit, 1, 1)
        b2 = icon_button("Choose report folder", "folder"); b2.clicked.connect(self._browse_xlsx)
        grid.addWidget(b2, 1, 2)
        grid.setColumnStretch(1, 1)
        olay.addLayout(grid)
        row = FlowLayout(spacing=8)
        self._export_btn = button("Export", primary=True)
        self._export_btn.setToolTip(
            "Writes everything for this job:\n"
            "  • Rhino DXF — all platforms side by side\n"
            "  • PRISMA / Solanki — R12 DXF, R2004 DXF and SVG\n"
            "  • the day's Excel report and the cumulative one")
        self._export_btn.clicked.connect(self._export)
        self._rhino_btn = button("Open in Rhino")
        self._rhino_btn.clicked.connect(self._open_in_rhino)
        self._folder_btn = button("Open folder")
        self._folder_btn.clicked.connect(self._open_folder)
        self._report_btn = button("Open report")
        self._report_btn.clicked.connect(self._open_report)
        for b in (self._export_btn, self._rhino_btn, self._folder_btn, self._report_btn):
            row.addWidget(b)
        olay.addLayout(row)
        v.addWidget(ocard)
        return wrap

    def _kpi(self, layout, title, accent=False):
        f = QFrame()
        f.setObjectName("kpi")
        f.setMinimumWidth(132)
        l = QVBoxLayout(f)
        l.setContentsMargins(12, 8, 12, 8)
        l.setSpacing(0)
        val = label("—", "kpiAccent" if accent else "kpiValue")
        l.addWidget(val)
        l.addWidget(label(title, "kpiLabel"))
        layout.addWidget(f)
        return val

    def _shortcuts(self):
        QShortcut(QKeySequence("Ctrl+R"), self, activated=self._run_or_stop)
        QShortcut(QKeySequence("Ctrl+E"), self, activated=self._export)
        QShortcut(QKeySequence("Ctrl+N"), self, activated=self._new_job)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=self._stop_nesting)
        QShortcut(QKeySequence(Qt.Key.Key_Left), self._canvas, activated=self._prev_plat)
        QShortcut(QKeySequence(Qt.Key.Key_Right), self._canvas, activated=self._next_plat)
        QShortcut(QKeySequence(Qt.Key.Key_Delete), self._q_tree, activated=self._remove_from_queue)
        QShortcut(QKeySequence(Qt.Key.Key_Backspace), self._q_tree, activated=self._remove_from_queue)

    # ── Theme ─────────────────────────────────────────────────────────────────
    def _apply_theme(self):
        t = self.theme
        QApplication.instance().setStyleSheet(build_stylesheet(t))
        pal = QApplication.instance().palette()
        pal.setColor(QPalette.ColorRole.Window, QColor(t['window']))
        pal.setColor(QPalette.ColorRole.Base, QColor(t['field']))
        pal.setColor(QPalette.ColorRole.Text, QColor(t['text']))
        pal.setColor(QPalette.ColorRole.WindowText, QColor(t['text']))
        pal.setColor(QPalette.ColorRole.Button, QColor(t['sunken']))
        pal.setColor(QPalette.ColorRole.ButtonText, QColor(t['text']))
        pal.setColor(QPalette.ColorRole.Highlight, QColor(t['accent']))
        pal.setColor(QPalette.ColorRole.PlaceholderText, QColor(t['faint']))
        QApplication.instance().setPalette(pal)
        for b in self.findChildren(QToolButton):
            name = b.property("iconName")
            if name:
                b.setIcon(make_icon(name, t['muted']))
        self._btn_theme.setIcon(make_icon("sun" if self.theme_name == "dark" else "moon",
                                          t['muted']))
        self._run_btn.setIcon(make_icon("stop" if self._is_nesting() else "play",
                                        t['accent_text'], 16))
        self._export_btn.setIcon(make_icon("export", t['accent_text'], 16))
        self._rhino_btn.setIcon(make_icon("rhino", t['text'], 16))
        self._folder_btn.setIcon(make_icon("folder", t['text'], 16))
        self._report_btn.setIcon(make_icon("sheet", t['text'], 16))
        self._canvas.set_theme(t, self.theme_name == "dark")
        self._thumb_cache.clear()
        self._refresh_lib()
        self._refresh_groups()
        self._refresh_queue(schedule=False)
        self._refresh_strip()

    def _toggle_theme(self):
        self.theme_name = "dark" if self.theme_name == "light" else "light"
        self.theme = THEMES[self.theme_name]
        self.cfg_mgr.cfg.theme = self.theme_name
        self.cfg_mgr.save()
        self._apply_theme()

    # ── Status ────────────────────────────────────────────────────────────────
    def _status(self, msg: str, pct: int = -1):
        self._status_lbl.setText(msg)
        if pct >= 0:
            self._prog.setValue(pct)

    def _status_later(self, msg="Ready", ms=3000):
        QTimer.singleShot(ms, lambda: (not self._is_nesting()) and self._status(msg, 0))

    def _is_nesting(self):
        return bool(self._nest_worker and self._nest_worker.isRunning())

    # ── Library ───────────────────────────────────────────────────────────────
    def _thumb(self, design_id: str, size: int) -> QIcon:
        key = (design_id, size, self.theme_name)
        pm = self._thumb_cache.get(key)
        if pm is None:
            geom, holes = self.lib_mgr.get_geometry(design_id)
            if geom is not None and holes is not None:
                try:
                    geom = geom.difference(holes)
                except Exception:
                    pass
            pm = outline_pixmap(geom, size, self.theme)
            self._thumb_cache[key] = pm
        return QIcon(pm)

    def _set_library_folder(self):
        start = self.lib_mgr.folder or QDir.homePath()
        folder = QFileDialog.getExistingDirectory(self, "Choose DXF library folder", start)
        if not folder:
            return
        folder = native_path(folder)
        self.lib_mgr.set_folder(folder)
        self.cfg_mgr.cfg.library_folder = folder
        self.cfg_mgr.save()
        self._async_scan()

    def _async_scan(self, silent=False):
        if not self.lib_mgr.folder:
            if not silent:
                QMessageBox.information(self, "No library folder",
                                        "Choose the folder that holds your DXF designs first.")
            return
        if self._scan_worker and self._scan_worker.isRunning():
            return
        self._scan_worker = ScanWorker(self.lib_mgr)
        self._scan_worker.progress.connect(
            lambda c, t, m: self._status(f"Reading designs… {c}/{t}  {m}", int(c / max(t, 1) * 100)))
        self._scan_worker.done.connect(self._scan_done)
        self._scan_worker.start()
        self._status("Reading designs…", 0)

    def _scan_done(self):
        self._thumb_cache.clear()
        self._refresh_lib()
        self._refresh_queue()
        n = len(self.lib_mgr.designs)
        self._status(f"Library ready — {n} designs", 100)
        self._status_later()

    def _refresh_lib(self):
        if not hasattr(self, "_lib_tree"):
            return
        folder = self.lib_mgr.folder
        self._folder_lbl.setText(("…" + folder[-40:]) if folder and len(folder) > 42
                                 else (folder or "No folder chosen"))
        self._folder_lbl.setToolTip(folder or "")
        q = self._search_edit.text().strip().lower()
        self._lib_tree.clear()
        shown = 0
        for e in self.lib_mgr.all_designs():
            if q and q not in e.display_name.lower():
                continue
            item = QTreeWidgetItem([e.display_name, f"{e.width_mm:.1f} × {e.height_mm:.1f}"])
            item.setData(0, Qt.ItemDataRole.UserRole, e.design_id)
            item.setIcon(0, self._thumb(e.design_id, 30))
            tip = [e.filename, f"Outline: {e.hull_source or '—'}"]
            if e.quality == 'hull':
                tip.append("⚠ Outline could not be closed — using convex hull")
                item.setForeground(0, QColor(self.theme['warn']))
            elif e.quality == 'all-layers':
                tip.append("No '0.5_Out' layer — outline built from all layers")
            if e.error:
                tip.append(f"Error: {e.error}")
                item.setForeground(0, QColor(self.theme['danger']))
            item.setToolTip(0, "\n".join(tip))
            item.setForeground(1, QColor(self.theme['muted']))
            self._lib_tree.addTopLevelItem(item)
            shown += 1
        total = len(self.lib_mgr.designs)
        self._lib_count.setText(f"{shown} of {total} designs" if q else f"{total} designs")

    def _refresh_groups(self):
        if not hasattr(self, "_grp_tree"):
            return
        self._grp_tree.clear()
        icon = make_icon("group", self.theme['muted'])
        for g in self.lib_mgr.all_groups():
            item = QTreeWidgetItem([f"{g.name}   ·  {g.pieces_per_set} per set"])
            item.setData(0, Qt.ItemDataRole.UserRole, g.group_id)
            item.setIcon(0, icon)
            item.setToolTip(0, "One set: " + ", ".join(
                f"{(self.lib_mgr.designs.get(d).display_name if self.lib_mgr.designs.get(d) else d)}"
                f" ×{c}" for d, c in g.members()) + "\n\nDouble-click to add to the job.")
            self._grp_tree.addTopLevelItem(item)

    def _rename_design(self):
        sel = self._lib_tree.selectedItems()
        if not sel:
            return
        did = sel[0].data(0, Qt.ItemDataRole.UserRole)
        e = self.lib_mgr.designs.get(did)
        if not e:
            return
        name, ok = QInputDialog.getText(self, "Rename design",
                                        "New name (the .dxf file on disk is renamed too):",
                                        text=e.display_name)
        if not ok or not name.strip():
            return
        _, msg, new_id = self.lib_mgr.rename(did, name.strip())
        for it in self._queue_items:
            if not it.group_id and it.design_id == did:
                it.design_id = new_id
                it.design_name = name.strip()
        self.job_mgr.rename_design_refs(did, new_id, name.strip())
        self._thumb_cache.clear()
        self._refresh_lib()
        self._refresh_queue()
        if msg:
            self._status(msg)
            self._status_later()

    # ── Groups ────────────────────────────────────────────────────────────────
    def _create_group(self):
        sel = self._lib_tree.selectedItems()
        if not sel:
            QMessageBox.information(self, "Select designs",
                                    "Select the designs for the group in the library first "
                                    "(Ctrl/Cmd-click for several).")
            return
        name, ok = QInputDialog.getText(self, "New group", "Group name:")
        if ok and name.strip():
            g = self.lib_mgr.create_group(name.strip(),
                                          [it.data(0, Qt.ItemDataRole.UserRole) for it in sel])
            self._refresh_groups()
            # straight into the editor so the per-set quantities can be set
            self._manage_groups(g.group_id)

    def _add_group_to_job(self):
        if not self._ensure_job():
            return
        sel = self._grp_tree.selectedItems()
        if not sel:
            return
        gid = sel[0].data(0, Qt.ItemDataRole.UserRole)
        g = self.lib_mgr.groups.get(gid)
        if not g:
            return
        for it in self._queue_items:
            if it.group_id == gid:
                it.quantity += 1
                break
        else:
            self._queue_items.append(JobItem(design_id=f"group:{gid}", design_name=g.name,
                                             quantity=1, group_id=gid))
        self._expanded_groups.add(gid)
        self._refresh_queue()
        self._status(f"Added '{g.name}' — {g.pieces_per_set} pieces per set, "
                     f"each set stays on one platform")
        self._status_later()

    def _manage_groups(self, focus=None):
        GroupsDialog(self, focus).exec()

    # ── Jobs ──────────────────────────────────────────────────────────────────
    def _refresh_job_combo(self):
        self._job_combo.blockSignals(True)
        self._job_combo.clear()
        self._job_ids = []
        for j in self.job_mgr.all_jobs():
            self._job_combo.addItem(j.name)
            self._job_ids.append(j.job_id)
        if self.current_job_id in self._job_ids:
            self._job_combo.setCurrentIndex(self._job_ids.index(self.current_job_id))
        elif not self._job_ids:
            self._job_combo.addItem("No jobs yet — click +")
        self._job_combo.blockSignals(False)
        has = bool(self._job_ids)
        for b in (self._btn_dup_job, self._btn_ren_job, self._btn_del_job):
            b.setEnabled(has)

    def _ensure_job(self) -> bool:
        if self.current_job_id and self.current_job_id in self.job_mgr.jobs:
            return True
        j = self.job_mgr.create_job(f"Job {datetime.now():%Y-%m-%d %H%M}",
                                    self._pw_spin.value(), self._ph_spin.value(),
                                    self._mg_spin.value(), self._rot_combo.currentData())
        self.current_job_id = j.job_id
        self._save_job()
        self._refresh_job_combo()
        return True

    def _on_job_selected(self, idx: int):
        if 0 <= idx < len(getattr(self, "_job_ids", [])):
            if self.current_job_id and self.current_job_id != self._job_ids[idx]:
                self._save_job()
            self._load_job(self._job_ids[idx])

    def _load_job(self, job_id: Optional[str]):
        self._loading = True
        try:
            if not job_id or job_id not in self.job_mgr.jobs:
                self.current_job_id = None
                self._queue_items = []
                self._set_quality("quick")
                self._refresh_queue()
                self._update_overflow_enabled()
                return
            self.current_job_id = job_id
            self.cfg_mgr.cfg.last_job_id = job_id
            self.cfg_mgr.save()
            j = self.job_mgr.jobs[job_id]
            self._pw_spin.setValue(j.platform_w)
            self._ph_spin.setValue(j.platform_h)
            self._mg_spin.setValue(j.margin_mm)
            self._ec_spin.setValue(j.edge_clearance)
            idx = self._rot_combo.findData(j.rotation_mode)
            self._rot_combo.setCurrentIndex(idx if idx >= 0 else 1)
            self._ovf_check.setChecked(j.overflow_enabled)
            self._ox_spin.setValue(j.overflow_x)
            self._oy_spin.setValue(j.overflow_y)
            self._holes_check.setChecked(j.allow_holes)
            self._keep_groups_check.setChecked(j.keep_groups)
            self._set_quality(j.quality)
            self._queue_items = [_copy_item(it) for it in j.items]
            self._update_overflow_enabled()
            self._refresh_queue()
            self._clear_results("Run nesting to see the layout")
        finally:
            self._loading = False

    def _set_quality(self, key):
        b = self._quality_btns.get(key) or self._quality_btns["quick"]
        b.setChecked(True)

    def _quality(self) -> str:
        for k, b in self._quality_btns.items():
            if b.isChecked():
                return k
        return "quick"

    def _save_job(self):
        if self._loading or not self.current_job_id or \
                self.current_job_id not in self.job_mgr.jobs:
            return
        self.job_mgr.update_job(
            self.current_job_id,
            platform_w=self._pw_spin.value(), platform_h=self._ph_spin.value(),
            margin_mm=self._mg_spin.value(), rotation_mode=self._rot_combo.currentData(),
            edge_clearance=self._ec_spin.value(), overflow_enabled=self._ovf_check.isChecked(),
            overflow_x=self._ox_spin.value(), overflow_y=self._oy_spin.value(),
            allow_holes=self._holes_check.isChecked(), quality=self._quality(),
            keep_groups=self._keep_groups_check.isChecked())
        self.job_mgr.set_items(self.current_job_id,
                               [_copy_item(it) for it in self._queue_items])
        c = self.cfg_mgr.cfg
        c.last_platform_w = self._pw_spin.value()
        c.last_platform_h = self._ph_spin.value()
        c.last_margin_mm = self._mg_spin.value()
        c.last_rotation = self._rot_combo.currentData()
        c.last_keep_groups = self._keep_groups_check.isChecked()
        self.cfg_mgr.save()

    def _settings_changed(self, *_):
        self._update_overflow_enabled()
        if self._loading:
            return
        self._save_timer.start(400)
        self._schedule_estimate()

    def _update_overflow_enabled(self):
        self._ovf_row.setVisible(self._ovf_check.isChecked())

    def _new_job(self):
        name, ok = QInputDialog.getText(self, "New job", "Job name:",
                                        text=f"Job {datetime.now():%Y-%m-%d %H%M}")
        if not ok or not name.strip():
            return
        self._save_job()
        j = self.job_mgr.create_job(name.strip(), self._pw_spin.value(), self._ph_spin.value(),
                                    self._mg_spin.value(), self._rot_combo.currentData())
        j.keep_groups = self._keep_groups_check.isChecked()
        self.current_job_id = j.job_id
        self._queue_items = []
        self._save_job()
        self._refresh_job_combo()
        self._load_job(j.job_id)

    def _dup_job(self):
        src = self.job_mgr.jobs.get(self.current_job_id)
        if not src:
            return
        self._save_job()
        name, ok = QInputDialog.getText(self, "Duplicate job", "Name for the copy:",
                                        text=f"{src.name} (copy)")
        if not ok or not name.strip():
            return
        new_j = self.job_mgr.duplicate_job(self.current_job_id, name.strip())
        if new_j:
            self.current_job_id = new_j.job_id
            self._refresh_job_combo()
            self._load_job(new_j.job_id)

    def _rename_job(self):
        j = self.job_mgr.jobs.get(self.current_job_id)
        if not j:
            return
        name, ok = QInputDialog.getText(self, "Rename job", "Job name:", text=j.name)
        if ok and name.strip():
            self.job_mgr.update_job(j.job_id, name=name.strip())
            self._refresh_job_combo()

    def _del_job(self):
        j = self.job_mgr.jobs.get(self.current_job_id)
        if not j:
            return
        if QMessageBox.question(self, "Delete job", f"Delete the job '{j.name}'?"
                                ) != QMessageBox.StandardButton.Yes:
            return
        self.job_mgr.delete_job(j.job_id)
        self.current_job_id = None
        self._queue_items = []
        self._refresh_job_combo()
        if self._job_ids:
            self._load_job(self._job_ids[0])
        else:
            self._load_job(None)

    # ── Queue ─────────────────────────────────────────────────────────────────
    def _add_to_job(self):
        sel = self._lib_tree.selectedItems()
        if not sel:
            return
        self._ensure_job()
        for s in sel:
            did = s.data(0, Qt.ItemDataRole.UserRole)
            e = self.lib_mgr.designs.get(did)
            if e:
                self._enqueue(did, e.display_name)
        self._refresh_queue()

    def _enqueue(self, design_id: str, design_name: str, qty: int = 1):
        for it in self._queue_items:
            if not it.group_id and it.design_id == design_id:
                it.quantity += qty
                return
        self._queue_items.append(JobItem(design_id=design_id, design_name=design_name,
                                         quantity=qty))

    def _spin(self, value, lo, hi, on_change, tip="", special="", steppers=False):
        sp = SpinBox()
        sp.setRange(lo, hi)
        sp.setValue(value)
        sp.setButtonSymbols(SpinBox.ButtonSymbols.NoButtons)
        sp.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sp.setKeyboardTracking(False)
        if special:
            sp.setSpecialValueText(special)
        if tip:
            sp.setToolTip(tip)
        sp.valueChanged.connect(on_change)
        holder = QWidget()
        hl = QHBoxLayout(holder)
        hl.setContentsMargins(2, 2, 2, 2)
        hl.setSpacing(0)
        if steppers:
            sp.setMinimumWidth(40)
            for text, step in (("−", -1), ("+", 1)):
                b = QToolButton()
                b.setObjectName("stepper")
                b.setText(text)
                b.setCursor(Qt.CursorShape.PointingHandCursor)
                b.setAutoRepeat(True)
                b.setAutoRepeatDelay(400)
                b.setAutoRepeatInterval(90)
                b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                b.clicked.connect(lambda _, sp=sp, step=step: sp.setValue(sp.value() + step))
                if step < 0:
                    hl.addWidget(b)
                else:
                    hl.addWidget(sp, 1)
                    hl.addWidget(b)
        else:
            hl.addWidget(sp, 1)
        return holder

    def _queue_rows_moved(self, *_):
        """A row was dragged — take the new order from the tree."""
        t = self._q_tree
        by_id = {id(x): x for x in self._queue_items}
        order, seen = [], set()
        for i in range(t.topLevelItemCount()):
            it = by_id.get(t.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole))
            if it is not None and id(it) not in seen:
                seen.add(id(it))
                order.append(it)
        order += [x for x in self._queue_items if id(x) not in seen]
        self._queue_items = order
        self._save_timer.start(300)
        # dragging drops the row's widgets, so the rows are rebuilt once
        QTimer.singleShot(0, lambda: self._refresh_queue(schedule=False))

    def _remember_expanded(self, item, expanded):
        key = item.data(0, Qt.ItemDataRole.UserRole + 1)
        if key:
            if expanded:
                self._expanded_groups.add(key)
            else:
                self._expanded_groups.discard(key)

    def group_members(self, it: JobItem):
        """[(design_id, name, per-set qty)] for a group row, job override applied."""
        g = self.lib_mgr.groups.get(it.group_id)
        if not g:
            return []
        out = []
        for did, count in g.members():
            e = self.lib_mgr.designs.get(did)
            qty = it.member_qty.get(did, count)
            out.append((did, e.display_name if e else did, max(0, int(qty))))
        return out

    ROW_H = 36        # fixed row height — a stable, correct scrollbar

    def _refresh_queue(self, schedule=True):
        if not hasattr(self, "_q_tree"):
            return
        t = self._q_tree
        # remember where the user was before the rows are rebuilt
        scroll = t.verticalScrollBar().value()
        selected = {w.data(0, Qt.ItemDataRole.UserRole)
                    for w in t.selectedItems() if w.parent() is None}
        t.blockSignals(True)
        t.clear()
        self._row_for = {}
        items = list(self._queue_items)     # the order they were added / dragged into
        group_icon = make_icon("group", self.theme['accent'], 22)
        for it in items:
            row = QTreeWidgetItem()
            row.setData(0, Qt.ItemDataRole.UserRole, id(it))
            if it.group_id:
                g = self.lib_mgr.groups.get(it.group_id)
                members = self.group_members(it)
                per_set = sum(q for _, _, q in members)
                row.setText(0, f"{it.design_name}   ·  {per_set} per set")
                row.setIcon(0, group_icon)
                row.setData(0, Qt.ItemDataRole.UserRole + 1, it.group_id)
                row.setToolTip(0, ("Group — one set always lands on the same platform.\n"
                                   if self._keep_groups_check.isChecked() else
                                   "Group — used here only to add the designs; the pieces are "
                                   "nested wherever they fit best.\n")
                                  + "Qty = number of sets. Open the row to set how many of "
                                    "each design go in one set (for this job only).")
                if not g:
                    row.setForeground(0, QColor(self.theme['danger']))
                    row.setToolTip(0, "This group no longer exists in the library")
            else:
                row.setText(0, it.design_name)
                row.setIcon(0, self._thumb(it.design_id, 26))
                if not self.lib_mgr.designs.get(it.design_id):
                    row.setForeground(0, QColor(self.theme['danger']))
                    row.setToolTip(0, "Design not found in the library")
            row.setSizeHint(0, QSize(0, self.ROW_H))
            row.setFlags((row.flags() | Qt.ItemFlag.ItemIsDragEnabled)
                         & ~Qt.ItemFlag.ItemIsDropEnabled)
            t.addTopLevelItem(row)
            self._row_for[id(it)] = row
            if id(it) in selected:
                row.setSelected(True)
            t.setItemWidget(row, 1, self._spin(
                it.quantity, 1, 9999, lambda v, it=it: self._set_qty(it, v),
                "Number of sets" if it.group_id else "Number of pieces", steppers=True))
            t.setItemWidget(row, 2, self._spin(
                it.priority, 0, 99, lambda v, it=it: self._set_priority(it, v),
                "Higher priority is placed first (on the earliest platforms)", "—"))
            if it.group_id:
                for did, name, qty in self.group_members(it):
                    ch = QTreeWidgetItem()
                    ch.setSizeHint(0, QSize(0, self.ROW_H))
                    ch.setFlags(ch.flags() & ~(Qt.ItemFlag.ItemIsDragEnabled
                                               | Qt.ItemFlag.ItemIsDropEnabled))
                    ch.setData(0, Qt.ItemDataRole.UserRole + 2, did)
                    ch.setText(0, name)
                    ch.setIcon(0, self._thumb(did, 22))
                    ch.setForeground(0, QColor(self.theme['muted']))
                    ch.setText(2, f"= {qty * it.quantity}")
                    ch.setForeground(2, QColor(self.theme['faint']))
                    ch.setTextAlignment(2, Qt.AlignmentFlag.AlignCenter)
                    ch.setToolTip(2, f"{qty} per set × {it.quantity} sets "
                                     f"= {qty * it.quantity} pieces in this job")
                    ch.setToolTip(0, "How many of this design go in ONE set (this job only)")
                    row.addChild(ch)
                    t.setItemWidget(ch, 1, self._spin(
                        qty, 0, 999,
                        lambda v, it=it, did=did: self._set_member_qty(it, did, v),
                        "Per set — 0 leaves this design out of this job", "skip",
                        steppers=True))
                row.setExpanded(it.group_id in self._expanded_groups)
        t.blockSignals(False)
        # put the view back exactly where it was (adding a row never jumps)
        bar = t.verticalScrollBar()
        bar.setValue(min(scroll, bar.maximum()))
        QTimer.singleShot(0, lambda v=scroll: self._q_tree.verticalScrollBar().setValue(
            min(v, self._q_tree.verticalScrollBar().maximum())))
        self._update_q_summary()
        if schedule:
            self._save_timer.start(300)
            self._schedule_estimate()

    def _update_q_summary(self):
        if self._queue_items:
            hint = "  ·  drag rows to reorder" if len(self._queue_items) > 1 else ""
            self._q_summary.setText(f"{len(self._queue_items)} rows  ·  "
                                    f"{self._total_pieces()} pieces{hint}")
        else:
            self._q_summary.setText("Queue is empty — double-click designs in "
                                    "the library to add them")

    def _update_group_row(self, it: JobItem):
        """Refresh one group row's wording and totals — no rebuild, no jump."""
        row = getattr(self, "_row_for", {}).get(id(it))
        if row is None or not it.group_id:
            return
        members = self.group_members(it)
        row.setText(0, f"{it.design_name}   ·  {sum(q for _, _, q in members)} per set")
        per = {did: q for did, _, q in members}
        for i in range(row.childCount()):
            ch = row.child(i)
            qty = per.get(ch.data(0, Qt.ItemDataRole.UserRole + 2), 0)
            ch.setText(2, f"= {qty * it.quantity}")
            ch.setToolTip(2, f"{qty} per set × {it.quantity} sets "
                             f"= {qty * it.quantity} pieces in this job")

    def _set_member_qty(self, it: JobItem, design_id: str, value: int):
        g = self.lib_mgr.groups.get(it.group_id)
        default = dict(g.members()).get(design_id) if g else None
        if default is not None and value == default:
            it.member_qty.pop(design_id, None)
        else:
            it.member_qty[design_id] = value
        self._update_group_row(it)
        self._update_q_summary()
        self._save_timer.start(400)
        self._schedule_estimate()

    def _total_pieces(self) -> int:
        n = 0
        for it in self._queue_items:
            if it.group_id:
                n += it.quantity * sum(q for _, _, q in self.group_members(it))
            else:
                n += it.quantity
        return n

    def _set_qty(self, it, v):
        it.quantity = v
        self._update_group_row(it)
        self._update_q_summary()
        self._save_timer.start(400)
        self._schedule_estimate()

    def _set_priority(self, it, v):
        # priority steers the nesting order, not the list order — rows stay put
        it.priority = v
        self._save_timer.start(400)

    def _selected_queue_items(self):
        ids = set()
        for w in self._q_tree.selectedItems():
            top = w if w.parent() is None else w.parent()
            ids.add(top.data(0, Qt.ItemDataRole.UserRole))
        return [it for it in self._queue_items if id(it) in ids]

    def _remove_from_queue(self):
        sel = self._selected_queue_items()
        if not sel:
            return
        self._queue_items = [x for x in self._queue_items if x not in sel]
        self._refresh_queue()

    def _clear_queue(self):
        if not self._queue_items:
            return
        if QMessageBox.question(self, "Clear queue", "Remove every row from the queue?"
                                ) == QMessageBox.StandardButton.Yes:
            self._queue_items = []
            self._refresh_queue()

    # ── Estimate ──────────────────────────────────────────────────────────────
    def _schedule_estimate(self):
        self._estimate_timer.start(500)

    def _run_estimate(self):
        if not self._queue_items:
            self._est_lbl.setText("")
            return
        try:
            job = self._build_nest_job(estimate_only=True)
            if job is None:
                self._est_lbl.setText("")
                return
            est = estimate_platforms(job)
            self._est_lbl.setText(f"≈ {est} platform{'s' if est != 1 else ''} expected  ·  "
                                  f"{self._total_pieces()} pieces")
        except Exception as ex:
            log.debug("estimate failed: %s", ex)
            self._est_lbl.setText("")

    # ── Nesting ───────────────────────────────────────────────────────────────
    def _sym(self, design_id: str) -> int:
        e = self.lib_mgr.designs.get(design_id)
        return max(1, int(getattr(e, "sym_order", 1) or 1)) if e else 1

    def _build_nest_job(self, estimate_only=False):
        requests, groups, missing = [], [], []
        for it in self._queue_items:
            if it.group_id:
                g = self.lib_mgr.groups.get(it.group_id)
                if not g:
                    missing.append(it.design_name)
                    continue
                keep = self._keep_groups_check.isChecked()
                members = []
                for did, name, cnt in self.group_members(it):
                    if cnt <= 0:
                        continue
                    poly, holes = self.lib_mgr.get_geometry(did)
                    if poly is None:
                        missing.append(name)
                        continue
                    if keep:
                        members.append(NestRequest(did, name, poly, cnt, it.priority, holes,
                                                   self._sym(did)))
                    else:
                        # the group is only a shortcut: nest its pieces freely
                        requests.append(NestRequest(did, name, poly, cnt * it.quantity,
                                                    it.priority, holes, self._sym(did)))
                if members:
                    groups.append(GroupRequest(it.design_name, members, it.quantity, it.priority))
            else:
                poly, holes = self.lib_mgr.get_geometry(it.design_id)
                if poly is None:
                    missing.append(it.design_name)
                    continue
                requests.append(NestRequest(it.design_id, it.design_name, poly, it.quantity,
                                            it.priority, holes, self._sym(it.design_id)))
        if not requests and not groups:
            return None
        ox, oy = (self._ox_spin.value(), self._oy_spin.value()) if self._ovf_check.isChecked() \
            else (0.0, 0.0)
        job = NestingJob(
            requests=requests, groups=groups,
            platform_w=self._pw_spin.value(), platform_h=self._ph_spin.value(),
            margin_mm=self._mg_spin.value(),
            rotations=ROTATION_MODES[self._rot_combo.currentData()][1],
            edge_clearance=self._ec_spin.value(), overflow_x=ox, overflow_y=oy,
            allow_holes=self._holes_check.isChecked(),
            time_limit=QUALITY_MODES[self._quality()][1])
        job._missing = missing
        return job

    def _run_or_stop(self):
        if self._is_nesting():
            self._stop_nesting()
        else:
            self._run_nesting()

    def _stop_nesting(self):
        if self._is_nesting() and not self._nest_worker.stop_event.is_set():
            self._nest_worker.stop_event.set()
            self._search_stop.setEnabled(False)
            self._search_stop.setText("Stopping…")
            self._status_hint.setText("")
            self._run_btn.setEnabled(False)
            self._status("Stopping — keeping the best layout found so far…")

    @staticmethod
    def _restyle(w):
        w.style().unpolish(w)
        w.style().polish(w)

    def _pulse_tick(self):
        self._pulse_on = not self._pulse_on
        d = getattr(self, "_stats", {}) or {}
        dot = "●" if self._pulse_on else "○"
        self._search_chip.setText(
            f"{dot}  Searching · {d['layouts']} layouts · {d['improvements']} better"
            if d else f"{dot}  Packing…")

    @staticmethod
    def _fmt_time(sec):
        sec = int(max(0, sec))
        return f"{sec // 60}:{sec % 60:02d}"

    def _on_stats(self, d):
        """Live search figures: a chip on the viewer, the detail in the status bar."""
        try:
            self._stats = d
            n, el, limit = d["platforms"], d["elapsed"], d["limit"]
            since = d["since_improve"]

            self._status_stat.setText(
                f"{d['layouts']} layouts · {d['improvements']} better · "
                f"best {n} platform{'s' if n != 1 else ''}, {d['metal_pct']:.1f}% metal"
                + (f" · {d['unplaced']} don't fit" if d.get("unplaced") else ""))
            if math.isinf(limit):
                self._status_time.setText(f"{self._fmt_time(el)} · runs until you stop")
                if self._prog.maximum() != 0:
                    self._prog.setRange(0, 0)           # busy indicator
            else:
                left = max(0.0, limit - el)
                self._status_time.setText(f"{self._fmt_time(el)} / {self._fmt_time(limit)}"
                                          f" · ~{self._fmt_time(left)} left")
                if self._prog.maximum() != 100:
                    self._prog.setRange(0, 100)
                self._prog.setValue(int(100 * min(1.0, el / limit)) if limit > 0 else 100)
            self._status("Searching for a tighter layout — last improvement "
                         f"{self._fmt_time(since)} ago" if d["improvements"]
                         else "Searching for a tighter layout…")

            hint = ""
            if n <= d["lower_bound"] and since > 5:
                hint = "✓ fewest platforms possible — safe to stop"
            elif el > 10 and since > max(20.0, 0.5 * el):
                hint = f"✓ nothing better for {self._fmt_time(since)} — good time to stop"
            self._status_hint.setText(hint)
            if self._is_nesting() and not self._nest_worker.stop_event.is_set():
                self._run_btn.setText(f"Stop · keep best ({n} platform{'s' if n != 1 else ''})")
                self._search_stop.setText(f"Stop · keep best ({n})")
        except Exception:
            log.exception("stats update failed")

    def _run_nesting(self):
        if not self._queue_items:
            QMessageBox.information(self, "Queue is empty", "Add designs to the queue first.")
            return
        self._save_job()
        job = self._build_nest_job()
        if job is None:
            QMessageBox.warning(self, "No usable designs",
                                "None of the queued designs could be read.\n"
                                "Check the library folder and re-scan.")
            return
        if job._missing:
            self._status("Skipped (not in library): " + ", ".join(job._missing))

        self.results_meta = dict(overflow=(job.overflow_x, job.overflow_y), job=job,
                                 started=datetime.now())
        self._last_export = None
        self.nesting_results = []
        self._plat_idx = 0
        self._set_running(True)
        self._status("Preparing shapes…", 0)

        self._nest_worker = NestWorker(job)
        self._nest_worker.progress.connect(lambda c, t, m: self._status(m, int(c / max(t, 1) * 100)))
        self._nest_worker.best.connect(self._on_best)
        self._nest_worker.stats.connect(self._on_stats)
        self._nest_worker.done.connect(self._nesting_done)
        self._stats = {}
        self._search_chip.setObjectName("chip")
        self._restyle(self._search_chip)
        self._search_chip.setText("●  Packing…")
        self._search_chip.show()
        self._search_stop.show()
        self._search_stop.setEnabled(True)
        self._search_stop.setText("Stop · keep best")
        self._status_hint.setText("")
        self._status_stat.setText("")
        limit = QUALITY_MODES[self._quality()][1]
        self._status_time.setText("starting…" if math.isinf(limit)
                                  else f"0:00 / {self._fmt_time(limit)}")
        self._pulse.start(550)
        self._nest_worker.start()

    def _set_running(self, running: bool):
        t = self.theme
        self._plat_sub.setVisible(not running)   # the chip takes this space
        self._run_btn.setText("Stop · keep best" if running else "Run nesting")
        self._run_btn.setIcon(make_icon("stop" if running else "play", t['accent_text'], 16))
        for w in (self._export_btn, self._pw_spin, self._ph_spin, self._mg_spin, self._ec_spin,
                  self._rot_combo, self._ovf_check, self._ox_spin, self._oy_spin,
                  self._holes_check, self._keep_groups_check, self._job_combo):
            w.setEnabled(not running)
        for b in self._quality_btns.values():
            b.setEnabled(not running)

    def _on_best(self, results):
        # Improvements can arrive in bursts — redraw at most ~4× per second
        if not results:
            return
        self._pending_best = results
        if not getattr(self, "_best_timer_armed", False):
            self._best_timer_armed = True
            QTimer.singleShot(250, self._apply_pending_best)

    def _apply_pending_best(self):
        self._best_timer_armed = False
        res = getattr(self, "_pending_best", None)
        self._pending_best = None
        if res is not None and self._is_nesting():
            self._show_results(res, live=True)

    def _nesting_done(self, results):
        self._pending_best = None
        self._pulse.stop()
        self._set_running(False)
        self._run_btn.setEnabled(True)
        self._last_nest_results = results
        d = getattr(self, "_stats", {}) or {}
        self._prog.setRange(0, 100)
        self._status_hint.setText("")
        self._search_stop.hide()
        if isinstance(results, Exception) or not results:
            self._search_chip.hide()
            self._status_stat.setText("")
            self._status_time.setText("")
        else:
            stopped = self._nest_worker is not None and self._nest_worker.stop_event.is_set()
            tried = getattr(results, "layouts_tried", d.get("layouts", 0))
            self._search_chip.setObjectName("chipDone")
            self._restyle(self._search_chip)
            self._search_chip.setText(("Stopped · best kept" if stopped else "Search finished")
                                      + f" · {tried} layouts")
            self._search_chip.show()
            self._status_stat.setText(
                f"{tried} layouts · {d.get('improvements', 0)} better · "
                f"{len(results)} platform{'s' if len(results) != 1 else ''}, "
                f"{self._metal_pct(results):.1f}% metal")
            self._status_time.setText(f"took {self._fmt_time(getattr(results, 'seconds', 0))}")
        if isinstance(results, Exception):
            self._status(f"Nesting failed: {results}", 0)
            QMessageBox.critical(self, "Nesting failed", str(results))
            return
        if not results:
            unpl = getattr(results, "unplaced", [])
            if unpl:
                self._show_unplaced(unpl)
                self._clear_results("Nothing fits on this platform size")
            else:
                self._clear_results("Nesting produced no layout")
            self._status("No layout — check the platform size", 0)
            return
        self._show_results(results, live=False)
        unpl = getattr(results, "unplaced", [])
        pieces = sum(r.piece_count for r in results)
        msg = (f"Done — {len(results)} platform{'s' if len(results) != 1 else ''}, "
               f"{pieces} pieces placed")
        if unpl:
            msg += f"  ·  {len(unpl)} did not fit"
        self._status(msg, 100)
        self._status_later(ms=8000)

    def _metal_pct(self, results):
        if not results:
            return 0.0
        area = results[0].platform_w * results[0].platform_h * len(results)
        return 100.0 * sum(r.used_area for r in results) / area if area else 0.0

    def _show_results(self, results, live=False):
        self.nesting_results = list(results)
        self._colour_map = colour_map_for(results)
        self._plat_idx = min(self._plat_idx, len(results) - 1)
        self._k_plat.setText(str(len(results)))
        self._k_pieces.setText(str(sum(r.piece_count for r in results)))
        self._k_metal.setText(f"{self._metal_pct(results):.1f}%")
        self._show_unplaced(getattr(results, "unplaced", []) if not live else [])
        self._refresh_strip()
        self._draw_viewer(keep_view=True)

    def _show_unplaced(self, unpl):
        if unpl:
            names = ", ".join(f"{n} ×{unpl.count(n)}" for n in sorted(set(unpl)))
            self._unplaced_lbl.setText(f"Did not fit on the platform: {names}")
            self._unplaced_lbl.show()
        else:
            self._unplaced_lbl.hide()

    def _clear_results(self, message=""):
        self.nesting_results = []
        if hasattr(self, "_search_chip") and not self._is_nesting():
            self._search_chip.hide()
            self._search_stop.hide()
        self._legend.setText("")
        self._canvas.clear(message)
        self._strip.clear()
        self._plat_lbl.setText("No results yet")
        self._plat_sub.setText("")
        for k in (self._k_plat, self._k_pieces, self._k_metal, self._k_this):
            k.setText("—")
        self._unplaced_lbl.hide()

    # ── Viewer ────────────────────────────────────────────────────────────────
    def _refresh_strip(self):
        if not hasattr(self, "_strip"):
            return
        self._strip.blockSignals(True)
        self._strip.clear()
        ovf = self.results_meta.get("overflow", (0.0, 0.0))
        for r in self.nesting_results:
            pm = render_thumb(r, self._colour_map, self.theme, self.theme_name == "dark", ovf)
            icon = QIcon()
            icon.addPixmap(pm, QIcon.Mode.Normal)
            icon.addPixmap(pm, QIcon.Mode.Selected)
            it = QListWidgetItem(icon, f"{r.platform_index + 1}  ·  {r.efficiency:.0f}%")
            it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            it.setSizeHint(QSize(162, 108))
            self._strip.addItem(it)
        if self.nesting_results:
            self._strip.setCurrentRow(self._plat_idx)
        self._strip.blockSignals(False)
        self._strip.setVisible(len(self.nesting_results) > 1)

    def _on_strip(self, row):
        if 0 <= row < len(self.nesting_results):
            self._plat_idx = row
            self._draw_viewer(keep_view=True)

    def _prev_plat(self):
        if self.nesting_results and self._plat_idx > 0:
            self._plat_idx -= 1
            self._strip.setCurrentRow(self._plat_idx)

    def _next_plat(self):
        if self.nesting_results and self._plat_idx < len(self.nesting_results) - 1:
            self._plat_idx += 1
            self._strip.setCurrentRow(self._plat_idx)

    def _draw_viewer(self, keep_view=True):
        if not self.nesting_results:
            self._canvas.clear()
            return
        idx = min(self._plat_idx, len(self.nesting_results) - 1)
        r = self.nesting_results[idx]
        n = len(self.nesting_results)
        self._plat_lbl.setText(f"Platform {idx + 1} of {n}")
        sub = [f"{r.piece_count} pieces", f"{r.platform_w:g} × {r.platform_h:g} mm"]
        if r.overflow_x > 0.001 or r.overflow_y > 0.001:
            sub.append(f"overflow ↔ {r.overflow_x:.2f}  ↕ {r.overflow_y:.2f} mm")
        self._plat_sub.setText("   ·   ".join(sub))
        self._k_this.setText(f"{r.efficiency:.1f}%")
        counts: Dict[str, list] = {}
        for it in r.placements:
            counts.setdefault(it.design_id, [it.design_name, 0])[1] += 1
        chips = []
        dark = self.theme_name == "dark"
        for did, (name, cnt) in counts.items():
            fill, stroke = design_colours(self.theme, self._colour_map.get(did, ("#EEE", "#888")), dark)
            chips.append(f'<span style="color:{stroke.name()}; font-size:15px;">&#9632;</span>'
                         f'&nbsp;{name}&nbsp;<b>×{cnt}</b>')
        self._legend.setText("&nbsp;&nbsp;&nbsp; ".join(chips))
        self._btn_prev.setEnabled(idx > 0)
        self._btn_next.setEnabled(idx < n - 1)
        self._canvas.set_result(r, self._colour_map, self.results_meta.get("overflow", (0, 0)),
                                keep_view=keep_view)

    # ── Output / export ──────────────────────────────────────────────────────
    def _out_edited(self):
        self.cfg_mgr.cfg.output_folder = native_path(self._out_edit.text()) \
            if self._out_edit.text().strip() else ""
        self.cfg_mgr.save()

    def _report_edited(self):
        self.cfg_mgr.cfg.report_folder = native_path(self._xlsx_edit.text()) \
            if self._xlsx_edit.text().strip() else ""
        self.cfg_mgr.save()

    def _browse_output(self):
        d = QFileDialog.getExistingDirectory(self, "Choose DXF output folder",
                                             self._out_edit.text() or QDir.homePath())
        if d:
            self._out_edit.setText(native_path(d))
            self._out_edited()

    def _browse_xlsx(self):
        d = QFileDialog.getExistingDirectory(self, "Choose report folder",
                                             self._xlsx_edit.text() or QDir.homePath())
        if d:
            self._xlsx_edit.setText(native_path(d))
            self._report_edited()

    def _job_file_stem(self, job) -> str:
        name = job.name if job else "job"
        for ch in r'\/:*?"<>| ':
            name = name.replace(ch, "_")
        return name

    def _export_context(self):
        """Everything an export needs, or None when it cannot run yet."""
        if self._is_nesting():
            return None
        if not self.nesting_results:
            QMessageBox.information(self, "Nothing to export", "Run nesting first.")
            return None
        if not self._out_edit.text().strip():
            d = QFileDialog.getExistingDirectory(self, "Choose output folder", QDir.homePath())
            if not d:
                return None
            self._out_edit.setText(native_path(d))
            self._out_edited()
        base = native_path(self._out_edit.text())
        self._out_edit.setText(base)
        self._save_job()
        job = self.job_mgr.jobs.get(self.current_job_id)
        src_map = {}
        for r in self.nesting_results:
            for it in r.placements:
                e = self.lib_mgr.designs.get(it.design_id)
                if e:
                    src_map[it.design_id] = e.filename
        return {
            "folders": output_folders(base), "job": job,
            "stem": self._job_file_stem(job), "src_map": src_map,
            "results": list(self.nesting_results),
            "overflow": self.results_meta.get("overflow", (0.0, 0.0)),
            "unplaced": list(getattr(self._last_nest_results, "unplaced", [])),
            "queue": [_copy_item(it) for it in self._queue_items],
            "run_seconds": getattr(self._last_nest_results, "seconds", 0.0),
            "report_dir": native_path(self._xlsx_edit.text().strip())
                          if self._xlsx_edit.text().strip() else "",
        }

    def _export(self):
        """Rhino DXF, the PRISMA / Solanki set, and the reports — in one go."""
        ctx = self._export_context()
        if ctx is None:
            return
        ox, oy = ctx["overflow"]
        lib, jm = self.lib_mgr, self.job_mgr
        job, results = ctx["job"], ctx["results"]
        unplaced, queue = ctx["unplaced"], ctx["queue"]
        report_dir, run_seconds = ctx["report_dir"], ctx["run_seconds"]
        rhino_dir, cam_dir = ctx["folders"]["rhino"], ctx["folders"]["cam"]
        root, stem, src_map = ctx["folders"]["root"], ctx["stem"], ctx["src_map"]

        def work():
            written = export_job(results, src_map, rhino_dir, stem,
                                 overflow_x=ox, overflow_y=oy)
            out = {"written": written, "out_dir": rhino_dir, "root": root,
                   "stem": stem, "report": None, "cam_files": [], "check": {}}
            if not written:
                return out
            try:
                cam = export_cam(written[0], cam_dir, stem)
                out["cam_files"] = cam["files"]
                out["check"] = cam.get("check", {})
            except Exception as ex:
                # the Rhino DXF is already safe on disk — say so and carry on
                log.exception("CAM export failed")
                out["cam_error"] = str(ex)
            if job:
                jm.append_log(job, results, written + cam["files"], len(unplaced))
                if report_dir:
                    out["report"] = write_report(report_dir, job, results, queue, lib,
                                                 written, rhino_dir, unplaced, run_seconds)
            return out

        self._run_export(work, self._export_btn, "Export",
                         "Writing DXF, R12 and SVG…")

    def _run_export(self, work, btn, label, message):
        btn.setText("Exporting…")
        self._export_btn.setEnabled(False)
        self._status(message, 30)
        self._export_worker = ExportWorker(work)
        self._export_worker.done.connect(
            lambda out, b=btn, l=label: self._export_done(out, b, l))
        self._export_worker.start()

    def _export_done(self, out: dict, btn=None, label=None):
        if btn is not None:
            btn.setText(label)
        self._export_btn.setEnabled(True)
        if out.get("error") or not out.get("written"):
            self._status("Export failed", 0)
            QMessageBox.critical(self, "Export failed",
                                 out.get("error") or "No files were written. Check the output folder.")
            return
        self._last_export = out
        cam = out.get("cam_files") or []
        chk = out.get("check") or {}
        msg = f"Exported {out['stem']} — Rhino DXF"
        if cam:
            msg += f" + {len(cam)} PRISMA / Solanki file{'s' if len(cam) != 1 else ''}"
        elif out.get("cam_error"):
            msg += "  ·  PRISMA / Solanki files FAILED"
        rep = out.get("report")
        if rep:
            ok, rmsg, _ = rep
            msg += "  ·  report updated" if ok else f"  ·  {rmsg}"
            if not ok:
                QMessageBox.warning(self, "Report not saved", rmsg)
        if chk.get("points"):
            msg += f"  ·  CAM shapes within {chk['worst_mm'] * 1000:.0f} µm"
            if chk.get("over_tol"):
                msg += f" ({chk['over_tol']} points off)"
        msg += f"  ·  {out['root']}"
        self._status(msg, 100)
        self._status_later(ms=10000)
        if out.get("cam_error"):
            QMessageBox.warning(self, "PRISMA / Solanki files not written",
                                f"The Rhino DXF and the reports are saved.\n\n"
                                f"The CAM conversion failed:\n{out['cam_error']}\n\n"
                                f"Details are in lazynest_data/lazynest.log")

    def _export_paths(self):
        """(the job's Rhino DXF or None, the day's folder) as exported today."""
        base = self._out_edit.text().strip()
        if not base:
            return None, ""
        folders = output_folders(native_path(base))
        stem = self._job_file_stem(self.job_mgr.jobs.get(self.current_job_id))
        dxf = os.path.join(folders["rhino"], combined_filename(stem))
        return (dxf if os.path.isfile(dxf) else None), folders["root"]

    def _open_in_rhino(self):
        dxf, _ = self._export_paths()
        if not dxf:
            self._status("Export the DXF first, then open it in Rhino")
            self._status_later()
            return
        if self.cfg_mgr.open_in_rhino([dxf]):
            self._status(f"Opened {os.path.basename(dxf)} in Rhino", 100)
        else:
            self._status("Rhino not found — opened with the default app instead", 0)
        self._status_later()

    def _open_folder(self):
        _, root = self._export_paths()
        folder = root or (self._last_export or {}).get("root") or self._out_edit.text().strip()
        if not folder:
            self._status("Choose an output folder first")
            self._status_later()
            return
        folder = native_path(folder)
        if not os.path.isdir(folder):
            QMessageBox.information(self, "Folder not found",
                                    f"This folder does not exist yet:\n{folder}\n\n"
                                    "It is created when you export.")
            return
        self.cfg_mgr.open_folder(folder)

    def _open_report(self):
        d = self._xlsx_edit.text().strip()
        if d:
            d = native_path(d)
            for path in (daily_report_path(d), master_report_path(d),
                         os.path.join(d, REPORT_FILENAME)):
                if os.path.isfile(path):
                    self.cfg_mgr._open_with_default(path)
                    return
        QMessageBox.information(self, "No report yet",
                                "Set a report folder and export once — the day's Excel "
                                "report and the cumulative one are created automatically.")

    # ── Window state ──────────────────────────────────────────────────────────
    def _restore_geometry(self):
        s = QSettings("LazyNest", "LazyNest2")
        geom = s.value("geometry")
        if geom:
            self.restoreGeometry(geom)
        else:
            self.resize(1440, 880)
        state = s.value("splitter")
        if state:
            self._main_split.restoreState(state)

    def closeEvent(self, event):
        if self._is_nesting():
            self._nest_worker.stop_event.set()
            self._nest_worker.wait(15000)
        if self._export_worker and self._export_worker.isRunning():
            self._export_worker.wait(30000)
        if self._scan_worker and self._scan_worker.isRunning():
            self._scan_worker.wait(30000)
        self._save_job()
        s = QSettings("LazyNest", "LazyNest2")
        s.setValue("geometry", self.saveGeometry())
        s.setValue("splitter", self._main_split.saveState())
        event.accept()


# ── Entry point ───────────────────────────────────────────────────────────────
def _install_crash_guard():
    """
    PyQt6 aborts the whole app on any uncaught exception inside a Qt slot.
    Log it (lazynest_data/lazynest.log) and keep running instead.
    """
    def hook(exc_type, exc, tb):
        logging.getLogger("LazyNest").error("Unhandled error", exc_info=(exc_type, exc, tb))
        try:
            w = QApplication.activeWindow()
            if isinstance(w, LazyNestApp):
                w._status(f"Something went wrong ({exc_type.__name__}: {exc}) — "
                          f"details in lazynest.log", 0)
        except Exception:
            pass
    sys.excepthook = hook
    threading.excepthook = lambda a: hook(a.exc_type, a.exc_value, a.exc_traceback)


def main():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    handlers = [logging.StreamHandler()]
    try:
        from logging.handlers import RotatingFileHandler
        handlers.append(RotatingFileHandler(os.path.join(CONFIG_DIR, "lazynest.log"),
                                            maxBytes=1_000_000, backupCount=2, encoding="utf-8"))
    except Exception:
        pass
    logging.basicConfig(level=logging.WARNING, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    _install_crash_guard()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("LazyNest")
    app.setStyle("Fusion")
    f = app.font()
    f.setPointSize(13 if sys.platform == "darwin" else 10)
    app.setFont(f)
    icon_path = os.path.join(RES_DIR, "icon.png")
    if os.path.isfile(icon_path):
        app.setWindowIcon(QIcon(icon_path))
    window = LazyNestApp()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
