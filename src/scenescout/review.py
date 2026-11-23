"""Publish review workspace, usable with or without OpenUSD imaging."""
from difflib import SequenceMatcher
from dataclasses import dataclass, replace
from html import escape
from pathlib import PurePosixPath
import re

from PySide6.QtCore import QItemSelectionModel, QModelIndex, QSignalBlocker, Qt, Signal
from PySide6.QtGui import QTextOption
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
    QSplitter, QTextEdit, QVBoxLayout, QWidget,
)

from .diff import field_label
from .finding_queue import ChangeTable
from .provenance import change_evidence
from .review_document import THUMBNAIL_REVIEW_LIMIT, ReviewFinding
from .timeline import ReviewTimeline


@dataclass(frozen=True)
class CoverageMessage:
    text: str
    warning: bool = False


def highlighted_values(before, after):
    token = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|\w+|\s+|."
    left, right = (re.findall(token, value) for value in (before, after))
    old, new = [], []
    for tag, a, b, c, d in SequenceMatcher(None, left, right).get_opcodes():
        removed = escape("".join(left[a:b]))
        added = escape("".join(right[c:d]))
        if tag != "equal":
            if removed:
                removed = f'<span style="background-color:#653444;color:#ffe2e8">{removed}</span>'
            if added:
                added = f'<span style="background-color:#214f43;color:#d7ffeb"><b>{added}</b></span>'
        old.append(removed)
        new.append(added)
    return "".join(old) or "—", "".join(new) or "—"


