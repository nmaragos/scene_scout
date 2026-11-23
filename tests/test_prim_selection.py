import os

from pxr import Usd

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from scenescout.app import SceneScoutWindow
from scenescout.diff import compare_scenes
from scenescout.usd_reader import snapshot_stage


def test_stage_switch_uses_snapshot_before_selected_widget_exists():
    app = QApplication.instance() or QApplication([])
    window = SceneScoutWindow()
    try:
        a, b = Usd.Stage.CreateInMemory(), Usd.Stage.CreateInMemory()
        a.DefinePrim("/Late", "Xform")
        a.DefinePrim("/OnlyInA", "Xform")
        for index in range(300):
            b.DefinePrim(f"/Earlier{index:03d}", "Scope")
        b.DefinePrim("/Late", "Scope")
        window.snapshots = {"A": snapshot_stage(a), "B": snapshot_stage(b)}
        window.review.stages.update({"A": a, "B": b})
        changes = compare_scenes(window.snapshots["A"], window.snapshots["B"])
        window.review.show_changes(changes, [], select_first=False)

        window.select_prim("A", "/Late")
        assert "TYPE  Xform" in window.details.toPlainText()
        finding = window.review.current_finding
        assert finding.path == "/Late"

        window.switch_stage("B")
        assert "/Late" not in window._tree_items
        assert window.review.current_finding == finding
        assert window.review.selection == ("B", "/Late")
        assert "TYPE  Scope" in window.details.toPlainText()
        assert window.missing_prim.isHidden()

        window.add_hierarchy_batch()
        assert window.tree.currentItem().data(0, Qt.UserRole) == "/Late"

        window.select_prim("A", "/OnlyInA")
        removed = window.review.current_finding
        assert removed.path == "/OnlyInA"
        window.switch_stage("B")
        assert window.details.toPlainText() == ""
        assert "absent from Stage B" in window.missing_prim.text()
        assert not window.missing_prim.isHidden()
        assert window.review.current_finding == removed

        window.switch_stage("A")
        assert "PRIM  /OnlyInA" in window.details.toPlainText()
        assert window.missing_prim.isHidden()
    finally:
        window.close()
