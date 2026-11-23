"""SceneScout desktop GUI; read-only inspector and semantic comparison."""
from __future__ import annotations

import json
import sys
from concurrent.futures import CancelledError
from pathlib import Path
from PySide6.QtCore import QObject, QSignalBlocker, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QFileDialog, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QPushButton, QSplitter, QToolButton,
    QTabWidget, QTextEdit,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)
from .analysis import check_cancelled
from .animation import compare_animation
from .diff import compare_scenes
from .models import SceneSnapshot
from .provenance import find_layer_contributions
from .review import CoverageMessage, ReviewWorkspace
from .review_document import export_snapshot
from .signoff import Signoff
from .trees import LayerStackTree, PrimHierarchyTree
from .usd_reader import PrimHierarchyEntry, load_stage_payloads, open_stage, snapshot_stage, stage_with_load_rules


STYLE = """
QMainWindow,QWidget { background:#151b23; color:#e7ebf2; }
QTreeWidget,QTableView,QTextEdit,QPlainTextEdit,QLineEdit { background:#192331; color:#e7ebf2; border:1px solid #354354; border-radius:5px; padding:5px; }
QTreeWidget::item:selected,QTableView::item:selected { background:#254969; }
QMenu#treeBranchMenu { background:#192331; border:1px solid #354354; }
QMenu#treeBranchMenu::item:selected { background:#254969; color:#e7ebf2; }
QPushButton,QToolButton { background:#253444; border:1px solid #425465; padding:5px 11px; border-radius:4px; }
QPushButton:hover,QToolButton:hover { background:#314759; }
QPushButton:checked { background:#1c5361; border-color:#59bdc8; color:white; }
QPushButton:disabled { background:#344350; color:#8b9aaa; }
QTabWidget::pane { border:1px solid #354354; }
QTabBar::tab { background:#23303f; padding:9px 15px; margin-right:2px; }
QTabBar::tab:selected { background:#24404c; border-bottom:2px solid #65c5cd; }
QHeaderView::section { background:#243244; color:#dbe4ef; padding:6px; border:0; }
QHeaderView::section:horizontal { border-right:1px solid #374657; }
QHeaderView::section:horizontal:last, QHeaderView::section:horizontal:only-one { border-right:0; }
QCheckBox,QLabel { background:transparent; }
QSplitter::handle { background:#344354; }
"""


class LoadWorker(QObject):
    hierarchy = Signal(str, int, object)
    progress = Signal(str, int, str, int, int)
    loaded = Signal(str, int, object, object, object, object)
    failed = Signal(str, int, str)
    finished = Signal()

    def __init__(self, slot, open_for_review, payload_paths=(), preview=None):
        super().__init__()
        self.slot = slot
        self.open_for_review = open_for_review
        self.payload_paths = payload_paths
        self.preview = preview
        self.generation = 0

    @Slot()
    def run(self):
        cancelled = QThread.currentThread().isInterruptionRequested
        try:
            check_cancelled(cancelled)
            stage = self.open_for_review()
            snapshot = snapshot_stage(
                stage,
                hierarchy=lambda rows: self.hierarchy.emit(self.slot, self.generation, rows),
                progress=lambda *progress: self.progress.emit(self.slot, self.generation, *progress),
                cancelled=cancelled,
            )
            preview = stage_with_load_rules(self.preview, stage.GetLoadRules()) if self.preview is not None else None
            check_cancelled(cancelled)
            self.loaded.emit(self.slot, self.generation, stage, snapshot, self.payload_paths, preview)
        except CancelledError:
            pass
        except Exception as exc:
            self.failed.emit(self.slot, self.generation, f"{type(exc).__name__}: {exc}")
        finally:
            self.finished.emit()


class SceneComparison(QThread):
    progress = Signal(int, str, int, int)

    def __init__(self, generation, stages, snapshots, rate_b, coverage, select_first, parent):
        super().__init__(parent)
        self.generation = generation
        self.stages = stages
        self.snapshots = snapshots
        self.rate_b = rate_b
        self.coverage = coverage
        self.select_first = select_first
        self.animated = any(scene.sample_range for scene in snapshots)
        self.changes = []
        self.error = ""

    def run(self):
        cancelled = self.isInterruptionRequested

        def progress(*values):
            self.progress.emit(self.generation, *values)

        try:
            a, b = self.snapshots
            changes = compare_scenes(a, b, cancelled=cancelled, progress=progress)
            bindings = {change.path for change in changes if change.field == "material_binding"}
            self.changes = [change for change in changes if change.field != "relationships.material:binding"
                            or change.path not in bindings]
            if self.animated:
                check_cancelled(cancelled)
                self.changes.extend(compare_animation(
                    self.stages[0], a, self.stages[1], b, self.rate_b,
                    cancelled=cancelled, progress=progress,
                ))
        except CancelledError:
            pass
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"


class LayerWorker(QObject):
    result = Signal(object, object, str)
    finished = Signal()

    def __init__(self, key):
        super().__init__()
        self.key = key

    def run(self):
        try:
            cancelled = QThread.currentThread().isInterruptionRequested
            self.result.emit(self.key, find_layer_contributions(*self.key, cancelled=cancelled), "")
        except CancelledError:
            pass
        except Exception as exc:
            self.result.emit(self.key, None, f"{type(exc).__name__}: {exc}")
        finally:
            self.finished.emit()


