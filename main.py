import sys
import os
import string
import ctypes
from bisect import bisect_left
from collections import defaultdict

from PySide6.QtWidgets import (QApplication, QAbstractButton, QMainWindow, QWidget,
                               QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QStackedWidget,
                               QTableView, QHeaderView, QProgressBar, QFrame,
                               QComboBox, QSplitter, QAbstractItemView, QMessageBox,
                               QStyledItemDelegate, QStyle, QDialog, QStyleOptionViewItem)
from PySide6.QtCore import (Qt, QTimer, QEvent, QRectF, QPointF, QUrl,
                            QPersistentModelIndex)
from PySide6.QtGui import (QFont, QPainter, QColor, QPen, QPolygonF, QIcon,
                           QFontMetricsF, QKeySequence, QPalette)

from results_model import ResultsModel, ReportTableModel, format_size
from scanner import ScanWorker, CleanWorker

# 字体：中文用宋体、英文/数字用 Times New Roman（Qt 按字形自动回退到后续家族）
FONT_FAMILIES = ["Times New Roman", "宋体"]
APP_ICON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "TuBiao", "LJT_256.png")


def app_font(point_size=10, bold=False):
    f = QFont()
    f.setFamilies(FONT_FAMILIES)
    f.setPointSize(point_size)
    f.setBold(bold)
    return f


# QSS 里 QTableView::item 的 11pt 不会反映到 delegate 的 option.font（实测为 10pt），
# 绘制与命中测试统一显式使用这个字体，保证文字位置和鼠标位置一致。
TABLE_ITEM_FONT = app_font(11)


def trash_icon_html(size=16):
    """返回透明垃圾桶图标的 HTML <img>，用于在 QLabel 富文本中内联显示。"""
    url = QUrl.fromLocalFile(APP_ICON).toString()
    return f'<img src="{url}" width="{size}" height="{size}" style="vertical-align: middle;">'


STYLESHEET = """
* { font-family: "Times New Roman", "宋体"; }
QMainWindow { background-color: #F5F9FF; }
#nav_frame { background-color: #E3F2FD; border-top-left-radius: 10px; border-bottom-left-radius: 10px; }
#nav_title { color: #0D47A1; font-size: 14pt; font-weight: bold; }
#nav_btn { text-align: left; padding: 12px 20px; border: none; border-radius: 8px; color: #1565C0; font-size: 13pt; font-weight: bold; }
#nav_btn:hover { background-color: #BBDEFB; }
#nav_btn:checked { background-color: #1976D2; color: white; }
#content_frame { background-color: white; border-radius: 10px; padding: 20px; }
#page_title { color: #0D47A1; font-size: 16pt; font-weight: bold; }
#section_card { background-color: #FBFCFE; border: 1px solid #E3E8F0; border-radius: 8px; }
#count_label { color: #0D47A1; font-size: 13pt; font-weight: bold; }
QTableView { border: 1px solid #E0E0E0; border-radius: 8px; gridline-color: #F0F0F0; selection-background-color: #BBDEFB; selection-color: #0D47A1; background-color: white; }
QTableView::item { padding: 6px; font-size: 11pt; }
QHeaderView::section { background-color: #E3F2FD; color: #1565C0; padding: 8px; border: none; font-weight: bold; font-size: 11pt; }
QPushButton#action_btn { background-color: #1976D2; color: white; border: none; border-radius: 6px; padding: 10px 24px; font-size: 13pt; font-weight: bold; }
QPushButton#action_btn:hover { background-color: #1565C0; }
QPushButton#action_btn:disabled { background-color: #B0BEC5; }
QPushButton#scan_cat_btn { background-color: #E3F2FD; color: #1565C0; border: 1px solid #BBDEFB; border-radius: 5px; padding: 3px 10px; font-size: 10pt; font-weight: bold; }
QPushButton#scan_cat_btn:hover { background-color: #BBDEFB; border-color: #1976D2; }
QPushButton#scan_cat_btn:pressed { background-color: #90CAF9; }
QPushButton#scan_cat_btn:disabled { background-color: #ECEFF1; color: #B0BEC5; border-color: #E0E0E0; }
QProgressBar {
    border: none; border-radius: 6px; background-color: #CFD8DC;
    text-align: center; color: #000000;
    min-height: 20px; max-height: 20px;
    font-family: "Times New Roman";
    font-size: 11pt; font-weight: bold;
}
QProgressBar::chunk { background-color: #1976D2; border-radius: 6px; }
QComboBox { border: 1px solid #BBDEFB; border-radius: 6px; padding: 8px 12px; min-width: 200px; font-size: 12pt; }
QComboBox:hover { border: 1px solid #1976D2; }
QComboBox::drop-down { border: none; width: 20px; }
#placeholder_title { color: #90A4AE; font-size: 18pt; font-weight: bold; }
#placeholder_sub { color: #B0BEC5; font-size: 12pt; }
#about_title { color: #0D47A1; font-size: 18pt; font-weight: bold; }
#about_sub { color: #546E7A; font-size: 12pt; }
#about_body { color: #37474F; font-size: 11pt; }
QDialog#clean_report_dialog { background-color: #F5F9FF; }
#report_title { color: #0D47A1; font-size: 17pt; font-weight: bold; }
#report_section_title { color: #0D47A1; font-size: 12pt; font-weight: bold; }
#report_hint { color: #78909C; font-size: 10pt; }
"""


