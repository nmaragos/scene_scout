"""Tree widgets for inspecting USD prims and layers."""
from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QColor, QContextMenuEvent, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QAbstractItemView, QMenu, QTreeWidget, QTreeWidgetItem, QTreeWidgetItemIterator


from PySide6.QtWidgets import QHeaderView


class BranchTree(QTreeWidget):
    addContextActions = Signal(object, object)

    def drawBranches(self, painter, rect, index):
        if not self.model().hasChildren(index):
            return
        rtl = self.layoutDirection() == Qt.RightToLeft
        x = rect.left() + self.indentation() / 2 if rtl else rect.right() - self.indentation() / 2
        y = rect.center().y()
        if self.isExpanded(index):
            points = [(-4, -2), (0, 2), (4, -2)]
        else:
            direction = -1 if rtl else 1
            points = [(-2 * direction, -4), (2 * direction, 0), (-2 * direction, 4)]
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor("#bde6fa"), 1.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.drawPolyline(QPolygonF([QPointF(x + dx, y + dy) for dx, dy in points]))
        painter.restore()

    def contextMenuEvent(self, event):
        item = self.currentItem() if event.reason() == QContextMenuEvent.Keyboard else self.itemAt(event.pos())
        menu = QMenu(self)
        menu.setObjectName("treeBranchMenu")
        if item is not None and item.childCount() and not item.isExpanded():
            menu.addAction("Expand", lambda: item.setExpanded(True))
        if self._has_collapsed_branch():
            menu.addAction("Expand All", self.expandAll)
        if item is not None and item.childCount() and item.isExpanded():
            menu.addAction("Collapse", lambda: item.setExpanded(False))
        self.addContextActions.emit(menu, item)
        if not menu.isEmpty():
            menu.exec(event.globalPos())
        menu.deleteLater()
        event.accept()

    def _has_collapsed_branch(self):
        iterator = QTreeWidgetItemIterator(self, QTreeWidgetItemIterator.HasChildren)
        while iterator.value() is not None:
            if not iterator.value().isExpanded():
                return True
            iterator += 1
        return False


class PrimHierarchyTree(BranchTree):
    def __init__(self):
        super().__init__()
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._auto_fit = True
        self._fitting = False
        self._type_width = 0
        self.setHeaderLabels(["Prim", "Type"])
        header = self.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.resizeSection(1, header.minimumSectionSize())
        header.setStretchLastSection(True)
        header.sectionResized.connect(self._section_resized)

    def fit_type_column(self, type_names):
        metrics = self.fontMetrics()
        widest = max((metrics.horizontalAdvance(name) for name in type_names), default=0)
        self._type_width = max(self.header().sectionSizeHint(1), widest + 16)
        self._fit_columns()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_columns()

    def _fit_columns(self):
        if not self._auto_fit or self._fitting or not self._type_width:
            return
        width = max(self.header().minimumSectionSize(), self.viewport().width() - self._type_width)
        self._fitting = True
        try:
            self.setColumnWidth(0, width)
        finally:
            self._fitting = False

    def _section_resized(self, column, _old, _new):
        if column == 0 and not self._fitting:
            self._auto_fit = False


class LayerStackTree(BranchTree):
    def __init__(self):
        super().__init__()
        self.setHeaderLabels(["Layer", "Identifier"])
        self.setColumnHidden(1, True)

    def show_stage(self, stage, scene):
        from pxr import Ar, Sdf

        self.clear()
        used = set(scene.layers)
        shown = set()

        def add_layer_item(parent, name, identifier):
            item = QTreeWidgetItem(parent, [name, identifier])
            item.setData(0, Qt.UserRole, identifier)
            item.setToolTip(0, identifier)
            item.setToolTip(1, identifier)
            return item

        def add_layer(parent, layer, ancestors):
            identifier = layer.identifier
            item = add_layer_item(parent, layer.GetDisplayName(), identifier)
            shown.add(identifier)
            if identifier in ancestors:
                item.setText(1, f"{identifier} (cycle)")
                return item
            ancestors = ancestors | {identifier}
            for path in layer.subLayerPaths:
                sublayer = Sdf.Layer.FindRelativeToLayer(layer, path)
                if sublayer and sublayer.identifier in used:
                    add_layer(item, sublayer, ancestors)
                else:
                    unavailable = QTreeWidgetItem(item, [path, "Unavailable in this stage"])
                    unavailable.setToolTip(0, "Unavailable in this stage")
            return item

        with Ar.ResolverContextBinder(stage.GetPathResolverContext()):
            session = stage.GetSessionLayer()
            if session:
                if not session.empty:
                    add_layer(self, session, set())
                shown.add(session.identifier)
            root = add_layer(self, stage.GetRootLayer(), set())
        root.setExpanded(True)
        other_layers = sorted(used - shown)
        if other_layers:
            group = QTreeWidgetItem(self, ["Other used layers"])
            for identifier in other_layers:
                add_layer_item(group, Sdf.Layer.GetDisplayNameFromIdentifier(identifier), identifier)
        for note in scene.notes:
            item = QTreeWidgetItem(self, ["Stage note", note])
            item.setToolTip(0, note)
            item.setToolTip(1, note)
