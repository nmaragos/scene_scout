"""Capture a review and reopen its recorded findings or live Compare position."""
from dataclasses import replace
from ipaddress import IPv4Address
from pathlib import Path

from PySide6.QtCore import QEventLoop, QObject, QSignalBlocker, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtNetwork import QNetworkInterface
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QInputDialog, QMessageBox,
    QPlainTextEdit, QProgressDialog, QTextBrowser, QVBoxLayout,
)

from .provenance import change_evidence
from .review_document import (
    REVIEW_DECISIONS, ReviewDocument, ReviewFinding, ReviewPosition, ReviewSettings,
    export_report, finding_issue, load_review, render_report, save_review,
)
from .shared_review import SharedReview


class CaptureReview(QThread):
    progress = Signal(int)

    def __init__(self, document, stages, parent):
        super().__init__(parent)
        self.document = document
        self.stages = stages
        self.captured = None
        self.error = ""

    def run(self):
        findings = []
        try:
            for index, finding in enumerate(self.document.findings):
                if self.isInterruptionRequested():
                    return
                change = finding.change
                sections = []
                for slot, stage, time in zip(("A", "B"), self.stages, (change.time, change.time_b)):
                    value, summary, opinions = change_evidence(stage, change, time)
                    lines = [f"Stage {slot} · {summary}"]
                    if value is not None:
                        lines.append(f"Evaluated value · {value}")
                    for opinion in opinions:
                        lines.extend((f"{opinion.layer} · {opinion.spec_path}", opinion.value))
                    sections.append("\n".join(lines))
                findings.append(replace(finding, evidence="\n\n".join(sections), issue=finding_issue(change)))
                if index % 32 == 0:
                    self.progress.emit(index + 1)
            if not self.isInterruptionRequested():
                self.captured = replace(self.document, findings=tuple(findings))
        except Exception as exc:
            self.error = str(exc)


def load_rule_pairs(stage):
    rules = stage.GetLoadRules()
    rules.Minimize()
    return tuple((str(path), rule.name) for path, rule in rules.GetRules())