def draw_checkbox(painter, box, checked):
    """绘制 16×16 勾选框：选中为蓝底白勾，未选中为白底灰边。"""
    if checked:
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#1976D2"))
        painter.drawRoundedRect(box, 3.0, 3.0)
        pen = QPen(QColor("white"), 2.2)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        poly = QPolygonF([
            QPointF(box.left() + 3.4, box.top() + 8.4),
            QPointF(box.left() + 6.8, box.top() + 11.6),
            QPointF(box.left() + 12.6, box.top() + 4.6),
        ])
        painter.drawPolyline(poly)
    else:
        painter.setPen(QPen(QColor("#B0BEC5"), 1.0))
        painter.setBrush(QColor("white"))
        painter.drawRoundedRect(box, 3.0, 3.0)


class CheckDelegate(QStyledItemDelegate):
    """「选择」列：自定义绘制勾选框（蓝色填充 + 白色对勾），点击整格切换状态。

    不走 QSS 的 ::indicator 或原生指示器，避免 Windows 原生样式叠加出第二个
    勾选框、以及命中区域错位的问题。
    """

    BOX = 16.0  # 勾选框边长

    def paint(self, painter, option, index):
        if index.column() != 0:
            super().paint(painter, option, index)
            return

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)

        bg = QColor("#BBDEFB") if (option.state & QStyle.State_Selected) else QColor("white")
        painter.fillRect(option.rect, bg)

        box = QRectF(option.rect.center().x() - self.BOX / 2.0,
                     option.rect.center().y() - self.BOX / 2.0,
                     self.BOX, self.BOX)
        draw_checkbox(painter, box, index.data(Qt.CheckStateRole) == Qt.Checked)

        painter.restore()

    def editorEvent(self, event, model, option, index):
        if (index.isValid() and index.column() == 0
                and event.type() == QEvent.MouseButtonRelease
                and event.button() == Qt.LeftButton):
            checked = index.data(Qt.CheckStateRole) == Qt.Checked
            model.setData(index, Qt.Unchecked if checked else Qt.Checked, Qt.CheckStateRole)
            return True
        return super().editorEvent(event, model, option, index)


class PathSelectDelegate(QStyledItemDelegate):
    """路径列：按住左键拖动可框选文字，Ctrl+C 复制选中内容，双击选中整条路径。

    视图不会把 MouseMove 转给 delegate.editorEvent，所以这里把事件过滤器装在
    viewport 上。选中范围始终以**原始完整路径**的字符坐标保存，列宽变化导致
    省略位置改变时，选中的内容不会漂移。
    """

    HIGHLIGHT = QColor("#64B5F6")

    def __init__(self, table, column):
        super().__init__(table)
        self.table = table
        self.column = column
        self._anchor = None          # QPersistentModelIndex
        self._path_snapshot = ""
        self._sel_start = 0
        self._sel_end = 0
        self._dragging = False
        self._drag_geom = None       # 拖拽期间复用的几何信息，避免每帧重复测量

        viewport = table.viewport()
        viewport.setMouseTracking(True)  # 默认关闭，否则不会出现 I 形光标
        viewport.installEventFilter(self)
        table.installEventFilter(self)

        model = table.model()
        if model is not None:
            model.layoutChanged.connect(self._on_layout_changed)
            model.modelReset.connect(self._clear_selection)
            model.rowsRemoved.connect(self._clear_selection)

    # ---- 事件处理 ----
    def eventFilter(self, watched, event):
        try:
            if watched is self.table.viewport():
                return self._viewport_event(event)
            if watched is self.table and event.type() == QEvent.KeyPress:
                if event.matches(QKeySequence.StandardKey.Copy):
                    text = self._selection_text()
                    if text:
                        QApplication.clipboard().setText(text)
                        return True
                elif event.key() == Qt.Key_Escape and self._has_selection():
                    self._clear_selection()
                    return True
        except RuntimeError:
            # 控件析构过程中信号/事件仍可能到达
            return False
        return False

    def _viewport_event(self, event):
        et = event.type()

        if et in (QEvent.MouseButtonPress, QEvent.MouseButtonDblClick):
            if event.button() != Qt.LeftButton:
                return False
            index = self.table.indexAt(event.position().toPoint())
            if index.isValid() and index.column() == self.column:
                # 消费 press 后视图不会自己获取焦点，这里手动设置
                self.table.setFocus(Qt.MouseFocusReason)
                self._begin_selection(index, event.position().x(),
                                      select_all=(et == QEvent.MouseButtonDblClick))
                return True
            self._clear_selection()
            return False

        if et == QEvent.MouseMove:
            if (event.buttons() & Qt.LeftButton) and self._dragging:
                self._update_selection(event.position().x())
                return True
            # 悬停：路径列显示 I 形光标
            index = self.table.indexAt(event.position().toPoint())
            if index.isValid() and index.column() == self.column:
                self.table.viewport().setCursor(Qt.IBeamCursor)
            else:
                self.table.viewport().unsetCursor()
            return False

        if et == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
            if self._dragging:
                self._dragging = False
                self._drag_geom = None
                self.table.viewport().update()
                return True
            return False

        if et == QEvent.Leave:
            if not self._dragging:
                self.table.viewport().unsetCursor()
            return False
        return False

    # ---- 选中状态 ----
    def _begin_selection(self, index, x, select_all=False):
        path = str(index.data(Qt.DisplayRole) or "")
        geom = self._geometry(index)
        _, fm, disp, x0, _, _, pieces = geom
        bounds = [x0 + fm.horizontalAdvance(disp[:i]) for i in range(len(disp) + 1)]

        self._anchor = QPersistentModelIndex(index)
        self._path_snapshot = path
        self._drag_geom = geom + (bounds,)
        self._dragging = True

        if select_all:
            self._sel_start, self._sel_end = 0, len(path)
        else:
            d = self._hit_boundary(x, bounds)
            pos = self._disp_to_orig(d, path, *pieces)
            self._sel_start = self._sel_end = pos
        self.table.viewport().update()

    def _update_selection(self, x):
        if (self._drag_geom is None or self._anchor is None
                or not self._anchor.isValid()
                or str(self._anchor.data(Qt.DisplayRole) or "") != self._path_snapshot):
            self._clear_selection()
            return
        bounds = self._drag_geom[-1]
        pieces = self._drag_geom[6]
        d = self._hit_boundary(x, bounds)
        self._sel_end = self._disp_to_orig(d, self._path_snapshot, *pieces)
        self.table.viewport().update()

    def _clear_selection(self, *args):
        self._anchor = None
        self._path_snapshot = ""
        self._sel_start = self._sel_end = 0
        self._dragging = False
        self._drag_geom = None
        try:
            self.table.viewport().update()
        except RuntimeError:
            pass

    def _on_layout_changed(self, *args):
        # 排序会重映射持久索引；若目标位置已经不是同一条路径，则放弃选中
        if self._anchor is None:
            return
        try:
            if (not self._anchor.isValid()
                    or str(self._anchor.data(Qt.DisplayRole) or "") != self._path_snapshot):
                self._clear_selection()
            else:
                self.table.viewport().update()
        except RuntimeError:
            pass

    def _has_selection(self):
        return self._sel_start != self._sel_end

    def _is_anchor(self, index):
        if (self._anchor is None or not self._anchor.isValid()
                or not self._has_selection()):
            return False
        return (index.model() is self._anchor.model()
                and index.row() == self._anchor.row()
                and index.column() == self._anchor.column()
                and str(index.data(Qt.DisplayRole) or "") == self._path_snapshot)

    def _selection_text(self):
        if (self._anchor is None or not self._anchor.isValid()
                or str(self._anchor.data(Qt.DisplayRole) or "") != self._path_snapshot):
            return ""
        start, end = sorted((self._sel_start, self._sel_end))
        return self._path_snapshot[start:end]

    # ---- 几何与映射 ----
    def _option(self, index):
        opt = QStyleOptionViewItem()
        opt.rect = self.table.visualRect(index)
        opt.font = TABLE_ITEM_FONT
        opt.displayAlignment = Qt.AlignLeft | Qt.AlignVCenter
        opt.textElideMode = self.table.textElideMode()
        opt.widget = self.table
        opt.state = QStyle.State_Enabled
        selection = self.table.selectionModel()
        if selection is not None and selection.isSelected(index):
            opt.state |= QStyle.State_Selected
        self.initStyleOption(opt, index)
        return opt

    def _geometry(self, index):
        """返回 (text_rect, font_metrics, displayed_text, x0, top, baseline, pieces)。"""
        opt = self._option(index)
        style = self.table.style()
        text_rect = style.subElementRect(QStyle.SE_ItemViewItemText, opt, self.table)
        fm = QFontMetricsF(TABLE_ITEM_FONT, self.table)
        disp = fm.elidedText(opt.text, opt.textElideMode, text_rect.width())
        total = fm.horizontalAdvance(disp)

        align = opt.displayAlignment
        if align & Qt.AlignRight:
            x0 = text_rect.x() + text_rect.width() - total
        elif align & Qt.AlignHCenter:
            x0 = text_rect.x() + (text_rect.width() - total) / 2
        else:
            x0 = text_rect.x()
        top = text_rect.y() + (text_rect.height() - fm.height()) / 2
        baseline = top + fm.ascent()
        pieces = self._split_elided(opt.text, disp)
        return text_rect, fm, disp, x0, top, baseline, pieces

    @staticmethod
    def _hit_boundary(x, bounds):
        if not bounds:
            return 0
        x = min(max(x, bounds[0]), bounds[-1])
        i = bisect_left(bounds, x)
        if i <= 0:
            return 0
        if i >= len(bounds):
            return len(bounds) - 1
        return i if (bounds[i] - x) < (x - bounds[i - 1]) else i - 1

    @staticmethod
    def _split_elided(orig, disp):
        """拆分省略显示串为 (k, left, right)；无省略时 k=-1。

        Qt 使用 U+2026（…）。路径本身也可能含该字符，因此逐个候选位置
        验证 left 是原文前缀、right 是原文后缀。
        """
        if disp == orig:
            return -1, orig, ""
        for k, ch in enumerate(disp):
            if ch != "…":
                continue
            left, right = disp[:k], disp[k + 1:]
            if orig.startswith(left) and orig.endswith(right):
                return k, left, right
        return -1, orig, ""

    @staticmethod
    def _disp_to_orig(d, orig, k, left, right):
        """显示串字符边界 → 原始路径字符边界。"""
        if k < 0:
            return max(0, min(d, len(orig)))
        disp_len = k + 1 + len(right)
        if d <= k:
            return d
        if d == k + 1:
            return len(orig) - len(right)
        return len(orig) - (disp_len - d)

    @staticmethod
    def _orig_to_disp_intervals(start, end, orig, k, left, right):
        """原始选中范围 → 需要在显示串上高亮的区间列表。"""
        if start > end:
            start, end = end, start
        if k < 0:
            return [(start, end)] if end > start else []

        left_len = len(left)
        gap_start = left_len
        gap_end = len(orig) - len(right)
        intervals = []

        a, b = max(start, 0), min(end, left_len)
        if b > a:
            intervals.append((a, b))

        # 选中范围跨过隐藏中段时，把省略号本身也高亮
        if end > gap_start and start < gap_end:
            intervals.append((k, k + 1))

        a = k + 1 + max(start, gap_end) - gap_end
        b = k + 1 + min(end, len(orig)) - gap_end
        if b > a:
            intervals.append((a, b))
        return intervals

    # ---- 绘制 ----
    def paint(self, painter, option, index):
        if index.column() != self.column:
            super().paint(painter, option, index)
            return

        opt = QStyleOptionViewItem(option)
        opt.font = TABLE_ITEM_FONT
        opt.displayAlignment = Qt.AlignLeft | Qt.AlignVCenter
        opt.textElideMode = self.table.textElideMode()
        self.initStyleOption(opt, index)

        style = opt.widget.style() if opt.widget is not None else QApplication.style()
        text_rect = style.subElementRect(QStyle.SE_ItemViewItemText, opt, opt.widget)

        # 背景/选中底色/焦点框交给样式绘制，文字自己画
        bg_opt = QStyleOptionViewItem(opt)
        bg_opt.text = ""
        style.drawControl(QStyle.CE_ItemViewItem, bg_opt, painter, opt.widget)

        fm = QFontMetricsF(TABLE_ITEM_FONT, self.table)
        disp = fm.elidedText(opt.text, opt.textElideMode, text_rect.width())
        total = fm.horizontalAdvance(disp)
        align = opt.displayAlignment
        if align & Qt.AlignRight:
            x0 = text_rect.x() + text_rect.width() - total
        elif align & Qt.AlignHCenter:
            x0 = text_rect.x() + (text_rect.width() - total) / 2
        else:
            x0 = text_rect.x()
        top = text_rect.y() + (text_rect.height() - fm.height()) / 2
        baseline = top + fm.ascent()

        painter.save()
        painter.setFont(TABLE_ITEM_FONT)
        if self._is_anchor(index):
            k, left, right = self._split_elided(opt.text, disp)
            for a, b in self._orig_to_disp_intervals(
                    self._sel_start, self._sel_end, opt.text, k, left, right):
                xa = x0 + fm.horizontalAdvance(disp[:a])
                xb = x0 + fm.horizontalAdvance(disp[:b])
                painter.fillRect(QRectF(xa, top, xb - xa, fm.height()), self.HIGHLIGHT)

        color = opt.palette.color(
            QPalette.HighlightedText if (opt.state & QStyle.State_Selected)
            else QPalette.Text)
        painter.setPen(color)
        painter.drawText(QPointF(x0, baseline), disp)
        painter.restore()