class SceneScoutWindow(QMainWindow):
    comparison_ready = Signal()
    analysis_state_changed = Signal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle("SceneScout — OpenUSD Publish Review")
        available = self.screen().availableGeometry()
        self.resize(min(1800, round(available.width() * 0.95)),
                    min(1050, round(available.height() * 0.93)))
        self.snapshots: dict[str, SceneSnapshot] = {}
        self.active_slot = "A"
        self._preferred_slot = "A"
        self._workers = {}
        self._load_threads = {}
        self._load_requests = {}
        self._load_generation = 0
        self._resume_review = None
        self._resume_candidates = {}
        self._loading_hierarchies = {}
        self._load_status = {}
        self._closing = False
        self._preview_stages = {}
        self._preview_selections = {}
        self._tree_items = {}
        self._tree_timer = QTimer(self, interval=0, singleShot=True)
        self._tree_timer.timeout.connect(self.add_hierarchy_batch)
        self._tree_rows = ()
        self._tree_owner = None
        self._tree_position = 0
        self._tree_expansions = set()
        self._tree_types = set()
        self._tree_term = ""
        self._selected_path = ""
        self._layer_key = None
        self._layer_result = None
        self._layer_thread = None
        self._layer_error = ""
        self._mute_active = False
        self._diff_generation = 0
        self._comparison_thread = None
        self._queued_comparison = None
        self._comparison_progress = ""
        self.comparison_available = False
        self.comparison_complete = False
        self._create_ui()
        self.statusBar().showMessage("Open a USD stage to begin. Files are read-only.")

    def _create_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        self.open_a = QPushButton("Open A…")
        self.open_a.clicked.connect(lambda: self.open_stage("A"))
        self.open_b = QPushButton("Open B…")
        self.open_b.clicked.connect(lambda: self.open_stage("B"))
        self.payloads = QCheckBox("Load payloads on open")
        for w in (self.open_a, self.open_b, self.payloads):
            toolbar.addWidget(w)
        self.cancel_button = QPushButton("Cancel analysis")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_analysis)
        toolbar.addWidget(self.cancel_button)
        self.analysis_status = QLabel()
        self.analysis_status.setTextFormat(Qt.PlainText)
        self.statusBar().addPermanentWidget(self.analysis_status)
        toolbar.addStretch()
        self.stage_buttons = QButtonGroup(self)
        for index, slot in enumerate(("A", "B")):
            button = QPushButton(f"Stage {slot}")
            button.setCheckable(True)
            button.setChecked(index == 0)
            button.setEnabled(False)
            self.stage_buttons.addButton(button, index)
            toolbar.addWidget(button)
        self.stage_buttons.idClicked.connect(self.stage_button_clicked)
        layout.addLayout(toolbar)
        self.summary = QLabel("No stage loaded")
        self.summary.setTextFormat(Qt.PlainText)
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("background:#1b2a38; padding:12px; border-radius:5px; color:#cde5ee;")
        layout.addWidget(self.summary)

        splitter = QSplitter(Qt.Horizontal)
        self.sidebar = QTabWidget()
        hierarchy = QWidget()
        left = QVBoxLayout(hierarchy)
        tree_tools = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter prims by path or type…")
        self.search.textChanged.connect(self.filter_tree)
        tree_tools.addWidget(self.search, 1)
        self.payload_menu = QMenu(self)
        self.payload_button = QToolButton()
        self.payload_button.setText("Payloads")
        self.payload_button.setPopupMode(QToolButton.InstantPopup)
        self.payload_button.setMenu(self.payload_menu)
        tree_tools.addWidget(self.payload_button)
        left.addLayout(tree_tools)
        self.tree = PrimHierarchyTree()
        self.tree.currentItemChanged.connect(self.prim_selected)
        self.tree.addContextActions.connect(self.add_payload_context_actions)
        left.addWidget(self.tree)
        self.missing_prim = QLabel()
        self.missing_prim.setTextFormat(Qt.PlainText)
        self.missing_prim.setWordWrap(True)
        self.missing_prim.hide()
        left.addWidget(self.missing_prim)
        self.sidebar.addTab(hierarchy, "Prim Hierarchy")
        self.layers = LayerStackTree()
        self.sidebar.addTab(self.layers, "Layer Stack")
        splitter.addWidget(self.sidebar)

        self.tabs = QTabWidget()
        inspector = QWidget()
        inspector_layout = QVBoxLayout(inspector)
        variant_toolbar = QHBoxLayout()
        variant_toolbar.addWidget(QLabel("Variant preview"))
        self.variant_set = QComboBox()
        self.variant_value = QComboBox()
        self.variant_set.setAccessibleName("Variant set")
        self.variant_value.setAccessibleName("Preview variant selection")
        self.variant_reset = QPushButton("Reset stage preview")
        variant_toolbar.addWidget(self.variant_set)
        variant_toolbar.addWidget(self.variant_value)
        variant_toolbar.addWidget(self.variant_reset)
        variant_toolbar.addStretch()
        self.variant_status = QLabel()
        self.variant_status.setTextFormat(Qt.PlainText)
        inspector_layout.addLayout(variant_toolbar)
        inspector_layout.addWidget(self.variant_status)
        self.variant_set.currentIndexChanged.connect(self.refresh_variant_values)
        self.variant_value.activated.connect(self.preview_variant)
        self.variant_reset.clicked.connect(self.reset_variant_preview)
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.details.setFont(QFont("Cascadia Mono", 10))
        inspector_layout.addWidget(self.details, 1)
        self.tabs.addTab(inspector, "Prim Inspector")
        self.review = ReviewWorkspace()
        self.review.timeline.alignmentChanged.connect(lambda: self.refresh_diff(select_first=False))
        self.review.primSelected.connect(self.select_prim)
        self.review.inspectPrim.connect(self.inspect_prim)
        self.review.previewUnavailable.connect(self.refresh_variants)
        self.review_index = self.tabs.addTab(self.review, "Review")
        self.tabs.currentChanged.connect(self.tab_changed)
        self.sidebar.currentChanged.connect(self.sidebar_changed)
        self.layers.currentItemChanged.connect(self.layer_changed)
        self.review.muteRequested.connect(self.toggle_layer_mute)
        self.review.previewUnavailable.connect(self.layer_changed)
        splitter.addWidget(self.tabs)
        splitter.setSizes([335, 1065])
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, stretch=1)
        file_menu = self.menuBar().addMenu("File")
        file_menu.addAction("Export active snapshot as JSON…", self.export_json)
        review_menu = self.menuBar().addMenu("Review")
        self.signoff = Signoff(self, review_menu)
        self.payload_menu.aboutToShow.connect(self.populate_payload_menu)
        self.update_payload_actions()
        self.refresh_variants()

    def open_stage(self, slot: str):
        filename, _ = QFileDialog.getOpenFileName(self, "Open OpenUSD Stage", "", "USD Files (*.usd *.usda *.usdc *.usdz)")
        if filename and self.signoff.confirm_discard():
            self.load_path(slot, filename)

    def load_path(self, slot: str, filename: str):
        if self._resume_review is not None:
            self.discard_loads()
        self.start_path_worker(slot, filename)

    def load_saved_pair(self, document, path):
        self.discard_loads()
        self._resume_review = document, path
        settings = document.settings
        self.start_path_worker("A", document.stage_a, rules=settings.payload_rules_a)
        self.start_path_worker("B", document.stage_b, rules=settings.payload_rules_b)

    def start_path_worker(self, slot, filename, *, rules=None):
        load_payloads = self.payloads.isChecked() if rules is None else False

        def open_for_review():
            stage = open_stage(filename, load_payloads)
            if rules is None:
                return stage
            from pxr import Sdf, Usd

            saved = Usd.StageLoadRules()
            rule_types = {rule.name: rule for rule in Usd.StageLoadRules.Rule.allValues}
            saved.SetRules([(Sdf.Path(path), rule_types[rule]) for path, rule in rules])
            return stage_with_load_rules(stage, saved)

        worker = LoadWorker(slot, open_for_review)
        self.start_worker(worker, f"Inspecting {Path(filename).name}…")

    def start_worker(self, worker, message):
        slot = worker.slot
        if self._resume_review is None and not worker.payload_paths:
            self._preferred_slot = slot
        self._load_generation += 1
        worker.generation = self._load_generation
        self._load_requests[slot] = worker
        self._load_status[slot] = message
        previous_rows = self._loading_hierarchies.pop(slot, None)
        if previous_rows is not None and slot == self.active_slot:
            self.switch_stage(slot)
        if slot in self._load_threads:
            self._load_threads[slot].requestInterruption()
        else:
            self.launch_worker(worker)
        self.update_analysis_controls()

    def launch_worker(self, worker):
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.hierarchy.connect(self.stage_hierarchy_received)
        worker.progress.connect(self.stage_progress_received)
        worker.loaded.connect(self.stage_loaded)
        worker.failed.connect(self.stage_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self.thread_done)
        thread.finished.connect(thread.deleteLater)
        self._workers[thread] = worker
        self._load_threads[worker.slot] = thread
        thread.start()

    def _slot_has_pending_load(self, slot):
        return self._resume_review is not None or slot in self._load_requests

    @property
    def analysis_pending(self):
        return bool(self._load_requests or self._comparison_progress)

    def update_analysis_status(self):
        messages = [f"{slot} · {self._load_status[slot]}" for slot in ("A", "B") if slot in self._load_status]
        if self._comparison_progress:
            messages.append(self._comparison_progress)
        self.analysis_status.setText("   |   ".join(messages))

    def update_analysis_controls(self):
        self.update_analysis_status()
        self.review.loading_notice.setVisible(bool(self._load_requests and self.review.viewport))
        self.cancel_button.setEnabled(self.analysis_pending)
        self.update_payload_actions()
        self.refresh_variants()
        self.analysis_state_changed.emit()

    def discard_loads(self):
        self._resume_review = None
        self._resume_candidates.clear()
        self._load_requests.clear()
        self._load_status.clear()
        restore_hierarchy = self.active_slot in self._loading_hierarchies
        for slot in self._loading_hierarchies:
            self.stage_buttons.button(0 if slot == "A" else 1).setEnabled(slot in self.snapshots)
        self._loading_hierarchies.clear()
        for thread in self._load_threads.values():
            thread.requestInterruption()
        if restore_hierarchy:
            slot = self.active_slot if self.active_slot in self.snapshots else next(iter(self.snapshots), "A")
            self.switch_stage(slot)

    def cancel_analysis(self):
        if self._load_requests:
            self.discard_loads()
        else:
            self.invalidate_comparison()
            coverage = [CoverageMessage("Analysis cancelled. Open a stage or change timeline alignment to compare again.")]
            self.review.show_changes([], coverage, False)
            self.sync_review_selection()
        self.statusBar().showMessage("Analysis cancelled.")
        self.update_analysis_controls()

    def is_current_load(self, slot, generation):
        request = self._load_requests.get(slot)
        return not self._closing and request is not None and request.generation == generation

    @Slot(str, int, object)
    def stage_hierarchy_received(self, slot, generation, rows):
        if not self.is_current_load(slot, generation) or self._resume_review is not None or slot in self.snapshots:
            return
        first = slot not in self._loading_hierarchies
        current = self._loading_hierarchies.setdefault(slot, [])
        current.extend(rows)
        if first:
            self.stage_buttons.button(0 if slot == "A" else 1).setEnabled(True)
            if slot == self.active_slot or slot == self._preferred_slot:
                self.switch_stage(slot)
        elif slot == self.active_slot and not self._tree_timer.isActive():
            self._tree_timer.start()

    @Slot(str, int, str, int, int)
    def stage_progress_received(self, slot, generation, phase, count, total):
        if self.is_current_load(slot, generation):
            self._load_status[slot] = self.format_progress(phase, count, total)
            self.update_analysis_status()

    @staticmethod
    def format_progress(phase, count, total):
        if total:
            return f"{phase} {count:,}/{total:,}"
        if count:
            return f"{phase} · {count:,} prims"
        return f"{phase}…"

    def eligible_payload_branches(self, branches, load):
        scene = self.snapshots.get(self.active_slot)
        if not scene or not branches or self._slot_has_pending_load(self.active_slot):
            return ()
        payloads = scene.unloaded_payloads if load else scene.loaded_payloads
        if "/" in branches:
            return ("/",) if payloads else ()
        selected = set(branches)
        matched = set()
        for path in payloads:
            while path:
                if path in selected:
                    matched.add(path)
                path = path.rpartition("/")[0]
            if matched == selected:
                break
        return tuple(path for path in branches if path in matched)

    def update_payload_actions(self):
        self.payload_button.setEnabled(any(self.eligible_payload_branches(("/",), load) for load in (True, False)))

    def populate_payload_menu(self):
        self.payload_menu.clear()
        self.add_payload_actions(self.payload_menu, self.selected_prim_paths(), "selected payloads")
        if not self.payload_menu.isEmpty():
            self.payload_menu.addSeparator()
        self.add_payload_actions(self.payload_menu, ("/",), "all payloads")

    def add_payload_context_actions(self, menu, item):
        if item is not None:
            branches = self.selected_prim_paths() if item.isSelected() else (item.data(0, Qt.UserRole),)
            self.add_payload_actions(menu, branches, "selected payloads")

    def selected_prim_paths(self):
        return tuple(item.data(0, Qt.UserRole) for item in self.tree.selectedItems())

    def add_payload_actions(self, menu, branches, target):
        slot = self.active_slot
        for load, verb in ((True, "Load"), (False, "Unload")):
            eligible = self.eligible_payload_branches(branches, load)
            if eligible:
                menu.addAction(
                    f"{verb} {target}",
                    lambda checked=False, load=load, paths=eligible: self.change_payloads(slot, paths, load),
                )

    def change_payloads(self, slot, branches, load):
        if slot != self.active_slot:
            return
        paths = self.eligible_payload_branches(branches, load)
        if not paths:
            return
        stage = self.review.stages[slot]
        worker = LoadWorker(slot, lambda: load_stage_payloads(stage, paths, load), paths, self._preview_stages.get(slot))
        if paths == ("/",):
            target = "all payloads"
        elif len(paths) == 1:
            target = f"payloads under {paths[0]}"
        else:
            target = f"payloads under {len(paths):,} selected prims"
        self.start_worker(worker, f"{'Loading' if load else 'Unloading'} {target} in Stage {slot}…")

    @Slot()
    def thread_done(self):
        worker = self._workers.pop(self.sender(), None)
        if worker is not None:
            slot = worker.slot
            self._load_threads.pop(slot, None)
            latest = self._load_requests.get(slot)
            if latest is not None and latest is not worker and not self._closing:
                self.launch_worker(latest)
        if self._closing and not self._workers:
            QTimer.singleShot(0, self.close)

    def closeEvent(self, event):
        if not self._closing and not self.signoff.confirm_discard():
            event.ignore()
            return
        if self._workers or self._layer_thread is not None or self._comparison_thread is not None:
            self._closing = True
            if self._comparison_thread:
                self._comparison_thread.requestInterruption()
            if self._layer_thread:
                self._layer_thread.requestInterruption()
            for thread in self._load_threads.values():
                thread.requestInterruption()
            self._load_requests.clear()
            self._resume_review = None
            self._resume_candidates.clear()
            self._queued_comparison = None
            self.statusBar().showMessage("Finishing the current read before closing…")
            event.ignore()
            return
        self.review.release()
        super().closeEvent(event)

    @Slot(str, int, object, object, object, object)
    def stage_loaded(self, slot, generation, stage, snapshot, payload_paths, preview):
        if not self.is_current_load(slot, generation):
            return
        self._load_requests.pop(slot)
        self._load_status.pop(slot, None)
        if self._resume_review is not None:
            self._resume_candidates[slot] = stage, snapshot
            if self._load_requests:
                self.update_analysis_controls()
            else:
                self.install_saved_pair()
            return
        rows = self._loading_hierarchies.pop(slot, None)
        if self._tree_rows is rows:
            self._tree_owner = snapshot
        self.on_loaded(slot, stage, snapshot, payload_paths, preview)

    def install_saved_pair(self):
        document, path = self._resume_review
        stages = {slot: self._resume_candidates[slot][0] for slot in ("A", "B")}
        snapshots = {slot: self._resume_candidates[slot][1] for slot in stages}
        self._resume_review = None
        self._resume_candidates.clear()
        self.end_layer_preview()
        self.signoff.install_saved(document, path)
        self._preview_stages.clear()
        self._preview_selections.clear()
        self.review.show_preview_notice(False)
        self.snapshots = snapshots
        self.review.set_stages(stages)
        for index in range(2):
            self.stage_buttons.button(index).setEnabled(True)
        self._preferred_slot = document.position.slot
        self._selected_path = document.position.path
        self.refresh_diff(select_first=not document.position.path)
        self.switch_stage(self._preferred_slot)
        self.tabs.setCurrentIndex(self.review_index)
        self.statusBar().showMessage("Loaded both stages from the saved review.")

    def on_loaded(self, slot, stage, snapshot, payload_paths, preview):
        if self._closing:
            return
        self.end_layer_preview()
        if payload_paths:
            self.on_payloads_loaded(slot, stage, snapshot, payload_paths, preview)
            return
        self.signoff.clear()
        self._preview_stages.pop(slot, None)
        self._preview_selections.pop(slot, None)
        self.review.show_preview_notice(bool(self._preview_stages))
        self.snapshots[slot] = snapshot
        self.review.set_stage(slot, stage)
        self.stage_buttons.button(0 if slot == "A" else 1).setEnabled(True)
        if slot in (self.active_slot, self._preferred_slot):
            self.switch_stage(slot)
        self.refresh_diff(select_first=not self._selected_path and not self.in_layer_mode())
        if self._selected_path:
            self.sync_review_selection()
        self.tabs.setCurrentIndex(self.review_index)
        self.statusBar().showMessage(f"Loaded {snapshot.prim_count} prims from {snapshot.source}")

    def on_payloads_loaded(self, slot, stage, snapshot, paths, preview):
        self.snapshots[slot] = snapshot
        self.review.set_stage(slot, stage, reframe=False)
        if preview is not None:
            self._preview_stages[slot] = preview
            self.review.set_preview(slot, preview)
        if slot == self.active_slot:
            self.switch_stage(slot)
            self._tree_expansions.update(paths)
            for path in paths:
                item = self._tree_items.get(path)
                if item:
                    item.setExpanded(True)
        self.refresh_diff(select_first=False)
        self.sync_review_selection()
        self.statusBar().showMessage(f"Stage {slot} · {snapshot.prim_count} prims · "
                                     f"{len(snapshot.unloaded_payloads)} unloaded payloads")

    @Slot(str, int, str)
    def stage_failed(self, slot, generation, message):
        if not self.is_current_load(slot, generation):
            return
        if self._resume_review is not None:
            self.discard_loads()
        else:
            self._load_requests.pop(slot)
            self._load_status.pop(slot, None)
            had_hierarchy = self._loading_hierarchies.pop(slot, None) is not None
            self.stage_buttons.button(0 if slot == "A" else 1).setEnabled(slot in self.snapshots)
            if had_hierarchy and slot == self.active_slot:
                self.switch_stage(next(iter(self.snapshots), slot))
        self.update_analysis_controls()
        QMessageBox.critical(self, f"Unable to load Stage {slot}", message)
        self.statusBar().showMessage(message)

    def stage_button_clicked(self, index):
        slot = ("A", "B")[index]
        self._preferred_slot = slot
        if slot != self.active_slot:
            self.switch_stage(slot)
        self.review.timeline.focus_time_field()

    def switch_stage(self, slot):
        self.end_layer_preview()
        progressive = self._loading_hierarchies.get(slot)
        scene = self.snapshots.get(slot) if progressive is None else None
        owner = progressive if progressive is not None else scene
        if self.active_slot != slot or self._tree_owner is not owner:
            for path, item in self._tree_items.items():
                if item.isExpanded():
                    self._tree_expansions.add(path)
                else:
                    self._tree_expansions.discard(path)
            self._tree_timer.stop()
            with QSignalBlocker(self.tree):
                self.tree.clear()
            self._tree_items.clear()
            self._tree_types.clear()
            self._tree_position = 0
            self._tree_owner = owner
            self._tree_rows = progressive if progressive is not None else ()
            if scene:
                self._tree_rows = tuple(PrimHierarchyEntry(p.path, p.name, p.type_name, p.loaded)
                                        for p in scene.prims.values())
        self.active_slot = slot
        self.review.timeline.set_active_slot(slot)
        self.stage_buttons.button(0 if slot == "A" else 1).setChecked(True)
        if not scene:
            self.summary.setText(f"Stage {slot} · inspecting hierarchy" if progressive is not None
                                 else f"Stage {slot} · open a publish")
            self.summary.setToolTip("")
            self.layers.clear()
        else:
            self.summary.setText(
                f"{Path(scene.source).name} · {scene.prim_count:,} prims · "
                f"Default {scene.default_prim} · {scene.up_axis}-up · {scene.time_codes_per_second} time codes/s"
            )
            self.summary.setToolTip(scene.source)
            previous_layer = self.layers.currentItem()
            layer_id = previous_layer.data(0, Qt.UserRole) if previous_layer else None
            with QSignalBlocker(self.layers):
                self.layers.show_stage(self.review.stages[slot], scene)
                if layer_id:
                    for row in self.layers.findItems("*", Qt.MatchWildcard | Qt.MatchRecursive, 0):
                        if row.data(0, Qt.UserRole) == layer_id:
                            self.layers.setCurrentItem(row)
                            break
        self.sync_tree_selection()
        self.add_hierarchy_batch()
        self.update_prim_selection()
        self.update_payload_actions()
        self.layer_changed()

    def add_hierarchy_batch(self):
        end = min(self._tree_position + 300, len(self._tree_rows))
        for path, name, type_name, loaded in self._tree_rows[self._tree_position:end]:
            item = QTreeWidgetItem([name, type_name])
            item.setData(0, Qt.UserRole, path)
            item.setToolTip(0, path)
            if not loaded:
                item.setForeground(0, QColor("#ecb86a"))
            parent_path = path.rsplit("/", 1)[0]
            parent = self._tree_items.get(parent_path)
            (parent.addChild(item) if parent else self.tree.addTopLevelItem(item))
            self._tree_items[path] = item
            self._tree_types.add(type_name)
            if path in self._tree_expansions or path.count("/") == 1:
                item.setExpanded(True)
            if self._tree_term:
                matches = self._tree_term in (path + " " + type_name).casefold()
                item.setHidden(not matches)
                while matches and parent:
                    parent.setHidden(False)
                    parent.setExpanded(True)
                    parent = parent.parent()
            if path == self._selected_path:
                with QSignalBlocker(self.tree):
                    self.reveal_prim(item)
        if end != self._tree_position:
            self.tree.fit_type_column(self._tree_types)
        self._tree_position = end
        if end < len(self._tree_rows):
            self._tree_timer.start()

    def filter_tree(self, text: str):
        term = text.casefold().strip()
        self._tree_term = term
        def visit(item):
            matches = term in (str(item.data(0, Qt.UserRole)) + " " + item.text(1)).casefold()
            descendant = False
            for i in range(item.childCount()):
                descendant = visit(item.child(i)) or descendant
            visible = not term or matches or descendant
            item.setHidden(not visible)
            if term and descendant:
                item.setExpanded(True)
            return visible
        for i in range(self.tree.topLevelItemCount()):
            visit(self.tree.topLevelItem(i))

    def prim_selected(self, item, _previous):
        self._selected_path = item.data(0, Qt.UserRole) if item else ""
        self.update_prim_selection()

    def selected_prim(self):
        scene = self.snapshots.get(self.active_slot)
        return scene.prims.get(self._selected_path) if scene else None

    def update_prim_selection(self):
        self.refresh_variants()
        if self.active_slot in self._loading_hierarchies:
            path = self._selected_path
            self.details.setPlainText(f"{path}\n\nInspecting stage details…" if path
                                      else "Prim details will appear as stage inspection finishes.")
        elif (prim := self.selected_prim()) is not None:
            parts = [f"PRIM  {prim.path}", f"TYPE  {prim.type_name}",
                     f"ACTIVE  {prim.active}     LOADED  {prim.loaded}     INSTANCE  {prim.instance}",
                     f"MATERIAL  {prim.material_binding or '(none)'}"]
            for title, data in (("VARIANTS", prim.variants), ("REFERENCES", prim.references),
                                ("PAYLOADS", prim.payloads), ("ASSET INFO", prim.asset_info),
                                ("ATTRIBUTES", prim.attributes), ("CONNECTIONS", prim.connections),
                                ("RELATIONSHIPS", prim.relationships)):
                parts.append(f"\n{title}\n" + json.dumps(data, indent=2, ensure_ascii=False, default=str))
            if prim.property_read_errors:
                errors = "\n".join(f"{field} · {error}" for field, error in prim.property_read_errors.items())
                parts.append(f"\nPROPERTY READ ERRORS\n{errors}")
            self.details.setPlainText("\n".join(parts))
        else:
            self.details.clear()
        self.sync_review_selection()

    def sync_review_selection(self, *, force=False):
        scene = self.snapshots.get(self.active_slot)
        present = scene and (self._selected_path == "/" or self.selected_prim() is not None)
        finding = self.review.current_finding
        path = self._selected_path if present or (finding and finding.path == self._selected_path) else ""
        if force or (self.active_slot, path) != self.review.selection:
            self.review.select_in_view(self.active_slot, path, notify=False)
        absent = bool(scene and self._selected_path and not present)
        self.missing_prim.setVisible(absent)
        if absent:
            self.missing_prim.setText(f"{self._selected_path} is absent from Stage {self.active_slot} in the current load state.")

    def tab_changed(self, index):
        if index != self.review_index and self.in_layer_mode():
            self.sidebar.setCurrentIndex(0)
        if index == self.review_index:
            self.sync_review_selection()

    def reveal_prim(self, item):
        if item.isHidden():
            self.search.clear()
        parent = item.parent()
        while parent:
            parent.setExpanded(True)
            parent = parent.parent()
        self.tree.setCurrentItem(item)
        self.tree.scrollToItem(item)

    def sync_tree_selection(self):
        item = self._tree_items.get(self._selected_path)
        with QSignalBlocker(self.tree):
            if item:
                self.reveal_prim(item)
            else:
                self.tree.setCurrentItem(None)

    def select_prim(self, slot, path):
        self._selected_path = path
        if self.active_slot != slot:
            self.switch_stage(slot)
        else:
            self.sync_tree_selection()
            self.update_prim_selection()
        if self.in_layer_mode():
            self.show_layer_result()

    def inspect_prim(self, slot, path):
        self.select_prim(slot, path)
        self.tabs.setCurrentIndex(0)

    def in_layer_mode(self):
        return self.sidebar.currentWidget() is self.layers

    def sidebar_changed(self, _index):
        enabled = self.in_layer_mode()
        self.review.set_layer_mode(enabled)
        if enabled:
            self.tabs.setCurrentIndex(self.review_index)
        self.layer_changed()
        if not enabled:
            self.sync_review_selection(force=True)

    def layer_changed(self, *_):
        self.end_layer_preview()
        stage = self._preview_stages.get(self.active_slot) or self.review.stages.get(self.active_slot)
        item = self.layers.currentItem()
        identifier = item.data(0, Qt.UserRole) if item and self.in_layer_mode() else None
        key = (stage, identifier) if stage and identifier else None
        if key != self._layer_key:
            if self._layer_thread:
                self._layer_thread.requestInterruption()
            self._layer_key = key
            self._layer_result = None
            self._layer_error = ""
        if self.in_layer_mode() and self.review.viewport:
            paths = self._layer_result.highlight_paths if self._layer_result else ()
            self.review.viewport.select_layer_paths(self.active_slot, paths)
        if self.in_layer_mode():
            self.show_layer_result()
        self.start_layer_read()

    def start_layer_read(self):
        if (not self._layer_key or self._layer_thread or self._closing
                or self._layer_result is not None or self._layer_error):
            return
        thread = QThread(self)
        worker = LayerWorker(self._layer_key)
        self._layer_thread = thread
        self._layer_worker = worker
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.result.connect(self.layer_read)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self.layer_thread_done)
        thread.finished.connect(thread.deleteLater)
        thread.start()

    @Slot(object, object, str)
    def layer_read(self, key, result, error):
        if key != self._layer_key or self._closing:
            return
        self._layer_result = result
        self._layer_error = error
        if self.review.viewport:
            self.review.viewport.select_layer_paths(self.active_slot, result.highlight_paths if result else ())
        self.show_layer_result()

    @Slot()
    def layer_thread_done(self):
        self._layer_thread = None
        self._layer_worker = None
        if self._closing:
            QTimer.singleShot(0, self.close)
        else:
            self.start_layer_read()

    def show_layer_result(self):
        if not self._layer_key:
            self.review.show_layer_evidence("Layer contributions", "Select a layer to see what it contributes to this stage.")
            return
        stage, identifier = self._layer_key
        item = self.layers.currentItem()
        title = item.text(0) if item else identifier
        result = self._layer_result
        if self._layer_error:
            body = f"Could not read this layer’s contributions. {self._layer_error}"
        elif result is None:
            body = "Finding layer contributions…"
        else:
            count = len(result.paths)
            body = f"{count} contributing prim{'s' if count != 1 else ''} in Stage {self.active_slot}."
            path = self._selected_path
            explanation = result.explain(path) if path else None
            if explanation:
                body += f"\n\n{path}\n{explanation}"
            elif count:
                paths = sorted(result.paths, key=lambda p: (p not in result.highlight_paths, p))
                examples = [f"{p}\n{result.summary(p)}" for p in paths[:5]]
                body += "\n\n" + "\n\n".join(examples)
                if count > 5:
                    body += f"\n… and {count - 5} more"
                if self.review.viewport:
                    body += "\n\nSelect a prim in the viewport for its authored opinions."
        root = stage.GetRootLayer()
        session = stage.GetSessionLayer()
        can_mute = (self.review.viewport is not None and identifier != root.identifier
                    and (not session or identifier != session.identifier) and not stage.IsLayerMuted(identifier))
        self.review.show_layer_evidence(title, body, can_mute)

    def end_layer_preview(self):
        if self._mute_active:
            if self.review.viewport:
                self.review.viewport.clear_layer_comparison()
            self._mute_active = False
            self.review.set_mute_ui(False)

    def toggle_layer_mute(self):
        if self._mute_active:
            self.end_layer_preview()
        elif self._layer_key and self.review.viewport:
            try:
                self.review.viewport.set_layer_comparison(self.active_slot, self._layer_key[1])
            except Exception as exc:
                self.statusBar().showMessage(f"Unable to preview this layer mute. {exc}")
                return
            self._mute_active = True
            self.review.set_mute_ui(True)

    def refresh_variants(self):
        prim = self.selected_prim()
        self._variant_prim = prim
        with QSignalBlocker(self.variant_set):
            self.variant_set.clear()
            if prim:
                self.variant_set.addItems(list(prim.variants))
        available = not self._slot_has_pending_load(self.active_slot)
        self.variant_set.setEnabled(bool(available and self.review.viewport and self.variant_set.count()))
        self.refresh_variant_values()

    def refresh_variant_values(self, *_):
        name = self.variant_set.currentText()
        values = self._variant_prim.variants.get(name, {}) if self._variant_prim else {}
        selections = self._preview_selections.get(self.active_slot, {})
        with QSignalBlocker(self.variant_value):
            self.variant_value.clear()
            self.variant_value.addItems(values.get("choices", []))
            if self._variant_prim:
                selected = selections.get((self._variant_prim.path, name), values.get("selected", ""))
                self.variant_value.setCurrentIndex(self.variant_value.findText(selected))
        available = not self._slot_has_pending_load(self.active_slot)
        self.variant_value.setEnabled(bool(available and self.review.viewport and self.variant_value.count()))
        self.variant_reset.setEnabled(bool(available and selections))
        previewing = self.review.viewport and selections
        self.variant_status.setText("Temporary viewport overrides active. Compare uses the published stages." if previewing else "")

    @Slot(int)
    def preview_variant(self, index):
        from pxr import Sdf, Usd

        slot = self.active_slot
        published = self.review.stages[slot]
        previous = self._preview_stages.get(slot, published)
        name, value = self.variant_set.currentText(), self.variant_value.itemText(index)
        path = self._variant_prim.path
        try:
            session = Sdf.Layer.CreateAnonymous("SceneScout preview.usda")
            if previous.GetSessionLayer():
                session.TransferContent(previous.GetSessionLayer())
            stage = Usd.Stage.Open(published.GetRootLayer(), session, published.GetPathResolverContext(), Usd.Stage.LoadNone)
            stage.SetLoadRules(published.GetLoadRules())
            prim = stage.GetPrimAtPath(path)
            if not prim or value not in prim.GetVariantSet(name).GetVariantNames():
                raise ValueError("This variant is unavailable with the current preview selections.")
            with Usd.EditContext(stage, stage.GetSessionLayer()):
                if not prim.GetVariantSet(name).SetVariantSelection(value):
                    raise ValueError("OpenUSD could not apply the variant selection.")
        except Exception as exc:
            self.refresh_variant_values()
            self.variant_status.setText(str(exc))
            return
        self._preview_stages[slot] = stage
        self._preview_selections.setdefault(slot, {})[path, name] = value
        self.review.set_preview(slot, stage)
        self.review.show_preview_notice(True)
        self.review.select_in_view(slot, path)
        self.tabs.setCurrentIndex(self.review_index)

    def reset_variant_preview(self):
        slot = self.active_slot
        self._preview_stages.pop(slot, None)
        self._preview_selections.pop(slot, None)
        self.review.set_preview(slot, self.review.stages[slot])
        self.review.show_preview_notice(bool(self._preview_stages))
        self.refresh_variant_values()

    def refresh_diff(self, select_first=True):
        self.invalidate_comparison()
        self.review.timeline.configure(self.snapshots)
        a, b = (self.snapshots.get(slot) for slot in ("A", "B"))
        coverage = []
        if a and b:
            coverage.append(CoverageMessage(f"A · {Path(a.source).name}     B · {Path(b.source).name}"))
            if (a.up_axis, a.meters_per_unit) != (b.up_axis, b.meters_per_unit):
                coverage.append(CoverageMessage(
                    "Stage axes or units differ. Preview uses raw stage coordinates.", warning=True,
                ))
            if a.unloaded_payloads or b.unloaded_payloads:
                coverage.append(CoverageMessage(
                    f"Unloaded branches outside review coverage · A {len(a.unloaded_payloads):,}"
                    f" · B {len(b.unloaded_payloads):,}", warning=True,
                ))
            stages = tuple(self.review.stages[slot] for slot in ("A", "B"))
            self._queued_comparison = (self._diff_generation, stages, (a, b), self.review.timeline.rate_b,
                                       coverage, select_first)
            self._comparison_progress = "Comparing publishes…"
            self.start_comparison()
        else:
            coverage.append(CoverageMessage("Open a second publish for A/B comparison."))
        self.review.show_changes([], coverage, False)
        self.tabs.setTabText(self.review_index, "Compare" if a and b else "Review")
        self.update_analysis_controls()

    def invalidate_comparison(self):
        self._diff_generation += 1
        self.comparison_available = False
        self.comparison_complete = False
        self.signoff.stop_sharing()
        self._queued_comparison = None
        self._comparison_progress = ""
        if self._comparison_thread:
            self._comparison_thread.requestInterruption()

    def start_comparison(self):
        if not self._queued_comparison or self._comparison_thread or self._closing:
            return
        job = SceneComparison(*self._queued_comparison, parent=self)
        self._queued_comparison = None
        self._comparison_thread = job
        job.progress.connect(self.comparison_progress)
        job.finished.connect(self.comparison_finished)
        job.start()

    @Slot(int, str, int, int)
    def comparison_progress(self, generation, phase, count, total):
        if generation == self._diff_generation and self._comparison_progress:
            self._comparison_progress = self.format_progress(phase, count, total)
            self.update_analysis_status()

    @Slot()
    def comparison_finished(self):
        job = self._comparison_thread
        self._comparison_thread = None
        job.deleteLater()
        if self._closing:
            QTimer.singleShot(0, self.close)
            return
        if job.generation == self._diff_generation:
            self._comparison_progress = ""
            coverage = job.coverage
            a, b = job.snapshots
            rules_a, rules_b = (stage.GetLoadRules() for stage in job.stages)
            mismatch = bool((a.unloaded_payloads or b.unloaded_payloads) and rules_a != rules_b)
            changes = job.changes
            unreadable = any(change.kind == "Unreadable" for change in changes)
            incomplete = bool(unreadable or a.sample_index_errors or b.sample_index_errors)
            if mismatch:
                coverage.append(CoverageMessage(
                    "Payload loading differs. Use matching load rules to review structure and animation.",
                    warning=True,
                ))
                changes = [change for change in changes
                           if change.field.startswith("dependencies.") or change.kind == "Unreadable"]
            if job.error:
                coverage.append(CoverageMessage(f"Comparison incomplete · {job.error}", warning=True))
            if incomplete:
                coverage.append(CoverageMessage(
                    "Comparison incomplete · Some property reads failed. Sign-off is unavailable.",
                    warning=True,
                ))
            if not (changes or mismatch or job.error or incomplete):
                coverage.append(CoverageMessage(
                    "No findings in the inspected structure, dependencies or compared sample times.",
                ))
            self.comparison_available = not (job.error or mismatch)
            self.comparison_complete = self.comparison_available and not incomplete
            self.signoff.prepare_comparison(changes, "\n".join(message.text for message in coverage))
            self.review.show_changes(changes, coverage, job.select_first)
            self.tabs.setTabText(self.review_index, f"Compare · {len(changes):,}")
            if self._selected_path and self.review.current_finding is None and not self.in_layer_mode():
                self.sync_review_selection(force=True)
            self.update_analysis_controls()
            if self.comparison_available:
                self.comparison_ready.emit()
        self.start_comparison()

    def export_json(self):
        scene = self.snapshots.get(self.active_slot)
        if not scene:
            QMessageBox.information(self, "No stage", "Open a USD stage first.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export Scene Snapshot", "scenescout-snapshot.json", "JSON (*.json)")
        if path:
            from pxr import Ar

            try:
                stages = (*self.review.stages.values(), *self._preview_stages.values())
                source_layers = {
                    Ar.SplitPackageRelativePathOuter(layer.realPath)[0]
                    if Ar.IsPackageRelativePath(layer.realPath) else layer.realPath
                    for stage in stages for layer in stage.GetUsedLayers() if layer.realPath
                }
                export_snapshot(path, scene, source_layers=source_layers)
                self.statusBar().showMessage(f"Exported: {path}")
            except (OSError, RuntimeError, ValueError) as exc:
                QMessageBox.critical(self, "Export failed", str(exc))


def main():
    from .launcher import run_with_sdk

    result, runtime_status = run_with_sdk(sys.argv[1:])
    if result is not None:
        return result
    app = QApplication(sys.argv)
    app.setApplicationName("SceneScout")
    if sys.platform == "win32":
        app.setFont(QFont("Segoe UI", 10))
    app.setStyleSheet(STYLE)
    window = SceneScoutWindow()
    runtime = QLabel()

    def update_runtime_status():
        viewport = window.review.viewport
        runtime.setText("Native OpenUSD" if viewport else "Inspection only")
        messages = [runtime_status]
        if viewport is None:
            messages.extend((window.review.render_status.text(), window.review.render_status.toolTip()))
        runtime.setToolTip("\n".join(filter(None, messages)))

    update_runtime_status()
    if runtime_status:
        window.statusBar().showMessage(runtime_status)
    window.statusBar().addPermanentWidget(runtime)
    window.review.previewUnavailable.connect(update_runtime_status)
    window.show()
    for slot, path in zip(("A", "B"), sys.argv[1:3]):
        window.load_path(slot, path)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
