"""虚拟化结果列表模型。

用 QAbstractTableModel + QTableView 取代原来的 QTableWidget + 每行一个 QCheckBox，
按需渲染可见行，是根治「扫描时界面未响应」的关键。模型实时维护每类列表的
总大小（total_size）与已勾选大小（selected_size），并通过 statsChanged 信号通知界面刷新。
"""
from dataclasses import dataclass

from PySide6.QtCore import Qt, QAbstractTableModel, QModelIndex, Signal


def format_size(size_bytes):
    if size_bytes >= 1024 ** 3:
        return f"{size_bytes / (1024 ** 3):.2f} GB"
    elif size_bytes >= 1024 ** 2:
        return f"{size_bytes / (1024 ** 2):.2f} MB"
    elif size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"


@dataclass
class ResultRow:
    ftype: str
    path: str
    size: int
    checked: bool = True       # 用户勾选状态（可编辑）
    recommended: bool = True   # 是否属于「推荐清理」集合（重复文件保留的一份、疑似残留目录为 False）


class ResultsModel(QAbstractTableModel):
    HEADERS = ["选择", "类型", "文件路径", "大小"]
    statsChanged = Signal('qint64', 'qint64', 'qint64')  # count, total_size, selected_size（qint64 避免 >2GB 溢出）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows = []
        self._total_size = 0
        self._selected_size = 0

    # ---- Qt 模型接口 ----
    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        return len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal and 0 <= section < len(self.HEADERS):
            return self.HEADERS[section]
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemIsEnabled
        flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if index.column() == 0:
            flags |= Qt.ItemIsUserCheckable
        return flags

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        col = index.column()
        if role == Qt.DisplayRole:
            if col == 1:
                return row.ftype
            if col == 2:
                return row.path
            if col == 3:
                return format_size(row.size)
        elif role == Qt.CheckStateRole and col == 0:
            return Qt.Checked if row.checked else Qt.Unchecked
        elif role == Qt.ToolTipRole and col == 2:
            return row.path
        return None

    def setData(self, index, value, role=Qt.EditRole):
        if role == Qt.CheckStateRole and index.isValid() and index.column() == 0:
            row = self._rows[index.row()]
            checked = (value == Qt.Checked)
            if row.checked != checked:
                row.checked = checked
                self._selected_size += row.size if checked else -row.size
                self.dataChanged.emit(index, index, [Qt.CheckStateRole])
                self.statsChanged.emit(len(self._rows), self._total_size, self._selected_size)
            return True
        return False

    # ---- 数据操作 ----
    def append_rows(self, rows):
        if not rows:
            return
        start = len(self._rows)
        self.beginInsertRows(QModelIndex(), start, start + len(rows) - 1)
        for r in rows:
            row = r if isinstance(r, ResultRow) else ResultRow(r[0], r[1], r[2], checked=r[3], recommended=r[3])
            self._rows.append(row)
            self._total_size += row.size
            if row.checked:
                self._selected_size += row.size
        self.endInsertRows()
        self.sort_by_size_desc()
        self.statsChanged.emit(len(self._rows), self._total_size, self._selected_size)

    def clear(self):
        self.beginResetModel()
        self._rows = []
        self._total_size = 0
        self._selected_size = 0
        self.endResetModel()
        self.statsChanged.emit(0, 0, 0)

    def remove_paths(self, paths):
        """按路径移除已成功清理的行，返回移除数量。"""
        if not paths:
            return 0
        removed = 0
        for i in range(len(self._rows) - 1, -1, -1):
            if self._rows[i].path in paths:
                row = self._rows[i]
                self.beginRemoveRows(QModelIndex(), i, i)
                self._total_size -= row.size
                if row.checked:
                    self._selected_size -= row.size
                del self._rows[i]
                self.endRemoveRows()
                removed += 1
        if removed:
            self.statsChanged.emit(len(self._rows), self._total_size, self._selected_size)
        return removed

    # ---- 查询 ----
    def count(self):
        return len(self._rows)

    def total_size(self):
        return self._total_size

    def selected_size(self):
        return self._selected_size

    def checked_paths(self):
        return [r.path for r in self._rows if r.checked]

    def recommended_paths(self):
        return [r.path for r in self._rows if r.recommended]

    def checked_items(self):
        """返回已勾选行的 (path, size) 列表。"""
        return [(r.path, r.size) for r in self._rows if r.checked]

    def recommended_items(self):
        """返回推荐清理行的 (path, size) 列表。"""
        return [(r.path, r.size) for r in self._rows if r.recommended]

    def checked_rows(self):
        """返回已勾选行的 (ftype, path, size) 快照。"""
        return [(r.ftype, r.path, r.size) for r in self._rows if r.checked]

    def recommended_rows(self):
        """返回推荐清理行的 (ftype, path, size) 快照。"""
        return [(r.ftype, r.path, r.size) for r in self._rows if r.recommended]

    def set_all_checked(self, checked):
        if not self._rows:
            return
        for r in self._rows:
            r.checked = checked
        self._selected_size = sum(r.size for r in self._rows if r.checked)
        self.dataChanged.emit(self.index(0, 0), self.index(len(self._rows) - 1, 0), [Qt.CheckStateRole])
        self.statsChanged.emit(len(self._rows), self._total_size, self._selected_size)

    def sort_by_size_desc(self):
        """按文件大小降序稳定排序，大文件排前（清理时优先处理）。"""
        if not self._rows:
            return
        self.layoutAboutToBeChanged.emit()
        persistent = self.persistentIndexList()
        tracked = [(idx, idx.column(), self._rows[idx.row()]) for idx in persistent]
        self._rows.sort(key=lambda r: r.size, reverse=True)
        if tracked:
            # 按对象身份重映射持久索引（ResultRow 是 dataclass，不能用值相等匹配）
            pos = {id(r): i for i, r in enumerate(self._rows)}
            new = [self.index(pos[id(row)], col) for _, col, row in tracked]
            self.changePersistentIndexList(persistent, new)
        self.layoutChanged.emit()


class ReportTableModel(QAbstractTableModel):
    """清理报告只读模型：[(ftype, path, size)]，无勾选列，按需渲染可见行。"""

    HEADERS = ["类型", "文件路径", "大小"]

    def __init__(self, rows=None, parent=None):
        super().__init__(parent)
        self._rows = list(rows) if rows else []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if (role == Qt.DisplayRole and orientation == Qt.Horizontal
                and 0 <= section < len(self.HEADERS)):
            return self.HEADERS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        ftype, path, size = self._rows[index.row()]
        col = index.column()
        if role == Qt.DisplayRole:
            if col == 0:
                return ftype
            if col == 1:
                return path
            if col == 2:
                return format_size(size)
        elif role == Qt.ToolTipRole and col == 1:
            return path
        elif role == Qt.TextAlignmentRole and col == 2:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable
