"""Sortable, filterable view of the current comparison."""
from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, Signal
from PySide6.QtWidgets import QHeaderView, QTableView

from .diff import field_label, finding_kind_label
from .review_document import finding_category, finding_issue


class Findings(QAbstractTableModel):
    headers = ("Change", "Prim", "Field", "Time (A)")

    def __init__(self, parent):
        super().__init__(parent)
        self.changes = []

    def reset(self, changes):
        self.beginResetModel()
        self.changes = changes
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.changes)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.headers)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.headers[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or role not in (Qt.DisplayRole, Qt.ToolTipRole):
            return None
        change = self.changes[index.row()]
        kind = finding_kind_label(change.kind)
        if role == Qt.ToolTipRole:
            detail = f"{finding_category(change)} · {kind}\n{change.path}\n{field_label(change.field)}"
            if change.time is not None:
                detail += f"\nTime code A {change.time:.15g}"
                if change.time_b is not None:
                    detail += f" · B {change.time_b:.15g}"
            return detail
        return (kind, change.path, field_label(change.field), change.time)[index.column()]


class FindingFilter(QSortFilterProxyModel):
    def __init__(self, annotations, parent):
        super().__init__(parent)
        self.annotations = annotations
        self.category = self.state = self.query = ""

    def filterAcceptsRow(self, row, _parent):
        change = self.sourceModel().changes[row]
        if self.category and self.category != finding_category(change):
            return False
        note = self.annotations.get(change)
        reviewed = bool(note and note.reviewed)
        if self.state == "To review" and reviewed or self.state == "Reviewed" and not reviewed:
            return False
        if self.state == "Open issues" and not (finding_issue(change) or note and note.open_issue):
            return False
        if self.query:
            text = (change.path, change.field, field_label(change.field), change.before, change.after,
                    finding_kind_label(change.kind), note.notes if note else "")
            return any(self.query in value.casefold() for value in text)
        return True


class ChangeTable(QTableView):
    rowActivated = Signal(int)

    def __init__(self, annotations):
        super().__init__()
        self.findings = Findings(self)
        self.filters = FindingFilter(annotations, self)
        self.filters.setSourceModel(self.findings)
        self.setModel(self.filters)
        header = self.horizontalHeader()
        header.setMinimumSectionSize(44)
        header.setSectionResizeMode(QHeaderView.Interactive)
        for section, width in enumerate((88, 140, 104, 68)):
            header.resizeSection(section, width)
        header.setResizeContentsPrecision(32)
        header.setSortIndicator(-1, Qt.AscendingOrder)
        self.setSortingEnabled(True)
        self.verticalHeader().hide()
        self.setSelectionBehavior(QTableView.SelectRows)
        self.setSelectionMode(QTableView.SingleSelection)
        self.setEditTriggers(QTableView.NoEditTriggers)
        self.setWordWrap(False)
        self.setAccessibleName("Publish findings")
        self.clicked.connect(lambda index: self.rowActivated.emit(index.row()))
        self.activated.connect(lambda index: self.rowActivated.emit(index.row()))

    def currentRow(self):
        return self.currentIndex().row()

    def change_at(self, row):
        index = self.filters.mapToSource(self.filters.index(row, 0))
        return self.findings.changes[index.row()] if index.isValid() else None

    def keyPressEvent(self, event):
        previous = self.currentRow()
        super().keyPressEvent(event)
        if self.currentRow() != previous:
            self.rowActivated.emit(self.currentRow())