class Signoff(QObject):
    browserReviewUpdated = Signal()

    def __init__(self, window, menu):
        super().__init__(window)
        self.window = window
        self.review = window.review
        self.notes = ""
        self.decision = "Pending"
        self.path = None
        self.last_comparison = None
        self.pending_restore = None
        self.dirty = False
        self._shared = None
        self._shared_revision = 0
        self._share_timer = QTimer(self, interval=500)
        self._share_timer.timeout.connect(self._sync_shared_review)
        self.add_action(menu, "Open saved review…", self.open_saved)
        self.save_action = self.add_action(menu, "Save review…", self.save)
        self.save_action.setShortcut("Ctrl+S")
        self.export_action = self.add_action(menu, "Export review as HTML…", self.export)
        self.decision_action = self.add_action(menu, "Sign-off…", self.edit_decision)
        menu.addSeparator()
        self.share_action = self.add_action(menu, "Share review on network…", self.share_review)
        self.copy_link_action = self.add_action(menu, "Copy private review link", self.copy_review_link)
        self.stop_share_action = self.add_action(menu, "Stop sharing review", self.stop_sharing)
        self.review.annotationChanged.connect(self.mark_dirty)
        self.review.reviewThumbnailsCaptured.connect(self._share_thumbnails)
        self.review.signoffRequested.connect(self.edit_decision)
        window.analysis_state_changed.connect(self.update_actions)
        window.comparison_ready.connect(self.restore_position)
        self.update_actions()

    def add_action(self, menu, label, callback):
        action = QAction(label, self)
        action.triggered.connect(callback)
        menu.addAction(action)
        return action

    def update_actions(self):
        for control in (self.save_action, self.export_action):
            control.setEnabled(self.window.comparison_available)
        for control in (self.decision_action, self.review.signoff_button):
            control.setEnabled(self.window.comparison_complete)
        self._update_share_actions()

    def _update_share_actions(self):
        ready = self.window.comparison_complete
        sharing = self._shared is not None
        self.share_action.setEnabled(ready and not sharing)
        self.copy_link_action.setEnabled(sharing)
        self.stop_share_action.setEnabled(sharing)

    def mark_dirty(self):
        self.dirty = True

    def confirm_discard(self):
        self._sync_shared_review()
        if not self.dirty:
            return True
        buttons = QMessageBox.Discard | QMessageBox.Cancel
        if self.window.comparison_available:
            buttons |= QMessageBox.Save
        choice = QMessageBox.question(self.window, "Unsaved review", "Save your review before leaving this pair?",
                                      buttons, QMessageBox.Cancel)
        if choice == QMessageBox.Save:
            return self.save() and not self.dirty
        return choice == QMessageBox.Discard

    def clear(self):
        self.stop_sharing()
        self.notes = ""
        self.decision = "Pending"
        self.path = None
        self.last_comparison = None
        self.pending_restore = None
        self.review.annotations.clear()
        self.review.signoff_button.setToolTip("Review decision · Pending")
        self.review.signoff_button.setText("Sign-off…")
        self.dirty = False

    def settings(self):
        a, b = (self.review.stages[slot] for slot in ("A", "B"))
        return ReviewSettings("frames" if self.review.timeline.alignment.currentIndex() else "seconds",
                              load_rule_pairs(a), load_rule_pairs(b), self.window.payloads.isChecked())

    def prepare_comparison(self, changes, coverage):
        current = (self.settings(), tuple(changes), coverage)
        if not self.window.comparison_complete and self.decision != "Pending":
            self.set_decision("Pending")
            self.window.statusBar().showMessage("Comparison is incomplete. Sign-off returned to Pending.")
        elif self.last_comparison and self.last_comparison != current and self.decision != "Pending":
            self.set_decision("Pending")
            self.window.statusBar().showMessage("Findings or coverage changed. Sign-off returned to Pending.")
        self.last_comparison = current

    def set_decision(self, decision):
        self.decision = decision
        self.review.signoff_button.setText("Sign-off…" if decision == "Pending"
                                          else "Decision…")
        self.review.signoff_button.setToolTip(f"Review decision · {decision}")
        self.mark_dirty()

    def edit_decision(self):
        if not self.window.comparison_complete:
            return
        generation = self.window._diff_generation
        self._sync_shared_review()
        sharing = self._shared is not None
        dialog = QDialog(self.window)
        dialog.setWindowTitle("Browser review sign-off" if sharing else "Review sign-off")
        dialog.resize(460, 260)
        form = QFormLayout(dialog)
        decision = QComboBox()
        decision.addItems(REVIEW_DECISIONS)
        decision.setCurrentText(self.decision)
        decision.setEnabled(not sharing)
        form.addRow("&Decision", decision)
        notes = QPlainTextEdit(self.notes)
        notes.setReadOnly(sharing)
        form.addRow("Review &notes", notes)
        buttons = QDialogButtonBox(QDialogButtonBox.Close if sharing else
                                   QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if sharing:
            def refresh():
                decision.setCurrentText(self.decision)
                if notes.toPlainText() != self.notes:
                    notes.setPlainText(self.notes)

            self.browserReviewUpdated.connect(refresh)
            try:
                dialog.exec()
            finally:
                self.browserReviewUpdated.disconnect(refresh)
        elif dialog.exec() and generation == self.window._diff_generation and self.window.comparison_complete:
            self.notes = notes.toPlainText()
            self.set_decision(decision.currentText())

    def draft(self):
        self._sync_shared_review()
        self.review.update_annotation()
        current = self.review.current_finding
        changes = self.review.changes
        findings = tuple(self.review.annotations.get(change, ReviewFinding(change)) for change in changes)
        times = self.review.timeline.times
        position = ReviewPosition(self.window.active_slot, self.window._selected_path,
                                  current.field if current else "", times.get("A"), times.get("B"),
                                  changes.index(current) if current in changes else None)
        return ReviewDocument(*(self.window.snapshots[slot].source for slot in ("A", "B")),
                              findings, self.review.coverage_summary, self.notes, self.decision,
                              position, self.settings())

    def capture(self):
        if not self.window.comparison_available:
            return None
        generation = self.window._diff_generation
        document = self.draft()
        stages = tuple(self.review.stages[slot] for slot in ("A", "B"))
        job = CaptureReview(document, stages, self.window)
        progress = QProgressDialog("Collecting authored evidence…", "Cancel", 0,
                                   max(1, len(document.findings)), self.window)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        job.progress.connect(progress.setValue)
        loop = QEventLoop()
        job.finished.connect(loop.quit)
        progress.canceled.connect(job.requestInterruption)
        progress.show()
        job.start()
        loop.exec()
        captured = None if progress.wasCanceled() or generation != self.window._diff_generation else job.captured
        progress.close()
        progress.deleteLater()
        if job.error:
            QMessageBox.critical(self.window, "Evidence capture failed", job.error)
        job.deleteLater()
        if captured is not None:
            captured = replace(captured, findings=tuple(
                replace(finding, thumbnails=self.review.annotations.get(finding.change, finding).thumbnails)
                for finding in captured.findings
            ))
        return captured

    def write(self, filename, document, writer):
        try:
            writer(filename, document)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self.window, "Unable to write review", str(exc))
            return False
        self.window.statusBar().showMessage(f"Written · {filename}")
        return True

    def save(self):
        filename, _ = QFileDialog.getSaveFileName(self.window, "Save SceneScout review",
                                                  str(self.path or "review.scenescout.json"), "Review (*.json)")
        if not filename:
            return False
        if not Path(filename).suffix:
            filename += ".scenescout.json"
        self._sync_shared_review()
        revision = self._shared_revision
        document = self.capture()
        if document is None or not self.write(filename, document, save_review):
            return False
        self.path = Path(filename)
        self._sync_shared_review()
        self.dirty = self._shared_revision != revision
        return True

    def share_review(self):
        addresses = set()
        for address in QNetworkInterface.allAddresses():
            ipv4 = address.toIPv4Address()
            if ipv4:
                value = IPv4Address(ipv4)
                if not (value.is_loopback or value.is_link_local or value.is_multicast):
                    addresses.add(value)
        options = [str(address) for address in sorted(addresses)] + ["127.0.0.1"]
        address, accepted = QInputDialog.getItem(
            self.window, "Share review on network",
            "Choose the workstation address reachable by the reviewer. Use 127.0.0.1 to review on this computer.",
            options, 0, False,
        )
        if not accepted or not self.window.comparison_complete:
            return
        document = self.capture()
        if document is None:
            return
        try:
            self._shared = SharedReview(address, document)
        except (OSError, RuntimeError) as exc:
            QMessageBox.critical(self.window, "Unable to share review", str(exc))
            return
        self._shared_revision = 0
        self.review.set_shared_review(True)
        self._update_share_actions()
        self._share_timer.start()
        self.copy_review_link()

    def _share_thumbnails(self, change, thumbnails):
        if self._shared:
            self._shared.add_thumbnails(change, thumbnails)

    def copy_review_link(self):
        if self._shared:
            QGuiApplication.clipboard().setText(self._shared.url)
            self.window.statusBar().showMessage("Private review link copied. Edit notes and sign off in the browser.", 6000)

    def _sync_shared_review(self):
        if not self._shared:
            return
        document, revision = self._shared.snapshot()
        if revision == self._shared_revision:
            return
        self._shared_revision = revision
        self.notes = document.notes
        self.set_decision(document.decision)
        for finding in document.findings:
            current = self.review.annotations.get(finding.change, ReviewFinding(finding.change))
            updated = replace(current, notes=finding.notes, reviewed=finding.reviewed, open_issue=finding.open_issue)
            if updated != current:
                self.review.annotations[finding.change] = updated
        self.review.show_annotation(self.review.current_finding)
        self.review.update_filters()
        self.browserReviewUpdated.emit()
        self.window.statusBar().showMessage("Browser review synced. Save the review to keep these changes.", 6000)

    def stop_sharing(self):
        if not self._shared:
            return
        self._share_timer.stop()
        self._shared.close()
        self._sync_shared_review()
        self._shared = None
        self.review.set_shared_review(False)
        self._update_share_actions()
        self.window.statusBar().showMessage("Review sharing stopped.", 6000)

    def export(self, document=None):
        filename, _ = QFileDialog.getSaveFileName(self.window, "Export review report",
                                                  "review.html", "HTML (*.html)")
        if filename:
            if not Path(filename).suffix:
                filename += ".html"
            if not isinstance(document, ReviewDocument):
                document = self.capture()
            if document is not None:
                self.write(filename, document, export_report)

    def open_saved(self):
        filename, _ = QFileDialog.getOpenFileName(self.window, "Open SceneScout review", "", "Review (*.json)")
        if not filename:
            return
        try:
            document = load_review(filename)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self.window, "Unable to open review", str(exc))
            return
        self.show_saved(document, Path(filename))

    def show_saved(self, document, path):
        dialog = QDialog(self.window)
        dialog.setWindowTitle("Saved review")
        dialog.resize(900, 680)
        layout = QVBoxLayout(dialog)
        report = QTextBrowser()
        report.setHtml(render_report(document))
        report.setOpenExternalLinks(False)
        layout.addWidget(report)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dialog.reject)
        resume = buttons.addButton("Resume in Compare", QDialogButtonBox.AcceptRole)
        resume.clicked.connect(dialog.accept)
        export = buttons.addButton("Export HTML…", QDialogButtonBox.ActionRole)
        export.clicked.connect(lambda: self.export(document))
        layout.addWidget(buttons)
        if document.position.finding_index is not None:
            report.scrollToAnchor(f"finding-{document.position.finding_index}")
        if dialog.exec() and self.confirm_discard():
            self.resume(document, path)

    def resume(self, document, path):
        self.window.load_saved_pair(document, path)

    def install_saved(self, document, path):
        self.clear()
        self.path = path
        self.notes = document.notes
        self.pending_restore = document, path
        settings = document.settings
        self.window.payloads.setChecked(settings.load_payloads_on_open)
        with QSignalBlocker(self.review.timeline.alignment):
            self.review.timeline.alignment.setCurrentIndex(int(settings.alignment == "frames"))

    def restore_position(self):
        if self.pending_restore is None:
            return
        document, self.path = self.pending_restore
        self.pending_restore = None
        self.notes = document.notes
        recorded = {finding.change: finding for finding in document.findings}
        current = self.review.changes
        self.review.annotations.update({
            change: recorded[change] for change in current if change.identity is not None and change in recorded
        })
        identity_available = all(change.identity is not None for change in recorded)
        same = (identity_available
                and set(recorded) == set(current)
                and document.coverage == self.review.coverage_summary)
        decision = document.decision if same and self.window.comparison_complete else "Pending"
        self.set_decision(decision)
        self.dirty = not same or decision != document.decision
        self.review.find_search.clear()
        self.review.find_category.setCurrentIndex(0)
        self.review.find_state.setCurrentIndex(0)
        self.review.update_filters()
        position = document.position
        restored_finding = None
        if position.finding_index is not None:
            restored_finding = document.findings[position.finding_index].change
            self.review.select_finding(restored_finding)
        self.window.select_prim(position.slot, position.path)
        if position.time is not None:
            self.review.timeline.set_time(position.time, time_b=position.time_b)
        if restored_finding is not None and self.review.current_finding == restored_finding:
            self.review.restore_pinned_view()
        if not self.window.comparison_complete:
            self.window.statusBar().showMessage("Comparison is incomplete. Decision is Pending.")
        elif not identity_available:
            self.window.statusBar().showMessage("Saved finding identity is unavailable. Decision is Pending.")
        elif not same:
            self.window.statusBar().showMessage("Findings or coverage differ from the saved review. Decision is Pending.")