class ReviewWorkspace(QWidget):
    primSelected = Signal(str, str)
    inspectPrim = Signal(str, str)
    previewUnavailable = Signal()
    muteRequested = Signal()
    annotationChanged = Signal()
    reviewThumbnailsCaptured = Signal(object, object)
    signoffRequested = Signal()

    def __init__(self):
        super().__init__()
        self.changes = []
        self.annotations = {}
        self._change_rows = {}
        self._path_rows = {}
        self._last_finding = None
        self.stages = {}
        self.selection = ("", "")
        self._layer_mode = False
        self._mute_active = False
        self._can_mute = False
        self._shared_review = False
        layout = QVBoxLayout(self)
        self.coverage_summary = "Open two USD publishes to compare them."
        self.coverage = QLabel(self.coverage_summary)
        self.coverage.setWordWrap(True)
        self.coverage.setTextFormat(Qt.PlainText)
        layout.addWidget(self.coverage)
        self.coverage_warning = QLabel()
        self.coverage_warning.setWordWrap(True)
        self.coverage_warning.setTextFormat(Qt.PlainText)
        self.coverage_warning.setStyleSheet(
            "background:#322716; color:#ffe1a3; border:1px solid #916b33; "
            "border-left:3px solid #e0ac55; border-radius:4px; padding:7px 9px;"
        )
        self.coverage_warning.hide()
        layout.addWidget(self.coverage_warning)
        self.preview_notice = QLabel("Temporary variants shown in the viewport. Changes and evidence use the published stages.")
        self.preview_notice.setWordWrap(True)
        self.preview_notice.hide()
        layout.addWidget(self.preview_notice)
        self.loading_notice = QLabel("Viewport updates when stage inspection finishes.")
        self.loading_notice.setWordWrap(True)
        self.loading_notice.hide()
        layout.addWidget(self.loading_notice)
        self.render_status = QLabel()
        self.render_status.setWordWrap(True)
        self.render_status.setTextFormat(Qt.PlainText)
        self.render_status.hide()
        layout.addWidget(self.render_status)

        panels = QSplitter(Qt.Horizontal)
        self.compare_panels = panels
        self.queue = QWidget()
        queue_layout = QVBoxLayout(self.queue)
        queue_layout.setContentsMargins(0, 0, 0, 0)
        self.find_search = QLineEdit()
        self.find_search.setPlaceholderText("Filter findings…")
        self.find_search.setAccessibleName("Filter findings")
        self.find_search.setClearButtonEnabled(True)
        queue_layout.addWidget(self.find_search)
        filters = QHBoxLayout()
        self.find_category = QComboBox()
        self.find_category.addItems(["All categories", "Structural", "Animation", "Dependency"])
        self.find_category.setAccessibleName("Finding category")
        self.find_state = QComboBox()
        self.find_state.addItems(["All statuses", "To review", "Reviewed", "Open issues"])
        self.find_state.setAccessibleName("Finding status")
        filters.addWidget(self.find_category, 1)
        filters.addWidget(self.find_state, 1)
        queue_layout.addLayout(filters)
        self.table = ChangeTable(self.annotations)
        self.table.selectionModel().currentChanged.connect(self._change_selected)
        self.table.rowActivated.connect(self._jump_to_change_time)
        self.find_search.textChanged.connect(self.update_filters)
        self.find_category.currentIndexChanged.connect(self.update_filters)
        self.find_state.currentIndexChanged.connect(self.update_filters)
        queue_layout.addWidget(self.table, 1)
        queue_footer = QHBoxLayout()
        self.find_progress = QLabel()
        self.find_progress.setWordWrap(True)
        queue_footer.addWidget(self.find_progress, 1)
        self.signoff_button = QPushButton("Sign-off…")
        self.signoff_button.setEnabled(False)
        self.signoff_button.clicked.connect(self.signoffRequested)
        queue_footer.addWidget(self.signoff_button)
        queue_layout.addLayout(queue_footer)
        panels.addWidget(self.queue)

        self.preview_panel = QWidget()
        preview_layout = QVBoxLayout(self.preview_panel)
        self.viewport = None
        try:
            from .viewport import AURORA_RENDERER, STORM_RENDERER, ReviewViewport
            self.viewport = ReviewViewport()
        except (ImportError, OSError, RuntimeError) as exc:
            self.render_status.setText("3D preview requires a full OpenUSD installation with Storm and PyOpenGL.")
            self.render_status.setToolTip(str(exc))
            self.render_status.show()
        self.controls = QWidget()
        controls = QHBoxLayout(self.controls)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.addStretch(1)
        self.frame_button = QPushButton("Frame selection")
        self.frame_all = QPushButton("Frame all")
        self.isolate_button = QPushButton("Isolate selection")
        self.isolate_button.setEnabled(False)
        controls.addWidget(self.isolate_button)
        controls.addWidget(self.frame_button)
        controls.addWidget(self.frame_all)
        self.mute_labels = QWidget()
        mute_labels_layout = QHBoxLayout(self.mute_labels)
        mute_labels_layout.setContentsMargins(0, 0, 0, 0)
        mute_labels_layout.addWidget(QLabel("Layer enabled", alignment=Qt.AlignCenter), 1)
        mute_labels_layout.addWidget(QLabel("Layer muted", alignment=Qt.AlignCenter), 1)
        self.mute_labels.hide()
        if self.viewport:
            self.viewport.primPicked.connect(self.select_in_view)
            self.viewport.renderFailed.connect(self._render_failed, Qt.QueuedConnection)
            self.viewport.thumbnailsReady.connect(self._store_review_thumbnails)
            self.viewport.reviewCameraChanged.connect(self._refresh_review_thumbnails)
            self.viewport.isolationChanged.connect(self._isolation_changed)
            self.isolate_button.clicked.connect(self._toggle_isolation)
            self.renderer_choice = QComboBox()
            self.renderer_choice.setAccessibleName("Viewport renderer")
            self.renderer_choice.addItem("Storm", STORM_RENDERER)
            self.renderer_choice.addItem("Aurora (DXR)", AURORA_RENDERER)
            self.renderer_choice.activated.connect(
                lambda index: self.viewport.set_renderer(self.renderer_choice.itemData(index)))
            self.viewport.rendererChanged.connect(self._renderer_changed)
            controls.insertWidget(0, self.renderer_choice)
            preview_layout.addWidget(self.viewport, 1)
            preview_layout.addWidget(self.mute_labels)
            self.frame_button.clicked.connect(
                lambda: self.viewport.frame(self.selection[1], refresh_thumbnails=True))
            self.frame_all.clicked.connect(lambda: self.viewport.frame(refresh_thumbnails=True))
            viewport_help = QLabel("Drag to orbit · Middle-drag to pan · Scroll to zoom · Hold A or B to blink")
            viewport_help.setWordWrap(True)
            preview_layout.addWidget(viewport_help)
        preview_layout.addWidget(self.controls)
        panels.addWidget(self.preview_panel)
        self.preview_panel.setVisible(self.viewport is not None)

        self.change_inspector = QWidget()
        inspector_layout = QVBoxLayout(self.change_inspector)
        inspector_layout.addWidget(QLabel("Finding evidence"))
        self.evidence = QTextEdit()
        self.evidence.setReadOnly(True)
        self.evidence.setWordWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        self.evidence.document().setDefaultFont(self.evidence.font())
        self.evidence.setPlaceholderText("Select a finding to see its evidence.")
        inspector_layout.addWidget(self.evidence, 1)
        self.find_reviewed = QCheckBox("Reviewed")
        self.find_issue = QCheckBox("Flag as open issue")
        self.find_note = QLineEdit()
        self.find_note.setPlaceholderText("Finding note")
        self.find_note.setAccessibleName("Finding note")
        for control in (self.find_reviewed, self.find_issue, self.find_note):
            inspector_layout.addWidget(control)
        self.find_reviewed.toggled.connect(self.update_annotation)
        self.find_issue.toggled.connect(self.update_annotation)
        self.find_note.editingFinished.connect(self.update_annotation)
        view_actions = QHBoxLayout()
        self.pin_view_button = QPushButton("Pin view")
        self.restore_view_button = QPushButton("Restore view")
        self.unpin_view_button = QPushButton("Unpin")
        self.pin_view_button.clicked.connect(self.pin_current_view)
        self.restore_view_button.clicked.connect(self.restore_pinned_view)
        self.unpin_view_button.clicked.connect(self.unpin_current_view)
        for button in (self.pin_view_button, self.restore_view_button, self.unpin_view_button):
            view_actions.addWidget(button)
        inspector_layout.addLayout(view_actions)
        self.inspect_button = QPushButton("Inspect prim")
        self.inspect_button.setEnabled(False)
        self.inspect_button.clicked.connect(lambda: self.inspectPrim.emit(*self.selection))
        inspector_layout.addWidget(self.inspect_button)
        panels.addWidget(self.change_inspector)
        panels.setSizes([400, 650, 320])
        panels.setStretchFactor(1, 1)
        layout.addWidget(panels, 1)

        self.timeline = ReviewTimeline()
        self.timeline.timesChanged.connect(self._times_changed)
        layout.addWidget(self.timeline)

        self.layer_panel = QWidget()
        layer_layout = QVBoxLayout(self.layer_panel)
        layer_layout.setContentsMargins(0, 0, 0, 0)
        layer_header = QHBoxLayout()
        self.layer_title = QLabel("Layer evidence", textFormat=Qt.PlainText)
        self.layer_title.setWordWrap(True)
        title_font = self.layer_title.font()
        title_font.setBold(True)
        self.layer_title.setFont(title_font)
        layer_header.addWidget(self.layer_title, 1)
        self.mute_button = QPushButton("Preview layer mute")
        self.mute_button.setEnabled(False)
        self.mute_button.clicked.connect(lambda: self.muteRequested.emit())
        layer_header.addWidget(self.mute_button)
        layer_layout.addLayout(layer_header)
        self.layer_evidence = QTextEdit()
        self.layer_evidence.setReadOnly(True)
        self.layer_evidence.setMinimumHeight(90)
        self.layer_evidence.setMaximumHeight(160)
        self.layer_evidence.setPlaceholderText("Select a layer to see its contribution.")
        layer_layout.addWidget(self.layer_evidence)
        layout.addWidget(self.layer_panel)
        self.layer_panel.hide()
        self.show_annotation(None)

    def set_layer_mode(self, enabled):
        was_layer_mode = self._layer_mode
        self._layer_mode = enabled
        self.queue.setVisible(not enabled)
        self.coverage.setVisible(not enabled and bool(self.coverage.text()))
        self.coverage_warning.setVisible(not enabled and bool(self.coverage_warning.text()))
        self.change_inspector.setVisible(not enabled)
        self.layer_panel.setVisible(enabled)
        if enabled and self.viewport:
            self.viewport.clear_isolation()
            self.viewport.request_review_thumbnails(None)
        self._update_isolation_button()
        if was_layer_mode and not enabled:
            change = self._change_at_row(self.table.currentRow())
            if change is not None and change.time is not None:
                self._show_change_evidence(change)

    def show_layer_evidence(self, title, body, can_mute=False):
        self.layer_title.setText(title)
        self.layer_evidence.setPlainText(body)
        self._can_mute = can_mute
        self._update_mute_button()

    def set_mute_ui(self, active):
        self._mute_active = active
        self.mute_labels.setVisible(active)
        self._update_mute_button()

    def _update_mute_button(self):
        self.mute_button.setText("Exit preview" if self._mute_active else "Preview layer mute")
        self.mute_button.setEnabled(bool(self.viewport and (self._mute_active or self._can_mute)))

    def _update_isolation_button(self):
        path = self.viewport.isolated_path if self.viewport else ""
        self.isolate_button.setText("Show all" if path else "Isolate selection")
        self.isolate_button.setToolTip(f"Isolated branch\n{path}" if path else
                                      "Show only the selected prim and its descendants in each stage.")
        self.isolate_button.setEnabled(bool(self.viewport and not self._layer_mode
                                           and (path or self.viewport.can_isolate(self.selection[1]))))

    def _isolation_changed(self):
        self._update_isolation_button()
        self._update_view_controls(self.current_finding)

    def _toggle_isolation(self):
        if self.viewport.isolated_path:
            self.viewport.clear_isolation()
            self._refresh_review_thumbnails()
        else:
            try:
                self.viewport.isolate(self.selection[1])
            except Exception as exc:
                QMessageBox.warning(self, "Could not isolate selection", str(exc))

    def _renderer_changed(self, renderer, message):
        self.renderer_choice.setCurrentIndex(self.renderer_choice.findData(renderer))
        self.render_status.setText(message)
        self.render_status.setVisible(bool(message))

    def _render_failed(self, message):
        if not self.viewport:
            return
        self.preview_panel.hide()
        self.viewport.release()
        self.viewport = None
        self.set_mute_ui(False)
        self.preview_notice.hide()
        self.loading_notice.hide()
        self._update_view_controls(self.current_finding)
        self.render_status.setText(f"Storm preview unavailable. {message}")
        self.render_status.show()
        self.previewUnavailable.emit()

    def set_stage(self, slot, stage, reframe=True):
        self.set_stages({slot: stage}, reframe=reframe)

    def set_stages(self, stages, reframe=True):
        for change, finding in self.annotations.items():
            if finding.thumbnails is not None:
                self.annotations[change] = replace(finding, thumbnails=None)
        self.stages.update(stages)
        if self.viewport:
            self.viewport.set_stages(stages, reframe=reframe)
            self._update_isolation_button()

    def set_preview(self, slot, stage):
        if self.viewport:
            self.viewport.set_stage(slot, stage, reframe=False)
            self._update_isolation_button()

    def show_preview_notice(self, has_overrides):
        self.preview_notice.setVisible(bool(self.viewport and has_overrides))
        if has_overrides and self.viewport:
            self.viewport.request_review_thumbnails(None)
        self._update_view_controls(self.current_finding)

    def release(self):
        if self.viewport:
            self.viewport.release()

    def show_changes(self, changes, coverage, select_first=True):
        self.coverage_summary = "\n".join(message.text for message in coverage)
        self.coverage.setText("\n".join(message.text for message in coverage if not message.warning))
        warnings = [message.text for message in coverage if message.warning]
        self.coverage_warning.setText("\n".join(["Warning" if len(warnings) == 1 else "Warnings", *warnings])
                                      if warnings else "")
        self.coverage.setVisible(not self._layer_mode and bool(self.coverage.text()))
        self.coverage_warning.setVisible(not self._layer_mode and bool(warnings))
        selected = self.current_finding or self._last_finding
        self.changes = changes
        self._change_rows = {change: row for row, change in enumerate(changes)}
        self._path_rows = {}
        for row, change in enumerate(changes):
            self._path_rows.setdefault(change.path, []).append(row)
        with QSignalBlocker(self.table.selectionModel()):
            self.table.findings.reset(changes)
        self.update_progress()
        self.evidence.clear()
        self.show_annotation(None)
        if not changes:
            if self.viewport:
                self.viewport.clear_finding()
                self.viewport.request_review_thumbnails(None)
            self._last_finding = selected
            return
        self._last_finding = None
        if self._select_row_for(selected, notify=False):
            self.show_annotation(selected)
            self._show_change_evidence(selected)
        elif select_first:
            self.pick_row(self.table.filters.index(0, 0))

    @property
    def current_finding(self):
        return self.table.change_at(self.table.currentRow())

    def pick_row(self, index, *, notify=True):
        model = self.table.selectionModel()
        with QSignalBlocker(model):
            model.setCurrentIndex(index, QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)
        if index.isValid():
            self.table.scrollTo(index)
        elif self.viewport:
            self.viewport.clear_finding()
            self.viewport.request_review_thumbnails(None)
        if notify:
            self._change_selected(index, QModelIndex())
        return index.isValid()

    def select_finding(self, change):
        return self._select_row_for(change, notify=True)

    def _select_row_for(self, change, *, notify):
        row = self._change_rows.get(change)
        if row is None:
            return False
        index = self.table.filters.mapFromSource(self.table.findings.index(row, 0))
        return index.isValid() and self.pick_row(index, notify=notify)

    def update_progress(self):
        count = sum(bool(self.annotations.get(change) and self.annotations[change].reviewed)
                    for change in self.changes)
        self.find_progress.setText(f"{count:,}/{len(self.changes):,} reviewed\n"
                                   f"{self.table.filters.rowCount():,} shown")

    def update_filters(self):
        change = self.current_finding
        proxy = self.table.filters
        proxy.category = self.find_category.currentText() if self.find_category.currentIndex() else ""
        proxy.state = self.find_state.currentText() if self.find_state.currentIndex() else ""
        proxy.query = self.find_search.text().strip().casefold()
        with QSignalBlocker(self.table.selectionModel()):
            proxy.invalidate()
        if self.current_finding != change:
            self.pick_row(QModelIndex(), notify=False)
            self.show_annotation(None)
            self.evidence.clear()
        self.update_progress()

    def show_annotation(self, change):
        finding = self.annotations.get(change)
        for control in (self.find_note, self.find_issue, self.find_reviewed):
            control.setEnabled(change is not None and not self._shared_review)
        with QSignalBlocker(self.find_note), QSignalBlocker(self.find_issue), QSignalBlocker(self.find_reviewed):
            self.find_note.setText(finding.notes if finding else "")
            self.find_issue.setChecked(bool(finding and finding.open_issue))
            self.find_reviewed.setChecked(bool(finding and finding.reviewed))
        self._update_view_controls(change)

    def set_shared_review(self, active):
        self._shared_review = active
        self.show_annotation(self.current_finding)

    def _update_view_controls(self, change):
        annotation = self.annotations.get(change)
        pinned = bool(annotation and annotation.view)
        available = bool(self.viewport and not self.viewport.isolated_path
                         and len(self.stages) == 2 and change is not None)
        self.pin_view_button.setText("Update view" if pinned else "Pin view")
        self.pin_view_button.setEnabled(available and self.preview_notice.isHidden())
        self.pin_view_button.setToolTip("Capture the camera, stage times and wipe for this finding. "
                                       "Show all prims and exit variant previews before pinning.")
        self.restore_view_button.setEnabled(available and pinned)
        self.unpin_view_button.setEnabled(pinned)
        for button in (self.pin_view_button, self.restore_view_button, self.unpin_view_button):
            button.setVisible(bool(self.viewport) or pinned)

    def pin_current_view(self):
        change = self.current_finding
        if change is None or self.viewport is None or not self.preview_notice.isHidden():
            return
        view = self.viewport.capture_review_view()
        if view is not None:
            self._set_pinned_view(change, view)

    def restore_pinned_view(self):
        finding = self.annotations.get(self.current_finding)
        if self.viewport is None or finding is None or finding.view is None:
            return
        view = finding.view
        if view.time is not None:
            self.timeline.set_time(view.time, time_b=view.time_b)
        self.viewport.restore_review_view(view)

    def unpin_current_view(self):
        change = self.current_finding
        if change in self.annotations:
            self._set_pinned_view(change, None)

    def _set_pinned_view(self, change, view):
        finding = self.annotations.get(change, ReviewFinding(change))
        self.annotations[change] = replace(finding, view=view)
        self._update_view_controls(change)
        self.annotationChanged.emit()

    def _refresh_review_thumbnails(self):
        if self.viewport and not self._layer_mode and self.preview_notice.isHidden():
            self.viewport.request_review_thumbnails(self.current_finding, debounce=True)

    def _store_review_thumbnails(self, change, thumbnails):
        if self.current_finding != change or self._layer_mode or not self.preview_notice.isHidden():
            return
        finding = self.annotations.get(change, ReviewFinding(change))
        if finding.thumbnails == thumbnails:
            return
        used = sum(item.thumbnails.size for key, item in self.annotations.items()
                   if key != change and item.thumbnails is not None)
        if used + thumbnails.size <= THUMBNAIL_REVIEW_LIMIT:
            self.annotations[change] = replace(finding, thumbnails=thumbnails)
            self.reviewThumbnailsCaptured.emit(change, thumbnails)

    def update_annotation(self):
        change = self.current_finding
        if change is None or self._shared_review:
            return
        finding = self.annotations.get(change, ReviewFinding(change))
        updated = replace(finding, notes=self.find_note.text(), reviewed=self.find_reviewed.isChecked(),
                          open_issue=self.find_issue.isChecked())
        if updated == finding:
            return
        self.annotations[change] = updated
        self.annotationChanged.emit()
        self.update_filters()

    def _select(self, slot, path, *, notify=True):
        self.selection = (slot, path)
        self.inspect_button.setEnabled(bool(path and path != "/"))
        if self.viewport and not self._layer_mode:
            self.viewport.select(path)
        self._update_isolation_button()
        if slot and notify:
            self.primSelected.emit(slot, path)

    def select_in_view(self, slot, path, *, notify=True):
        if self._layer_mode:
            self._select(slot, path, notify=notify)
            return
        current = self.table.currentRow()
        change = self._change_at_row(current)
        if change is not None and change.path == path:
            index = self.table.currentIndex()
        else:
            visible = (self.table.filters.mapFromSource(self.table.findings.index(row, 0))
                       for row in self._path_rows.get(path, ()))
            index = next((item for item in visible if item.isValid()), QModelIndex())
        self.pick_row(index, notify=False)
        change = self.current_finding
        self.show_annotation(change)
        if change is not None:
            self._show_change_evidence(change)
        else:
            self.evidence.setPlainText(f"{path}\n\nNo listed publish changes for this prim." if path else "")
        self._select(slot, path, notify=notify)

    def _change_at_row(self, row):
        return self.table.change_at(row)

    def _change_selected(self, current, previous):
        change = self._change_at_row(current.row())
        if change is None or current.row() == previous.row() or self._layer_mode:
            return
        stage_b = self.stages.get("B")
        slot = "A" if change.kind == "Removed" or not stage_b or not stage_b.GetPrimAtPath(change.path) else "B"
        self._select(slot, change.path)
        self._jump_to_change_time(current.row())
        if self.viewport:
            self.viewport.focus_finding(change)
            annotation = self.annotations.get(change)
            capture = self.preview_notice.isHidden() and not (annotation and annotation.thumbnails)
            self.viewport.request_review_thumbnails(change if capture else None)
        self.show_annotation(change)
        self._show_change_evidence(change)

    def _jump_to_change_time(self, row):
        change = self._change_at_row(row)
        if not self._layer_mode and change is not None and change.time is not None:
            self.timeline.set_time(change.time, time_b=change.time_b)

    def _times_changed(self, times):
        if self.viewport:
            self.viewport.set_times(times)
        change = self._change_at_row(self.table.currentRow())
        if not self._layer_mode and change is not None and change.time is not None:
            self._show_change_evidence(change)

    def _show_change_evidence(self, change):
        sections = [f"<p><b>{escape(field_label(change.field))}</b><br><code>{escape(change.path)}</code></p>"]
        timed = change.time is not None
        values = {"A": change.before, "B": change.after}
        labels = {"A": "Stage A", "B": "Stage B"}
        if timed:
            sections.append(f"<p>Difference at shared time code {change.time:.15g}. "
                            "Values below follow the timeline.</p>")
            values = {"A": "—", "B": "—"}
        sources = []
        for slot in ("A", "B"):
            stage = self.stages.get(slot)
            if stage is None:
                continue
            root_layer = PurePosixPath(stage.GetRootLayer().identifier.replace("\\", "/"))
            if timed:
                time = self.timeline.local_time(slot)
                if time is None:
                    labels[slot] = f"Stage {slot} · No timeline time"
                    continue
                labels[slot] = f"Stage {slot} · Time code {time:.15g}"
            value, summary, opinions = change_evidence(stage, change, time if timed else None)
            if timed:
                values[slot] = str(value) if value is not None else "—"
            sources.append(f"<p><b>{slot} · {escape(root_layer.name)}</b><br>"
                            f"{escape(summary).replace(chr(10), '<br>')}</p>")
            for opinion in opinions:
                layer = PurePosixPath(opinion.layer.replace("\\", "/"))
                if layer.is_relative_to(root_layer.parent):
                    layer = layer.relative_to(root_layer.parent)
                sources.append(f"<p><b>{escape(str(layer))}</b><br><code>{escape(opinion.spec_path)}<br>"
                               f"{escape(opinion.value).replace(chr(10), '<br>')}</code></p>")
        for slot, value in zip(("A", "B"), highlighted_values(values["A"], values["B"])):
            sections.append(f'<p><b>{escape(labels[slot])}</b><br><code style="white-space:pre-wrap">'
                            f'{value}</code></p>')
        self.evidence.setHtml(
            "<style>p { margin-top: 0; margin-bottom: 10px; }</style>" + "".join(sections + sources)
        )