class CheckBoxButton(QAbstractButton):
    """「全选」勾选框：绘制与列表勾选框一致，点击切换全选/全不选。"""

    BOX = 16

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(self.BOX, self.BOX)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        draw_checkbox(painter, QRectF(0, 0, self.BOX, self.BOX), self.isChecked())
        painter.end()


class CleanReportDialog(QDialog):
    """清理结果窗口：已清理 / 未能清理两个堆叠分区。

    cleaned、failed 均为 [(ftype, path, size)]，已按大小降序排好。
    """

    def __init__(self, cleaned, failed, parent=None):
        super().__init__(parent)
        self.setObjectName("clean_report_dialog")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setWindowTitle("清理结果")
        self.setWindowIcon(QIcon(APP_ICON))
        self.resize(840, 660)
        self.setMinimumSize(680, 500)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(12)

        title = QLabel("🧹 清理结果")
        title.setObjectName("report_title")
        layout.addWidget(title)

        cleaned_size = sum(r[2] for r in cleaned)
        failed_size = sum(r[2] for r in failed)

        section_ok = self._build_section(
            f"✅ 已清理文件 ({len(cleaned)}) | 共 {format_size(cleaned_size)}",
            cleaned)
        section_fail = self._build_section(
            f"⚠️ 未能清理文件 ({len(failed)}) | 共 {format_size(failed_size)}",
            failed,
            hint="可能被占用、受保护或无权限，已跳过。")

        # 空分区仍显示，但让有内容的分区获得更多高度
        layout.addWidget(section_ok, 2 if cleaned else 1)
        layout.addWidget(section_fail, 2 if failed else 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        close_btn = QPushButton("关闭")
        close_btn.setObjectName("action_btn")
        close_btn.setDefault(True)
        close_btn.setMinimumWidth(110)
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    def _build_section(self, title_text, rows, hint=None):
        card = QFrame()
        card.setObjectName("section_card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(12, 10, 12, 12)
        card_layout.setSpacing(6)

        title = QLabel(title_text)
        title.setObjectName("report_section_title")
        card_layout.addWidget(title)

        if hint:
            hint_label = QLabel(hint)
            hint_label.setObjectName("report_hint")
            hint_label.setWordWrap(True)
            card_layout.addWidget(hint_label)

        table = self._make_table(rows)
        card_layout.addWidget(table, 1)
        return card

    def _make_table(self, rows):
        model = ReportTableModel(rows, self)  # parent=self 保证模型生命周期
        table = QTableView()
        table.setModel(model)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setWordWrap(False)
        table.setTextElideMode(Qt.ElideMiddle)  # 长路径中间省略，保留盘符与文件名
        table.setFont(TABLE_ITEM_FONT)
        table.setItemDelegateForColumn(1, PathSelectDelegate(table, 1))
        table.verticalHeader().setVisible(False)
        table.setMinimumHeight(140)
        table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)

        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        return table


class CleanerApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("磁盘清理助手 v1.1")
        self.setWindowIcon(QIcon(APP_ICON))
        self.resize(900, 600)
        self.setMinimumSize(800, 500)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(10)

        # 左侧导航
        nav_frame = QFrame()
        nav_frame.setObjectName("nav_frame")
        nav_frame.setFixedWidth(180)
        nav_layout = QVBoxLayout(nav_frame)
        nav_layout.setContentsMargins(10, 20, 10, 20)
        nav_layout.setSpacing(8)

        title_box = QHBoxLayout()
        title_box.setSpacing(6)
        title_icon = QLabel()
        title_icon.setObjectName("nav_icon")
        title_icon.setPixmap(QIcon(APP_ICON).pixmap(20, 20))
        title_icon.setFixedSize(20, 20)
        title_label = QLabel("磁盘清理助手")
        title_label.setObjectName("nav_title")
        title_box.addWidget(title_icon, 0, Qt.AlignVCenter)
        title_box.addWidget(title_label, 0, Qt.AlignVCenter)
        title_box.addStretch()
        nav_layout.addLayout(title_box)
        nav_layout.addSpacing(20)

        self.nav_buttons = []
        for i, text in enumerate(["首页概览", "扫描设置", "关于软件"]):
            btn = QPushButton(text)
            btn.setObjectName("nav_btn")
            btn.setCheckable(True)
            btn.clicked.connect(lambda checked, index=i: self.switch_page(index))
            nav_layout.addWidget(btn)
            self.nav_buttons.append(btn)
        nav_layout.addStretch()
        main_layout.addWidget(nav_frame)

        # 右侧内容
        content_frame = QFrame()
        content_frame.setObjectName("content_frame")
        content_layout = QVBoxLayout(content_frame)
        content_layout.setContentsMargins(20, 20, 20, 20)

        self.stack = QStackedWidget()

        # 首页
        home_page = QWidget()
        home_layout = QVBoxLayout(home_page)
        home_layout.setSpacing(10)

        page_title = QLabel("扫描与清理")
        page_title.setObjectName("page_title")
        home_layout.addWidget(page_title)

        top_bar = QHBoxLayout()
        top_bar.setSpacing(8)
        disk_label = QLabel("选择磁盘:")
        disk_label.setFont(app_font(11))
        top_bar.addWidget(disk_label)
        self.disk_combo = QComboBox()
        self.disk_combo.setMinimumWidth(250)
        self.disk_combo.currentIndexChanged.connect(self.refresh_disk_space)
        top_bar.addWidget(self.disk_combo)
        self.btn_scan = QPushButton("🔍 一键扫描")
        self.btn_scan.setObjectName("action_btn")
        self.btn_scan.clicked.connect(self.start_scan)
        top_bar.addWidget(self.btn_scan)
        self.btn_stop = QPushButton("⏹ 停止扫描")
        self.btn_stop.setObjectName("action_btn")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_scan)
        top_bar.addWidget(self.btn_stop)
        top_bar.addStretch()
        home_layout.addLayout(top_bar)

        self.status_label = QLabel("准备就绪，请选择磁盘开始扫描")
        self.status_label.setFont(app_font(11))
        self.status_label.setStyleSheet("color: #333;")
        home_layout.addWidget(self.status_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        home_layout.addWidget(self.progress_bar)

        splitter = QSplitter(Qt.Vertical)
        self.models = {}
        self.tables = {}
        self.count_labels = {}
        self.category_titles = {}
        self.select_all_buttons = {}
        self.scan_category_buttons = {}
        categories = [
            ("uninstall", "🗑️ 卸载残留文件",     "扫描卸载残留"),
            ("duplicate", "📋 重复文件",         "扫描重复文件"),
            ("empty",     "📂 空文件或空文件夹", "扫描空文件"),
        ]

        for cat_key, cat_title, scan_label in categories:
            section_widget = QWidget()
            section_widget.setObjectName("section_card")
            section_layout = QVBoxLayout(section_widget)
            section_layout.setContentsMargins(10, 8, 10, 10)
            section_layout.setSpacing(4)

            count_lbl = QLabel()
            count_lbl.setTextFormat(Qt.RichText)
            count_lbl.setObjectName("count_label")
            self.count_labels[cat_key] = count_lbl
            self.category_titles[cat_key] = cat_title

            select_all = CheckBoxButton()
            select_all.setObjectName("select_all_btn")
            select_all.setToolTip("全选 / 全不选")

            title_row = QHBoxLayout()
            title_row.setSpacing(8)
            title_row.addWidget(select_all, 0, Qt.AlignVCenter)
            title_row.addWidget(count_lbl, 0, Qt.AlignVCenter)
            title_row.addStretch()

            scan_cat_btn = QPushButton(f"🔍 {scan_label}")
            scan_cat_btn.setObjectName("scan_cat_btn")
            scan_cat_btn.setCursor(Qt.PointingHandCursor)
            scan_cat_btn.setToolTip(f"仅重新扫描「{cat_title}」，保留其他类别结果")
            scan_cat_btn.clicked.connect(
                lambda _checked=False, c=cat_key: self.start_scan_category(c))
            title_row.addWidget(scan_cat_btn, 0, Qt.AlignVCenter)
            self.scan_category_buttons[cat_key] = scan_cat_btn
            section_layout.addLayout(title_row)

            model = ResultsModel(self)
            model.statsChanged.connect(
                lambda cnt, sz, sel, c=cat_key: self._update_count_label(c, cnt, sz, sel))
            select_all.toggled.connect(model.set_all_checked)
            self.select_all_buttons[cat_key] = select_all

            table = QTableView()
            table.setModel(model)
            table.setItemDelegate(CheckDelegate(table))
            table.setFont(TABLE_ITEM_FONT)
            table.setItemDelegateForColumn(2, PathSelectDelegate(table, 2))
            table.setSelectionBehavior(QAbstractItemView.SelectRows)
            table.setSelectionMode(QAbstractItemView.ExtendedSelection)
            table.setEditTriggers(QAbstractItemView.NoEditTriggers)
            table.verticalHeader().setVisible(False)
            header = table.horizontalHeader()
            header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
            header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
            header.setSectionResizeMode(2, QHeaderView.Stretch)
            header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
            section_layout.addWidget(table)

            self.models[cat_key] = model
            self.tables[cat_key] = table
            self._update_count_label(cat_key, 0, 0, 0)
            splitter.addWidget(section_widget)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 1)
        home_layout.addWidget(splitter, stretch=1)

        action_bar = QHBoxLayout()
        action_bar.addStretch()
        self.btn_clean_selected = QPushButton("清理选中项")
        self.btn_clean_selected.setObjectName("action_btn")
        self.btn_clean_selected.setEnabled(False)
        self.btn_clean_selected.clicked.connect(self.clean_selected)
        self.btn_clean_all = QPushButton("一键全部清理")
        self.btn_clean_all.setObjectName("action_btn")
        self.btn_clean_all.setEnabled(False)
        self.btn_clean_all.clicked.connect(self.clean_all)
        action_bar.addWidget(self.btn_clean_selected)
        action_bar.addWidget(self.btn_clean_all)
        home_layout.addLayout(action_bar)

        self.stack.addWidget(home_page)

        # 扫描设置（本轮占位，不接后端）
        settings_page = QWidget()
        sp_layout = QVBoxLayout(settings_page)
        sp_layout.setAlignment(Qt.AlignCenter)
        sp_layout.setSpacing(12)
        st_title = QLabel("⚙️ 扫描设置")
        st_title.setObjectName("placeholder_title")
        st_title.setAlignment(Qt.AlignCenter)
        sp_layout.addWidget(st_title)
        st_sub = QLabel("扫描规则自定义即将上线\n\n后续将支持：最小重复文件大小、跳过系统目录、自定义垃圾文件扩展名等")
        st_sub.setObjectName("placeholder_sub")
        st_sub.setAlignment(Qt.AlignCenter)
        sp_layout.addWidget(st_sub)
        self.stack.addWidget(settings_page)

        # 关于软件
        about_page = QWidget()
        ap_layout = QVBoxLayout(about_page)
        ap_layout.setAlignment(Qt.AlignCenter)
        ap_layout.setSpacing(10)
        ap_layout.setContentsMargins(30, 10, 30, 10)
        ap_title = QLabel(f'{trash_icon_html(26)} 磁盘清理助手')
        ap_title.setTextFormat(Qt.RichText)
        ap_title.setObjectName("about_title")
        ap_title.setAlignment(Qt.AlignCenter)
        ap_layout.addWidget(ap_title)
        ap_ver = QLabel("版本 v1.1 · 简洁、安全的 Windows 磁盘清理工具")
        ap_ver.setObjectName("about_sub")
        ap_ver.setAlignment(Qt.AlignCenter)
        ap_layout.addWidget(ap_ver)
        ap_body = QLabel(
            "• 扫描软件卸载残留、重复文件、空文件与空文件夹\n"
            "• 支持勾选清理与一键全部清理\n"
            "• 清理项目仅移入回收站，可随时还原\n"
            "• 系统目录与已安装软件目录受保护，不会误删\n\n"
            "技术栈：Python + PySide6")
        ap_body.setObjectName("about_body")
        ap_body.setAlignment(Qt.AlignCenter)
        ap_layout.addWidget(ap_body)
        self.stack.addWidget(about_page)

        content_layout.addWidget(self.stack)
        main_layout.addWidget(content_frame)

        self.switch_page(0)
        self.load_disks()

        self.worker = None
        self.clean_worker = None
        self._scan_targets = set(self.models)  # 当前扫描覆盖的类别
        self._scan_stopped = False
        self._deleted_paths = set()
        self._report_rows = []  # [(ftype, path, size)] 清理前快照

        self._pending = defaultdict(list)
        self._ui_timer = QTimer(self)
        self._ui_timer.setInterval(150)
        self._ui_timer.timeout.connect(self._flush_ui_updates)

        self.space_timer = QTimer(self)
        self.space_timer.timeout.connect(self.refresh_disk_space)
        self.space_timer.start(3000)

    # ---- UI 更新 ----
    def _update_count_label(self, cat, count, total_size, selected_size):
        title = self.category_titles[cat]
        self.count_labels[cat].setText(
            f"{title} ({count}) | 共 <span style='color:#D32F2F;'>{format_size(total_size)}</span>"
            f" | 已选 <span style='color:#1976D2;'>{format_size(selected_size)}</span>"
        )

    def on_batch_found(self, category, items):
        if not items:
            return
        self._pending[category].extend(items)
        if not self._ui_timer.isActive():
            self._ui_timer.start()

    def _flush_ui_updates(self):
        if not any(self._pending.values()):
            self._ui_timer.stop()
            return
        for category, items in list(self._pending.items()):
            if items and category in self.models:
                self.models[category].append_rows(items)
                self._pending[category] = []

    # ---- 扫描 ----
    def start_scan(self):
        self._begin_scan(None)

    def start_scan_category(self, category):
        """仅扫描指定分类，保留其他分类已有结果。"""
        self._begin_scan({category})

    def _begin_scan(self, categories):
        """categories=None 表示三类全扫；否则只扫给定类别。"""
        if self.worker is not None or self.clean_worker is not None:
            self.status_label.setText("⚠️ 已有扫描或清理任务正在进行，请稍候。")
            return

        drive = self.disk_combo.currentData()
        if not drive:
            self.status_label.setText("⚠️ 请先选择一个磁盘！")
            return

        all_cats = set(self.models)
        targets = all_cats if categories is None else (set(categories) & all_cats)
        if not targets:
            targets = all_cats

        self._scan_targets = targets
        self._scan_stopped = False

        # 只丢弃目标类别的待刷新批次；其他类别结果与勾选状态原样保留
        if categories is None:
            self._pending.clear()
        else:
            for cat in targets:
                self._pending.pop(cat, None)
        if not any(self._pending.values()):
            self._ui_timer.stop()

        for cat in targets:
            self.models[cat].clear()
            self.select_all_buttons[cat].setChecked(False)

        self.progress_bar.setRange(0, 0)  # 遍历阶段：不确定进度
        self.progress_bar.setValue(0)
        if len(targets) == 1:
            title = self.category_titles[next(iter(targets))]
            self.status_label.setText(f"正在扫描 {drive} · {title} ...")
        else:
            self.status_label.setText(f"正在扫描 {drive} ...")
        self._set_scan_buttons_enabled(False)
        self.btn_stop.setEnabled(True)
        self._set_clean_buttons_enabled(False)

        self.worker = ScanWorker(drive, targets)
        self.worker.phase.connect(self.on_phase)
        self.worker.status.connect(self.status_label.setText)
        self.worker.progress.connect(self.progress_bar.setValue)
        self.worker.batch_found.connect(self.on_batch_found)
        self.worker.finished.connect(self.on_scan_finished)
        self.worker.start()

    def stop_scan(self):
        if self.worker is not None:
            self._scan_stopped = True
            self.worker.stop()
            self.status_label.setText("正在停止扫描...")

    def on_phase(self, phase):
        if phase == "hashing":
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(0)
            self.status_label.setText("正在比对重复文件内容...")

    def on_scan_finished(self, stats):
        self._flush_ui_updates()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        self._set_scan_buttons_enabled(True)
        self.btn_stop.setEnabled(False)
        self._set_clean_buttons_enabled(True)
        parts = []
        if "uninstall" in self._scan_targets:
            parts.append(f"卸载残留:{stats.get('uninstall', 0)}")
        if "duplicate" in self._scan_targets:
            parts.append(f"重复文件:{stats.get('duplicate', 0)}")
        if "empty" in self._scan_targets:
            parts.append(f"空文件/夹:{stats.get('empty', 0)}")
        summary = " | ".join(parts)
        if self._scan_stopped:
            self.status_label.setText(f"⏹ 扫描已停止（部分结果）：{summary}")
        else:
            self.status_label.setText(f"✅ 扫描完成！{summary}")
        self._scan_stopped = False
        self.worker = None

    # ---- 清理 ----
    def _set_scan_buttons_enabled(self, enabled):
        self.btn_scan.setEnabled(enabled)
        for btn in self.scan_category_buttons.values():
            btn.setEnabled(enabled)

    def _set_clean_buttons_enabled(self, enabled):
        self.btn_clean_selected.setEnabled(enabled)
        self.btn_clean_all.setEnabled(enabled)

    def _collect_items(self, mode):
        """返回 (items, rows)。

        items: [(path, size)] 供 CleanWorker；rows: [(ftype, path, size)] 报告快照。
        同一路径可能同时出现在多个分类中（例如 .tmp 文件既在卸载残留又在重复文件中），
        按 Windows 路径规则去重，保留首次出现的分类。
        """
        rows = []
        for model in self.models.values():
            if mode == "selected":
                rows.extend(model.checked_rows())
            else:
                rows.extend(model.recommended_rows())

        seen = set()
        unique_rows = []
        for row in rows:
            key = os.path.normcase(os.path.normpath(row[1]))
            if key in seen:
                continue
            seen.add(key)
            unique_rows.append(row)

        items = [(path, size) for _, path, size in unique_rows]
        return items, unique_rows

    def clean_selected(self):
        items, rows = self._collect_items("selected")
        self._start_clean(items, rows)

    def clean_all(self):
        items, rows = self._collect_items("recommended")
        self._start_clean(items, rows)

    def _start_clean(self, items, rows):
        if not items:
            self.status_label.setText("没有可清理的项。")
            return
        ret = QMessageBox.question(
            self, "确认清理",
            f"将把 {len(items)} 项移入回收站，是否继续？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret != QMessageBox.Yes:
            return

        self._deleted_paths = set()
        self._report_rows = list(rows)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self._set_scan_buttons_enabled(False)
        self.btn_stop.setEnabled(False)
        self._set_clean_buttons_enabled(False)

        self.clean_worker = CleanWorker(items)
        self.clean_worker.progress.connect(self.progress_bar.setValue)
        self.clean_worker.deleted.connect(self.on_clean_deleted)
        self.clean_worker.failed.connect(self.on_clean_failed)
        self.clean_worker.finished.connect(self.on_clean_finished)
        self.clean_worker.start()

    def on_clean_deleted(self, paths):
        self._deleted_paths.update(paths)
        self.status_label.setText(f"正在清理... 已处理 {len(self._deleted_paths)} 项")

    def on_clean_failed(self, items):
        if items:
            self.status_label.setText(f"跳过无法清理：{items[-1][0]}")

    def on_clean_finished(self, ok, fail):
        for model in self.models.values():
            model.remove_paths(self._deleted_paths)

        self._set_scan_buttons_enabled(True)
        self._set_clean_buttons_enabled(True)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        self.status_label.setText(f"✅ 清理完成：成功 {ok} 项，失败 {fail} 项。")

        deleted = self._deleted_paths
        cleaned = [r for r in self._report_rows if r[1] in deleted]
        failed = [r for r in self._report_rows if r[1] not in deleted]
        cleaned.sort(key=lambda r: r[2], reverse=True)
        failed.sort(key=lambda r: r[2], reverse=True)

        dlg = CleanReportDialog(cleaned, failed, self)
        dlg.exec()
        dlg.deleteLater()

        # 自定义 finished 信号在 run() 返回前发射；此处确保线程真正结束再释放引用
        if self.clean_worker is not None:
            self.clean_worker.wait()
            self.clean_worker = None

    # ---- 磁盘 ----
    def get_disk_info(self, drive_path):
        try:
            free = ctypes.c_ulonglong(0)
            total = ctypes.c_ulonglong(0)
            ctypes.windll.kernel32.GetDiskFreeSpaceExW(
                ctypes.c_wchar_p(drive_path), None,
                ctypes.pointer(total), ctypes.pointer(free))
            return total.value / (1024 ** 3), free.value / (1024 ** 3)
        except Exception:
            return None, None

    def refresh_disk_space(self):
        idx = self.disk_combo.currentIndex()
        if idx < 0:
            return
        drive = self.disk_combo.currentData()
        if not drive:
            return
        total_gb, free_gb = self.get_disk_info(drive)
        if total_gb is not None:
            new_text = f"{drive}  ({free_gb:.1f} GB 可用 / 共 {total_gb:.1f} GB)"
            if self.disk_combo.itemText(idx) != new_text:
                self.disk_combo.setItemText(idx, new_text)

    def load_disks(self):
        self.disk_combo.blockSignals(True)
        self.disk_combo.clear()
        for letter in string.ascii_uppercase:
            drive = f"{letter}:\\"
            if os.path.exists(drive):
                total_gb, free_gb = self.get_disk_info(drive)
                text = (f"{drive}  ({free_gb:.1f} GB 可用 / 共 {total_gb:.1f} GB)"
                        if total_gb is not None else drive)
                self.disk_combo.addItem(text, drive)
        self.disk_combo.blockSignals(False)

        if self.disk_combo.count() == 0:
            self.status_label.setText("⚠️ 未检测到任何可用磁盘！")
        else:
            self.status_label.setText(f"准备就绪，已加载 {self.disk_combo.count()} 个磁盘。")
            self.refresh_disk_space()

    def switch_page(self, index):
        self.stack.setCurrentIndex(index)
        for i, btn in enumerate(self.nav_buttons):
            btn.setChecked(i == index)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLESHEET)
    app.setFont(app_font(10))
    app.setWindowIcon(QIcon(APP_ICON))
    window = CleanerApp()
    window.show()
    sys.exit(app.exec())
